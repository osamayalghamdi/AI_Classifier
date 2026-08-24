"""Admin console API tests — taxonomy overrides, env, groups, status, reset.

Auth: every /admin/* endpoint requires the bearer token (same dependency
as /api/v1/*). Tests use the isolated-store + TestClient pattern from
tests/services/ingest/test_integration_api.py so the global store singleton
is never touched, and _clean_state truncates the test DB after each test.

Taxonomy overrides are exercised against the REAL DB (test DB) — the frozen
base taxonomy is never modified; the runtime registry is reset by the
autouse fixture so tests don't leak overrides into each other.
"""

from __future__ import annotations

import os

os.environ.setdefault("INTEGRATION_TOKEN", "test-token")
os.environ.setdefault("INTEGRATION_WORKER_ENABLED", "0")

import pytest
from fastapi.testclient import TestClient

from tests.services.classify.test_cascade import _settings_with, make_fake_completion

import ai_classification.services.classify.classifier as classifier_mod
import ai_classification.services.classify.llm as mod_llm
from ai_classification.app import app
from ai_classification.domain.taxonomy import set_runtime_overrides

TOKEN = "test-token"
AUTH = {"Authorization": f"Bearer {TOKEN}"}

FAKE_CLASSIFICATION = os.environ.get(
    "FAKE_CLASSIFICATION",
    '{"affected_system":"Nusuk Masar Haj","service":"pilgrim groups and issue permit - Nusuk Masar Haj.Issue Permits",'
    '"incident_type":"Unavailability","severity":"Major","urgency":"High","category":"Software",'
    '"confidence":"high","reasoning":"admin test","canonical_statement":"permit fails",'
    '"signature":"permit fails on date"}',
)


@pytest.fixture(autouse=True)
def _reset_overrides():
    """Never leak taxonomy overrides between tests."""
    yield
    set_runtime_overrides({})


@pytest.fixture(autouse=True)
def _fake_llm(monkeypatch):
    monkeypatch.setattr(classifier_mod, "settings", _settings_with(False))

    def fake_completion(**kwargs):
        return make_fake_completion(FAKE_CLASSIFICATION)

    monkeypatch.setattr(mod_llm, "completion", fake_completion)
    yield


@pytest.fixture(scope="module")
def client():
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
def _clean_state():
    yield
    import psycopg2
    from ai_classification.shared.config import settings
    conn = psycopg2.connect(host=settings.pg_host, port=settings.pg_port,
                            user=settings.pg_user, password=settings.pg_password,
                            dbname=settings.pg_database)
    conn.autocommit = True
    with conn.cursor() as cur:
        cur.execute("TRUNCATE ingestion_jobs, incidents RESTART IDENTITY CASCADE")
        cur.execute("TRUNCATE taxonomy_overrides")
        cur.execute("TRUNCATE clusters, cluster_members RESTART IDENTITY CASCADE")
    conn.close()


# ── Auth: every admin endpoint requires the token ────────────────────

def test_admin_all_endpoints_require_auth(client):
    cases = [
        ("get", "/admin/status", {}),
        ("get", "/admin/taxonomy", {}),
        ("post", "/admin/taxonomy/service", {"json": {"system": "CRM", "service": "X"}}),
        ("get", "/admin/env", {}),
        ("post", "/admin/incidents", {"json": {"title": "t", "description": "d"}}),
        ("post", "/admin/reset", {}),
        ("get", "/admin/groups", {}),
        ("post", "/admin/groups", {"json": {"name_ar": "g"}}),
    ]
    for method, url, kw in cases:
        r = getattr(client, method)(url, **kw)
        assert r.status_code == 401, f"{method} {url} -> {r.status_code}"


def test_admin_status(client):
    r = client.get("/admin/status", headers=AUTH)
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["incidents"]["total"] >= 0
    assert "clusters" in d and "unassigned_pool" in d
    assert d["store_ready"] is True


# ── Taxonomy overrides ───────────────────────────────────────────────

