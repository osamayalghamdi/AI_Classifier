"""Cascade classifier: stage-0 triage, the system → service → offering
cascade, routing, and the legacy single-shot path. Never raises — every
failure degrades to the generic fallback.
"""

import logging

from ai_classification.domain.models import ClassificationResult, OFFERING_GAP_SENTINEL
from ai_classification.domain.taxonomy import (
    AffectedSystem,
    Category,
    TicketKind,
    effective_services_by_system,
)
from ai_classification.services.classify.llm import call_llm
from ai_classification.services.classify.parsing import (
    _normalize_canonical,
    _parse_and_validate,
    _parse_stage_offering,
    _parse_stage_system,
    _parse_triage,
)
from ai_classification.services.classify.persistence import (
    _log_classification,
    _record_taxonomy_gap,
)
from ai_classification.services.classify.prompts import (
    _SYSTEM_PROMPT,
    _TRIAGE_SYSTEM_PROMPT,
    _build_retry_prompt,
    _build_stage_system_prompt,
    _build_user_prompt,
)
from ai_classification.services.classify.verification import (
    _enforce_confidence_honesty,
    _self_consistency,
    _verify_classification,
)


_log = logging.getLogger(__name__)


def _triage(title: str, description: str, *, incident_ref: str | None = None) -> TicketKind:
    """Stage 0 — one LLM call deciding the ticket kind. Never raises.

    LLM/parse failure → kind=incident (conservative), logged, and the
    pipeline continues.
    """
    try:
        raw = call_llm([
            {"role": "system", "content": _TRIAGE_SYSTEM_PROMPT},
            {"role": "user", "content": _build_user_prompt(title, description)},
        ], max_tokens=600, temperature=0.0)
    except Exception as e:
        _log.warning("Triage LLM call failed: %s — defaulting to incident", e)
        _log_classification(incident_ref, "triage", f"<triage call failed: {e}>",
                            extra={"fallback": "incident"})
        return TicketKind.incident
    _log_classification(incident_ref, "triage", raw)
    kind = _parse_triage(raw)
    if kind is not None:
        _log.debug("Triage kind=%s", kind.value)
        return kind
    _log.warning("Triage response unrecognized — defaulting to incident")
    _log_classification(incident_ref, "triage", f"<unparseable triage response: {raw[:200]}>",
                        extra={"fallback": "incident"})
    return TicketKind.incident


def _classify_single_shot(title: str, description: str, *, incident_ref: str | None = None) -> ClassificationResult:
    """Legacy single-shot path (CASCADE_CLASSIFICATION=false).

    Byte-identical to the pre-cascade contract except: the canonical_statement
    (not the signature) is normalized, every LLM decision is logged, and the
    fallback marks classification_status="failed" with incident fields null
    (the "Classification failed after 2 attempts" marker is unchanged — E5,
    heal, and recovery depend on it).
    """
    user_prompt = _build_user_prompt(title, description)

    try:
        raw = call_llm([
            {"role": "system", "content": _SYSTEM_PROMPT},
            {"role": "user", "content": user_prompt},
        ], max_tokens=600, temperature=0.0)
        _log_classification(incident_ref, "stage1", raw, extra={"path": "single-shot", "attempt": 1})
        result = _parse_and_validate(raw)
        result.canonical_statement = _normalize_canonical(result.canonical_statement)
        _log.info("Classification succeeded — system=%s, severity=%s, confidence=%s",
                  result.affected_system, result.severity, result.confidence)
        return result
    except Exception as e:
        last_error = str(e)
        _log.warning("First classification attempt failed: %s", last_error)

    try:
        raw = call_llm([
            {"role": "system", "content": _SYSTEM_PROMPT},
            {"role": "user", "content": _build_retry_prompt(user_prompt, last_error)},
        ], max_tokens=600, temperature=0.0)
        _log_classification(incident_ref, "stage1", raw, extra={"path": "single-shot", "attempt": 2})
        result = _parse_and_validate(raw)
        result.canonical_statement = _normalize_canonical(result.canonical_statement)
        _log.info("Classification succeeded on retry — system=%s, severity=%s",
                  result.affected_system, result.severity)
        return result
    except Exception as e:
        last_error = str(e)
        _log.error("Classification failed after retry: %s", last_error)

    _log.warning("Using fallback low-confidence classification for '%s'", title[:60])
    return ClassificationResult(
        affected_system=AffectedSystem.other,
        service="General / Unspecified",
        incident_type=None,
        severity=None,
        urgency=None,
        category=Category.other,
        confidence="low",
        signature="Generic/Unknown",
        reasoning=f"Classification failed after 2 attempts. Last error: {last_error}",
        canonical_statement=f"Incident reported: {title[:120]}",
        classification_status="failed",
        needs_review=True,  # a row the system KNOWS it failed on needs a human
    )
