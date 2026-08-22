"""Fix #2 — cross-offering repool phase.

Phase 1 matches within the ticket's OWN offering (feed_incident, 0.60).
Phase 2 re-tries the survivors against ALL active sub-offerings at a
stricter threshold (0.75) so a problem spanning two offerings still gets
clustered — without turning the pool into a grab-bag.
"""

import numpy as np

import legacy.suboffering_engine.store_suboffering as store_mod
import legacy.suboffering_engine.suboffering as sub_mod
from legacy.suboffering_engine import repool

_VEC = np.ones(8, dtype=np.float32)


class FakeStore:
    def __init__(self, incidents, pool_entries, subs, exemplars):
        self._incidents = {i["id"]: dict(i) for i in incidents}
        self._pool = [dict(e) for e in pool_entries]
        self._subs = [dict(s) for s in subs]
        self._ex = {sid: [dict(r) for r in rows] for sid, rows in exemplars.items()}
        self.added = []     # (sub_id, incident_id)
        self.removed = []   # incident_ids removed from the pool
        self._proposals = []

    def pool_list(self, offering_id=None):
        return [dict(e) for e in self._pool]

    def list_incidents(self):
        return list(self._incidents.values())

    def list_sub_offerings(self, offering_id=None, status=None):
        out = [dict(s) for s in self._subs]
        if offering_id is not None:
            out = [s for s in out if s["offering_id"] == offering_id]
        return out

    def list_exemplars(self, sub_id):
        return list(self._ex.get(sub_id, []))

    def pool_remove(self, offering_id, incident_id):
        self.removed.append(incident_id)
        self._pool = [e for e in self._pool if e["incident_id"] != incident_id]

    def add_exemplar(self, sub_id, incident_id, title, description, emb):
        self.added.append((sub_id, incident_id))

    def list_proposals(self, status=None):
        return list(self._proposals)


def _ticket(tid: str, service: str) -> dict:
    return {"id": tid, "title": f"ticket {tid}", "description": "",
            "classification_dict": {"service": service}}


def _pool_entry(tid: str, offering: str) -> dict:
    return {"offering_id": offering, "incident_id": tid}


def _sub(sid: str, offering: str) -> dict:
    return {"id": sid, "offering_id": offering}


def _exemplar(sid: str) -> dict:
    return {"sub_offering_id": sid, "embedding": "[1.0]"}


def _patch_store(monkeypatch, fstore):
    monkeypatch.setattr(store_mod, "store", fstore)


def _patch_no_phase1(monkeypatch):
    """Phase 1 never matches (own-offering scoped, no hits); embedding works."""
    def no_match(inc):
        svc = (inc.get("classification_dict") or {}).get("service", "")
        return {"offering": sub_mod.offering_of(svc) or sub_mod.OFFERING_000,
                "routed": "pool", "matched": False}
    monkeypatch.setattr(sub_mod, "feed_incident", no_match)
    # repool_once imports embed_pure from the LIVE module
    # (ai_classification.services.match.suboffering) — patch there too.
    import ai_classification.services.match.suboffering as live_sub_mod
    monkeypatch.setattr(live_sub_mod, "embed_pure", lambda t, d: _VEC)


def test_phase1_never_crosses_offerings(monkeypatch):
    """A ticket whose only match lives in ANOTHER offering is not moved by
    phase 1 (scoped to its own offering) — exactly the gap phase 2 closes."""
    fs = FakeStore(
        incidents=[_ticket("tA1", "OfferingA.Service1")],
        pool_entries=[_pool_entry("tA1", "OfferingA")],
        subs=[_sub("subB", "OfferingB"), _sub("subA", "OfferingA")],
        exemplars={"subB": [_exemplar("subB")], "subA": [_exemplar("subA")]},
    )
    _patch_store(monkeypatch, fs)
    _patch_no_phase1(monkeypatch)

    stats = repool.repool_once()

    assert stats["phase1_moved"] == 0
    assert stats["phase2_moved"] == 0
    assert fs.removed == []


