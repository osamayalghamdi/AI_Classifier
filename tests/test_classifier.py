"""Tests for classifier — Pydantic-as-gatekeeper contract."""

import json
import pytest

from ai_classification.core.classifier import classify
from ai_classification.core.llm import strip_json_fences
from ai_classification.domain.models import ClassificationResult


# ── strip_json_fences (minimal fence-stripper) ────────────────────────


class TestStripJsonFences:
    def test_plain_json_passes_through(self):
        raw = '{"affected_system": "CRM"}'
        assert strip_json_fences(raw) == raw

    def test_strips_triple_backtick_fences(self):
        raw = '```json\n{"affected_system": "CRM"}\n```'
        assert strip_json_fences(raw) == '{"affected_system": "CRM"}'

    def test_strips_fences_without_lang_tag(self):
        raw = '```\n{"affected_system": "CRM"}\n```'
        assert strip_json_fences(raw) == '{"affected_system": "CRM"}'

    def test_strips_inline_fences(self):
        raw = '```{"affected_system": "CRM"}```'
        assert strip_json_fences(raw) == '{"affected_system": "CRM"}'

    def test_no_fences_strips_whitespace(self):
        """Surrounding whitespace is stripped — json.loads tolerates it."""
        assert strip_json_fences('  {"a": 1}  ') == '{"a": 1}'


# ── classify() — real integration (mocked LLM) ───────────────────────


def make_fake_completion(body: str):
    """Return a minimal fake for the OpenAI response object."""

    class FakeChoice:
        class FakeMessage:
            def __init__(self, content: str):
                self.content = content

        def __init__(self, content: str):
            self.message = self.FakeMessage(content)

    class FakeResponse:
        def __init__(self, content: str):
            self.choices = [FakeChoice(content)]

    return FakeResponse(body)


@pytest.fixture(autouse=True)
def _patch_completion(monkeypatch):
    """Replace `completion` so every test controls what the LLM returns.

    Supports retry: stores outputs in a list and pops from the front
    on each call, so both attempt 1 and retry can return the same data.
    """
    import ai_classification.core.llm as mod_llm

    outputs = []

    def fake_completion(**kwargs):
        if outputs:
            return make_fake_completion(outputs.pop(0))
        return make_fake_completion("{}")

    monkeypatch.setattr(mod_llm, "completion", fake_completion)
    return outputs


# ── Happy path ────────────────────────────────────────────────────────


class TestClassifyHappyPath:
    def test_valid_full_output(self, _patch_completion):
        _patch_completion.append(json.dumps({
            "affected_system": "Other",
            "service": "General / Unspecified",
            "incident_type": "Degradation",
            "severity": "Major",
            "urgency": "High",
            "category": "Software",
            "confidence": "high",
            "reasoning": "CRM portal slow under load",
            "signature": "test signature",
            "failure_mode": "FM-000",
            "canonical_statement": "CRM portal responds slowly under load.",
        }))
        result = classify("CRM slow", "Portal is crawling")
        assert isinstance(result, ClassificationResult)
        assert result.affected_system == "Other"
        assert result.service == "General / Unspecified"
        assert result.incident_type == "Degradation"
        assert result.severity == "Major"
        assert result.urgency == "High"
        assert result.category == "Software"
        assert result.confidence == "high"
        assert result.reasoning == "CRM portal slow under load"

    def test_valid_minimal_no_reasoning(self, _patch_completion):
        _patch_completion.append(json.dumps({
            "affected_system": "Other",
            "service": "General / Unspecified",
            "incident_type": "Outage",
            "severity": "Critical",
            "urgency": "Immediate",
            "category": "Network Issue",
            "confidence": "high",
            "signature": "test signature",
            "failure_mode": "FM-000",
            "canonical_statement": "All DNS queries fail.",
        }))
        result = classify("DNS down", "All DNS queries failing")
        assert result.reasoning is None

    def test_handles_fenced_json(self, _patch_completion):
        _patch_completion.append(
            '```json\n{"affected_system": "OldSM", "service": "OldSM",'
            '"incident_type": "Unavailability", "severity": "Major",'
            '"urgency": "High", "category": "Software",'
            '"confidence": "medium", "reasoning": "SMTP relay unreachable",'
            '"signature": "test signature", "failure_mode": "FM-000",'
            '"canonical_statement": "SMTP relay is unreachable, outgoing email fails."}\n```'
        )
        result = classify("Email down", "Cannot send emails")
        assert result.affected_system == "OldSM"