def test_taxonomy_add_service_then_delete(client):
    # add service with offerings
    r = client.post("/admin/taxonomy/service", headers=AUTH,
                    json={"system": "CRM", "service": "Admin Test Service",
                          "offerings": ["Offer One", "Offer Two"]})
    assert r.status_code == 200, r.text
    # effective view now contains it (immediate effect)
    r = client.get("/admin/taxonomy", headers=AUTH)
    syss = {s["system"]: s for s in r.json()["systems"]}
    svcs = {s["service"]: s for s in syss["CRM"]["services"]}
    assert "Admin Test Service" in svcs
    assert svcs["Admin Test Service"]["offerings"] == ["Offer One", "Offer Two"]
    # delete -> gone
    r = client.request("DELETE", "/admin/taxonomy/service", headers=AUTH,
                       json={"system": "CRM", "service": "Admin Test Service"})
    assert r.status_code == 200
    r = client.get("/admin/taxonomy", headers=AUTH)
    syss = {s["system"]: s for s in r.json()["systems"]}
    svcs = {s["service"]: s for s in syss["CRM"]["services"]}
    assert "Admin Test Service" not in svcs


def test_taxonomy_add_offering_requires_existing_service_flow(client):
    # add a service first, then an offering via the offering endpoint
    client.post("/admin/taxonomy/service", headers=AUTH,
                json={"system": "CRM", "service": "Svc A"})
    r = client.post("/admin/taxonomy/offering", headers=AUTH,
                    json={"system": "CRM", "service": "Svc A", "offering": "Off X"})
    assert r.status_code == 200, r.text
    r = client.get("/admin/taxonomy", headers=AUTH)
    syss = {s["system"]: s for s in r.json()["systems"]}
    svcs = {s["service"]: s for s in syss["CRM"]["services"]}
    assert "Off X" in svcs["Svc A"]["offerings"]


def test_taxonomy_missing_fields_rejected(client):
    r = client.post("/admin/taxonomy/service", headers=AUTH, json={"system": ""})
    assert r.status_code == 422


# ── Env ──────────────────────────────────────────────────────────────

def test_env_list_masks_secrets(client):
    r = client.get("/admin/env", headers=AUTH)
    assert r.status_code == 200
    keys = {k["key"]: k for k in r.json()["keys"]}
    assert "LLM_API_KEY" in keys
    # masked value never equals the raw env value in full
    raw = os.environ.get("LLM_API_KEY", "")
    if raw:
        assert keys["LLM_API_KEY"]["masked"] != raw


def test_env_write_rejects_unknown_key(client):
    r = client.post("/admin/env", headers=AUTH, json={"key": "NOT_MANAGED", "value": "x"})
    assert r.status_code == 422


# ── Incidents ────────────────────────────────────────────────────────