# Stage 1 deterministic resolution — no LLM call.
_SYSTEM_EXACT_NAMES = {
    AffectedSystem.nusuk_masar_haj: "nusuk masar haj",
    AffectedSystem.nusuk_masar_umrah: "nusuk masar umrah",
    AffectedSystem.old_sm: "oldsm",
    AffectedSystem.crm: "crm",
}


_SYSTEM_ALIASES = {
    AffectedSystem.nusuk_masar_haj: ("haj", "hajj"),
    AffectedSystem.nusuk_masar_umrah: ("umrah",),
    AffectedSystem.old_sm: ("old sm", "old system"),
    AffectedSystem.crm: ("crm",),
}


# ── Cascade classifier (CASCADE_CLASSIFICATION=true) ──────────────────
# Coarse-to-fine system → service → offering. Each stage's LLM call returns
# the FULL ClassificationResult JSON with only that stage's field constrained
# to a short option list; the final result is the last executed stage's parsed
# result. Never raises — every stage failure degrades to the generic fallback
# (same shape as the legacy fallback). The flat 193-option prompt is never
# rebuilt in this path.


def _cascade_fallback(title: str, err: str, kind: TicketKind = TicketKind.incident) -> ClassificationResult:
    """Generic low-confidence fallback — same shape as the legacy fallback.

    Honest failure (v3): incident fields are null and classification_status is
    "failed"; the reasoning marker "Classification failed after 2 attempts" is
    kept exactly (E5 — integration worker, heal, and recovery depend on it).
    """
    return ClassificationResult(
        affected_system=AffectedSystem.other,
        service="General / Unspecified",
        incident_type=None,
        severity=None,
        urgency=None,
        category=Category.other,
        confidence="low",
        signature="Generic/Unknown",
        reasoning=f"Classification failed after 2 attempts. Last error: {err}",
        canonical_statement=f"Incident reported: {title[:120]}",
        ticket_kind=kind,
        classification_status="failed",
        needs_review=True,  # honest flag — the system failed on this row
    )


def _resolve_system_deterministic(title: str, description: str) -> AffectedSystem | None:
    """Resolve the affected system from ticket text without any LLM call.

    Priority: (i) an exact system-name substring wins immediately; (ii) else
    count systems with at least one alias hit — exactly one hit is
    deterministic, zero or multiple hits is ambiguous (None → LLM stage 1).
    """
    text = f"{title}\n{description}".lower()
    for system, name in _SYSTEM_EXACT_NAMES.items():
        if name in text:
            return system
    hits = [
        system
        for system, aliases in _SYSTEM_ALIASES.items()
        if any(alias in text for alias in aliases)
    ]
    if len(hits) == 1:
        return hits[0]
    return None


