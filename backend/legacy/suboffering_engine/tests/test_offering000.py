"""OFFERING-000 fallback tests (W3): contamination guard (F4) + deterministic
order-independence (F3, mocked verifier).

F4 — the guard: a deliberately-unrelated pair injected into the cross-domain
OFFERING-000 pool must NOT be merged. Two layers tested:
  A) strict verifier says NO -> union-find keeps it out (edge never formed);
  B) verifier lies (YES on everything) -> the member-level purity rule
     (member FM not in cluster top-2 -> needs_review) trips the >=1/3
     exclusion and the polluted cluster never becomes a proposal.

Uses the same conventions as test_suboffering: real test Postgres, mocked
embedding model, FakeVerifier in place of the live LLM.
"""
import random

import numpy as np
import pytest

from ai_classification.shared.config import settings as base_settings
from ai_classification.services.match.suboffering import OFFERING_000
from legacy.suboffering_engine.suboffering_cluster import run_pool

from tests.shared.test_incident_store import FixedVecModel, _make_store
from tests.conftest import TEST_PG_DATABASE


class FakeVerifier:
    """Deterministic verifier: verdict map keyed on sorted id pair."""

    def __init__(self, verdicts: dict[tuple[str, str], tuple[str, str]]):
        self.verdicts = verdicts
        self.unresolved = []
        self.usage = {"prompt_tokens": 0, "completion_tokens": 0, "calls": 0}

    @staticmethod
    def _key(a: dict, b: dict) -> tuple[str, str]:
        return tuple(sorted([a["id"], b["id"]]))

    def verify_pairs(self, pairs) -> list[dict]:
        out = []
        for a, b in pairs:
            d, r = self.verdicts.get(self._key(a, b), ("NO", "default NO"))
            out.append({"decision": d, "reason": r})
        return out

    def _ask_individual(self, a, b):
        d, r = self.verdicts.get(self._key(a, b), ("NO", ""))
        return (d, r)

    def _call(self, messages, max_tokens=1000) -> str:
        return "{}"  # drift remove-none / labels-empty


def _ticket(iid: str, text: str, svc: str) -> dict:
    return {"id": iid, "title": text, "description": text,
            "classification_dict": {"service": svc}}


@pytest.fixture
def engine_store(monkeypatch):
    """Real test Postgres + mocked embeddings + engine tables truncated."""
    import ai_classification.shared.store as store_mod
    from dataclasses import replace as _replace
    from legacy.suboffering_engine.store_suboffering import LegacySubOfferingStore

    monkeypatch.setattr(store_mod, "SentenceTransformer", lambda *a, **_: FixedVecModel())
    test_settings = _replace(base_settings, pg_database=TEST_PG_DATABASE)
    monkeypatch.setattr(store_mod, "settings", test_settings)
    s = LegacySubOfferingStore()
    s.setup()
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


def _vecs_for(pairs: dict[tuple[str, str], float]) -> dict[str, np.ndarray]:
    """Vectors with EXACT pairwise cosines: Cholesky of the target correlation
    matrix S (unit diagonal, off-diagonal = requested sims). Rows of L have
    unit norm and dot product S[i, j]."""
    ids = sorted({x for p in pairs for x in p})
    n = len(ids)
    S = np.eye(n)
    for (i, j), s in pairs.items():
        a, b = ids.index(i), ids.index(j)
        S[a, b] = S[b, a] = s
    L = np.linalg.cholesky(S)  # raises if S not positive-definite
    return {iid: L[k].astype(np.float32) for k, iid in enumerate(ids)}


def _patch_stores(monkeypatch, s):
    """Point both engine modules' module-level `store` at the test store so
    embed_pure / create_proposal hit the test DB."""
    monkeypatch.setattr("legacy.suboffering_engine.suboffering_cluster.store", s)
    monkeypatch.setattr("ai_classification.services.match.suboffering.store", s)


