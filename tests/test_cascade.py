"""Cascade (coarse-to-fine) classifier tests + per-stage prompt-size proof.

Verifies the spec §3/§5/§8 contract:
- Stage option counts: system=4, service=that system's service count, offering=that
  service's offering count (never the flat 193).
- LLM calls per ticket: 2 (deterministic system) / 3 (LLM system fallback),
  minus 1 when the offering stage is skipped (empty/singular offering list).
- Stage failure -> generic fallback, never raise, never flat-list fallback.
- Dot-path service validation in ClassificationResult (base service key match,
  affected_system auto-correct).
- Flag OFF -> single-shot path unchanged (byte-identical contract).
"""

import json
import types

import pytest

from ai_classification.config import settings
from ai_classification.core import classifier as classifier_mod
from ai_classification.domain.models import ClassificationResult
from ai_classification.domain.taxonomy import (
    AffectedSystem,
    SERVICES_BY_SYSTEM,
    flatten_services,
)


# ── Helpers ─────────────────────────────────────────────────────────────


def make_fake_completion(body: str):
    """Minimal fake for the LiteLLM response object."""

    class FakeChoice:
        class FakeMessage:
            def __init__(self, content):
                self.content = content

        def __init__(self, content):
            self.message = self.FakeMessage(content)

    class FakeResponse:
        def __init__(self, content):
            self.choices = [FakeChoice(content)]

    return FakeResponse(body)


def _settings_with(cascade: bool):
    """A copy of Settings with cascade_classification pinned (frozen dataclass)."""
    d = {k: v for k, v in settings.__dict__.items() if not k.startswith("_")}
    d["cascade_classification"] = cascade
    return types.SimpleNamespace(**d)


@pytest.fixture(autouse=True)
def _cascade_on(monkeypatch):
    """Default: cascade ON (the env default). Individual tests may override."""
    monkeypatch.setattr(classifier_mod, "settings", _settings_with(True))
    yield


@pytest.fixture
def fake_completion(monkeypatch):
    """Queue of LLM responses; pops from the front per call. Tracks call count.

    Patches `ai_classification.core.llm.completion` — call_llm (used by every
    cascade stage) imports completion there.
    """
    import ai_classification.core.llm as mod_llm

    outputs = []
    calls = []

    def fake_completion(**kwargs):
        calls.append(kwargs)
        if outputs:
            return make_fake_completion(outputs.pop(0))
        return make_fake_completion("{}")

    monkeypatch.setattr(mod_llm, "completion", fake_completion)
    return outputs, calls


def _full_result(**overrides) -> dict:
    out = {
        "affected_system": "Nusuk Masar Haj",
        "service": "System/Application - Nusuk Masar Haj",
        "incident_type": "Degradation",
        "severity": "Major",
        "urgency": "High",
        "category": "Software",
        "confidence": "high",
        "reasoning": "test",
        "canonical_statement": "Test incident.",
        "signature": "Test incident signature for grouping",
        "failure_mode": "FM-000",
    }
    out.update(overrides)
    return out


def _full_result_json(**overrides) -> str:
    return json.dumps(_full_result(**overrides))


# ── §8.1 Prompt-size proof (per-stage option counts) ───────────────────


class TestStageOptionCounts:
    def test_system_stage_has_4_options(self):
        assert len(list(AffectedSystem)) == 4

    def test_service_stage_is_only_that_systems_services(self):
        # Nusuk Masar Haj: 29 services in the hierarchy — NOT the flat 193
        assert len(SERVICES_BY_SYSTEM[AffectedSystem.nusuk_masar_haj]) == 29
        flat_total = sum(len(v) for v in flatten_services().values())
        assert flat_total == 193
        assert len(SERVICES_BY_SYSTEM[AffectedSystem.nusuk_masar_haj]) < flat_total

    def test_offering_stage_is_only_that_services_offerings(self):
        svc = "Registration - Nusuk Masar Haj"
        assert len(SERVICES_BY_SYSTEM[AffectedSystem.nusuk_masar_haj][svc]) == 18

    def test_offering_stage_skip_cases(self):
        haj = SERVICES_BY_SYSTEM[AffectedSystem.nusuk_masar_haj]
        empty = [k for k, v in haj.items() if not v]
        singular = [k for k, v in haj.items() if len(v) == 1]
        assert "Housing Preference Services" in empty
        assert "hajj B2C local resrevation - Nusuk Masar Haj" in singular


# ── Cascade path: call counts + result shape ───────────────────────────