def _stage_system_llm(
    title: str,
    description: str,
    *,
    incident_ref: str | None = None,
    temperature: float = 0.0,
    log_stage: str = "stage1",
    log_extra: dict | None = None,
) -> ClassificationResult | None:
    """Stage 1 LLM fallback — option list is ONLY the 4 AffectedSystem values.

    Returns None on any LLM/parse failure (the caller returns the generic
    fallback — never the flat 193-option list).
    """
    systems = "\n".join(f"  - {s.value}" for s in AffectedSystem)
    rules = (
        "This is stage 1 of 3 (system resolution).\n"
        "- affected_system MUST be exactly one of the 4 allowed values below — pick the system the ticket is about.\n"
        "- CRM: choose ONLY when the ticket text explicitly mentions CRM (the CRM application/system). Never pick CRM "
        "for contracts, accommodation, licensing, reporting, or other inquiries that do not mention CRM.\n"
        "- Tax/VAT data tickets (ضريب/فوترة/VAT/tax invoice): choose Nusuk Masar Haj — tax invoice data is managed "
        "under Haj Invoicing ('7.1 Invoicing and Billing - Nusuk Masar Haj' / 'Tax Data Management').\n"
        "- service: give your best provisional guess as a single string (it will be refined in stage 2); "
        "prefer a value that belongs to the chosen system.\n"
        "- Fill ALL other JSON fields with your best judgment."
    )
    allowed = f"affected_system (pick one — ONLY these {len(list(AffectedSystem))}):\n{systems}"
    _log.debug("Cascade stage 1/3 (system) — %d options", len(list(AffectedSystem)))
    try:
        # max_tokens=600 (not 100): the stage-1 response is the FULL
        # ClassificationResult JSON; 100 tokens truncated it mid-string, so
        # json.loads failed with "Unterminated string" on most tickets.
        raw = call_llm([
            {"role": "system", "content": _build_stage_system_prompt(rules, allowed)},
            {"role": "user", "content": _build_user_prompt(title, description)},
        ], max_tokens=600, temperature=temperature)
    except Exception as e:
        _log.warning("Cascade stage 1/3 (system) LLM call failed: %s", e)
        _log_classification(incident_ref, log_stage, f"<stage call failed: {e}>", extra=log_extra)
        return None
    _log_classification(incident_ref, log_stage, raw, extra=log_extra)
    return _parse_stage_system(raw)


def _stage_service_llm(
    title: str,
    description: str,
    system: AffectedSystem,
    *,
    incident_ref: str | None = None,
    temperature: float = 0.0,
    log_stage: str = "stage2",
    log_extra: dict | None = None,
) -> ClassificationResult | None:
    """Stage 2 — option list is ONLY the resolved system's services.

    Returns None on any LLM/parse failure.
    """
    services = effective_services_by_system().get(system, {})
    options = "\n".join(f"  - {s}" for s in services)
    rules = (
        f"affected_system is FIXED to '{system.value}' (resolved in stage 1) — do not change it.\n"
        "- service MUST be exactly one of the service options listed below (ONLY this system's services).\n"
        "- Fill ALL other JSON fields with your best judgment."
    )
    allowed = (
        f"affected_system: {system.value} (fixed)\n"
        f"service (pick one — ONLY these {len(services)}):\n{options}"
    )
    _log.debug("Cascade stage 2/3 (service) — system='%s', %d options", system.value, len(services))
    try:
        raw = call_llm([
            {"role": "system", "content": _build_stage_system_prompt(rules, allowed)},
            {"role": "user", "content": _build_user_prompt(title, description)},
        ], max_tokens=600, temperature=temperature)
    except Exception as e:
        _log.warning("Cascade stage 2/3 (service) failed for system '%s': %s", system.value, e)
        _log_classification(incident_ref, log_stage, f"<stage call failed: {e}>", extra=log_extra)
        return None
    _log_classification(incident_ref, log_stage, raw, extra=log_extra)
    try:
        return _parse_and_validate(raw)
    except Exception as e:
        _log.warning("Cascade stage 2/3 (service) failed for system '%s': %s", system.value, e)
        return None


