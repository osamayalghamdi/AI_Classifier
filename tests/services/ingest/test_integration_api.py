"""E1-E9 integration API tests (async ingest, fetch, batch, readiness,
dry-run, auth, idempotency, strict schema, real-failure retry path).

Run convention (documented in docs/INTEGRATION_GUIDE.md):
    export PG_DATABASE=ai_incidents_test INTEGRATION_TOKEN=test-token \
           INTEGRATION_WORKER_ENABLED=0
    pytest tests/test_integration_api.py
The E5 real-failure test spawns a subprocess pointed at the REAL
unreachable company endpoint (llms.elm.sa — NXDOMAIN from this box) —
no mock, the actual DNS/connection error path.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time

# Auth + worker env MUST be set before ai_classification.shared.config is imported
# (Settings is a module-level singleton evaluated at import time).
os.environ.setdefault("INTEGRATION_TOKEN", "test-token")
os.environ.setdefault("INTEGRATION_WORKER_ENABLED", "0")

import psycopg2
import pytest
from fastapi.testclient import TestClient

from tests.services.classify.test_cascade import _settings_with, make_fake_completion

import ai_classification.services.classify.classifier as classifier_mod
import ai_classification.services.classify.llm as mod_llm
from ai_classification.services.ingest.routes import app
from ai_classification.shared.config import settings
from ai_classification.services.jobs.integration import get_job, list_jobs, worker_tick
from ai_classification.services.jobs.integration.schemas import Err


def _job(ref: str) -> dict:
    j = get_job(ref)
    assert j is not None
    return j

TOKEN = os.environ.get("INTEGRATION_TOKEN", "test-token")
AUTH = {"Authorization": f"Bearer {TOKEN}"}

FAKE_CLASSIFICATION = json.dumps({
    "affected_system": "Nusuk Masar Haj",
    "service": "pilgrim groups and issue permit - Nusuk Masar Haj.Issue Permits",
    "incident_type": "Unavailability",
    "severity": "Major",
    "urgency": "High",
    "category": "Software",
    "confidence": "high",
    "reasoning": "integration test",
    "canonical_statement": "Permit issuance fails when selecting a date for the group.",
    "signature": "permit issuance fails on date selection",
    "failure_mode": "FM-018",
})

PAYLOAD = {
    "source_reference": "TKT-1001",
    "title": "Rawdah permit date error",
    "description": "Error when selecting a date for the pilgrim group",
    "status": "active",
}


@pytest.fixture(autouse=True)
def _fake_llm(monkeypatch):
    """Cascade off (one LLM call per classify) + canned classification."""
    monkeypatch.setattr(classifier_mod, "settings", _settings_with(False))

    def fake_completion(**kwargs):
        return make_fake_completion(FAKE_CLASSIFICATION)

    monkeypatch.setattr(mod_llm, "completion", fake_completion)
    yield


@pytest.fixture(scope="module")
def client():
    """The app under test, pointed at an ISOLATED store instance.

    The global store singleton's pool must never be opened/closed by these
    tests (the lifespan's setup/close would leak a closed pool into later
    tests — manager diagnosis). We swap the module-global `store` for a
    fresh instance (same test DB); the lifespan start/stop then applies to
    the isolated instance and the global singleton is untouched.
    """
    import ai_classification.shared.store as store_mod
    import ai_classification.services.classify.classifier as classifier_mod
    from ai_classification.shared.store import IncidentStore

    # classifier.py binds `store` at import time (persist path) — patch it
    # too so classify_and_store writes through the isolated instance.
    originals = (store_mod.store, classifier_mod.store)
    isolated = IncidentStore()
    store_mod.store = isolated
    classifier_mod.store = isolated
    try:
        with TestClient(app) as c:
            yield c
    finally:
        store_mod.store, classifier_mod.store = originals


@pytest.fixture(autouse=True)
def _clean_state():
    yield
    conn = psycopg2.connect(
        host=settings.pg_host, port=settings.pg_port,
        user=settings.pg_user, password=settings.pg_password,
        dbname=settings.pg_database,
    )
    conn.autocommit = True
    with conn.cursor() as cur:
        cur.execute("TRUNCATE ingestion_jobs, incidents RESTART IDENTITY CASCADE")
    conn.close()


# ── E1: async ingest ─────────────────────────────────────────────────

def test_e1_ingest_returns_immediately_then_processes(client):
    r = client.post("/api/v1/incidents", json=PAYLOAD, headers=AUTH)
    assert r.status_code == 202, r.text
    body = r.json()
    assert body["reference"] == "TKT-1001"
    assert body["status"] == "pending"
    assert body["location"] == "/api/v1/incidents/TKT-1001"

    # still pending until the worker runs (caller never waits on the LLM)
    job = _job("TKT-1001")
    assert job["status"] == "pending"

    n = worker_tick()
    assert n == 1
    job = _job("TKT-1001")
    assert job["status"] == "succeeded"
    assert job["result"]["is_new"] is True
    assert job["result"]["incident_id"]
    assert job["result"]["classification"]["failure_mode"] == "FM-018"
    assert job["result"]["write_back"]["mode"] in ("suggestions", "none", "full")


# ── E2: fetch result by reference ────────────────────────────────────

def test_e2_fetch_structured_result(client):
    client.post("/api/v1/incidents", json=PAYLOAD, headers=AUTH)
    worker_tick()
    r = client.get("/api/v1/incidents/TKT-1001", headers=AUTH)
    assert r.status_code == 200
    body = r.json()
    assert body["source_reference"] == "TKT-1001"
    assert body["status"] == "succeeded"
    assert body["error"] is None
    assert body["result"]["classification"]["affected_system"] == "Nusuk Masar Haj"
    assert "suggestions" in body["result"]


def test_e2_fetch_pending_and_not_found(client):
    client.post("/api/v1/incidents", json=PAYLOAD, headers=AUTH)
    job = _job("TKT-1001")
    assert job["status"] == "pending"  # not yet processed

    r = client.get("/api/v1/incidents/DOES-NOT-EXIST", headers=AUTH)
    assert r.status_code == 404
    assert r.json()["error"]["code"] == Err.NOT_FOUND


# ── E3: batch / backfill ─────────────────────────────────────────────

def test_e3_backfill_all_succeed(client):
    batch = {
        "incidents": [
            {**PAYLOAD, "source_reference": f"BF-{i}", "title": f"Ticket {i}"}
            for i in range(3)
        ]
    }
    r = client.post("/api/v1/backfill", json=batch, headers=AUTH)
    assert r.status_code == 202
    body = r.json()
    assert body["total"] == 3
    assert len(body["references"]) == 3

    n = worker_tick(limit=10)
    assert n == 3
    for ref in body["references"]:
        assert _job(ref)["status"] == "succeeded"


# ── E4: health (liveness) + readiness ────────────────────────────────

def test_e4_health_no_auth(client):
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


def test_e4_readiness_checks_reported_individually(client):
    r = client.get("/ready")
    assert r.status_code in (200, 503)
    body = r.json()
    assert set(body["checks"].keys()) == {"db", "embedding", "llm"}
    assert body["checks"]["db"] == "ok"
    assert body["checks"]["embedding"] == "ok"
    assert body["status"] in ("ok", "degraded")


# ── E5: dry-run persists nothing ─────────────────────────────────────

def test_e5_dry_run_writes_nothing(client):
    before = worker_count_of_incidents()
    r = client.post("/api/v1/incidents/dry-run", json=PAYLOAD, headers=AUTH)
    assert r.status_code == 200
    body = r.json()
    assert body["reference"] == "TKT-1001"
    assert body["is_new"] is True
    assert body["classification"]["failure_mode"] == "FM-018"
    assert body["would_write"]["dry_run"] is True
    assert body["write_back"]["applied"] is False

    # nothing persisted: no job row, no incident row
    assert get_job("TKT-1001") is None
    assert worker_count_of_incidents() == before


def worker_count_of_incidents() -> int:
    conn = psycopg2.connect(
        host=settings.pg_host, port=settings.pg_port,
        user=settings.pg_user, password=settings.pg_password,
        dbname=settings.pg_database,
    )
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM incidents")
        n = cur.fetchone()[0]
    conn.close()
    return n


# ── E5 (real): LLM unreachable -> retryable -> flagged ───────────────

def test_e5_real_llm_unreachable_retry_then_flag():
    """The REAL failure, no mock: LLM_API_BASE=https://llms.elm.sa/v1 is
    NXDOMAIN from this box. The job must go retryable (with the real error)
    and FLAGGED after max attempts — never silently dropped."""
    db = "ai_w3i_e5test"
    mconn = psycopg2.connect(
        host=settings.pg_host, port=settings.pg_port,
        user=settings.pg_user, password=settings.pg_password,
        dbname="postgres",
    )
    mconn.autocommit = True
    with mconn.cursor() as cur:
        cur.execute("SELECT 1 FROM pg_database WHERE datname = %s", (db,))
        if cur.fetchone() is None:
            cur.execute(f"CREATE DATABASE {db}")
    mconn.close()

    ref = f"e5-{int(time.time())}"
    script = """
