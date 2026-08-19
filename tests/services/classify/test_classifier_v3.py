"""Classifier v3 — triage, routing, abstention, honest failure, verification.

Covers the v3 contract (spec §Stage 0-4):
- triage routes each of the 7 TicketKind values correctly
- non-incident kinds: system+service only, incident fields null, NO verification
- stage-3 abstention: NONE_OF_THE_ABOVE -> <Service>.OFFERING-GAP + taxonomy gap
  recorded; the strict validator accepts the sentinel and rejects invented values
- honest failure: classification_status="failed", incident fields null, E5 marker
- verification: approve keeps the result; valid corrections applied through the
  strict validator; invalid corrections discarded + logged; bare-service
  correction re-runs stage 3 scoped to the corrected service
- self-consistency flag defaults OFF
- stage-3 prompt uses real newlines (the literal `\\n` bug is fixed)
"""

import json
import types

import pytest

from ai_classification.shared.config import settings
import ai_classification.services.classify.classifier as classifier_mod
from ai_classification.domain.models import ClassificationResult, OFFERING_GAP_SENTINEL
from ai_classification.domain.taxonomy import AffectedSystem, TicketKind


# ── Helpers (mirror test_cascade.py) ───────────────────────────────────


def make_fake_completion(body: str):
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
    d = {k: v for k, v in settings.__dict__.items() if not k.startswith("_")}
    d["cascade_classification"] = cascade
    return types.SimpleNamespace(**d)


@pytest.fixture(autouse=True)
def _cascade_on(monkeypatch):
    monkeypatch.setattr(classifier_mod, "settings", _settings_with(True))
    yield


@pytest.fixture
def fake_completion(monkeypatch):
    import ai_classification.services.classify.llm as mod_llm

    outputs = []
    calls = []

    def fake_completion(**kwargs):
        calls.append(kwargs)
        if outputs:
            return make_fake_completion(outputs.pop(0))
        return make_fake_completion("{}")

    monkeypatch.setattr(mod_llm, "completion", fake_completion)
    return outputs, calls


@pytest.fixture
def fake_store(monkeypatch):
    """Replace classifier_mod.store with a recorder (log + gap methods only)."""

    class FakeStore:
        def __init__(self):
            self.logged = []
            self.gaps = []

        def log_classification(self, incident_ref, stage, prompt_version,
                               model, raw_verdict, extra=None):
            self.logged.append({
                "incident_ref": incident_ref, "stage": stage,
                "prompt_version": prompt_version, "model": model,
                "raw": raw_verdict, "extra": extra,
            })

        def record_taxonomy_gap(self, service, suggested_offering, incident_ref):
            self.gaps.append((service, suggested_offering, incident_ref))

    fs = FakeStore()
    monkeypatch.setattr(classifier_mod, "store", fs)
    return fs


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
    }
    out.update(overrides)
    return out


def _full_result_json(**overrides) -> str:
    return json.dumps(_full_result(**overrides))


def _triage_json(kind: str = "incident") -> str:
    return json.dumps({"kind": kind, "reason": "test"})


def _verify_json(verdict: str = "approve", corrections=None, reason: str = "ok") -> str:
    return json.dumps({"verdict": verdict, "corrections": corrections, "reason": reason})


SVC = "System/Application - Nusuk Masar Haj"
OFFERING = f"{SVC}.Service Unavailability"

FULL_KINDS = (TicketKind.incident, TicketKind.service_request)
ROUTED_KINDS = (TicketKind.administrative, TicketKind.inquiry,
                TicketKind.feature_request, TicketKind.test, TicketKind.content_thin)


# ── Stage 0 triage routing ─────────────────────────────────────────────