class TestF4ContaminationGuard:
    def test_unrelated_pair_not_merged(self, engine_store, monkeypatch):
        """F4-A: strict verifier NO on the unrelated pairs -> no merge."""
        _patch_stores(monkeypatch, engine_store)
        related = [_ticket(f"t-{i}", "rawdah permit date selection fails", "pilgrim groups.Nusuk.Issue Permits")
                   for i in range(3)]
        x = _ticket("t-x", "company evaluation icon missing from interface", "System/Application.Nusuk.Service Unavailability")  # unrelated
        incidents = related + [x]
        # related triple sim 0.70 mutual; unrelated at 0.60 to everyone (all >= floor)
        pairs = {("t-0", "t-1"): 0.70, ("t-0", "t-2"): 0.70, ("t-1", "t-2"): 0.70,
                 ("t-0", "t-x"): 0.60, ("t-1", "t-x"): 0.60, ("t-2", "t-x"): 0.60}
        vecs = _vecs_for(pairs)
        vec_map = {f"{t['title']}\n{t['description']}": vecs[t["id"]] for t in incidents}
        monkeypatch.setattr(engine_store, "_model", FixedVecModel(vec_map))
        verdicts = {("t-0", "t-1"): ("YES", "same failing action + surface"),
                    ("t-0", "t-2"): ("YES", "same failing action + surface"),
                    ("t-1", "t-2"): ("YES", "same failing action + surface")}
        verifier = FakeVerifier(verdicts)  # cross pairs default NO
        report = run_pool(OFFERING_000, incidents, verifier, max_proposal_members=10)
        proposals = report["proposals"]
        assert len(proposals) == 1, f"expected 1 proposal, got {len(proposals)}"
        members = set(proposals[0]["member_ids"])
        assert members == {"t-0", "t-1", "t-2"}, f"unrelated ticket merged: {members}"
        assert "t-x" not in members
        # related triple merged via identical-text auto-accept (sim 1.0);
        # the verifier's job was rejecting the cross pairs (t-x never merged)
        assert report["auto_accepted"] == 3

    def test_lying_verifier_caught_by_purity_floor(self, engine_store, monkeypatch):
        """F4-B: even if the verifier wrongly YESes everything, the member-level
        purity rule (cad886 class) excludes the polluted cluster from proposals."""
        _patch_stores(monkeypatch, engine_store)
        # 3 same-problem Rawdah tickets + 2 unrelated with distinct FMs (the
        # cad886 class: minority FM members)
        related = [_ticket(f"t-r{i}", "rawdah permit date selection fails", "pilgrim groups.Nusuk.Issue Permits") for i in range(3)]
        odd1 = _ticket("t-x", "approval issuance expedite request", "pilgrim groups.Nusuk.Expedite")
        odd2 = _ticket("t-y", "external pilgrim permit follow-up complaint", "pilgrim groups.Nusuk.Follow-up")
        incidents = related + [odd1, odd2]
        # identical text -> identical vectors -> sim 1.0 -> AUTO-ACCEPT all pairs
        # (bypasses the verifier entirely; the guard must catch the pollution)
        vec = np.zeros(16, dtype=np.float32)
        vec[0] = 1.0
        vec_map = {f"{t['title']}\n{t['description']}": vec.copy() for t in incidents}
        monkeypatch.setattr(engine_store, "_model", FixedVecModel(vec_map))
        verifier = FakeVerifier({})  # everything auto-accepted; verifier unused
        report = run_pool(OFFERING_000, incidents, verifier, max_proposal_members=10)
        assert report["proposals"] == [], "polluted cluster must NOT become a proposal"
        nr = report["needs_review_clusters"]
        assert len(nr) == 1 and nr[0]["size"] == 5, f"expected 5-member needs_review, got {nr}"
        assert nr[0]["flags"]["needs_review"] is True