# ── Validation errors (strict — no silent coercion) ──────────────────


class TestClassifyValidationErrors:
    def test_non_json_returns_fallback(self, _patch_completion):
        # Supply bad data for both attempt 1 and retry
        _patch_completion.append("I think this is a CRM issue")
        _patch_completion.append("Still not JSON either")
        result = classify("test", "test")
        assert isinstance(result, ClassificationResult)
        assert result.confidence == "low"
        assert "Classification failed after 2 attempts" in (result.reasoning or "")
        assert result.affected_system == "Other"

    def test_invalid_enum_value_returns_fallback(self, _patch_completion):
        bad = json.dumps({
            "affected_system": "Other",
            "service": "General / Unspecified",
            "incident_type": "Degradation",
            "severity": "SuperCritical",       # not in Severity enum
            "urgency": "High",
            "category": "Software",
            "confidence": "high",
            "signature": "test signature",
            "failure_mode": "FM-000",
            "canonical_statement": "Test incident.",
        })
        _patch_completion.append(bad)
        _patch_completion.append(bad)  # retry also fails
        result = classify("test", "test")
        assert isinstance(result, ClassificationResult)
        assert result.confidence == "low"
        assert "Classification failed" in (result.reasoning or "")

    def test_missing_required_field_returns_fallback(self, _patch_completion):
        bad = json.dumps({
            "affected_system": "Other",
            # missing service, incident_type, severity, urgency, category, confidence, canonical_statement
            "signature": "test signature",
            "failure_mode": "FM-000",
        })
        _patch_completion.append(bad)
        _patch_completion.append(bad)
        result = classify("test", "test")
        assert isinstance(result, ClassificationResult)
        assert result.confidence == "low"

    def test_invalid_confidence_returns_fallback(self, _patch_completion):
        bad = json.dumps({
            "affected_system": "Other",
            "service": "General / Unspecified",
            "incident_type": "Degradation",
            "severity": "Major",
            "urgency": "High",
            "category": "Software",
            "confidence": "very high",          # not in pattern
            "signature": "test signature",
            "failure_mode": "FM-000",
            "canonical_statement": "Test incident.",
        })
        _patch_completion.append(bad)
        _patch_completion.append(bad)
        result = classify("test", "test")
        assert isinstance(result, ClassificationResult)
        assert result.confidence == "low"

    def test_empty_json_object_returns_fallback(self, _patch_completion):
        _patch_completion.append("{}")
        _patch_completion.append("{}")
        result = classify("test", "test")
        assert isinstance(result, ClassificationResult)
        assert result.confidence == "low"
        assert result.affected_system == "Other"


# ── Typo / extra whitespace in enums — still strict ──────────────────


class TestEnumStrictness:
    def test_case_mismatch_returns_fallback(self, _patch_completion):
        """Pydantic StrEnum is case-sensitive; 'nusuk masar haj' != 'Nusuk Masar Haj'."""
        bad = json.dumps({
            "affected_system": "nusuk masar haj",  # wrong case — won't match
            "service": "General / Unspecified",
            "incident_type": "Degradation",
            "severity": "Major",
            "urgency": "High",
            "category": "Software",
            "confidence": "high",
            "signature": "test signature",
            "failure_mode": "FM-000",
        })
        _patch_completion.append(bad)
        _patch_completion.append(bad)
        result = classify("test", "test")
        assert isinstance(result, ClassificationResult)
        assert result.confidence == "low"


# ── Retry behaviour ──────────────────────────────────────────────────


class TestClassifyRetry:
    def test_retry_on_first_failure(self, _patch_completion):
        """First attempt returns bad JSON, retry returns valid JSON."""
        bad = "not json at all"
        good = json.dumps({
            "affected_system": "Other",
            "service": "General / Unspecified",
            "incident_type": "Degradation",
            "severity": "Major",
            "urgency": "High",
            "category": "Software",
            "confidence": "high",
            "signature": "test signature",
            "failure_mode": "FM-000",
            "canonical_statement": "Test incident.",
        })
        _patch_completion.append(bad)
        _patch_completion.append(good)
        result = classify("test", "test")
        assert isinstance(result, ClassificationResult)
        assert result.affected_system == "Other"
