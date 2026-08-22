"""Stage-4 verification audit, corrections, confidence honesty, self-consistency."""

import json
import logging

from collections import Counter

from ai_classification.domain.models import ClassificationResult, OFFERING_GAP_SENTINEL
from ai_classification.domain.taxonomy import (
    IncidentType,
    Severity,
    SERVICES_BY_SYSTEM,
)
from ai_classification.services.classify.llm import call_llm, strip_json_fences
from ai_classification.services.classify.persistence import (
    _log_classification,
    _record_taxonomy_gap,
)
from ai_classification.services.classify.prompts import _build_user_prompt


_log = logging.getLogger(__name__)


def _enforce_confidence_honesty(result: ClassificationResult) -> None:
    """Post-classification confidence rule (user directive, 2026-08-19):

    A classification is only 'high'/'medium' when the LLM actually landed on
    a real taxonomy service. Guesses and fallbacks must be marked 'low':
      - 'General / Unspecified' service (no real service found)
      - OFFERING-GAP abstention (already forced low, kept for safety)
      - hedging reasoning that admits a guess ('closest', 'not in the
        allowed list', 'safe bet', 'best match available', ...)

    This kills the misleading 'high'-confidence / wrong-service combos the
    audit found (e.g. 4 CRM tickets with conf=high on General/Unspecified).
    """
    svc = (result.service or "").lower()
    if "general" in svc or "unspecified" in svc or result.service.endswith(".OFFERING-GAP"):
        result.confidence = "low"
        return
    reasoning = (result.reasoning or "").lower()
    hedge = ("closest" in reasoning or "not in the allowed list" in reasoning
             or "safe bet" in reasoning or "best match available" in reasoning
             or "closest related" in reasoning)
    if hedge and result.confidence != "low":
        result.confidence = "low"


# ── Stage 4 verification ──────────────────────────────────────────────
# One fresh LLM call after the cascade (incident/service_request kinds ONLY).
# The auditor NEVER sees the cascade reasoning — only the ticket text and the
# final verdict fields. Corrections go through the STRICT validator; invalid
# ones are discarded + logged. Verifier failure never fails the ticket.

_VERIFIABLE_FIELDS = ("affected_system", "service", "incident_type", "severity")


def _bare_service_key(result: ClassificationResult) -> str | None:
    """Resolve the BARE service key of a (possibly dot-path / OFFERING-GAP) value."""
    services = SERVICES_BY_SYSTEM.get(result.affected_system, {})
    svc = result.service
    if svc in services:
        return svc
    for key in services:
        if svc.startswith(key + "."):
            return key
    return None


def _build_verification_prompt(title: str, description: str, result: ClassificationResult) -> str:
    """Stage-4 auditor prompt — ticket text + final verdict fields only."""
    key = _bare_service_key(result)
    if key:
        offering_options = "\n".join(
            f"  - {key}.{o}"
            for o in SERVICES_BY_SYSTEM.get(result.affected_system, {}).get(key, [])
        )
    else:
        offering_options = f"  - {result.service}"
    types = "\n".join(f"  - {t.value}" for t in IncidentType)
    severities = "\n".join(f"  - {s.value}" for s in Severity)
    return f"""A ticket was classified. Check the classification against the ticket text.

Ticket:
Title: {title}
Description: {description}

Classification:
- affected_system: {result.affected_system.value}
- service: {result.service}
- incident_type: {result.incident_type.value if result.incident_type else "none"}
- severity: {result.severity.value if result.severity else "none"}

Check:
1. Does the offering describe the ticket's actual problem (not just its general area)?
2. Is incident_type the SYMPTOM (what happened), not the cause?
3. Is severity proportionate?

Return JSON: {{"verdict": "approve" | "correct", "corrections": {{field: new_value, ...}} | null, "reason": "one sentence"}}

Corrections must use only these allowed taxonomy values:
- service (offerings of the current service):
{offering_options}
  - NONE_OF_THE_ABOVE
- incident_type:
{types}
- severity:
{severities}
"""