# ── decision handler: OFFERING-000 approve can mint a NEW offering ──
class TestDecisionMintNewOffering:
    def test_approve_with_new_offering_name_mints_under_new_offering(self, engine_store, monkeypatch):
        """W3: approve + new_offering_name -> sub_offering minted under the NEW
        offering id (not the proposal's OFFERING-000 pool id)."""
        import legacy.suboffering_engine.proposal_routes as pr
        import ai_classification.services.match.suboffering as so
        monkeypatch.setattr(pr, "store", engine_store)
        monkeypatch.setattr(so, "store", engine_store)
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        from tests.shared.test_incident_store import _insert_raw, _make_result
        app = FastAPI()
        app.include_router(pr.router)
        s = engine_store
        for iid in ("i1", "i2", "i3"):
            _insert_raw(s, iid, f"title {iid}", f"desc {iid}", "",
                        np.zeros(1024, dtype=np.float32),
                        _make_result(), status="active")
        prop = s.create_proposal("OFFERING-000", ["i1", "i2", "i3"], 0.6, {},
                                 {"needs_review": False}, proposed_label="License number workflow")
        client = TestClient(app)
        r = client.post(f"/proposals/{prop['id']}/decision",
                        json={"decision": "approve", "new_offering_name": "Housing Provider Licensing"})
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["status"] == "approve"
        sub = s.get_sub_offering(body["target_sub_offering_id"])
        assert sub is not None
        assert sub["offering_id"] == "Housing Provider Licensing", \
            f"minted under wrong offering: {sub['offering_id']}"
        assert sub["name"] == "License number workflow"
        assert len(s.list_exemplars(sub["id"])) == 3

    def test_approve_without_name_keeps_pool_offering(self, engine_store, monkeypatch):
        """W2 semantics preserved: plain approve mints under the proposal's pool offering."""
        import legacy.suboffering_engine.proposal_routes as pr
        import ai_classification.services.match.suboffering as so
        monkeypatch.setattr(pr, "store", engine_store)
        monkeypatch.setattr(so, "store", engine_store)
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        app = FastAPI()
        app.include_router(pr.router)
        s = engine_store
        prop = s.create_proposal("some-pool", ["i1", "i2", "i3"], 0.6, {},
                                 {"needs_review": False})
        client = TestClient(app)
        r = client.post(f"/proposals/{prop['id']}/decision", json={"decision": "approve"})
        assert r.status_code == 200
        sub = s.get_sub_offering(r.json()["target_sub_offering_id"])
        assert sub["offering_id"] == "some-pool"


class TestF3OrderIndependence:
    def test_seed42_shuffle_same_membership(self, engine_store, monkeypatch):
        """F3 (mocked verifier): shuffled input order -> identical proposal membership."""
        _patch_stores(monkeypatch, engine_store)
        tickets = [_ticket(f"t-{i}", f"problem text number {i} in the system",
                           "svc-a" if i < 3 else f"svc-{(i % 5) + 1}")
                   for i in range(8)]
        # triangle 0.50 on the 0-1-2 clique (cohesion 0.50 >= 0.45 floor) +
        # chain 0.50 onward; verifier YES only on the clique edges
        pairs = {("t-0", "t-1"): 0.50, ("t-0", "t-2"): 0.50, ("t-1", "t-2"): 0.50}
        pairs.update({(f"t-{i}", f"t-{i + 1}"): 0.50 for i in range(2, 7)})
        vecs = _vecs_for(pairs)
        vec_map = {f"{t['title']}\n{t['description']}": vecs[t["id"]] for t in tickets}
        monkeypatch.setattr(engine_store, "_model", FixedVecModel(vec_map))
        # deterministic verifier: YES only for the 0-1-2 clique edges
        verdicts = {("t-0", "t-1"): ("YES", "same"), ("t-0", "t-2"): ("YES", "same"),
                    ("t-1", "t-2"): ("YES", "same")}
        natural = tickets[:]
        shuffled = tickets[:]
        random.Random(42).shuffle(shuffled)
        r1 = run_pool(OFFERING_000, natural, FakeVerifier(verdicts), max_proposal_members=10)
        r2 = run_pool(OFFERING_000, shuffled, FakeVerifier(verdicts), max_proposal_members=10)
        m1 = {frozenset(p["member_ids"]) for p in r1["proposals"]}
        m2 = {frozenset(p["member_ids"]) for p in r2["proposals"]}
        assert m1 == m2, f"membership changed under shuffle: {m1} vs {m2}"
        assert any(len(m) >= 3 for m in m1), "clique should have been proposed"