def _stage_offering_llm(
    title: str,
    description: str,
    result: ClassificationResult,
    *,
    service_key: str | None = None,
    incident_ref: str | None = None,
    temperature: float = 0.0,
    log_stage: str = "stage3",
    log_extra: dict | None = None,
) -> tuple[ClassificationResult | None, str | None]:
    """Stage 3 — option list is ONLY the chosen service's offering list.

    Empty or single-offering lists SKIP the LLM call (deterministic):
    empty → bare service name, single → "Service.Offering".
    The option list includes NONE_OF_THE_ABOVE: picking it stores the
    OFFERING-GAP sentinel (service="<key>.OFFERING-GAP", confidence="low")
    and records a taxonomy gap with the optional suggested_offering.
    On LLM/parse failure (incl. an invented offering rejected by the
    taxonomy validator): retries ONCE with the validation error, then
    degrades honestly to the BARE service key with confidence "low" — never
    a deterministic fake offering pick. May raise only if the service cannot
    be resolved to an offering list (the caller degrades to fallback).

    Returns (result, suggested_offering) — suggested_offering is the
    optional extra JSON key carried by NONE_OF_THE_ABOVE abstentions.
    """
    system = result.affected_system
    services = effective_services_by_system().get(system, {})
    # Defensive: stage 2 may have returned a dot-path, or the validator may
    # have auto-corrected the system — resolve the bare service key.
    if service_key is not None:
        key = service_key
    else:
        key = result.service if result.service in services else next(
            (k for k in services if result.service.startswith(k + ".")), None
        )
    if key is None:
        raise ValueError(
            f"service '{result.service}' not found in system '{system.value}'"
        )
    offerings = services[key]

    if not offerings:
        _log.debug("Cascade stage 3/3 (offering) skipped — 0 offerings for '%s'", key)
        result.service = key
        return result, None
    if len(offerings) == 1:
        _log.debug("Cascade stage 3/3 (offering) skipped — 1 offering for '%s'", key)
        result.service = f"{key}.{offerings[0]}"
        return result, None

    options = "\n".join(f"  - {o}" for o in offerings)
    rules = (
        f"affected_system is FIXED to '{system.value}'. service is FIXED to '{key}'.\n"
        "- Pick the offering that best matches the ticket.\n"
        "- The offering MUST be one of the listed options EXACTLY as written — copy it verbatim.\n"
        "- NEVER invent a new offering, NEVER rephrase/shorten/translate one, NEVER use the service name as the offering.\n"
        "- 'Error Spikes' is ONLY for tickets that explicitly report a sudden increase in errors/alerts/spikes. If the ticket mentions no spike of errors, do NOT pick it — pick the closest other option or set confidence low.\n"
        "- NONE_OF_THE_ABOVE — use when the ticket's problem genuinely matches no listed offering. If you pick this, also return \"suggested_offering\": a short name for the missing offering.\n"
        f"- Set the service field to '{key}.<offering>' — the service name, a dot, then the chosen offering."
    )
    allowed = (
        f"service (pick one — ONLY these {len(offerings)} offerings, respond as '{key}.<offering>'):\n{options}\n"
        f"  - NONE_OF_THE_ABOVE"
    )
    _log.debug("Cascade stage 3/3 (offering) — service='%s', %d options", key, len(offerings))
    user_prompt = _build_user_prompt(title, description)
    extra = dict(log_extra or {})
    last_error = ""
    for attempt in (1, 2):
        try:
            raw = call_llm([
                {"role": "system", "content": _build_stage_system_prompt(rules, allowed)},
                {"role": "user", "content": user_prompt if attempt == 1 else _build_retry_prompt(user_prompt, last_error)},
            ], max_tokens=600, temperature=temperature)
        except Exception as e:
            last_error = str(e)
            _log.warning("Cascade stage 3/3 (offering) LLM call failed for service '%s': %s", key, last_error)
            _log_classification(incident_ref, log_stage, f"<stage call failed: {last_error}>",
                                extra={**extra, "attempt": attempt})
            continue
        _log_classification(incident_ref, log_stage, raw, extra={**extra, "attempt": attempt})
        try:
            parsed, suggested = _parse_stage_offering(raw, key)
        except Exception as e:
            last_error = str(e)
            _log.warning("Cascade stage 3/3 (offering) invalid for service '%s': %s", key, last_error)
            continue
        if parsed.service.endswith(OFFERING_GAP_SENTINEL):
            parsed.confidence = "low"
            _record_taxonomy_gap(key, suggested or "(unspecified)", incident_ref)
            _log.info(
                "Cascade stage 3/3 (offering) — NONE_OF_THE_ABOVE for '%s'; stored OFFERING-GAP sentinel",
                key,
            )
        return parsed, suggested

    # Both attempts failed — honest degradation to the BARE service key,
    # never a deterministic fake offering pick.
    _log.warning(
        "Cascade stage 3/3 (offering) retry failed for service '%s' — storing bare service key",
        key,
    )
    result.service = key
    result.confidence = "low"
    result.reasoning = (result.reasoning or "") + (
        " offering selection failed after retry; stored at service level."
    )
    return result, None


