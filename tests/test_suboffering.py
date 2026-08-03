"""Tests for the sub-offering engine (Phase 2): store round-trip, first-segment
offering key, deterministic candidates, prompt-v3 identity, proposal API flow.

Runs against the real test Postgres (conftest TEST_PG_DATABASE), like
test_incident_store — catches real SQL/pgvector integration bugs.
"""
import numpy as np
import pytest
from dataclasses import replace
from fastapi.testclient import TestClient

from ai_classification.config import settings as base_settings
from ai_classification.core.store import IncidentStore
from ai_classification.core.suboffering import offering_of, embed_pure
from ai_classification.core.suboffering_cluster import generate_candidates
from ai_classification.core import verifier as verifier_mod

from .conftest import TEST_PG_DATABASE
from .test_incident_store import FixedVecModel, _truncate, _make_store


# ── prompt identity (drift guard: engine copy must equal the canary copy) ──
def test_prompt_identity_with_canary():
    import ast
    import re
    import tests.test_pairwise_canary as canary
    engine = verifier_mod.STRICT_PROMPT_V3
    canary_prompt = canary.STRICT_PROMPT_V3
    assert engine == canary_prompt, (
        "core/verifier.py STRICT_PROMPT_V3 drifted from tests/test_pairwise_canary.py"
    )
    assert verifier_mod.PROMPT_VERSION == "v3"


# ── offering key: FIRST segment of service string ─────────────────────
class TestOfferingOf:
    def test_first_segment(self):
        assert offering_of("pilgrim groups and issue permit - Nusuk Masar Haj.Issue Permits") == \
            "pilgrim groups and issue permit - Nusuk Masar Haj"
        assert offering_of("System/Application - Nusuk Masar Haj.Service Unavailability") == \
            "System/Application - Nusuk Masar Haj"

    def test_numeric_prefix_artifact(self):
        # "7.1 Invoicing..." contains a dot in the service name itself — literal
        # first segment is "7". Documented artifact (see W2 report).
        assert offering_of("7.1 Invoicing and Billing - Nusuk Masar Haj.Bill Payment") == "7"

    def test_no_offering_is_none(self):
        assert offering_of("General / Unspecified") is None
        assert offering_of("") is None
        assert offering_of(None) is None


# ── candidate generation: frozen params + deterministic tie-break ─────
class TestCandidates:
    def test_tiebreak_sim_desc_id_asc(self):
        # 3 tickets; sim matrix crafted: two equal-sim pairs (0.8) for ticket 0
        n = 3
        sim = np.zeros((n, n), dtype=np.float32)
        sim[0, 1] = sim[1, 0] = 0.8
        sim[0, 2] = sim[2, 0] = 0.8
        sim[1, 2] = sim[2, 1] = 0.5
        np.fill_diagonal(sim, -1.0)
        inc = [{"id": f"id-{i}"} for i in range(n)]
        cands = generate_candidates(inc, sim, floor=0.40, top_n=10)
        # pair (0,1) and (0,2) tie at 0.8 -> ordered by (min id, max id): (0,1) first
        assert cands[0][:2] == (0, 1)
        assert cands[1][:2] == (0, 2)
        # sim DESC overall
        sims = [c[2] for c in cands]
        assert sims == sorted(sims, reverse=True)

    def test_floor_and_topn(self):
        n = 5
        sim = np.full((n, n), 0.3, dtype=np.float32)
        np.fill_diagonal(sim, -1.0)
        inc = [{"id": f"id-{i}"} for i in range(n)]
        assert generate_candidates(inc, sim, floor=0.40, top_n=10) == []  # all below floor
        sim = np.full((n, n), 0.6, dtype=np.float32)
        np.fill_diagonal(sim, -1.0)
        cands = generate_candidates(inc, sim, floor=0.40, top_n=10)
        assert len(cands) == 10  # C(5,2) = 10, all above floor


# ── engine store round-trip ───────────────────────────────────────────
@pytest.fixture
def engine_store(monkeypatch):
    s = _make_store(monkeypatch, FixedVecModel())
    conn = s._getconn()
    try:
        with conn.cursor() as cur:
            for t in ("sub_offering_exemplars", "sub_offerings", "unmatched_pool", "cluster_proposals"):
                cur.execute(f"TRUNCATE {t}")
        conn.commit()
    finally:
        s._putconn(conn)
    yield s
    s.close()


