"""Volume-adaptive clustering sensitivity — tests at both volume extremes.

The sensitivity (embedding threshold + minimum cluster size) is a pure
function of the active-incident count:
  <= LOOSE_AT (20)  → loose regime:  threshold 0.40, min size 2
  >= TIGHT_AT (150) → tight regime:  threshold 0.60, min size 4
  in between        → linear interpolation (deterministic)

Graph-level behavior is tested with synthetic similarity matrices and a
monkeypatched LLM validator (no network, no DB).
"""

import numpy as np
import pytest

from ai_classification.core import grouping as g


@pytest.fixture(autouse=True)
def _clear_verdict_cache():
    """The verdict cache is module-global and keyed by incident-ID
    fingerprint — clear it between tests so cached verdicts from one test
    never leak into another (discovered: coherent verdict from a prior test
    made an incoherent-verdict test pass for the wrong reason)."""
    g._verdict_cache.clear()
    yield
    g._verdict_cache.clear()


# ── Pure function: _sensitivity_params ───────────────────────────────────

class TestSensitivityParams:
    def test_loose_regime_at_zero(self):
        assert g._sensitivity_params(0) == (0.40, 2)

    def test_loose_regime_at_low_volume(self):
        assert g._sensitivity_params(5) == (0.40, 2)
        assert g._sensitivity_params(20) == (0.40, 2)

    def test_tight_regime_at_flood(self):
        assert g._sensitivity_params(150) == (0.60, 4)
        assert g._sensitivity_params(500) == (0.60, 4)

    def test_interpolation_is_monotonic_and_bounded(self):
        prev = None
        for count in range(21, 150):
            t, m = g._sensitivity_params(count)
            assert 0.40 <= t <= 0.60, f"threshold out of bounds at {count}"
            assert 2 <= m <= 4, f"min_size out of bounds at {count}"
            if prev is not None:
                assert t >= prev - 1e-9, f"threshold not monotonic at {count}"
            prev = t

    def test_midpoint_interpolation(self):
        # 85 = midpoint between 20 and 150 → threshold midpoint 0.50, min 3
        t, m = g._sensitivity_params(85)
        assert t == pytest.approx(0.50, abs=0.001)
        assert m == 3

    def test_deterministic(self):
        for count in (0, 7, 42, 99, 151, 1000):
            assert g._sensitivity_params(count) == g._sensitivity_params(count)


# ── Graph behavior at the two extremes ───────────────────────────────────

def _fake_verdict(monkeypatch, verdict=None):
    """Patch the LLM validator so graph tests need no network."""
    default = {"is_coherent": True, "keep": None, "remove": [],
               "name": "مشكلة مشتركة", "description": "test"}
    v = verdict or default

    def _fake(validator_input):
        ids = [x["id"] for x in validator_input]
        keep = v.get("keep")
        return {
            "is_coherent": v.get("is_coherent", True),
            "keep": keep if keep is not None else ids,
            "remove": v.get("remove", []),
            "name": v.get("name", "مشكلة مشتركة"),
            "description": v.get("description", "test"),
        }

    monkeypatch.setattr(g, "validate_group", _fake)


def _mk_inc(i: int, fm: str = "FM-000", title: str = "") -> dict:
    return {
        "id": f"inc{i:04d}",
        "title": title or f"Incident {i}",
        "description": f"description {i}",
        "classification_dict": {"failure_mode": fm},
        "status": "active",
    }