def _run_cascade(
    title: str,
    description: str,
    *,
    kind: TicketKind = TicketKind.incident,
    incident_ref: str | None = None,
    temperature: float = 0.0,
    log_stage: str | None = None,
    log_extra: dict | None = None,
    pinned_system: AffectedSystem | None = None,
) -> ClassificationResult:
    """Coarse-to-fine cascade: system → service → offering. Never raises.

    LLM calls per ticket (guaranteed by construction):
      - pinned system (payload) / deterministic system hit: 2 (service +
        offering), 1 when the offering stage is skipped (empty/single
        offering list);
      - LLM system fallback:      3 (system + service + offering), 2 when the
        offering stage is skipped.
    log_stage overrides the per-call classification_log stage label (e.g.
    "self-consistency" for consistency re-runs); the natural cascade stage is
    carried in log_extra["cascade_stage"].
    """
    try:
        # ── Stage 1 — system resolution (pinned > deterministic > LLM) ──
        system = pinned_system
        if system is not None:
            _log.debug("Cascade stage 1/3 (system) pinned from payload — %s", system.value)
        else:
            system = _resolve_system_deterministic(title, description)
            if system is not None:
                _log.debug("Cascade stage 1/3 (system) deterministic — %s", system.value)
            else:
                result = _stage_system_llm(
                    title, description,
                    incident_ref=incident_ref, temperature=temperature,
                    log_stage=log_stage or "stage1",
                    log_extra={**(log_extra or {}), "cascade_stage": "stage1"},
                )
                if result is None:
                    _log.warning("Cascade stage 1/3 (system) failed — generic fallback")
                    return _cascade_fallback(title, "system resolution failed", kind=kind)
                system = result.affected_system

        # ── Stage 2 — service selection (1 LLM call, short list) ──
        result = _stage_service_llm(
            title, description, system,
            incident_ref=incident_ref, temperature=temperature,
            log_stage=log_stage or "stage2",
            log_extra={**(log_extra or {}), "cascade_stage": "stage2"},
        )
        if result is None:
            return _cascade_fallback(title, "service selection failed", kind=kind)

        # ── Stage 3 — offering selection (1 LLM call unless skipped) ──
        result, _suggested = _stage_offering_llm(
            title, description, result,
            incident_ref=incident_ref, temperature=temperature,
            log_stage=log_stage or "stage3",
            log_extra={**(log_extra or {}), "cascade_stage": "stage3"},
        )
        if result is None:
            return _cascade_fallback(title, "offering selection failed", kind=kind)
        result.ticket_kind = kind
        if pinned_system is not None:
            result.affected_system = pinned_system
        result.canonical_statement = _normalize_canonical(result.canonical_statement)
        _log.info("Cascade classification succeeded — system=%s, service=%s, severity=%s, confidence=%s",
                  result.affected_system, result.service, result.severity, result.confidence)
        return result
    except Exception as e:
        _log.error("Cascade classification failed: %s", e)
        return _cascade_fallback(title, str(e), kind=kind)


def _classify_routed(
    title: str,
    description: str,
    kind: TicketKind,
    *,
    incident_ref: str | None = None,
    pinned_system: AffectedSystem | None = None,
) -> ClassificationResult:
    """Non-incident kinds — system + service ONLY (stages 1-2).

    Stage 3 (offering) is skipped entirely; the BARE service key is stored
    (no dot-path); incident_type/severity/urgency are None; no verification
    pass. category stays required — filled by the stage-2 LLM (best guess).
    """
    try:
        # ── Stage 1 — system resolution (pinned > deterministic > LLM) ──
        system = pinned_system
        if system is not None:
            _log.debug("Routed stage 1/2 (system) pinned from payload — %s", system.value)
        else:
            system = _resolve_system_deterministic(title, description)
            if system is not None:
                _log.debug("Routed stage 1/2 (system) deterministic — %s", system.value)
            else:
                result = _stage_system_llm(title, description, incident_ref=incident_ref)
                if result is None:
                    _log.warning("Routed stage 1/2 (system) failed — generic fallback")
                    return _cascade_fallback(title, "system resolution failed", kind=kind)
                system = result.affected_system

        # ── Stage 2 — service selection (1 LLM call, short list) ──
        result = _stage_service_llm(title, description, system, incident_ref=incident_ref)
        if result is None:
            return _cascade_fallback(title, "service selection failed", kind=kind)

        # Routing contract: no incident fields, bare service key, no offering.
        result.incident_type = None
        result.severity = None
        result.urgency = None
        services = effective_services_by_system().get(system, {})
        key = result.service if result.service in services else next(
            (k for k in services if result.service.startswith(k + ".")), None
        )
        if key is None:
            _log.warning(
                "Routed classification: service '%s' not in system '%s'",
                result.service, system.value,
            )
            return _cascade_fallback(
                title, f"service '{result.service}' not found in system '{system.value}'", kind=kind
            )
        result.service = key
        result.ticket_kind = kind
        result.canonical_statement = _normalize_canonical(result.canonical_statement)
        # Confidence honesty applies to routed kinds too (2026-08-21): an
        # administrative/inquiry/other ticket landing on General / Unspecified
        # is a guess, not a high-confidence verdict — same rule as incidents.
        _enforce_confidence_honesty(result)
        _log.info("Routed classification (kind=%s) — system=%s, service=%s, confidence=%s",
                  kind.value, result.affected_system, result.service, result.confidence)
        return result
    except Exception as e:
        _log.error("Routed classification failed: %s", e)
        return _cascade_fallback(title, str(e), kind=kind)


