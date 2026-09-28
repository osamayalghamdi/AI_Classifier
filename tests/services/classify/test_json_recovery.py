"""JSON recovery: a prose-wrapped fenced reply must not lose the stage.

Regression (parsing.py L49): only a LEADING ``` fence was stripped, so a
reply like "Sure, here it is:\\n```json\\n{...}\\n```" failed json.loads and
the ticket was thrown away. Recovery scans for the first BALANCED JSON
object. Already-valid input keeps the old behaviour exactly, and a TRUNCATED
reply still raises json.JSONDecodeError — the stage-1 truncation recovery
keys off that exception type.
"""

import json
import types

import pytest

import ai_classification.services.classify.classifier as classifier_mod
from ai_classification.services.classify.parsing import _parse_and_validate
from ai_classification.shared.config import settings


def _payload(**overrides) -> str:
    data = {
        "affected_system": "OldSM",
        "service": "OldSM",
        "incident_type": "Unavailability",
        "severity": "Major",
        "urgency": "High",
        "category": "Software",
        "confidence": "medium",
        "reasoning": "SMTP relay unreachable",
        "signature": "test signature",
        "canonical_statement": "SMTP relay is unreachable, outgoing email fails.",
    }
    data.update(overrides)
    return json.dumps(data)


class TestProseWrappedJson:
    def test_prose_then_fenced_json_is_recovered(self):
        raw = "Sure, here it is:\n```json\n" + _payload() + "\n```"
        assert _parse_and_validate(raw).affected_system == "OldSM"

    def test_prose_then_bare_json_is_recovered(self):
        raw = "Here is the classification: " + _payload()
        assert _parse_and_validate(raw).service == "OldSM"

    def test_leading_fence_unchanged(self):
        raw = "```json\n" + _payload() + "\n```"
        assert _parse_and_validate(raw).affected_system == "OldSM"

    def test_bare_json_unchanged(self):
        assert _parse_and_validate(_payload()).affected_system == "OldSM"

    def test_no_json_object_still_raises(self):
        with pytest.raises(json.JSONDecodeError):
            _parse_and_validate("I think this is a CRM issue")

    def test_truncated_json_still_raises_jsondecodeerror(self):
        # No balanced object anywhere — the stage-1 recovery path depends on
        # the ORIGINAL JSONDecodeError being re-raised.
        truncated = (
            '{\n  "affected_system": "Nusuk Masar Haj",\n'
            '  "service": "Registration - Nusuk Masar Haj",\n'
            '  "reasoning": "The ticket descri'
        )
        with pytest.raises(json.JSONDecodeError):
            _parse_and_validate(truncated)


# ── End-to-end: the prose reply is parsed on the FIRST attempt ─────────

@pytest.fixture(autouse=True)
def _legacy_single_shot(monkeypatch):
    """Pin the single-shot path so one LLM reply == one attempt."""
    d = {k: v for k, v in settings.__dict__.items() if not k.startswith("_")}
    d["cascade_classification"] = False
    monkeypatch.setattr(classifier_mod, "settings", types.SimpleNamespace(**d))


def test_prose_wrapped_reply_classifies_without_a_retry(monkeypatch):
    import ai_classification.services.classify.llm as mod_llm

    calls = []

    class FakeMessage:
        def __init__(self, content):
            self.content = content

    class FakeChoice:
        def __init__(self, content):
            self.message = FakeMessage(content)

    class FakeResponse:
        def __init__(self, content):
            self.choices = [FakeChoice(content)]

    def fake_completion(**kwargs):
        calls.append(kwargs)
        return FakeResponse("Sure, here it is:\n```json\n" + _payload() + "\n```")

    monkeypatch.setattr(mod_llm, "completion", fake_completion)
    result = classifier_mod.classify("Email down", "Cannot send emails")
    assert len(calls) == 1          # parsed first time — no retry, no fallback
    assert result.affected_system == "OldSM"
    assert result.classification_status == "ok"
