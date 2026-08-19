"""v3 clustering input filter — only incident/service_request tickets feed
_build_clusters (embedding clustering) and _subsystem_rollup.

Everything else (administrative, inquiry, feature_request, test,
content_thin) is excluded from clustering input. Same monkeypatch
convention as the other cluster tests: g.store methods + g.call_llm patched,
no network, no DB.
"""

import numpy as np
import pytest

import ai_classification.services.cluster.grouping as g

_EMB = np.ones(4, dtype=np.float32)


@pytest.fixture(autouse=True)
def _clear_caches():
    """Module-global caches keyed by member-ID fingerprints — clear between
    tests so cached verdicts/names from one test never leak into another."""
    g._verdict_cache.clear()
    g._ar_name_cache.clear()
    g._snapshot.clear()
    yield
    g._verdict_cache.clear()
    g._ar_name_cache.clear()
    g._snapshot.clear()


def _mk_inc(i: int, kind: str,
            service: str = "pilgrim groups - Nusuk Masar Haj.Issue Permits") -> dict:
    return {
        "id": f"inc{i:04d}",
        "title": f"Ticket {i}",
        "description": f"description {i}",
        "ticket_kind": kind,
        "classification_dict": {
            "service": service,
            "affected_system": "Nusuk Masar Haj",
            "severity": "Major",
        },
        "status": "active",
    }


def _patch_store(monkeypatch, incidents: list[dict]) -> None:
    raw = [(inc, _EMB) for inc in incidents]
    monkeypatch.setattr(g.store, "list_incidents", lambda status="active": list(incidents))
    monkeypatch.setattr(g.store, "list_incidents_with_embeddings", lambda: raw)
    monkeypatch.setattr(g.store, "list_sub_offerings", lambda *a, **k: [])
    monkeypatch.setattr(g.store, "list_exemplars", lambda *a, **k: [])
    monkeypatch.setattr(g, "call_llm", lambda *a, **k: "مشكلة مشتركة")


def _all_member_ids(result: dict) -> list[str]:
    out = []
    for c in result["clusters"]:
        out.extend(inc["id"] for inc in c["incidents"])
    return out


class TestClusterInputFilter:
    def test_non_incident_kinds_excluded_from_clusters(self, monkeypatch):
        incidents = [
            _mk_inc(1, "incident"),
            _mk_inc(2, "incident"),
            _mk_inc(3, "administrative"),
            _mk_inc(4, "feature_request"),
        ]
        _patch_store(monkeypatch, incidents)
        result = g._build_clusters("daily")

        # Only the 2 incident tickets are clustering input.
        assert result["total_incidents"] == 2
        assert set(_all_member_ids(result)) == {"inc0001", "inc0002"}

    def test_subsystem_rollup_excludes_non_incident_kinds(self, monkeypatch):
        incidents = [
            _mk_inc(1, "incident"),
            _mk_inc(2, "service_request"),
            _mk_inc(3, "inquiry"),
            _mk_inc(4, "test"),
        ]
        _patch_store(monkeypatch, incidents)
        result = g._build_clusters("daily")

        rollup_ids = [iid for r in result["subsystem_summary"] for iid in r["incident_ids"]]
        assert set(rollup_ids) == {"inc0001", "inc0002"}
        assert result["subsystem_summary"][0]["count"] == 2

    def test_service_request_is_cluster_input(self, monkeypatch):
        incidents = [_mk_inc(1, "incident"), _mk_inc(2, "service_request")]
        _patch_store(monkeypatch, incidents)
        result = g._build_clusters("daily")
        assert result["total_incidents"] == 2
        assert set(_all_member_ids(result)) == {"inc0001", "inc0002"}

    def test_falls_back_to_classification_dict_kind(self, monkeypatch):
        """Pre-column rows: kind lives only inside classification_dict."""
        incidents = [_mk_inc(1, "incident"), _mk_inc(2, "incident")]
        for inc in incidents:
            inc.pop("ticket_kind")
            inc["classification_dict"]["ticket_kind"] = "incident"
        _patch_store(monkeypatch, incidents)
        result = g._build_clusters("daily")
        assert result["total_incidents"] == 2

    def test_legacy_rows_without_kind_default_to_incident(self, monkeypatch):
        """No ticket_kind anywhere (legacy rows / fixtures) → treated as
        incident, the pre-v3 behavior — clustering must not break."""
        incidents = [_mk_inc(1, "incident"), _mk_inc(2, "incident")]
        for inc in incidents:
            inc.pop("ticket_kind")
            inc["classification_dict"].pop("ticket_kind", None)
        _patch_store(monkeypatch, incidents)
        result = g._build_clusters("daily")
        assert result["total_incidents"] == 2

    def test_all_excluded_returns_empty_clusters(self, monkeypatch):
        incidents = [_mk_inc(1, "test"), _mk_inc(2, "inquiry"), _mk_inc(3, "content_thin")]
        _patch_store(monkeypatch, incidents)
        result = g._build_clusters("daily")
        assert result["total_incidents"] == 0
        assert result["clusters"] == []
