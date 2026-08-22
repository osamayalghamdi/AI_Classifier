"""Tests for the SMAX payload translation (smax_models.py).

No imports from the classifier app; no network; no LLM.
"""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

from integrations.smax.smax_models import Incident, from_smax, to_smax_suggestion


class TestFromSmax:
    def test_maps_fields(self):
        inc = from_smax({
            "ticket_id": "INC-100",
            "title": "Permit portal down",
            "description": "Users cannot log in",
            "created_at": "2025-01-01T10:00:00Z",
            "updated_at": "2025-01-02T11:30:00Z",
            "ignored_field": "whatever",
        })
        assert isinstance(inc, Incident)
        assert inc.source_reference == "INC-100"
        assert inc.title == "Permit portal down"
        assert inc.description == "Users cannot log in"
        assert inc.created_at == datetime(2025, 1, 1, 10, 0, tzinfo=timezone.utc)
        assert inc.updated_at == datetime(2025, 1, 2, 11, 30, tzinfo=timezone.utc)
        # Defaults per the classifier's normalized Incident shape.
        assert inc.status == "active"
        assert inc.affected_system == ""
        assert inc.attachments == []

    def test_alternate_key_aliases(self):
        inc = from_smax({"number": "T-9", "subject": "subj", "notes": "note text",
                         "opened_at": "2025-02-01T00:00:00+00:00"})
        assert inc.source_reference == "T-9"
        assert inc.title == "subj"
        assert inc.description == "note text"
        assert inc.created_at is not None

    def test_malformed_payload_degrades_gracefully(self):
        inc = from_smax({})
        assert inc.source_reference == ""
        assert inc.title == ""
        assert inc.description == ""
        assert inc.created_at is None
        assert inc.updated_at is None

    def test_unparseable_timestamp_ignored(self):
        inc = from_smax({"id": "1", "title": "t", "updated_at": "not-a-date"})
        assert inc.updated_at is None


class TestToSmaxSuggestion:
    def test_bug1_regression_attribute_access_not_get(self):
        """BUG-1 regression: classification is a Pydantic-model-like OBJECT
        (attribute access), NOT a dict — the serializer must use getattr."""
        result = SimpleNamespace(
            source_reference="INC-1",
            classification=SimpleNamespace(
                affected_system="SAP ERP", service="Payments", severity="high"
            ),
            similar_tickets=[{"id": "T-100"}, {"id": "T-200"}],
            suggestions=["Restart the gateway"],
            confidence="high",
            model_version="m1",
            prompt_version="p1",
            processed_at=datetime(2025, 1, 1, 12, 0, tzinfo=timezone.utc),
        )
        payload = to_smax_suggestion(result)
        assert payload["classification"] == {
            "affected_system": "SAP ERP", "service": "Payments", "severity": "high",
        }
        assert payload["similar_ticket_ids"] == ["T-100", "T-200"]
        assert payload["suggestions"] == ["Restart the gateway"]
        assert payload["confidence"] == "high"
        assert payload["model_version"] == "m1"
        assert payload["prompt_version"] == "p1"
        assert payload["processed_at"] == "2025-01-01T12:00:00+00:00"

    def test_none_classification_serializes_null_fields(self):
        result = SimpleNamespace(
            classification=None,
            similar_tickets=[],
            suggestions=[],
            confidence="",
            model_version="",
            prompt_version="",
            processed_at=None,
        )
        payload = to_smax_suggestion(result)
        assert payload["classification"] == {
            "affected_system": None, "service": None, "severity": None,
        }
        assert payload["similar_ticket_ids"] == []
        assert payload["processed_at"] is None

    def test_missing_attributes_do_not_raise(self):
        result = SimpleNamespace(
            classification=SimpleNamespace(severity="low"),
            similar_tickets=None,
            suggestions=None,
            confidence="low",
            model_version="",
            prompt_version="",
            processed_at=None,
        )
        payload = to_smax_suggestion(result)
        assert payload["classification"]["affected_system"] is None
        assert payload["classification"]["service"] is None
        assert payload["classification"]["severity"] == "low"
        assert payload["similar_ticket_ids"] == []
        assert payload["suggestions"] is None
