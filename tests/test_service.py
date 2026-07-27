"""Tests for classify_and_store/resolve_incident — orchestration between
core.classifier and core.store (formerly one service.py module, now split
along the same seam: store lifecycle in core.store, classify orchestration
in core.classifier)."""

import pytest

from ai_classification.core import classifier
from ai_classification.core import store as store_module
from ai_classification.core.store import store, SimilarMatch
from ai_classification.domain.models import ClassificationResult
from ai_classification.domain.taxonomy import AffectedSystem, IncidentType, Severity, Urgency, Category


def _make_result(**overrides) -> ClassificationResult:
    defaults = dict(
        affected_system=AffectedSystem.nusuk_masar_haj,
        service="System/Application",
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