class TestTriageRouting:
    @pytest.mark.parametrize("kind", [k.value for k in FULL_KINDS])
    def test_full_cascade_kinds(self, kind, fake_completion, fake_store):
        outputs, calls = fake_completion
        outputs.append(_triage_json(kind))
        outputs.append(_full_result_json(service=SVC))
        outputs.append(_full_result_json(service=OFFERING))
        outputs.append(_verify_json())
        result = classifier_mod.classify("Nusuk Masar Haj x", "broken on nusuk masar haj",
                                         incident_ref="ref-1")
        assert result.ticket_kind == TicketKind(kind)
        assert result.incident_type is not None
        assert result.severity is not None
        assert result.service == OFFERING
        assert len(calls) == 4  # triage + stage2 + stage3 + verification
        stages = [e["stage"] for e in fake_store.logged]
        assert "triage" in stages and "stage2" in stages and "stage3" in stages
        assert "verification" in stages
        # every log row carries the incident ref
        assert all(e["incident_ref"] == "ref-1" for e in fake_store.logged)
        assert all(e["prompt_version"] == classifier_mod.PROMPT_VERSION for e in fake_store.logged)

    @pytest.mark.parametrize("kind", [k.value for k in ROUTED_KINDS])
    def test_routed_kinds_system_service_only(self, kind, fake_completion, fake_store):
        outputs, calls = fake_completion
        outputs.append(_triage_json(kind))
        outputs.append(_full_result_json(service=SVC))
        result = classifier_mod.classify("Nusuk Masar Haj x", "something about nusuk masar haj",
                                         incident_ref="ref-2")
        assert result.ticket_kind == TicketKind(kind)
        # no incident fields — null, not fabricated
        assert result.incident_type is None
        assert result.severity is None
        assert result.urgency is None
        # bare service key — no offering dot-path
        assert result.service == SVC
        assert len(calls) == 2  # triage + stage2 ONLY — no stage 3, no verification
        assert all(e["stage"] != "verification" for e in fake_store.logged)
        assert all(e["stage"] != "stage3" for e in fake_store.logged)

    def test_triage_failure_defaults_to_incident(self, fake_completion, fake_store):
        outputs, calls = fake_completion
        outputs.append("not json at all")  # triage fails
        outputs.append(_full_result_json(service=SVC))
        outputs.append(_full_result_json(service=OFFERING))
        outputs.append(_verify_json())
        result = classifier_mod.classify("Nusuk Masar Haj x", "broken on nusuk masar haj")
        assert result.ticket_kind == TicketKind.incident  # conservative default
        assert result.classification_status == "ok"


# ── Stage 3 abstention ─────────────────────────────────────────────────


class TestAbstention:
    def test_none_of_the_above_records_gap(self, fake_completion, fake_store):
        outputs, calls = fake_completion
        outputs.append(_triage_json())
        outputs.append(_full_result_json(service=SVC))
        outputs.append(json.dumps({
            **_full_result(service="NONE_OF_THE_ABOVE", confidence="low"),
            "suggested_offering": "Company Evaluation",
        }))
        outputs.append(_verify_json())
        result = classifier_mod.classify(
            "Nusuk Masar Haj company evaluation page missing",
            "evaluation icon does not appear on nusuk masar haj",
            incident_ref="ref-3",
        )
        assert result.service == f"{SVC}{OFFERING_GAP_SENTINEL}"
        assert result.confidence == "low"
        # the gap is recorded with the service key + suggested offering
        assert fake_store.gaps == [(SVC, "Company Evaluation", "ref-3")]
        assert result.classification_status == "ok"

    def test_none_of_the_above_without_suggestion_uses_unspecified(self, fake_completion, fake_store):
        outputs, calls = fake_completion
        outputs.append(_triage_json())
        outputs.append(_full_result_json(service=SVC))
        outputs.append(json.dumps({**_full_result(service="NONE_OF_THE_ABOVE")}))
        outputs.append(_verify_json())
        result = classifier_mod.classify("Nusuk Masar Haj x", "something odd on nusuk masar haj",
                                         incident_ref="ref-4")
        assert result.service == f"{SVC}{OFFERING_GAP_SENTINEL}"
        assert fake_store.gaps == [(SVC, "(unspecified)", "ref-4")]

    def test_validator_accepts_sentinel_rejects_invention(self):
        ok = ClassificationResult(**_full_result(
            service=f"{SVC}{OFFERING_GAP_SENTINEL}",
            confidence="low",
        ))
        assert ok.service.endswith(OFFERING_GAP_SENTINEL)
        with pytest.raises(ValueError):
            ClassificationResult(**_full_result(service=f"{SVC}.MadeUpOffering"))
        with pytest.raises(ValueError):
            ClassificationResult(**_full_result(service=f"NotAService{OFFERING_GAP_SENTINEL}"))


# ── Honest failure ─────────────────────────────────────────────────────


class TestHonestFailure:
    def test_llm_failure_marks_failed_not_fake_incident(self, fake_completion):
        outputs, calls = fake_completion
        outputs.append(_triage_json())
        outputs.append("garbage")  # stage 1 fails -> cascade fallback
        result = classifier_mod.classify("random ticket", "no system hints here")
        assert result.classification_status == "failed"
        assert result.incident_type is None
        assert result.severity is None
        assert result.urgency is None
        assert result.affected_system == AffectedSystem.other
        assert result.service == "General / Unspecified"
        # E5 marker intact — integration worker / heal / recovery key on it
        assert (result.reasoning or "").startswith("Classification failed after 2 attempts.")
        # no verification pass on a failed ticket
        assert len(calls) == 2


# ── Stage 4 verification ───────────────────────────────────────────────


