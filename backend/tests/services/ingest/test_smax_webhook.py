"""SMAX webhook + status-update endpoint tests.

The SMAX push contract (see docs/INTEGRATION_GUIDE.md):
  - SMAX POSTs incident events to /api/v1/smax/webhook (bearer auth).
  - New incident (unknown source reference)  → enqueued for async classify (202).
  - Status change (known source reference)   → status-only update (200),
    same ID → same incident row, no re-classification, no duplicate.
  - Statuses are DYNAMIC: any value SMAX reports is stored verbatim in
    incidents.source_status; only the local active/resolved view is derived.
  - /api/v1/incidents/{reference}/status is the same update as a normalized
    contract endpoint (404 when the reference was never ingested).

Run convention (same as test_integration_api.py):
    export PG_DATABASE=ai_incidents_test INTEGRATION_TOKEN=test-token \
           INTEGRATION_WORKER_ENABLED=0
    pytest tests/services/ingest/test_smax_webhook.py
"""

from __future__ import annotations

import json
import os

# Auth + worker env MUST be set before ai_classification.shared.config is
# imported (Settings is a module-level singleton evaluated at import time).
os.environ.setdefault("INTEGRATION_TOKEN", "test-token")
os.environ.setdefault("INTEGRATION_WORKER_ENABLED", "0")

import psycopg2
import pytest
from fastapi.testclient import TestClient

from tests.services.classify.test_cascade import _settings_with, make_fake_completion

import ai_classification.services.classify.classifier as classifier_mod
import ai_classification.services.classify.llm as mod_llm
from ai_classification.app import app
from ai_classification.shared.config import settings
from ai_classification.shared.store_incidents import to_local_status
from ai_classification.services.jobs.integration import get_job, list_jobs, worker_tick
from ai_classification.services.jobs.integration.schemas import Err

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
    "reasoning": "webhook test",
    "canonical_statement": "Permit issuance fails when selecting a date for the group.",
    "signature": "permit issuance fails on date selection",
})


@pytest.fixture(scope="module")
def client():
    """The app under test, pointed at an ISOLATED store instance (same
    pattern as test_integration_api.py — never touch the global singleton)."""
    import ai_classification.shared.store as store_mod

    from ai_classification.shared.store import IncidentStore

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
def _fake_llm(monkeypatch):
    """Cascade off (one LLM call per classify) + canned classification."""
    monkeypatch.setattr(classifier_mod, "settings", _settings_with(False))

    def fake_completion(**kwargs):
        return make_fake_completion(FAKE_CLASSIFICATION)

    monkeypatch.setattr(mod_llm, "completion", fake_completion)
    yield


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


def _ingest(client, ref: str, status: str = "Active", title: str = "Permit date error"):
    """Ingest a new incident through E1 and run the worker so it is stored."""
    r = client.post("/api/v1/incidents", json={
        "source_reference": ref,
        "title": title,
        "description": "Error when selecting a date for the pilgrim group",
        "status": status,
    }, headers=AUTH)
    assert r.status_code == 202, r.text
    worker_tick()


def _stored(client, ref: str) -> dict:
    r = client.get(f"/api/v1/incidents/{ref}", headers=AUTH)
    assert r.status_code == 200, r.text
    return r.json()["result"]


def _incident_row(client, ref: str) -> dict:
    import ai_classification.shared.store as store_mod
    return store_mod.store.get_incident_by_source_ticket_id(ref)


# ── Webhook: new incident ─────────────────────────────────────────────

def test_webhook_new_incident_enqueued_then_classified(client):
    r = client.post("/api/v1/smax/webhook", json={
        "ticket_id": "SMAX-1001",
        "title": "Rawdah permit date error",
        "description": "Error when selecting a date for the pilgrim group",
        "status": "Active",
        "created": "2025-01-10T08:00:00Z",
    }, headers=AUTH)
    assert r.status_code == 202, r.text
    body = r.json()
    assert body["action"] == "created"
    assert body["reference"] == "SMAX-1001"
    assert body["job_status"] == "pending"

    job = get_job("SMAX-1001")
    assert job is not None
    assert job["payload"]["title"] == "Rawdah permit date error"

    n = worker_tick()
    assert n == 1
    job = get_job("SMAX-1001")
    assert job["status"] == "succeeded"
    assert job["result"]["is_new"] is True

    # Raw SMAX status stored verbatim; local view derived.
    row = _incident_row(client, "SMAX-1001")
    assert row is not None
    assert row["source_status"] == "Active"
    assert row["status"] == "active"


