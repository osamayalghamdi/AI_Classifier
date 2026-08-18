"""Tests for classify_and_store/resolve_incident — orchestration between
core.classifier and core.store (formerly one service.py module, now split
along the same seam: store lifecycle in core.store, classify orchestration
in core.classifier)."""

import pytest
from datetime import datetime, timedelta, timezone

import ai_classification.services.classify.classifier as classifier
import ai_classification.shared.store as store_module
from ai_classification.shared.store import store, SimilarMatch
from ai_classification.domain.models import ClassificationResult
from ai_classification.domain.taxonomy import AffectedSystem, IncidentType, Severity, Urgency, Category


def _make_result(**overrides) -> ClassificationResult:
    defaults = dict(
        affected_system=AffectedSystem.nusuk_masar_haj,
        service="System/Application - Nusuk Masar Haj",
        incident_type=IncidentType.degradation,
        severity=Severity.major,
        urgency=Urgency.high,
        category=Category.software,
        confidence="high",
        reasoning="test",
        canonical_statement="Test incident.",
        signature="Test incident signature for grouping",
    )
    defaults.update(overrides)
    return ClassificationResult(**defaults)


# ── classify_and_store ──────────────────────────────────────────────


class TestClassifyAndStore:
    def test_saves_incident_and_returns_id(self, monkeypatch):
        result_cls = _make_result()
        monkeypatch.setattr(classifier, "classify", lambda title, desc: result_cls)
        monkeypatch.setattr(store, "find_similar", lambda *a, **k: [])
        saved = {}
        monkeypatch.setattr(
            store, "save_incident",
            lambda iid, title, desc, cls, extracted_text="", **k: saved.update(id=iid, title=title),
        )
        monkeypatch.setattr(store, "generate_id", lambda: "abc123")

        resp = classifier.classify_and_store("Checkout down", "504 errors")
        assert resp.incident_id == "abc123"
        assert resp.incident_title == "Checkout down"
        assert resp.classification == result_cls
        assert saved == {"id": "abc123", "title": "Checkout down"}

    def test_maps_similar_matches_to_response(self, monkeypatch):
        result_cls = _make_result()
        monkeypatch.setattr(classifier, "classify", lambda title, desc: result_cls)
        monkeypatch.setattr(store, "save_incident", lambda *a, **k: None)
        monkeypatch.setattr(store, "generate_id", lambda: "new-id")
        match = SimilarMatch(id="dup-1", title="Similar incident", similarity=0.91, classification=result_cls)
        monkeypatch.setattr(store, "find_similar", lambda *a, **k: [match])

        resp = classifier.classify_and_store("Checkout down", "504 errors")
        assert len(resp.similar_open_incidents) == 1
        dupe = resp.similar_open_incidents[0]
        assert dupe.id == "dup-1"
        assert dupe.title == "Similar incident"
        assert dupe.similarity == pytest.approx(0.91)

    def test_no_similar_incidents_returns_empty_list(self, monkeypatch):
        result_cls = _make_result()
        monkeypatch.setattr(classifier, "classify", lambda title, desc: result_cls)
        monkeypatch.setattr(store, "save_incident", lambda *a, **k: None)
        monkeypatch.setattr(store, "generate_id", lambda: "new-id")
        monkeypatch.setattr(store, "find_similar", lambda *a, **k: [])

        resp = classifier.classify_and_store("Checkout down", "504 errors")
        assert resp.similar_open_incidents == []


# ── Dedupe: content-hash gate (no source_ticket_id) + ID path ─────────