class TestEngineStore:
    def test_sub_offering_round_trip(self, engine_store):
        s = engine_store
        sub = s.create_sub_offering("pilgrim groups - X", "Rawdah issue", status="active")
        assert sub is not None and sub["status"] == "active"
        assert s.get_sub_offering(sub["id"])["name"] == "Rawdah issue"
        assert len(s.list_sub_offerings(offering_id="pilgrim groups - X")) == 1
        assert s.list_sub_offerings(offering_id="other") == []
        assert s.set_sub_offering_status(sub["id"], "rejected") is True
        assert s.get_sub_offering(sub["id"])["status"] == "rejected"

    def test_exemplar_round_trip(self, engine_store):
        s = engine_store
        sub = s.create_sub_offering("o1", "n1")
        emb = np.zeros((1024,), dtype=np.float32)
        emb[0] = 1.0
        ex = s.add_exemplar(sub["id"], "inc-1", "title", "desc", emb)
        assert ex is not None and ex["incident_id"] == "inc-1"
        rows = s.list_exemplars(sub["id"])
        assert len(rows) == 1 and rows[0]["embedding"] is not None

    def test_pool_round_trip(self, engine_store):
        s = engine_store
        s.pool_add("o1", "i1")
        s.pool_add("o1", "i2")
        s.pool_add("o1", "i2")  # conflict -> no dup
        s.pool_add("o2", "i3")
        assert len(s.pool_list("o1")) == 2
        assert len(s.pool_list()) == 3
        s.pool_remove("o1", "i1")
        assert len(s.pool_list("o1")) == 1
        s.pool_remove_many("o1", ["i2"])
        assert s.pool_list("o1") == []
        assert s.pool_clear() == 1

    def test_proposal_round_trip_and_one_shot(self, engine_store):
        s = engine_store
        prop = s.create_proposal("o1", ["i1", "i2"], 0.72,
                                 {"a~b": "same failing action"}, {"mean_sim": 0.72, "needs_review": False})
        assert prop["status"] == "pending" and prop["member_ids"] == ["i1", "i2"]
        decided = s.decide_proposal(prop["id"], "approve", note="ok")
        assert decided["status"] == "approve"
        # one-shot: second decision is a no-op (stays approve)
        again = s.decide_proposal(prop["id"], "reject", note="late")
        assert again["status"] == "approve"
        assert s.list_proposals(status="approve")[0]["id"] == prop["id"]


# ── proposal API flow (routes against the test store) ─────────────────
@pytest.fixture
def api(engine_store, monkeypatch):
    import ai_classification.api.proposal_routes as pr
    import ai_classification.core.suboffering as so
    monkeypatch.setattr(pr, "store", engine_store)
    monkeypatch.setattr(so, "store", engine_store)
    from ai_classification.api.routes import app
    return TestClient(app)


class TestProposalAPI:
    def test_empty_list(self, api):
        r = api.get("/proposals")
        assert r.status_code == 200 and r.json()["total"] == 0

    def test_approve_mints_sub_offering(self, api, engine_store):
        s = engine_store
        prop = s.create_proposal("o1", ["i1"], 0.7, {}, {"needs_review": False},
                                 proposed_label="Rawdah err")
        r = api.post(f"/proposals/{prop['id']}/decision",
                     json={"decision": "approve", "note": "looks right"})
        assert r.status_code == 200
        body = r.json()
        assert body["status"] == "approve" and body["target_sub_offering_id"]
        sub = s.get_sub_offering(body["target_sub_offering_id"])
        assert sub is not None and sub["status"] == "active" and sub["name"] == "Rawdah err"

    def test_reject_sets_cooldown(self, api, engine_store):
        s = engine_store
        prop = s.create_proposal("o1", ["i1"], 0.7, {}, {"needs_review": False})
        r = api.post(f"/proposals/{prop['id']}/decision", json={"decision": "reject"})
        assert r.status_code == 200 and r.json()["status"] == "reject"
        pool = s.pool_list("o1")
        assert any(p["incident_id"] == "i1" and p["cooldown_until"] is not None for p in pool)

    def test_merge_requires_target(self, api, engine_store):
        s = engine_store
        prop = s.create_proposal("o1", ["i1"], 0.7, {}, {"needs_review": False})
        r = api.post(f"/proposals/{prop['id']}/decision", json={"decision": "merge"})
        assert r.status_code == 422  # missing target_sub_offering_id

    def test_double_decision_409(self, api, engine_store):
        s = engine_store
        prop = s.create_proposal("o1", ["i1"], 0.7, {}, {"needs_review": False})
        api.post(f"/proposals/{prop['id']}/decision", json={"decision": "approve"})
        r = api.post(f"/proposals/{prop['id']}/decision", json={"decision": "reject"})
        assert r.status_code == 409

    def test_decision_404(self, api):
        r = api.post("/proposals/nonexistent/decision", json={"decision": "approve"})
        assert r.status_code == 404
