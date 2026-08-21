"""Tests for the SMAX boundary models (from_smax / to_smax_suggestion).

BUG-1 regression: to_smax_suggestion must handle a real PipelineResult whose
`classification` is a Pydantic ClassificationResult (attribute access), not a
dict (.get() raised AttributeError on the first real write-back).
"""

from datetime import datetime, timezone

from ai_classification.domain.models import ClassificationResult
from ai_classification.seams.port import Incident, PipelineResult
from ai_classification.seams.smax.models import from_smax, to_smax_suggestion


def _make_classification() -> ClassificationResult:
    return ClassificationResult(
        affected_system="Nusuk Masar Haj",
        service="pilgrim groups and issue permit - Nusuk Masar Haj.Issue Permits",
        incident_type="Unavailability",
        severity="Major",
        urgency="High",
        category="Software",
        confidence="high",
        reasoning="test reasoning",
        canonical_statement="Permits issuance unavailable.",
        signature="permits issuance fails",
    )


def _make_result() -> PipelineResult:
    now = datetime.now(timezone.utc)
    return PipelineResult(
        source_reference="EXT-1",
        title="T1",
        description="D1",
        is_new=True,
        incident_id=None,
        classification=_make_classification(),
        similar_tickets=[
            {"id": "abc123", "title": "Similar", "similarity": 0.8},
            {"id": "def456", "title": "Also similar", "similarity": 0.7},
        ],
        suggestions=["Similar", "Also similar"],
        confidence="high",
        model_version="model-x",
        prompt_version="prompt-y",
        processed_at=now,
        status="open",
    )


def test_to_smax_suggestion_accepts_pydantic_classification():
    """BUG-1: classification is a Pydantic model, not a dict — must not crash."""
    result = _make_result()
    payload = to_smax_suggestion(result)
    assert payload["classification"]["affected_system"] == "Nusuk Masar Haj"
    assert payload["classification"]["service"].startswith("pilgrim groups")
    assert payload["classification"]["severity"] == "Major"
    assert payload["similar_ticket_ids"] == ["abc123", "def456"]
    assert payload["suggestions"] == ["Similar", "Also similar"]
    assert payload["confidence"] == "high"
    assert payload["model_version"] == "model-x"
    assert payload["prompt_version"] == "prompt-y"
    assert payload["processed_at"] == result.processed_at.isoformat()


def test_to_smax_suggestion_handles_none_classification():
    """Classification may be None (failed pipeline run) — serialize as nulls."""
    result = _make_result()
    result.classification = None
    payload = to_smax_suggestion(result)
    assert payload["classification"] == {
        "affected_system": None,
        "service": None,
        "severity": None,
    }


def test_to_smax_suggestion_handles_no_similar_tickets():
    result = _make_result()
    result.similar_tickets = []
    result.suggestions = []
    payload = to_smax_suggestion(result)
    assert payload["similar_ticket_ids"] == []
    assert payload["suggestions"] == []


def test_from_smax_maps_wire_fields():
    payload = {
        "ticket_id": "SMX-99",
        "title": "Permits down",
        "description": "Cannot issue permits",
        "created_at": "2026-08-01T10:00:00Z",
        "updated": "2026-08-01T11:30:00Z",
        "unused_extra": "ignored",
    }
    inc = from_smax(payload)
    assert inc.source_reference == "SMX-99"
    assert inc.title == "Permits down"
    assert inc.description == "Cannot issue permits"
    assert inc.created_at is not None
    assert inc.created_at.tzinfo is not None  # normalized to UTC
    assert inc.updated_at is not None
