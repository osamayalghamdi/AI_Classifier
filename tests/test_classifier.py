"""Tests for classifier — Pydantic-as-gatekeeper contract."""

import json
import pytest

from ai_classification.classifier import classify, llm_rerank_similar, summarize_cluster, _extract_json_str
from ai_classification.models import ClassificationResult


# ── _extract_json_str (minimal fence-stripper) ────────────────────────


class TestExtractJsonStr:
    def test_plain_json_passes_through(self):
        raw = '{"affected_system": "CRM"}'
        assert _extract_json_str(raw) == raw

    def test_strips_triple_backtick_fences(self):
        raw = '```json\n{"affected_system": "CRM"}\n```'
        assert _extract_json_str(raw) == '{"affected_system": "CRM"}'

    def test_strips_fences_without_lang_tag(self):
        raw = '```\n{"affected_system": "CRM"}\n```'
        assert _extract_json_str(raw) == '{"affected_system": "CRM"}'

    def test_strips_inline_fences(self):
        raw = '```{"affected_system": "CRM"}```'
        assert _extract_json_str(raw) == '{"affected_system": "CRM"}'

    def test_no_fences_strips_whitespace(self):
        """Surrounding whitespace is stripped — json.loads tolerates it."""
        assert _extract_json_str('  {"a": 1}  ') == '{"a": 1}'


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
    import ai_classification.classifier as mod

    outputs = []

    def fake_completion(**kwargs):
        if outputs:
            return make_fake_completion(outputs.pop(0))
        return make_fake_completion("{}")

    monkeypatch.setattr(mod, "completion", fake_completion)
    return outputs


# ── Happy path ────────────────────────────────────────────────────────