def test_admin_add_incident(client):
    r = client.post("/admin/incidents", headers=AUTH,
                    json={"title": "Rawdah permit fails", "description": "error"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body.get("incident_id") or body.get("id")


# ── Groups ───────────────────────────────────────────────────────────

def test_groups_crud(client):
    r = client.post("/admin/groups", headers=AUTH,
                    json={"name_ar": "مجموعة اختبار", "description": "test group"})
    assert r.status_code == 200, r.text
    cid = r.json()["cluster"]["id"]
    # list
    r = client.get("/admin/groups", headers=AUTH)
    assert any(g["cluster_id"] == cid for g in r.json()["groups"])
    # adjust (rename)
    r = client.patch(f"/admin/groups/{cid}", headers=AUTH, json={"name_ar": "اسم جديد"})
    assert r.status_code == 200
    assert r.json()["cluster"]["name_ar"] == "اسم جديد"
    # missing cluster -> 404
    r = client.patch("/admin/groups/nope", headers=AUTH, json={"name_ar": "x"})
    assert r.status_code == 404


def test_group_delete(client):
    r = client.post("/admin/groups", headers=AUTH, json={"name_ar": "to delete"})
    cid = r.json()["cluster"]["id"]
    r = client.delete(f"/admin/groups/{cid}", headers=AUTH)
    assert r.status_code == 200, r.text
    assert r.json()["deleted"] is True
    # gone from listing
    r = client.get("/admin/groups", headers=AUTH)
    assert not any(g["cluster_id"] == cid for g in r.json()["groups"])
    # deleting a missing group -> 404
    r = client.delete("/admin/groups/does-not-exist", headers=AUTH)
    assert r.status_code == 404


def test_taxonomy_json_import(client):
    payload = {"CRM": {"Imported Svc": ["Off 1", "Off 2"], "Imported Bare": []}}
    r = client.post("/admin/taxonomy/import", headers=AUTH, json=payload)
    assert r.status_code == 200, r.text
    assert r.json()["services_added"] == 2
    assert r.json()["offerings_added"] == 2
    # effective immediately
    r = client.get("/admin/taxonomy", headers=AUTH)
    syss = {s["system"]: s for s in r.json()["systems"]}
    svcs = {s["service"]: s for s in syss["CRM"]["services"]}
    assert "Imported Svc" in svcs
    assert svcs["Imported Svc"]["offerings"] == ["Off 1", "Off 2"]
    assert "Imported Bare" in svcs
    # invalid payloads rejected
    r = client.post("/admin/taxonomy/import", headers=AUTH, json={})
    assert r.status_code == 422
    r = client.post("/admin/taxonomy/import", headers=AUTH, json=[])
    assert r.status_code == 422


def test_group_member_add_remove(client):
    r = client.post("/admin/groups", headers=AUTH, json={"name_ar": "grp"})
    cid = r.json()["cluster"]["id"]
    # add an incident first
    inc = client.post("/admin/incidents", headers=AUTH,
                      json={"title": "member test", "description": "x"}).json()
    iid = inc.get("incident_id") or inc.get("id")
    r = client.post(f"/admin/groups/{cid}/members", headers=AUTH, json={"incident_id": iid})
    assert r.status_code == 200, r.text
    r = client.get("/admin/groups", headers=AUTH)
    g = next(g for g in r.json()["groups"] if g["cluster_id"] == cid)
    assert g["member_count"] == 1
    r = client.delete(f"/admin/groups/{cid}/members/{iid}", headers=AUTH)
    assert r.status_code == 200
    r = client.get("/admin/groups", headers=AUTH)
    g = next(g for g in r.json()["groups"] if g["cluster_id"] == cid)
    assert g["member_count"] == 0


# ── Reset ────────────────────────────────────────────────────────────

def test_admin_reset(client):
    # seed one incident, reset, expect zero
    client.post("/admin/incidents", headers=AUTH,
                json={"title": "to be wiped", "description": "x"})
    r = client.post("/admin/reset", headers=AUTH)
    assert r.status_code == 200, r.text
    assert r.json()["incidents_deleted"] >= 1
    d = client.get("/admin/status", headers=AUTH).json()
    assert d["incidents"]["total"] == 0


# ── Assignment groups ─────────────────────────────────────────────────

def test_assignment_groups_seeded(client):
    r = client.get("/admin/assignment-groups", headers=AUTH)
    assert r.status_code == 200
    names = {g["name"] for g in r.json()["groups"]}
    assert {"Payments", "Infrastructure", "Operations", "App Support"} <= names


def test_assignment_group_add_patch_delete(client):
    r = client.post("/admin/assignment-groups", headers=AUTH,
                    json={"name": "Network Ops", "description": "network team",
                          "sort_order": 7})
    assert r.status_code == 200, r.text
    gid = r.json()["group"]["id"]
    # patch (rename + deactivate)
    r = client.patch(f"/admin/assignment-groups/{gid}", headers=AUTH,
                     json={"name": "Network Ops 2", "active": False})
    assert r.status_code == 200
    groups = r.json()["group"]
    updated = next(g for g in groups if g["id"] == gid)
    assert updated["name"] == "Network Ops 2" and updated["active"] is False
    # missing name -> 422
    r = client.post("/admin/assignment-groups", headers=AUTH, json={"name": ""})
    assert r.status_code == 422
    # delete
    r = client.delete(f"/admin/assignment-groups/{gid}", headers=AUTH)
    assert r.status_code == 200
    r = client.delete(f"/admin/assignment-groups/{gid}", headers=AUTH)
    assert r.status_code == 404


def test_assignment_group_requires_auth(client):
    r = client.get("/admin/assignment-groups")
    assert r.status_code == 401


# ── System activation ─────────────────────────────────────────────────

def test_systems_list_all_present(client):
    r = client.get("/admin/systems", headers=AUTH)
    assert r.status_code == 200
    names = {s["system"] for s in r.json()["systems"]}
    assert {"Nusuk Masar Haj", "CRM", "Nusuk Masar Umrah", "OldSM", "Other"} <= names


def test_system_deactivate_hides_from_effective_view(client):
    from ai_classification.domain.taxonomy import effective_services_by_system, AffectedSystem
    assert AffectedSystem.crm in effective_services_by_system()
    r = client.patch("/admin/systems/CRM", headers=AUTH,
                     json={"active": False, "note": "covered by call centre"})
    assert r.status_code == 200, r.text
    # effective immediately: CRM gone from the merged taxonomy
    assert AffectedSystem.crm not in effective_services_by_system()
    assert AffectedSystem.nusuk_masar_haj in effective_services_by_system()
    assert AffectedSystem.other in effective_services_by_system()  # always kept
    # re-activate
    r = client.patch("/admin/systems/CRM", headers=AUTH, json={"active": True})
    assert r.status_code == 200
    assert AffectedSystem.crm in effective_services_by_system()
    # cleanup the row
    r = client.delete("/admin/systems/CRM", headers=AUTH)
    assert r.status_code == 200
    assert AffectedSystem.crm in effective_services_by_system()


def test_system_unknown_rejected(client):
    r = client.patch("/admin/systems/NotASystem", headers=AUTH, json={"active": False})
    assert r.status_code == 422
    r = client.patch("/admin/systems/CRM", headers=AUTH, json={})
    assert r.status_code == 422


# ── Endpoints reference ───────────────────────────────────────────────

def test_endpoints_list(client):
    r = client.get("/admin/endpoints", headers=AUTH)
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["count"] >= 40
    paths = {e["path"] for e in d["endpoints"]}
    assert "/classify" in paths and "/admin/status" in paths and "/api/v1/incidents" in paths
    ep = [e for e in d["endpoints"] if e["path"] == "/classify"]
    assert {e["method"] for e in ep} == {"GET", "POST"}  # both methods registered
    assert all(e["summary"] for e in ep)  # has a description


# ── Model registry (enable / disable control) ────────────────────────

def test_models_list_all_registry_entries(client):
    r = client.get("/admin/models", headers=AUTH)
    assert r.status_code == 200, r.text
    d = r.json()
    names = {m["name"] for m in d["models"]}
    assert {"QWEN3_6", "GEMMA_4", "QWEN3_VL_32B", "OLMOCR_2_7B", "BGE_RERANKER"} <= names
    by_name = {m["name"]: m for m in d["models"]}
    assert by_name["QWEN3_6"]["role"] == "classifier"
    assert by_name["QWEN3_VL_32B"]["role"] == "ocr"
    assert by_name["BGE_RERANKER"]["role"] == "reranker"
    assert "active" in d and "classifier" in d["active"]


def test_models_list_requires_auth(client):
    r = client.get("/admin/models")
    assert r.status_code == 401


def test_models_enable_disable_writes_env_and_rejects_unknown(monkeypatch, client, tmp_path):
    """enable/disable write MODEL_<NAME>_ENABLED to the env file and
    reject unknown model names (422)."""
    import ai_classification.api.admin as admin_mod
    from ai_classification.shared.config import settings as base_settings
    from dataclasses import replace

    env_file = tmp_path / "env_test"
    fake_settings = replace(base_settings, admin_env_file=str(env_file))
    monkeypatch.setattr(admin_mod, "settings", fake_settings)

    # Unknown model → 422
    r = client.post("/admin/models/NOPE/enable", headers=AUTH, json={})
    assert r.status_code == 422

    # Disable GEMMA_4 → env file gets MODEL_GEMMA_4_ENABLED=0
    r = client.post("/admin/models/GEMMA_4/disable", headers=AUTH, json={})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["enabled"] is False and body["restart_required"] is True
    assert "MODEL_GEMMA_4_ENABLED=0" in env_file.read_text()

    # Enable it back → env file gets =1
    r = client.post("/admin/models/GEMMA_4/enable", headers=AUTH, json={})
    assert r.status_code == 200
    assert "MODEL_GEMMA_4_ENABLED=1" in env_file.read_text()


def test_registry_qwen36_defaults_enabled_when_llm_model_set(monkeypatch):
    """Backward compat: with LLM_MODEL set (existing .env), QWEN3_6 is
    the active classifier even without an explicit MODEL_* flag."""
    import ai_classification.shared.config as cfg
    monkeypatch.setenv("LLM_MODEL", "openai/qwen3.6")
    monkeypatch.delenv("MODEL_QWEN3_6_ENABLED", raising=False)
    reg = cfg._build_model_registry()
    assert reg["QWEN3_6"].enabled is True
    active = cfg._resolve_active_model("classifier")
    assert active is not None and active.name == "QWEN3_6"


def test_registry_all_disabled_yields_no_active_classifier(monkeypatch):
    import ai_classification.shared.config as cfg
    monkeypatch.setenv("LLM_MODEL", "")
    for name in cfg.MODEL_CATALOG:
        monkeypatch.setenv(f"MODEL_{name}_ENABLED", "0")
    reg = cfg._build_model_registry()
    assert all(not e.enabled for e in reg.values())
    assert cfg._resolve_active_model("classifier") is None
