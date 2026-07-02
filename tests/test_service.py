"""Tests for service — orchestration between classifier and store."""

import pytest

import ai_classification.service as service
from ai_classification.models import ClassificationResult
from ai_classification.incident_store import SimilarMatch
from ai_classification.schemas import AffectedSystem, IncidentType, Severity, Urgency, Category


def _make_result(**overrides) -> ClassificationResult:
    defaults = dict(
        affected_system=AffectedSystem.crm,
        service="Customer Portal",
        incident_type=IncidentType.degradation,
        severity=Severity.major,
        urgency=Urgency.high,
        category=Category.software,
        confidence="high",
        reasoning="test",
    )
    defaults.update(overrides)
    return ClassificationResult(**defaults)


# ── classify_and_store ──────────────────────────────────────────────


class TestClassifyAndStore:
    def test_saves_incident_and_returns_id(self, monkeypatch):
        result_cls = _make_result()
        monkeypatch.setattr(service, "classify", lambda title, desc: result_cls)
        monkeypatch.setattr(service.store, "find_similar", lambda *a, **k: [])
        saved = {}
        monkeypatch.setattr(
            service.store, "save_incident",
            lambda iid, title, desc, cls, extracted_text="": saved.update(id=iid, title=title),
        )
        monkeypatch.setattr(service.store, "generate_id", lambda: "abc123")

        resp = service.classify_and_store("Checkout down", "504 errors")
        assert resp.incident_id == "abc123"
        assert resp.incident_title == "Checkout down"
        assert resp.classification == result_cls
        assert saved == {"id": "abc123", "title": "Checkout down"}

    def test_maps_similar_matches_to_response(self, monkeypatch):
        result_cls = _make_result()
        monkeypatch.setattr(service, "classify", lambda title, desc: result_cls)
        monkeypatch.setattr(service.store, "save_incident", lambda *a, **k: None)
        monkeypatch.setattr(service.store, "generate_id", lambda: "new-id")
        match = SimilarMatch(id="dup-1", title="Similar incident", similarity=0.91, classification=result_cls)
        monkeypatch.setattr(service.store, "find_similar", lambda *a, **k: [match])

        resp = service.classify_and_store("Checkout down", "504 errors")
        assert len(resp.similar_open_incidents) == 1
        dupe = resp.similar_open_incidents[0]
        assert dupe.id == "dup-1"
        assert dupe.title == "Similar incident"
        assert dupe.similarity == pytest.approx(0.91)

    def test_no_similar_incidents_returns_empty_list(self, monkeypatch):
        result_cls = _make_result()
        monkeypatch.setattr(service, "classify", lambda title, desc: result_cls)
        monkeypatch.setattr(service.store, "save_incident", lambda *a, **k: None)
        monkeypatch.setattr(service.store, "generate_id", lambda: "new-id")
        monkeypatch.setattr(service.store, "find_similar", lambda *a, **k: [])

        resp = service.classify_and_store("Checkout down", "504 errors")
        assert resp.similar_open_incidents == []


# ── resolve_incident ─────────────────────────────────────────────────


class TestResolveIncident:
    def test_delegates_to_store(self, monkeypatch):
        calls = []
        monkeypatch.setattr(service.store, "resolve_incident", lambda iid: calls.append(iid) or True)
        assert service.resolve_incident("abc123") is True
        assert calls == ["abc123"]

    def test_returns_false_for_unknown_incident(self, monkeypatch):
        monkeypatch.setattr(service.store, "resolve_incident", lambda iid: False)
        assert service.resolve_incident("does-not-exist") is False