class TestClassifyHappyPath:
    def test_valid_full_output(self, _patch_completion):
        _patch_completion.append(json.dumps({
            "affected_system": "CRM",
            "service": "Customer Portal",
            "incident_type": "Degradation",
            "severity": "Major",
            "urgency": "High",
            "category": "Software",
            "confidence": "high",
            "reasoning": "CRM portal slow under load",
        }))
        result = classify("CRM slow", "Portal is crawling")
        assert isinstance(result, ClassificationResult)
        assert result.affected_system == "CRM"
        assert result.service == "Customer Portal"
        assert result.incident_type == "Degradation"
        assert result.severity == "Major"
        assert result.urgency == "High"
        assert result.category == "Software"
        assert result.confidence == "high"
        assert result.reasoning == "CRM portal slow under load"

    def test_valid_minimal_no_reasoning(self, _patch_completion):
        _patch_completion.append(json.dumps({
            "affected_system": "Infrastructure",
            "service": "DNS",
            "incident_type": "Outage",
            "severity": "Critical",
            "urgency": "Immediate",
            "category": "Network Issue",
            "confidence": "high",
        }))
        result = classify("DNS down", "All DNS queries failing")
        assert result.reasoning is None

    def test_handles_fenced_json(self, _patch_completion):
        _patch_completion.append(
            '```json\n{"affected_system": "Email", "service": "SMTP Relay",'
            '"incident_type": "Unavailability", "severity": "Major",'
            '"urgency": "High", "category": "Software",'
            '"confidence": "medium", "reasoning": "SMTP relay unreachable"}\n```'
        )
        result = classify("Email down", "Cannot send emails")
        assert result.affected_system == "Email"


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
            "affected_system": "CRM",
            "service": "Customer Portal",
            "incident_type": "Degradation",
            "severity": "SuperCritical",       # not in Severity enum
            "urgency": "High",
            "category": "Software",
            "confidence": "high",
        })
        _patch_completion.append(bad)
        _patch_completion.append(bad)  # retry also fails
        result = classify("test", "test")
        assert isinstance(result, ClassificationResult)
        assert result.confidence == "low"
        assert "Classification failed" in (result.reasoning or "")

    def test_missing_required_field_returns_fallback(self, _patch_completion):
        bad = json.dumps({
            "affected_system": "CRM",
            "service": "Customer Portal",
            # missing incident_type, severity, urgency, category, confidence
        })
        _patch_completion.append(bad)
        _patch_completion.append(bad)
        result = classify("test", "test")
        assert isinstance(result, ClassificationResult)
        assert result.confidence == "low"

    def test_invalid_confidence_returns_fallback(self, _patch_completion):
        bad = json.dumps({
            "affected_system": "CRM",
            "service": "Customer Portal",
            "incident_type": "Degradation",
            "severity": "Major",
            "urgency": "High",
            "category": "Software",
            "confidence": "very high",          # not in pattern
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
        """Pydantic StrEnum is case-sensitive; 'crm' != 'CRM'."""
        bad = json.dumps({
            "affected_system": "crm",            # lowercase — won't match
            "service": "Customer Portal",
            "incident_type": "Degradation",
            "severity": "Major",
            "urgency": "High",
            "category": "Software",
            "confidence": "high",
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
            "affected_system": "CRM",
            "service": "Customer Portal",
            "incident_type": "Degradation",
            "severity": "Major",
            "urgency": "High",
            "category": "Software",
            "confidence": "high",
        })
        _patch_completion.append(bad)
        _patch_completion.append(good)
        result = classify("test", "test")
        assert isinstance(result, ClassificationResult)
        assert result.affected_system == "CRM"


# ── llm_rerank_similar() ───────────────────────────────────────────────


def _make_candidate(id_="abc123", title="Similar incident", description="desc"):
    return {
        "id": id_,
        "title": title,
        "description": description,
        "classification_json": json.dumps({
            "affected_system": "CRM",
            "service": "Customer Portal",
            "incident_type": "Degradation",
            "severity": "Major",
            "urgency": "High",
            "category": "Software",
            "confidence": "high",
        }),
    }


class TestLlmRerankSimilar:
    def test_empty_candidates_returns_empty(self, _patch_completion):
        assert llm_rerank_similar("title", "desc", []) == []

    def test_valid_response_returns_ranked_items(self, _patch_completion):
        _patch_completion.append(json.dumps([
            {"id": "abc123", "similarity": 87, "reasoning": "Same root cause"},
        ]))
        result = llm_rerank_similar("New incident", "desc", [_make_candidate()])
        assert len(result) == 1
        assert result[0].id == "abc123"
        assert result[0].similarity == 87
        assert result[0].reasoning == "Same root cause"

    def test_handles_fenced_json_array(self, _patch_completion):
        _patch_completion.append('```json\n[{"id": "abc123", "similarity": 90}]\n```')
        result = llm_rerank_similar("New incident", "desc", [_make_candidate()])
        assert len(result) == 1

    def test_clamps_out_of_range_similarity(self, _patch_completion):
        _patch_completion.append(json.dumps([{"id": "abc123", "similarity": 150}]))
        result = llm_rerank_similar("New incident", "desc", [_make_candidate()])
        assert result[0].similarity == 100

    def test_coerces_numeric_id(self, _patch_completion):
        _patch_completion.append(json.dumps([{"id": 123, "similarity": 80}]))
        result = llm_rerank_similar("New incident", "desc", [_make_candidate(id_="123")])
        assert result[0].id == "123"

    def test_skips_invalid_items_keeps_valid_ones(self, _patch_completion):
        _patch_completion.append(json.dumps([
            {"id": "abc123", "similarity": 90},
            {"reasoning": "missing required fields"},
        ]))
        result = llm_rerank_similar("New incident", "desc", [_make_candidate()])
        assert len(result) == 1
        assert result[0].id == "abc123"

    def test_caps_at_five_items(self, _patch_completion):
        _patch_completion.append(json.dumps([
            {"id": f"id{i}", "similarity": 90 - i} for i in range(8)
        ]))
        result = llm_rerank_similar("New incident", "desc", [_make_candidate()])
        assert len(result) == 5

    def test_retries_once_on_parse_failure_then_succeeds(self, _patch_completion):
        _patch_completion.append("not json at all")
        _patch_completion.append(json.dumps([{"id": "abc123", "similarity": 75}]))
        result = llm_rerank_similar("New incident", "desc", [_make_candidate()])
        assert len(result) == 1
        assert result[0].id == "abc123"

    def test_raises_after_both_attempts_fail(self, _patch_completion):
        _patch_completion.append("not json")
        _patch_completion.append("still not json")
        with pytest.raises(Exception):
            llm_rerank_similar("New incident", "desc", [_make_candidate()])

    def test_non_array_response_retries_then_raises(self, _patch_completion):
        _patch_completion.append(json.dumps({"not": "an array"}))
        _patch_completion.append(json.dumps({"still": "not an array"}))
        with pytest.raises(Exception):
            llm_rerank_similar("New incident", "desc", [_make_candidate()])


# ── summarize_cluster() fallback — severity ranking ────────────────────


def _cls(severity: str) -> ClassificationResult:
    return ClassificationResult(
        affected_system="CRM",
        service="Customer Portal",
        incident_type="Degradation",
        severity=severity,
        urgency="High",
        category="Software",
        confidence="high",
    )


class TestSummarizeClusterFallback:
    def test_fallback_picks_critical_over_minor(self, _patch_completion):
        """Alphabetically 'Minor' > 'Critical' (M > C) — the fallback must not use plain max()."""
        _patch_completion.append("")  # empty content -> _call_llm raises -> fallback path
        incidents = [
            {"title": "T1", "description": "D1", "classification": _cls("Minor")},
            {"title": "T2", "description": "D2", "classification": _cls("Critical")},
        ]
        result = summarize_cluster(incidents)
        assert "Worst severity: Critical" in result

    def test_fallback_picks_major_over_cosmetic(self, _patch_completion):
        _patch_completion.append("")
        incidents = [
            {"title": "T1", "description": "D1", "classification": _cls("Cosmetic")},
            {"title": "T2", "description": "D2", "classification": _cls("Major")},
        ]
        result = summarize_cluster(incidents)
        assert "Worst severity: Major" in result
