"""Fix #1 — cluster-name cache invalidation.

Names are cached per member-ID fingerprint with a 24h TTL. When a ticket's
membership changes (moved between clusters, re-classified), the OLD and NEW
clusters' cached names must not be served stale: invalidate_incident()
evicts every cache entry whose fingerprint contains the moved ticket, and
the store mutation points (pool_remove / update_classification) call it.
"""

import time

from ai_classification.core import grouping as g


def _inc(i: str) -> dict:
    return {"id": i, "title": f"Rawdah permit fails {i}", "description": ""}


def _mk(*ids: str) -> list[dict]:
    return [_inc(i) for i in ids]


def _patch_llm(monkeypatch, calls: list | None = None):
    def fake(*a, **k):
        if calls is not None:
            calls.append(1)
        return "مشكلة الحجاج"
    monkeypatch.setattr(g, "call_llm", fake)


def test_invalidate_incident_evicts_old_and_new_clusters(monkeypatch):
    """Ticket i3 left cluster A and joined cluster B: BOTH A and B must lose
    their cached name; an unrelated cluster C keeps its cache."""
    calls = []
    _patch_llm(monkeypatch, calls)
    g._ar_name_cache.clear()
    a = _mk("i1", "i2", "i3", "i4", "i5")   # old cluster (i3 moved out)
    b = _mk("i3", "i6", "i7")               # new cluster (i3 moved in)
    c = _mk("i9", "i10")                    # unrelated
    g._arabic_cluster_name(a)
    g._arabic_cluster_name(b)
    g._arabic_cluster_name(c)
    assert len(calls) == 3

    g.invalidate_incident("i3")

    assert g._arabic_cluster_name(a) == "مشكلة الحجاج"  # re-ran LLM (evicted)
    assert g._arabic_cluster_name(b) == "مشكلة الحجاج"  # re-ran LLM (evicted)
    assert g._arabic_cluster_name(c) == "مشكلة الحجاج"  # cache hit, no LLM
    assert len(calls) == 5  # 2 fresh + 1 cached


def test_invalidate_incident_clears_snapshot_only_when_dropped(monkeypatch):
    g._ar_name_cache.clear()
    g._snapshot["daily"] = {"total_incidents": 1, "clusters": [], "status": "ok"}
    g.invalidate_incident("ghost-id")
    assert "daily" in g._snapshot  # nothing dropped -> no rebuild churn

    g._ar_name_cache["i1,i2"] = {"name": "x", "_cached_at": time.time()}
    g.invalidate_incident("i2")
    assert "daily" not in g._snapshot  # dropped -> next read rebuilds


def test_invalidate_cache_clears_ar_name_cache(monkeypatch):
    _patch_llm(monkeypatch)
    g._ar_name_cache.clear()
    g._verdict_cache["a,b"] = {"name": "v", "_cached_at": time.time()}
    g._arabic_cluster_name(_mk("i1", "i2"))
    assert g._ar_name_cache
    g.invalidate_cache()
    assert g._ar_name_cache == {}
    assert g._verdict_cache == {}


def test_ar_name_cache_ttl_expires(monkeypatch):
    calls = []
    _patch_llm(monkeypatch, calls)
    g._ar_name_cache.clear()
    incs = _mk("i1", "i2")
    g._arabic_cluster_name(incs)
    assert len(calls) == 1

    for fp in g._ar_name_cache:  # age the entry past the 24h TTL
        g._ar_name_cache[fp]["_cached_at"] = time.time() - g._AR_NAME_TTL - 1
    g._arabic_cluster_name(incs)
    assert len(calls) == 2  # expired -> LLM re-called, name regenerated


def test_store_mutation_helper_routes_to_grouping(monkeypatch):
    """The lazy-import bridge (store -> grouping) actually fires — a move or
    re-classification invalidates the cluster caches."""
    from ai_classification.core import grouping as grouping_mod
    from ai_classification.core import store as store_mod
    seen = []
    monkeypatch.setattr(grouping_mod, "invalidate_incident", lambda iid: seen.append(iid))

    store_mod.IncidentStore._invalidate_cluster_caches("i3")
    assert seen == ["i3"]