def _verify_classification(
    title: str,
    description: str,
    result: ClassificationResult,
    *,
    incident_ref: str | None = None,
) -> ClassificationResult:
    """Stage 4 — audit the final verdict with a fresh LLM call.

    Verifier LLM/parse failure → keep the cascade result, log it, never fail
    the ticket.
    """
    try:
        raw = call_llm([
            {"role": "system", "content": _build_verification_prompt(title, description, result)},
            {"role": "user", "content": _build_user_prompt(title, description)},
        ], max_tokens=600, temperature=0.0)
    except Exception as e:
        _log.warning("Verification LLM call failed: %s", e)
        _log_classification(incident_ref, "verification", f"<verification call failed: {e}>",
                            extra={"verdict": "error"})
        return result
    try:
        data = json.loads(strip_json_fences(raw))
        verdict = data.get("verdict", "approve")
        corrections = data.get("corrections")
        reason = data.get("reason", "")
    except Exception as e:
        _log.warning("Verification response unparseable: %s", e)
        _log_classification(incident_ref, "verification", raw, extra={"verdict": "parse-error"})
        return result
    _log_classification(incident_ref, "verification", raw,
                        extra={"verdict": verdict, "reason": reason})
    _log.info("Verification verdict=%s corrections=%s reason=%s", verdict, corrections, reason)
    if corrections:
        result = _apply_verification_corrections(
            title, description, result, corrections, incident_ref=incident_ref
        )
    return result


def _apply_verification_corrections(
    title: str,
    description: str,
    result: ClassificationResult,
    corrections: dict,
    *,
    incident_ref: str | None = None,
) -> ClassificationResult:
    """Apply verifier corrections through the STRICT validator.

    Invalid corrections are DISCARDED + logged; the original value is kept.
    A valid service correction re-runs stage 3 scoped to the corrected
    service (or applies the OFFERING-GAP sentinel for NONE_OF_THE_ABOVE).
    """
    current = result
    for field, new_value in (corrections or {}).items():
        if field not in _VERIFIABLE_FIELDS:
            _log.warning("Verification: correction for non-verifiable field '%s' ignored", field)
            continue
        if field == "service":
            current = _apply_service_correction(
                title, description, current, new_value, incident_ref=incident_ref
            )
            continue
        try:
            data = current.model_dump()
            data[field] = new_value
            corrected = ClassificationResult.model_validate(data)
        except Exception as e:
            _log.warning(
                "Verification: correction for '%s' rejected (%s) — keeping original",
                field, e,
            )
            continue
        _log.info("Verification: %s corrected to '%s'", field, new_value)
        current = corrected
    return current