def _resolve_pinned_system(affected_system: str | None) -> AffectedSystem | None:
    """Validate a payload-supplied affected system (ticketing system sends it).

    Valid → the system is PINNED and stage 1 (deterministic/LLM resolution)
    is skipped. Invalid → logged and None (fall back to the normal
    resolution). Never invents a system.
    """
    if not affected_system or not str(affected_system).strip():
        return None
    try:
        system = AffectedSystem(str(affected_system).strip())
    except (ValueError, TypeError):
        _log.warning(
            "Payload affected_system %r is not a known system — falling back to LLM resolution",
            affected_system,
        )
        return None
    _log.info("Affected system pinned from payload: %s", system.value)
    return system


def _classify_v3(
    title: str,
    description: str,
    *,
    incident_ref: str | None = None,
    affected_system: str | None = None,
) -> ClassificationResult:
    """Classifier v3 pipeline: triage (stage 0) → cascade (stages 1-3) → verification (stage 4).

    Routing: incident/service_request get the FULL cascade (severity/urgency/
    incident_type filled) + verification; every other kind gets system+service
    only, with incident fields null and no verification. Never raises.
    affected_system (from the ticketing payload, when present) pins the
    system — stage 1 resolution is skipped entirely.
    """
    from ai_classification.services.classify import classifier as classifier_mod

    settings = classifier_mod.settings
    pinned = _resolve_pinned_system(affected_system)
    kind = _triage(title, description, incident_ref=incident_ref)
    if kind not in (TicketKind.incident, TicketKind.service_request):
        return _classify_routed(
            title, description, kind, incident_ref=incident_ref, pinned_system=pinned
        )

    # Cascade retry loop (v3 resilience): a stage's LLM response can fail to
    # parse/validate on a flaky call ("service selection failed") — that is a
    # transient bad-content response, NOT a network error (call_llm already
    # retries those). Re-run the whole cascade with fresh LLM calls before
    # accepting the honest fallback. Config: CLASSIFY_CASCADE_RETRIES (0 =
    # single attempt). Every retry is logged so the rate of residual failures
    # stays observable.
    max_retries = int(getattr(settings, "cascade_retries", 2) or 0)
    result = None
    for attempt in range(max_retries + 1):
        if attempt:
            _log.warning(
                "Cascade attempt %d/%d failed — retrying classification of %r",
                attempt, max_retries, title[:60],
            )
        result = _run_cascade(
            title, description, kind=kind, incident_ref=incident_ref, pinned_system=pinned
        )
        if result.classification_status != "failed":
            break
    assert result is not None
    if result.classification_status == "failed":
        return result  # honest failure after all retries — nothing to verify
    result = _verify_classification(title, description, result, incident_ref=incident_ref)
    if getattr(settings, "classify_self_consistency", False) and result.confidence == "low":
        result = _self_consistency(title, description, result, incident_ref=incident_ref)
    _enforce_confidence_honesty(result)
    return result