class TestClusterPassVolumeExtremes:
    def test_loose_regime_groups_related_pairs(self, monkeypatch):
        """5 incidents: two related pairs (sim 0.55) — in the loose regime
        (threshold 0.40, min size 2) they MUST group; a fixed 0.50
        threshold would have left them as singletons."""
        _fake_verdict(monkeypatch)
        incidents = tuple(_mk_inc(i) for i in range(5))
        # 0-1 similar (0.55), 2-3 similar (0.55), 4 isolated
        sim = np.zeros((5, 5))
        sim[0, 1] = sim[1, 0] = 0.55
        sim[2, 3] = sim[3, 2] = 0.55

        t, m = g._sensitivity_params(len(incidents))   # loose regime
        assert t == 0.40 and m == 2
        clusters, _ = g._cluster_pass(incidents, sim, t, list(range(5)), m)
        assert len(clusters) == 2, "loose regime must group both related pairs"
        sizes = sorted(c["count"] for c in clusters)
        assert sizes == [2, 2]

    def test_tight_regime_rejects_weak_pairs(self, monkeypatch):
        """150 incidents: a weak pair (sim 0.55) must NOT group in the
        tight regime (threshold 0.60, min size 4) — precision wins."""
        _fake_verdict(monkeypatch)
        incidents = tuple(_mk_inc(i) for i in range(150))
        sim = np.zeros((150, 150))
        sim[0, 1] = sim[1, 0] = 0.55   # below tight threshold

        t, m = g._sensitivity_params(len(incidents))   # tight regime
        assert t == 0.60 and m == 4
        clusters, used = g._cluster_pass(incidents, sim, t, list(range(150)), m)
        assert clusters == [], "tight regime must not group a weak pair"
        assert used == set()

    def test_tight_regime_accepts_strong_group(self, monkeypatch):
        """Flood: a strong 5-member group (all-pairs sim 0.80) still forms."""
        _fake_verdict(monkeypatch)
        incidents = tuple(_mk_inc(i) for i in range(200))
        sim = np.zeros((200, 200))
        for a in range(5):
            for b in range(5):
                if a != b:
                    sim[a, b] = sim[b, a] = 0.80

        t, m = g._sensitivity_params(len(incidents))
        assert t == 0.60 and m == 4
        clusters, _ = g._cluster_pass(incidents, sim, t, list(range(200)), m)
        assert len(clusters) == 1 and clusters[0]["count"] == 5

    def test_loose_min_size_two_allows_pair_groups(self, monkeypatch):
        """Few incidents: a 2-member group is a real group (min size 2)."""
        _fake_verdict(monkeypatch)
        incidents = tuple(_mk_inc(i) for i in range(3))
        sim = np.zeros((3, 3))
        sim[0, 1] = sim[1, 0] = 0.85

        clusters, _ = g._cluster_pass(incidents, sim, 0.40, list(range(3)), 2)
        assert len(clusters) == 1 and clusters[0]["count"] == 2

    def test_incoherent_group_rejected(self, monkeypatch):
        """Even in the loose regime, an LLM-rejected group is not emitted."""
        _fake_verdict(monkeypatch, verdict={"is_coherent": False, "keep": [], "remove": []})
        incidents = tuple(_mk_inc(i) for i in range(5))
        sim = np.zeros((5, 5))
        sim[0, 1] = sim[1, 0] = 0.55
        sim[2, 3] = sim[3, 2] = 0.55

        clusters, _ = g._cluster_pass(incidents, sim, 0.40, list(range(5)), 2)
        assert clusters == []


# ── End-to-end: _build_clusters respects the adaptive params ─────────────

class TestBuildClustersAdaptive:
    def test_few_incidents_returns_early_without_errors(self, monkeypatch):
        """Empty-ish store: no crash, empty clusters, subsystem rollup."""
        monkeypatch.setattr(g.store, "list_incidents", lambda status="active": [])
        monkeypatch.setattr(g.store, "list_incidents_with_embeddings", lambda: [])
        result = g._build_clusters("daily")
        assert result["total_incidents"] == 0
        assert result["clusters"] == []

    def test_single_incident_no_crash(self, monkeypatch):
        one = [(_mk_inc(0), np.ones(4, dtype=np.float32))]
        monkeypatch.setattr(g.store, "list_incidents", lambda status="active": [one[0][0]])
        monkeypatch.setattr(g.store, "list_incidents_with_embeddings", lambda: one)
        result = g._build_clusters("daily")
        assert result["total_incidents"] == 1
        assert result["clusters"] == []