class TestDedupe:
    """Both dedupe paths in classify_and_store:

    - content-hash gate: same {title, description} with NO source_ticket_id
      twice → same incident_id, occurrence_count incremented, no re-classify.
    - ID path: same source_ticket_id twice → same incident_id, no increment.
    """

    @staticmethod
    def _existing_by_hash(incident_id="dup-hash-1", last_seen=None) -> dict:
        return {
            "id": incident_id,
            "occurrence_count": 1,
            "first_seen": datetime.now(timezone.utc),
            "last_seen": last_seen or datetime.now(timezone.utc),
            "source_ticket_ids": ["inc-1"],
            "classification_json": _make_result().model_dump(mode="json"),
        }

    @staticmethod
    def _existing_by_ticket_id(incident_id="dup-id-1") -> dict:
        return {
            "id": incident_id,
            "title": "Duplicate ticket",
            "description": "dup",
            "classification_dict": _make_result().model_dump(mode="python"),
            "status": "active",
        }

    def test_content_hash_gate_returns_existing_and_increments(self, monkeypatch):
        """Same {title, description}, no source_ticket_id → same incident_id."""
        existing = self._existing_by_hash()
        monkeypatch.setattr(classifier, "classify", lambda *a, **k: (_ for _ in ()).throw(AssertionError("classify must not run")))
        monkeypatch.setattr(store, "get_incident_by_hash", lambda h: existing)
        incremented = []
        monkeypatch.setattr(store, "increment_occurrence", lambda iid: incremented.append(iid))
        monkeypatch.setattr(store, "find_similar", lambda *a, **k: [])

        resp1 = classifier.classify_and_store("Same ticket", "same description")
        resp2 = classifier.classify_and_store("Same ticket", "same description")

        assert resp1.incident_id == "dup-hash-1"
        assert resp2.incident_id == resp1.incident_id
        assert incremented == ["dup-hash-1", "dup-hash-1"]  # once per duplicate call

    def test_content_hash_gate_expired_window_reclassifies(self, monkeypatch):
        """last_seen older than 7 days → dedupe window expired → fresh classify."""
        old = datetime.now(timezone.utc) - timedelta(days=8)
        existing = self._existing_by_hash(last_seen=old)
        monkeypatch.setattr(store, "get_incident_by_hash", lambda h: existing)
        monkeypatch.setattr(store, "find_similar", lambda *a, **k: [])
        monkeypatch.setattr(store, "generate_id", lambda: "fresh-id")
        saved = []
        monkeypatch.setattr(store, "save_incident", lambda iid, title, desc, cls, extracted_text="", **k: saved.append(iid))
        monkeypatch.setattr(classifier, "classify", lambda title, desc: _make_result())
        incremented = []
        monkeypatch.setattr(store, "increment_occurrence", lambda iid: incremented.append(iid))

        resp = classifier.classify_and_store("Old duplicate", "old description")

        assert resp.incident_id == "fresh-id"
        assert incremented == []  # expired window must NOT increment

    def test_id_path_returns_same_incident_without_increment(self, monkeypatch):
        """Same source_ticket_id twice → same incident_id, no occurrence bump."""
        existing = self._existing_by_ticket_id()
        monkeypatch.setattr(store, "get_incident_by_source_ticket_id", lambda tid: existing)
        monkeypatch.setattr(classifier, "classify", lambda *a, **k: (_ for _ in ()).throw(AssertionError("classify must not run")))
        incremented = []
        monkeypatch.setattr(store, "increment_occurrence", lambda iid: incremented.append(iid))
        monkeypatch.setattr(store, "find_similar", lambda *a, **k: [])

        resp1 = classifier.classify_and_store("Ticket one", "desc", source_ticket_id="T-DUP-1")
        resp2 = classifier.classify_and_store("Ticket one", "desc", source_ticket_id="T-DUP-1")

        assert resp1.incident_id == "dup-id-1"
        assert resp2.incident_id == resp1.incident_id
        assert incremented == []

    def test_id_path_skips_content_hash_gate(self, monkeypatch):
        """ID path must not consult the content-hash gate at all."""
        monkeypatch.setattr(store, "get_incident_by_source_ticket_id", lambda tid: self._existing_by_ticket_id())
        monkeypatch.setattr(classifier, "classify", lambda *a, **k: (_ for _ in ()).throw(AssertionError("classify must not run")))
        hash_calls = []
        monkeypatch.setattr(store, "get_incident_by_hash", lambda h: hash_calls.append(h) or None)
        monkeypatch.setattr(store, "find_similar", lambda *a, **k: [])

        classifier.classify_and_store("Ticket one", "desc", source_ticket_id="T-DUP-1")

        assert hash_calls == []  # content-hash gate not consulted


# ── resolve_incident ─────────────────────────────────────────────────


class TestResolveIncident:
    def test_delegates_to_store(self, monkeypatch):
        calls = []
        monkeypatch.setattr(store, "resolve_incident", lambda iid: calls.append(iid) or True)
        assert store_module.resolve_incident("abc123") is True
        assert calls == ["abc123"]

    def test_returns_false_for_unknown_incident(self, monkeypatch):
        monkeypatch.setattr(store, "resolve_incident", lambda iid: False)
        assert store_module.resolve_incident("does-not-exist") is False