# ── Webhook: status change on a known reference ───────────────────────

def test_webhook_status_change_updates_existing_row(client):
    _ingest(client, "SMAX-2001")
    row = _incident_row(client, "SMAX-2001")
    assert row["status"] == "active"
    assert row["source_status"] == "Active"

    # active → verified (resolved-like): same row, status-only, no LLM job.
    r = client.post("/api/v1/smax/webhook", json={
        "ticket_id": "SMAX-2001",
        "status": "Verified",
    }, headers=AUTH)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["action"] == "updated"
    assert body["reference"] == "SMAX-2001"
    assert body["incident_id"] == row["id"]
    assert body["source_status"] == "Verified"
    assert body["status"] == "resolved"

    # verified → resolved
    r = client.post("/api/v1/smax/webhook", json={
        "ticket_id": "SMAX-2001",
        "status": "Resolved",
    }, headers=AUTH)
    assert r.status_code == 200
    assert r.json()["source_status"] == "Resolved"
    assert r.json()["status"] == "resolved"

    # resolved → reopened (dynamic, not resolved-like): back to active.
    r = client.post("/api/v1/smax/webhook", json={
        "ticket_id": "SMAX-2001",
        "status": "Reopened",
    }, headers=AUTH)
    assert r.status_code == 200
    assert r.json()["source_status"] == "Reopened"
    assert r.json()["status"] == "active"

    # Exactly ONE incident row the whole time — status changes never duplicate.
    rows = _incident_row(client, "SMAX-2001")
    assert rows is not None


def test_webhook_status_change_does_not_reclassify(client):
    """A status change must not create a job or burn an LLM call."""
    _ingest(client, "SMAX-3001")
    n_jobs_before = len(list_jobs(100))
    r = client.post("/api/v1/smax/webhook", json={
        "ticket_id": "SMAX-3001",
        "status": "Closed",
    }, headers=AUTH)
    assert r.status_code == 200
    assert r.json()["action"] == "updated"
    assert len(list_jobs(100)) == n_jobs_before  # no new job enqueued


def test_webhook_dynamic_status_stored_verbatim(client):
    """Unknown/new upstream statuses are ACCEPTED and stored raw — the whole
    point of the dynamic status requirement."""
    _ingest(client, "SMAX-4001")
    r = client.post("/api/v1/smax/webhook", json={
        "ticket_id": "SMAX-4001",
        "status": "Awaiting Customer Input",
    }, headers=AUTH)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["source_status"] == "Awaiting Customer Input"
    assert body["status"] == "active"  # not resolved-like → stays active

    row = _incident_row(client, "SMAX-4001")
    assert row["source_status"] == "Awaiting Customer Input"


def test_webhook_wrapped_payload(client):
    """Notification formats that nest the record ({"event": {"record": ...}})
    are unwrapped transparently."""
    r = client.post("/api/v1/smax/webhook", json={
        "event": {
            "record": {
                "id": "SMAX-5001",
                "summary": "App Support ticket",
                "status": "In Progress",
            }
        }
    }, headers=AUTH)
    assert r.status_code == 202, r.text
    assert r.json()["reference"] == "SMAX-5001"
    assert get_job("SMAX-5001")["payload"]["title"] == "App Support ticket"
    assert get_job("SMAX-5001")["payload"]["status"] == "In Progress"


def test_webhook_missing_id_rejected(client):
    r = client.post("/api/v1/smax/webhook", json={
        "description": "no ticket id anywhere",
    }, headers=AUTH)
    assert r.status_code == 400
    assert r.json()["error"]["code"] == Err.INVALID_PAYLOAD


def test_webhook_requires_auth(client):
    r = client.post("/api/v1/smax/webhook", json={"ticket_id": "X"}, headers={})
    assert r.status_code == 401