def _apply_service_correction(
    title: str,
    description: str,
    result: ClassificationResult,
    new_value: object,
    *,
    incident_ref: str | None = None,
) -> ClassificationResult:
    """Apply a service correction: strict-validate, then abstain or re-run stage 3."""
    raw_svc = str(new_value).strip()

    # Abstention: the auditor says no listed offering fits.
    if raw_svc == "NONE_OF_THE_ABOVE" or raw_svc.endswith(".NONE_OF_THE_ABOVE"):
        key = _bare_service_key(result)
        if key is None:
            _log.warning(
                "Verification: service correction NONE_OF_THE_ABOVE has no resolvable service key — ignored"
            )
            return result
        try:
            data = result.model_dump()
            data["service"] = f"{key}{OFFERING_GAP_SENTINEL}"
            data["confidence"] = "low"
            corrected = ClassificationResult.model_validate(data)
        except Exception as e:
            _log.warning("Verification: service correction NONE_OF_THE_ABOVE rejected (%s)", e)
            return result
        _record_taxonomy_gap(key, "(unspecified)", incident_ref)
        _log.info("Verification: service corrected to OFFERING-GAP for '%s'", key)
        return corrected

    # Normal path — strict validator gate first.
    try:
        data = result.model_dump()
        data["service"] = raw_svc
        corrected = ClassificationResult.model_validate(data)
    except Exception as e:
        _log.warning(
            "Verification: service correction '%s' rejected (%s) — keeping original",
            raw_svc, e,
        )
        return result

    key = _bare_service_key(corrected)
    if key is None:
        _log.warning(
            "Verification: service correction '%s' has no resolvable service key — keeping original",
            raw_svc,
        )
        return result
    if corrected.service == key:
        # Bare service correction: re-run stage 3 scoped to the corrected service.
        try:
            from ai_classification.services.classify.cascade import _stage_offering_llm
            rerun, _suggested = _stage_offering_llm(
                title, description, corrected, service_key=key, incident_ref=incident_ref
            )
            if rerun is not None:
                _log.info("Verification: service corrected to '%s' (stage-3 re-run)", rerun.service)
                return rerun
        except Exception as e:
            _log.warning(
                "Verification: stage-3 re-run for corrected service '%s' failed (%s)",
                key, e,
            )
        corrected.service = key
        corrected.confidence = "low"
    _log.info("Verification: service corrected to '%s'", corrected.service)
    return corrected


# ── Self-consistency (OFF by default) ─────────────────────────────────
# When enabled, tickets ending confidence=low are re-run 3× at temperature
# 0.7 (full cascade) and majority-voted per field. No majority → the
# low-confidence result is kept with needs_review=True.

_SELF_CONSISTENCY_FIELDS = ("affected_system", "service", "incident_type", "severity", "urgency", "category")


def _self_consistency(
    title: str,
    description: str,
    result: ClassificationResult,
    *,
    incident_ref: str | None = None,
) -> ClassificationResult:
    """Re-run the full cascade 3× at temperature 0.7; majority vote per field."""
    runs: list[ClassificationResult] = []
    for i in range(3):
        try:
            from ai_classification.services.classify.cascade import _run_cascade
            r = _run_cascade(
                title, description,
                kind=result.ticket_kind,
                incident_ref=incident_ref,
                temperature=0.7,
                log_stage="self-consistency",
                log_extra={"run": i},
            )
            runs.append(r)
        except Exception as e:
            _log.warning("Self-consistency re-run %d failed: %s", i, e)
    runs = [r for r in runs if r.classification_status == "ok"]
    if len(runs) < 2:
        _log.warning("Self-consistency: no usable re-runs — keeping result, needs_review=True")
        result.needs_review = True
        _log_classification(
            incident_ref, "self-consistency",
            json.dumps({"outcome": "no-usable-runs", "runs": len(runs)}, ensure_ascii=False),
            extra={"outcome": "no-usable-runs"},
        )
        return result

    votes = {
        field: Counter(getattr(r, field) for r in runs)
        for field in _SELF_CONSISTENCY_FIELDS
    }
    majority = {}
    for field, counter in votes.items():
        value, count = counter.most_common(1)[0]
        if count >= 2:
            majority[field] = value
    _log_classification(
        incident_ref, "self-consistency",
        json.dumps({
            "outcome": "majority" if majority else "no-majority",
            "majority": {k: (v.value if hasattr(v, "value") else v) for k, v in majority.items()},
            "runs": len(runs),
        }, ensure_ascii=False),
        extra={"outcome": "majority" if majority else "no-majority"},
    )
    if not majority:
        _log.warning("Self-consistency: no field majority — keeping result, needs_review=True")
        result.needs_review = True
        return result
    try:
        data = result.model_dump()
        for field, value in majority.items():
            data[field] = value.value if hasattr(value, "value") else value
        voted = ClassificationResult.model_validate(data)
        _log.info("Self-consistency majority applied: %s", {f: data[f] for f in majority})
        return voted
    except Exception as e:
        _log.warning("Self-consistency majority vote failed to validate: %s", e)
        result.needs_review = True
        return result
