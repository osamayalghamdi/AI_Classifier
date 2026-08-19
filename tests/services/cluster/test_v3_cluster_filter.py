"""v3 clustering input filter — only incident/service_request tickets feed
clusters and the subsystem rollup.

The v2 refactor (feat/llm-first-clustering) DELETED the stateless rebuild
(_build_clusters / embedding graph clustering): clusters are now persistent
DB rows decided by the LLM (persistent.py Flows A/B/C/D). The v3 kind filter
therefore lives at the two remaining seams:

  1. _subsystem_rollup (grouping.py) — non-incident kinds never pollute the
     rollup the dashboard shows.
  2. Flow A's entry (persistent.assign_in_background) — non-incident kinds
     never join a problem cluster on arrival.

No network, no DB: grouping tests patch pure functions; the Flow A gate is
tested with a fake store module-level patch.
"""

import pytest

import ai_classification.services.cluster.grouping as g
import ai_classification.services.cluster.persistent as pc


def _mk_inc(i: int, kind: str,
            service: str = "pilgrim groups - Nusuk Masar Haj.Issue Permits") -> dict:
    inc = {
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
    return inc


class TestSubsystemRollupFilter:
    def test_non_incident_kinds_excluded_from_rollup(self):
        incidents = [
            _mk_inc(1, "incident"),
            _mk_inc(2, "service_request"),
            _mk_inc(3, "inquiry"),
            _mk_inc(4, "test"),
        ]
        rollup = g._subsystem_rollup(incidents)
        ids = [iid for r in rollup for iid in r["incident_ids"]]
        assert set(ids) == {"inc0001", "inc0002"}
        assert rollup[0]["count"] == 2

    def test_falls_back_to_classification_dict_kind(self):
        """Pre-column rows: kind lives only inside classification_dict."""
        incidents = [_mk_inc(1, "incident"), _mk_inc(2, "incident"), _mk_inc(3, "inquiry")]
        for inc in incidents:
            inc.pop("ticket_kind")
        incidents[0]["classification_dict"]["ticket_kind"] = "incident"
        incidents[1]["classification_dict"]["ticket_kind"] = "incident"
        incidents[2]["classification_dict"]["ticket_kind"] = "inquiry"
        rollup = g._subsystem_rollup(incidents)
        ids = [iid for r in rollup for iid in r["incident_ids"]]
        assert set(ids) == {"inc0001", "inc0002"}

    def test_legacy_rows_without_kind_default_to_incident(self):
        """No ticket_kind anywhere (legacy rows / fixtures) → treated as
        incident, the pre-v3 behavior — rollup must not break."""
        incidents = [_mk_inc(1, "incident"), _mk_inc(2, "incident")]
        for inc in incidents:
            inc.pop("ticket_kind")
            inc["classification_dict"].pop("ticket_kind", None)
        rollup = g._subsystem_rollup(incidents)
        ids = [iid for r in rollup for iid in r["incident_ids"]]
        assert set(ids) == {"inc0001", "inc0002"}

    def test_all_excluded_returns_empty_rollup(self):
        incidents = [_mk_inc(1, "test"), _mk_inc(2, "inquiry"), _mk_inc(3, "content_thin")]
        assert g._subsystem_rollup(incidents) == []


class TestFlowAGate:
    def test_non_incident_kind_skips_assignment(self, monkeypatch):
        """administrative ticket lands → Flow A must NOT fire."""
        called = []
        monkeypatch.setattr(pc.store, "get_incident", lambda iid: _mk_inc(1, "administrative"))
        monkeypatch.setattr(pc, "assign_incident", lambda iid: called.append(iid))
        pc.assign_in_background("inc0001")
        assert called == []

    def test_incident_kind_triggers_assignment(self, monkeypatch):
        called = []
        monkeypatch.setattr(pc.store, "get_incident", lambda iid: _mk_inc(1, "incident"))
        monkeypatch.setattr(pc, "assign_incident", lambda iid: called.append(iid))
        pc.assign_in_background("inc0001")
        # Thread starts async; give it a beat.
        import time
        for _ in range(50):
            if called:
                break
            time.sleep(0.01)
        assert called == ["inc0001"]

    def test_missing_incident_skips_assignment(self, monkeypatch):
        called = []
        monkeypatch.setattr(pc.store, "get_incident", lambda iid: None)
        monkeypatch.setattr(pc, "assign_incident", lambda iid: called.append(iid))
        pc.assign_in_background("inc9999")
        assert called == []

    def test_service_request_kind_triggers_assignment(self, monkeypatch):
        called = []
        monkeypatch.setattr(pc.store, "get_incident", lambda iid: _mk_inc(2, "service_request"))
        monkeypatch.setattr(pc, "assign_incident", lambda iid: called.append(iid))
        pc.assign_in_background("inc0002")
        import time
        for _ in range(50):
            if called:
                break
            time.sleep(0.01)
        assert called == ["inc0002"]