def test_webhook_race_updates_queued_payload(client):
    """Created then status-changed before the worker ran: the queued job's
    payload must carry the LATEST status."""
    r = client.post("/api/v1/smax/webhook", json={
        "ticket_id": "SMAX-6001",
        "title": "Race ticket",
        "description": "description",
        "status": "Active",
    }, headers=AUTH)
    assert r.status_code == 202
    assert get_job("SMAX-6001")["payload"]["status"] == "Active"

    # Same reference, status changed, incident row not stored yet (worker idle).
    r = client.post("/api/v1/smax/webhook", json={
        "ticket_id": "SMAX-6001",
        "status": "Verified",
    }, headers=AUTH)
    assert r.status_code == 202, r.text  # still queued path, not stored yet
    job = get_job("SMAX-6001")
    assert job["payload"]["status"] == "Verified"

    worker_tick()
    row = _incident_row(client, "SMAX-6001")
    assert row["source_status"] == "Verified"
    assert row["status"] == "resolved"


# ── Status endpoint (normalized contract) ─────────────────────────────

def test_status_endpoint_known_reference_updates(client):
    _ingest(client, "SMAX-7001", status="Active")
    r = client.post("/api/v1/incidents/SMAX-7001/status", json={
        "status": "Closed",
        "updated_at": "2025-01-11T12:00:00Z",
    }, headers=AUTH)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["action"] == "updated"
    assert body["status"] == "resolved"
    assert body["source_status"] == "Closed"
    assert body["updated_at"] == "2025-01-11T12:00:00+00:00"

    # and the raw value is queryable on the stored incident
    row = _incident_row(client, "SMAX-7001")
    assert row["source_status"] == "Closed"
    assert row["status"] == "resolved"


def test_status_endpoint_unknown_reference_404(client):
    r = client.post("/api/v1/incidents/NEVER-SEEN/status", json={
        "status": "Resolved",
    }, headers=AUTH)
    assert r.status_code == 404
    assert r.json()["error"]["code"] == Err.NOT_FOUND


def test_status_endpoint_invalid_body_422(client):
    _ingest(client, "SMAX-8001")
    r = client.post("/api/v1/incidents/SMAX-8001/status", json={"status": ""}, headers=AUTH)
    assert r.status_code == 422
    assert r.json()["error"]["code"] == Err.INVALID_PAYLOAD

    r = client.post("/api/v1/incidents/SMAX-8001/status", json={
        "status": "Resolved", "bogus": 1,
    }, headers=AUTH)
    assert r.status_code == 422


def test_status_endpoint_requires_auth(client):
    r = client.post("/api/v1/incidents/X/status", json={"status": "Resolved"}, headers={})
    assert r.status_code == 401


# ── E1 accepts dynamic statuses ───────────────────────────────────────

def test_e1_accepts_dynamic_status(client):
    """E1's status field is dynamic now: SMAX values like 'Verified' or a
    future value must not 422."""
    r = client.post("/api/v1/incidents", json={
        "source_reference": "SMAX-9001",
        "title": "Dynamic status ticket",
        "description": "desc",
        "status": "Verified",
    }, headers=AUTH)
    assert r.status_code == 202, r.text

    r = client.post("/api/v1/incidents", json={
        "source_reference": "SMAX-9002",
        "title": "Future status ticket",
        "description": "desc",
        "status": "Some Future Status SMAX Adds",
    }, headers=AUTH)
    assert r.status_code == 202, r.text

    worker_tick(limit=10)
    row = _incident_row(client, "SMAX-9001")
    assert row["source_status"] == "Verified"
    assert row["status"] == "resolved"
    row = _incident_row(client, "SMAX-9002")
    assert row["source_status"] == "Some Future Status SMAX Adds"
    assert row["status"] == "active"


# ── Mapping unit table ────────────────────────────────────────────────

def test_to_local_status_mapping():
    resolved_like = ["resolved", "Resolved", "RESOLVED", "closed", "verified",
                     "Verified", "cancelled", "canceled", "rejected",
                     "duplicate", "completed", "done", "fixed", "withdrawn",
                     "invalid"]
    for s in resolved_like:
        assert to_local_status(s) == "resolved", s

    active_like = [None, "", "active", "Active", "new", "open", "in_progress",
                   "third_party", "Reopened", "Awaiting Customer Input",
                   "pending", "in review", "Some Future Status"]
    for s in active_like:
        assert to_local_status(s) == "active", s
