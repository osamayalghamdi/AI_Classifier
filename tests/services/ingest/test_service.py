"""Tests for classify_and_store/resolve_incident — orchestration between the
classifier and the incident store (formerly one service.py module)."""

import pytest
from datetime import datetime, timedelta, timezone

import ai_classification.services.classify.classifier as classifier
from ai_classification.api.incidents import resolve_incident as store_module_resolve_incident
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
        monkeypatch.setattr(classifier, "classify", lambda title, desc, incident_ref=None, affected_system=None: result_cls)
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
        monkeypatch.setattr(classifier, "classify", lambda title, desc, incident_ref=None, affected_system=None: result_cls)
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
        monkeypatch.setattr(classifier, "classify", lambda title, desc, incident_ref=None, affected_system=None: result_cls)
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
        monkeypatch.setattr(classifier, "classify", lambda title, desc, incident_ref=None, affected_system=None: _make_result())
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
        assert store_module_resolve_incident("abc123") is True
        assert calls == ["abc123"]

    def test_returns_false_for_unknown_incident(self, monkeypatch):
        monkeypatch.setattr(store, "resolve_incident", lambda iid: False)
        assert store_module_resolve_incident("does-not-exist") is False


# ── classify_batch / import endpoint (regression: settings binding) ──

def test_classify_batch_binds_settings_at_call_time(monkeypatch):
    """classify_batch must resolve `settings` through the facade at call
    time (regression: it used a module-level name that was never bound in
    the C-2 split → NameError on every /import call)."""
    import ai_classification.services.classify.persistence as persistence_mod
    from ai_classification.shared.config import settings as real_settings
    from ai_classification.api import schemas as schemas_mod
    from dataclasses import replace

    fake = replace(real_settings, classify_batch_sleep_s=0)
    monkeypatch.setattr(classifier, "settings", fake)

    calls = []

    def _fake_classify_and_store(title, description, *a, **kw):
        calls.append((title, description))
        cls = _make_result(canonical_statement=f"CS {title}", signature="sg")
        return schemas_mod.ClassifyResponse(
            incident_id=f"id-{len(calls)}",
            incident_title=title,
            classification=cls,
            similar_open_incidents=[],
        )

    monkeypatch.setattr(persistence_mod, "classify_and_store", _fake_classify_and_store)

    from ai_classification.services.classify.classifier import classify_batch
    resp = classify_batch([{"title": "T1", "description": "D1"},
                           {"title": "T2", "description": "D2"}])
    assert len(calls) == 2
    assert calls[0] == ("T1", "D1")
    assert resp.total == 2


def test_import_incidents_from_body_endpoint(monkeypatch):
    """/import from body maps DisplayLabel/Description and classifies —
    regression guard for the import_service NameError."""
    from ai_classification.services.ingest import import_service as import_mod
    from ai_classification.services.classify.classifier import classify_batch as real_batch
    from ai_classification.domain.models import ClassificationResult
    import ai_classification.api.schemas as schemas_mod

    def _fake_batch(mapped):
        return schemas_mod.ClassifyBatchResponse(
            results=[
                schemas_mod.ClassifyResponse(
                    incident_id=f"id-{i}", incident_title=m["title"],
                    classification=_make_result(canonical_statement="CS", signature="sg"),
                    similar_open_incidents=[],
                )
                for i, m in enumerate(mapped)
            ],
            total=len(mapped), failed=0,
        )

    monkeypatch.setattr(import_mod, "classify_batch", _fake_batch)
    from ai_classification.services.ingest.import_service import import_incidents_from_body
    resp = import_incidents_from_body([
        {"DisplayLabel": "Rawdah permit fails", "Description": "cannot book"},
        {"DisplayLabel": "", "Description": "skipped"},
    ])
    assert resp.total == 1  # only the non-empty title
    assert resp.failed == 0