class TestVerification:
    def test_approve_keeps_cascade_result(self, fake_completion, fake_store):
        outputs, calls = fake_completion
        outputs.append(_triage_json())
        outputs.append(_full_result_json(service=SVC))
        outputs.append(_full_result_json(service=OFFERING))
        outputs.append(_verify_json("approve"))
        result = classifier_mod.classify("Nusuk Masar Haj x", "broken on nusuk masar haj",
                                         incident_ref="ref-5")
        assert result.service == OFFERING
        assert result.incident_type is not None and result.incident_type.value == "Degradation"
        assert result.classification_status == "ok"
        verdict_rows = [e for e in fake_store.logged if e["stage"] == "verification"]
        assert len(verdict_rows) == 1
        assert verdict_rows[0]["extra"]["verdict"] == "approve"

    def test_correct_applies_valid_correction(self, fake_completion):
        outputs, calls = fake_completion
        outputs.append(_triage_json())
        outputs.append(_full_result_json(service=SVC))
        outputs.append(_full_result_json(service=OFFERING))
        outputs.append(_verify_json("correct", corrections={"incident_type": "Unavailability"}))
        result = classifier_mod.classify("Nusuk Masar Haj x", "broken on nusuk masar haj")
        assert result.incident_type is not None and result.incident_type.value == "Unavailability"  # correction applied
        assert result.service == OFFERING

    def test_invalid_correction_discarded_and_logged(self, fake_completion, fake_store):
        outputs, calls = fake_completion
        outputs.append(_triage_json())
        outputs.append(_full_result_json(service=SVC))
        outputs.append(_full_result_json(service=OFFERING))
        outputs.append(_verify_json("correct", corrections={"incident_type": "Banana"}))
        result = classifier_mod.classify("Nusuk Masar Haj x", "broken on nusuk masar haj",
                                         incident_ref="ref-6")
        assert result.incident_type is not None and result.incident_type.value == "Degradation"  # original kept
        # the rejection is logged
        logged = json.dumps(fake_store.logged)
        assert "rejected" in logged or "Banana" in logged

    def test_bare_service_correction_reruns_stage3(self, fake_completion):
        outputs, calls = fake_completion
        outputs.append(_triage_json())
        outputs.append(_full_result_json(service=SVC))
        outputs.append(_full_result_json(service=OFFERING))
        # verifier corrects service to the BARE key -> stage 3 re-runs scoped to it
        outputs.append(_verify_json("correct", corrections={"service": SVC}))
        outputs.append(_full_result_json(service="System/Application - Nusuk Masar Haj.Slowness"))
        result = classifier_mod.classify("Nusuk Masar Haj x", "broken on nusuk masar haj",
                                         incident_ref="ref-7")
        assert result.service == "System/Application - Nusuk Masar Haj.Slowness"
        assert len(calls) == 5  # triage + stage2 + stage3 + verification + stage-3 re-run

    def test_verifier_failure_keeps_cascade_result(self, fake_completion, fake_store):
        outputs, calls = fake_completion
        outputs.append(_triage_json())
        outputs.append(_full_result_json(service=SVC))
        outputs.append(_full_result_json(service=OFFERING))
        outputs.append("garbage")  # verifier LLM response unparseable
        result = classifier_mod.classify("Nusuk Masar Haj x", "broken on nusuk masar haj",
                                         incident_ref="ref-8")
        assert result.service == OFFERING
        assert result.classification_status == "ok"  # never fails the ticket
        assert any(e["stage"] == "verification" for e in fake_store.logged)


# ── Self-consistency (OFF by default) ──────────────────────────────────


class TestSelfConsistency:
    def test_flag_defaults_off(self):
        assert settings.classify_self_consistency is False

    def test_low_confidence_does_not_trigger_reruns_when_off(self, fake_completion):
        outputs, calls = fake_completion
        outputs.append(_triage_json())
        outputs.append(_full_result_json(service=SVC))
        outputs.append(_full_result_json(service=OFFERING, confidence="low"))
        outputs.append(_verify_json())
        result = classifier_mod.classify("Nusuk Masar Haj x", "broken on nusuk masar haj")
        assert result.confidence == "low"
        assert result.needs_review is False
        assert len(calls) == 4  # no self-consistency re-runs


# ── Prompt hygiene (the literal `\\n` bug) ──────────────────────────────


class TestPromptHygiene:
    def test_stage3_prompt_uses_real_newlines(self, fake_completion):
        outputs, calls = fake_completion
        outputs.append(_triage_json())
        outputs.append(_full_result_json(service=SVC))
        outputs.append(_full_result_json(service=OFFERING))
        outputs.append(_verify_json())
        classifier_mod.classify("Nusuk Masar Haj x", "broken on nusuk masar haj")
        stage3_msgs = calls[2]["messages"]  # third call = stage 3
        system = next(m for m in stage3_msgs if m["role"] == "system")
        # real newlines present, literal backslash-n absent
        assert "\n" in system["content"]
        assert "\\n" not in system["content"]

    def test_no_literal_backslash_n_in_any_prompt(self, fake_completion):
        outputs, calls = fake_completion
        outputs.append(_triage_json())
        outputs.append(_full_result_json(service=SVC))
        outputs.append(_full_result_json(service=OFFERING))
        outputs.append(_verify_json())
        classifier_mod.classify("Nusuk Masar Haj x", "broken on nusuk masar haj")
        for c in calls:
            for msg in c["messages"]:
                assert "\\n" not in msg.get("content", "")