import os, time
from ai_classification.services.jobs.integration import enqueue, ensure_jobs_table, get_job, worker_tick
ref = os.environ["E5_REF"]
ensure_jobs_table()
enqueue({"source_reference": ref, "title": "E5 real NXDOMAIN",
         "description": "company endpoint unreachable from dev box", "status": "active"})
for i in range(3):
    worker_tick()
    j = get_job(ref)
    e = j.get("error") or {}
    print(f"attempt {i+1}: status={j['status']} code={e.get('code')} "
          f"err={(e.get('message') or '')[:300]}", flush=True)
    time.sleep(2.6)  # linear backoff = base*attempts (1s,2s) — wait it out
print(f"FINAL: {j['status']} attempts={j['attempts']}", flush=True)
"""
    env = os.environ.copy()
    env.update({
        "LLM_API_BASE": "https://llms.elm.sa/v1",   # the REAL unreachable endpoint
        "LLM_MODEL": "openai/qwen3.6",
        "LLM_API_KEY": "sk-none",
        "CASCADE_CLASSIFICATION": "false",  # legacy path: call_llm -> the real DNS failure
        "PG_DATABASE": db,
        "INTEGRATION_MAX_ATTEMPTS": "3",
        "INTEGRATION_RETRY_BASE_S": "1",
        "E5_REF": ref,
        "PYTHONPATH": os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    })
    r = subprocess.run([sys.executable, "-c", script], capture_output=True,
                       text=True, env=env, timeout=240)
    out = r.stdout + r.stderr

    assert "FINAL: flagged attempts=3" in out, out
    assert "attempt 1: status=retryable" in out, out
    assert "attempt 2: status=retryable" in out, out
    # the REAL failure signature (NXDOMAIN / resolution / connection)
    low = out.lower()
    assert any(k in low for k in (
        "getaddrinfo", "name resolution", "temporary failure", "failed to resolve",
        "connection error", "connect error", "max retries", "Temporary failure",
    )), out


# ── E6: auth on every non-health endpoint ────────────────────────────

def test_e6_all_integration_endpoints_require_auth(client):
    cases = [
        ("post", "/api/v1/incidents", {"json": PAYLOAD}),
        ("get", "/api/v1/incidents/TKT-1001", {}),
        ("post", "/api/v1/incidents/dry-run", {"json": PAYLOAD}),
        ("post", "/api/v1/backfill", {"json": {"incidents": [PAYLOAD]}}),
        ("get", "/api/v1/jobs", {}),
        ("post", "/api/v1/worker/tick", {}),
    ]
    for method, url, kw in cases:
        r = getattr(client, method)(url, **kw)  # no auth header
        assert r.status_code == 401, f"{method} {url} -> {r.status_code}"
        assert r.json()["error"]["code"] == Err.UNAUTHORIZED


def test_e6_bad_token_rejected(client):
    r = client.post("/api/v1/incidents", json=PAYLOAD,
                    headers={"Authorization": "Bearer WRONG"})
    assert r.status_code == 401
    assert r.json()["error"]["code"] == Err.UNAUTHORIZED


def test_e6_health_and_ready_exempt_from_auth(client):
    assert client.get("/health").status_code == 200
    assert client.get("/ready").status_code in (200, 503)


# ── E7: idempotent replay on source_reference ────────────────────────

def test_e7_replay_safe(client):
    r1 = client.post("/api/v1/incidents", json=PAYLOAD, headers=AUTH)
    r2 = client.post("/api/v1/incidents", json=PAYLOAD, headers=AUTH)
    assert r1.status_code == 202 and r2.status_code == 202
    assert r1.json()["reference"] == r2.json()["reference"]

    # ONE job row; replay did not enqueue a second
    jobs = list_jobs(10)
    assert len([j for j in jobs if j["source_reference"] == "TKT-1001"]) == 1

    worker_tick()
    job = _job("TKT-1001")
    assert job["status"] == "succeeded"
    assert job["attempts"] == 0  # replay never incremented attempts


# ── E8: strict schema — unknown fields rejected ──────────────────────

def test_e8_unknown_field_rejected(client):
    bad = {**PAYLOAD, "bogus_field": "not in the contract"}
    r = client.post("/api/v1/incidents", json=bad, headers=AUTH)
    assert r.status_code == 422, r.text
    body = r.json()
    assert body["error"]["code"] == Err.INVALID_PAYLOAD
    assert any(f["field"] == "bogus_field" for f in body["error"]["fields"])

    # same strictness on batch + dry-run
    r = client.post("/api/v1/backfill", json={"incidents": [bad]}, headers=AUTH)
    assert r.status_code == 422
    assert r.json()["error"]["code"] == Err.INVALID_PAYLOAD


def test_e8_missing_required_field(client):
    r = client.post("/api/v1/incidents",
                    json={"source_reference": "X", "description": "no title"},
                    headers=AUTH)
    assert r.status_code == 422
    assert r.json()["error"]["code"] == Err.INVALID_PAYLOAD


# ── E9: integration guide ships with the repo ────────────────────────

def test_e9_guide_exists_with_required_sections():
    guide = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))),
                         "docs", "INTEGRATION_GUIDE.md")
    assert os.path.exists(guide), f"missing {guide}"
    text = open(guide).read()
    for section in ("Authentication", "Error Codes", "Retry", "curl"):
        assert section.lower() in text.lower(), f"guide missing section: {section}"