def test_phase2_matches_across_offerings(monkeypatch):
    fs = FakeStore(
        incidents=[_ticket("tA1", "OfferingA.Service1")],
        pool_entries=[_pool_entry("tA1", "OfferingA")],
        subs=[_sub("subB", "OfferingB")],
        exemplars={"subB": [_exemplar("subB")]},
    )
    _patch_store(monkeypatch, fs)
    _patch_no_phase1(monkeypatch)
    monkeypatch.setattr(sub_mod, "match_against_exemplars",
                        lambda emb, exs: ("subB", 0.80))  # >= 0.75

    stats = repool.repool_once()

    assert stats["phase1_moved"] == 0
    assert stats["phase2_moved"] == 1
    assert stats["remaining"] == 0
    assert fs.added == [("subB", "tA1")]
    assert fs.removed == ["tA1"]


def test_phase2_threshold_blocks_grab_bags(monkeypatch):
    fs = FakeStore(
        incidents=[_ticket("tA1", "OfferingA.Service1")],
        pool_entries=[_pool_entry("tA1", "OfferingA")],
        subs=[_sub("subB", "OfferingB")],
        exemplars={"subB": [_exemplar("subB")]},
    )
    _patch_store(monkeypatch, fs)
    _patch_no_phase1(monkeypatch)
    monkeypatch.setattr(sub_mod, "match_against_exemplars",
                        lambda emb, exs: ("subB", 0.70))  # < 0.75

    stats = repool.repool_once()

    assert stats["phase2_moved"] == 0
    assert stats["remaining"] == 1
    assert fs.added == [] and fs.removed == []


def test_stats_report_both_phases(monkeypatch):
    """One ticket moved by phase 1, one by phase 2 (cross-offering, 0.80),
    one left over (0.70 < 0.75)."""
    fs = FakeStore(
        incidents=[_ticket("tP1", "OfferingA.Service1"),
                   _ticket("tA1", "OfferingA.Service2"),
                   _ticket("tL1", "OfferingA.Service3")],
        pool_entries=[_pool_entry("tP1", "OfferingA"),
                      _pool_entry("tA1", "OfferingA"),
                      _pool_entry("tL1", "OfferingA")],
        subs=[_sub("subA", "OfferingA"), _sub("subB", "OfferingB")],
        exemplars={"subA": [_exemplar("subA")], "subB": [_exemplar("subB")]},
    )
    _patch_store(monkeypatch, fs)
    import ai_classification.services.match.suboffering as live_sub_mod
    monkeypatch.setattr(live_sub_mod, "embed_pure",
                        lambda t, d: np.full(8, 1.0, dtype=np.float32) if "tA1" in t
                        else np.full(8, 2.0, dtype=np.float32))

    def feed(inc):
        if inc["id"] == "tP1":
            return {"offering": "OfferingA", "routed": "matched", "matched": True,
                    "sub_offering_id": "subA", "sim": 0.9}
        return {"offering": "OfferingA", "routed": "pool", "matched": False}
    monkeypatch.setattr(sub_mod, "feed_incident", feed)

    def match(emb, exs):
        return ("subB", 0.80 if emb[0] == 1.0 else 0.70)
    monkeypatch.setattr(sub_mod, "match_against_exemplars", match)

    stats = repool.repool_once()

    assert stats["phase1_moved"] == 1      # tP1 (own offering)
    assert stats["phase2_moved"] == 1      # tA1 (cross-offering, 0.80)
    assert stats["remaining"] == 1         # tL1 (0.70 < 0.75)
    assert fs.removed == ["tP1", "tA1"]
    # tP1's add_exemplar happens inside the real feed_incident (stubbed
    # here); only the phase-2 cross-match is observable through repool.
    assert fs.added == [("subB", "tA1")]


def test_dry_run_no_writes_no_embedding(monkeypatch):
    fs = FakeStore(
        incidents=[_ticket("tA1", "OfferingA.Service1")],
        pool_entries=[_pool_entry("tA1", "OfferingA")],
        subs=[_sub("subB", "OfferingB")],
        exemplars={"subB": [_exemplar("subB")]},
    )
    _patch_store(monkeypatch, fs)
    monkeypatch.setattr(sub_mod, "feed_incident",
                        lambda inc: (_ for _ in ()).throw(
                            AssertionError("dry-run must not call feed_incident")))
    monkeypatch.setattr(sub_mod, "embed_pure",
                        lambda t, d: (_ for _ in ()).throw(
                            AssertionError("dry-run must not embed")))

    stats = repool.repool_once(dry_run=True)

    assert stats["phase1_moved"] == 0 and stats["phase2_moved"] == 0
    assert stats["remaining"] == 1
    assert stats["pool_before"] == 1
    assert fs.added == [] and fs.removed == []