class TestCascadeCalls:
    def test_deterministic_system_two_calls(self, fake_completion):
        outputs, calls = fake_completion
        # Stage 2 (service) + stage 3 (offering) responses; system resolved deterministically
        outputs.append(_full_result_json(service="Registration - Nusuk Masar Haj"))
        outputs.append(_full_result_json(
            service="Registration - Nusuk Masar Haj.Create Registration Request (SPC)"
        ))
        result = classifier_mod.classify(
            "Nusuk Masar Haj registration form fails",
            "SPC cannot create registration request on Nusuk Masar Haj",
        )
        assert len(calls) == 2
        assert result.service == (
            "Registration - Nusuk Masar Haj.Create Registration Request (SPC)"
        )

    def test_offering_singular_skips_call(self, fake_completion):
        outputs, calls = fake_completion
        # 'hajj B2C local resrevation - Nusuk Masar Haj' has exactly 1 offering
        # -> offering stage skipped, deterministic 'Service.Offering'
        outputs.append(_full_result_json(service="hajj B2C local resrevation - Nusuk Masar Haj"))
        result = classifier_mod.classify(
            "Nusuk Masar Haj permit booking error",
            "B2C reservation permit issuance broken on Nusuk Masar Haj",
        )
        assert len(calls) == 1
        assert result.service == "hajj B2C local resrevation - Nusuk Masar Haj.Permits Issuance"

    def test_offering_empty_keeps_bare_service(self, fake_completion):
        outputs, calls = fake_completion
        # 'Housing Preference Services' has 0 offerings -> bare service, no stage 3
        outputs.append(_full_result_json(service="Housing Preference Services"))
        result = classifier_mod.classify(
            "Nusuk Masar Haj housing preference",
            "housing preference services broken on Nusuk Masar Haj",
        )
        assert len(calls) == 1
        assert result.service == "Housing Preference Services"

    def test_llm_system_fallback_three_calls(self, fake_completion):
        outputs, calls = fake_completion
        # Ambiguous ticket (both 'haj' and 'umrah' aliases) -> stage 1 LLM call
        outputs.append(_full_result_json(affected_system="Nusuk Masar Haj"))
        outputs.append(_full_result_json(service="System/Application - Nusuk Masar Haj"))
        outputs.append(_full_result_json(
            service="System/Application - Nusuk Masar Haj.Service Unavailability"
        ))
        result = classifier_mod.classify(
            "Hajj and umrah portal down",
            "Both haj and umrah systems unreachable",
        )
        assert len(calls) == 3
        assert result.service == "System/Application - Nusuk Masar Haj.Service Unavailability"

    def test_stage_calls_use_temperature_zero_and_seed(self, fake_completion):
        outputs, calls = fake_completion
        outputs.append(_full_result_json(service="Registration - Nusuk Masar Haj"))
        outputs.append(_full_result_json(
            service="Registration - Nusuk Masar Haj.Create Registration Request (SPC)"
        ))
        classifier_mod.classify("Nusuk Masar Haj x", "registration issue on nusuk masar haj")
        assert len(calls) == 2
        for c in calls:
            assert c.get("temperature") == 0.0
            assert c.get("seed") == 42


# ── Never-raise / stage fallback ───────────────────────────────────────


class TestCascadeFallback:
    def test_system_stage_failure_returns_generic_fallback(self, fake_completion):
        outputs, calls = fake_completion
        outputs.append("not json at all")  # stage 1 fails
        result = classifier_mod.classify("random ticket", "no system hints here")
        assert isinstance(result, ClassificationResult)
        assert result.confidence == "low"
        assert result.affected_system == AffectedSystem.other
        assert result.service == "General / Unspecified"
        assert len(calls) == 1  # no further stages after system failure

    def test_service_stage_failure_returns_generic_fallback(self, fake_completion):
        outputs, calls = fake_completion
        outputs.append(_full_result_json())  # stage 2 ok
        outputs.append("garbage")            # stage 3 fails
        result = classifier_mod.classify("Nusuk Masar Haj x", "nusuk masar haj issue")
        assert isinstance(result, ClassificationResult)
        assert result.confidence == "low"
        assert result.affected_system == AffectedSystem.other
        assert len(calls) == 2

    def test_never_raises(self, fake_completion):
        outputs, calls = fake_completion
        outputs.append("garbage")
        outputs.append("garbage")
        outputs.append("garbage")
        result = classifier_mod.classify("anything", "at all")  # must not raise
        assert isinstance(result, ClassificationResult)


# ── Dot-path validator ─────────────────────────────────────────────────


class TestDotPathValidator:
    def test_accepts_valid_dot_path(self):
        r = ClassificationResult(**_full_result(
            service="7.1 Invoicing and Billing - Nusuk Masar Haj.Bill Expiry/Cancellation"
        ))
        assert r.service == "7.1 Invoicing and Billing - Nusuk Masar Haj.Bill Expiry/Cancellation"

    def test_autocorrects_system_from_dot_path_base(self):
        r = ClassificationResult(**_full_result(
            affected_system="Nusuk Masar Haj",
            service="OldSM.OldSM",
        ))
        assert r.affected_system == AffectedSystem.old_sm

    def test_invalid_dot_path_raises(self):
        with pytest.raises(ValueError):
            ClassificationResult(**_full_result(service="No Such Service.Offering"))

    def test_bare_service_old_behavior_unchanged(self):
        r = ClassificationResult(**_full_result(
            affected_system="Other", service="General / Unspecified"
        ))
        assert r.service == "General / Unspecified"
        with pytest.raises(ValueError):
            ClassificationResult(**_full_result(service="Not A Real Service"))


# ── Flag OFF: single-shot path unchanged ───────────────────────────────


class TestFlagOff:
    def test_flag_off_uses_single_shot(self, monkeypatch, fake_completion):
        monkeypatch.setattr(classifier_mod, "settings", _settings_with(False))
        outputs, calls = fake_completion
        outputs.append(_full_result_json(
            affected_system="Other", service="General / Unspecified"
        ))
        result = classifier_mod.classify("test", "test")
        assert len(calls) == 1
        assert result.affected_system == AffectedSystem.other
        assert result.service == "General / Unspecified"

    def test_flag_off_retry_still_works(self, monkeypatch, fake_completion):
        monkeypatch.setattr(classifier_mod, "settings", _settings_with(False))
        outputs, calls = fake_completion
        outputs.append("not json")
        outputs.append(_full_result_json(
            affected_system="Other", service="General / Unspecified"
        ))
        result = classifier_mod.classify("test", "test")
        assert len(calls) == 2
        assert result.confidence == "high"
