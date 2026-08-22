"""Lenient/strict JSON parsing + normalization for the classifier cascade."""

import json
import logging
import re

from ai_classification.domain.models import ClassificationResult, OFFERING_GAP_SENTINEL
from ai_classification.domain.taxonomy import (
    AffectedSystem,
    IncidentType,
    Severity,
    Urgency,
    Category,
    TicketKind,
    SERVICES_BY_SYSTEM,
)
from ai_classification.services.classify.llm import strip_json_fences


_log = logging.getLogger(__name__)


_TICKET_KIND_VALUES = {k.value for k in TicketKind}


def _parse_triage(raw: str) -> TicketKind | None:
    """Lenient stage-0 parse — recover the kind even from truncated JSON."""
    try:
        data = json.loads(strip_json_fences(raw))
        if isinstance(data, dict):
            kind = data.get("kind")
            if kind in _TICKET_KIND_VALUES:
                return TicketKind(kind)
    except Exception:
        pass
    # Truncation recovery: "kind" appears early in the JSON.
    m = re.search(r'"kind"\s*:\s*"([^"]+)"', raw)
    if m and m.group(1) in _TICKET_KIND_VALUES:
        return TicketKind(m.group(1))
    return None


# ── JSON parsing ──────────────────────────────────────────────────────


# Parse JSON and validate against ClassificationResult schema


def _parse_and_validate(raw: str) -> ClassificationResult:
    return ClassificationResult.model_validate(json.loads(strip_json_fences(raw)))


def _parse_stage_system(raw: str) -> ClassificationResult | None:
    """Lenient stage-1 parse — validate ONLY affected_system.

    Stage 1 asks the LLM for a PROVISIONAL service guess ("refined in
    stage 2") which is usually NOT a taxonomy value. The strict
    _parse_and_validate would reject the whole stage via
    ClassificationResult._check_service_in_system (ValueError), so nearly
    every LLM-resolved ticket fell back to Other/General/low. Here the
    provisional service is coerced to a valid value for the chosen system
    (first service, or "General / Unspecified") and all other LLM fields are
    kept. Returns None only when affected_system is missing/invalid or
    pydantic still rejects (bad enum etc.). Stages 2/3 and the single-shot
    path keep the strict validator — this leniency is stage-1 ONLY.

    Truncation safety: the full stage-1 JSON occasionally exceeds max_tokens
    and is cut mid-string (json.loads: "Unterminated string"). affected_system
    is the first schema field, so it is always intact — extract it with a
    regex fallback rather than failing the stage.
    """
    try:
        data = json.loads(strip_json_fences(raw))
        if not isinstance(data, dict):
            return None
        try:
            system = AffectedSystem(data.get("affected_system"))
        except (ValueError, TypeError):
            return None

        services = SERVICES_BY_SYSTEM.get(system, {})
        service = data.get("service") or ""
        if service not in services and not any(
            service.startswith(k + ".") for k in services
        ):
            service = next(iter(services), "General / Unspecified")

        data["affected_system"] = system.value
        data["service"] = service
        return ClassificationResult.model_validate(data)
    except (json.JSONDecodeError, ValueError) as e:
        # Truncated JSON (or otherwise unparseable) — recover the system.
        _log.warning(
            "Cascade stage 1/3 (system) JSON incomplete (%s) — recovering affected_system from partial response",
            e,
        )
        return _stage_system_from_partial(raw)
    except Exception as e:
        _log.warning("Cascade stage 1/3 (system) lenient parse failed: %s", e)
        return None


def _parse_stage_offering(raw: str, key: str) -> tuple[ClassificationResult, str | None]:
    """Parse a stage-3 response.

    Extracts the optional ``suggested_offering`` key BEFORE the strict
    ClassificationResult validation (it is not part of the result schema),
    and rewrites a NONE_OF_THE_ABOVE pick to the OFFERING-GAP sentinel
    (service="<key>.OFFERING-GAP", confidence="low"). Raises on invalid JSON
    or an invented offering — the caller retries once.
    """
    data = json.loads(strip_json_fences(raw))
    if not isinstance(data, dict):
        raise ValueError("stage-3 response is not a JSON object")
    suggested = data.pop("suggested_offering", None)
    if not isinstance(suggested, str) or not suggested.strip():
        suggested = None
    else:
        suggested = suggested.strip()
    svc = str(data.get("service", ""))
    if svc == "NONE_OF_THE_ABOVE" or svc == f"{key}.NONE_OF_THE_ABOVE" or svc.endswith(".NONE_OF_THE_ABOVE"):
        data["service"] = f"{key}{OFFERING_GAP_SENTINEL}"
        data["confidence"] = "low"
    return ClassificationResult.model_validate(data), suggested


def _stage_system_from_partial(raw: str) -> ClassificationResult | None:
    """Recover affected_system from a truncated stage-1 response.

    Only the system (and optionally confidence) are needed from stage 1 —
    stage 2 replaces every other field. Never raises; returns None if the
    system cannot be determined.
    """
    try:
        m = re.search(r'"affected_system"\s*:\s*"([^"]+)"', raw)
        if not m:
            return None
        system = AffectedSystem(m.group(1))
        service = next(iter(SERVICES_BY_SYSTEM.get(system, {})), "General / Unspecified")
        cm = re.search(r'"confidence"\s*:\s*"([^"]+)"', raw)
        confidence = cm.group(1) if cm and cm.group(1) in ("low", "medium", "high") else "low"
        return ClassificationResult(
            affected_system=system,
            service=service,
            incident_type=IncidentType.degradation,
            severity=Severity.minor,
            urgency=Urgency.low,
            category=Category.other,
            confidence=confidence,
            signature="Generic/Unknown",
            reasoning="Stage-1 response truncated; affected_system recovered from partial JSON.",
            canonical_statement=f"Incident affecting {system.value}; details recovered from partial classification.",
        )
    except Exception as e:
        _log.warning("Cascade stage 1/3 (system) partial recovery failed: %s", e)
        return None


# ── Canonical statement post-processing ──────────────────────────────────
# Strip noise that varies between runs but has no grouping value.


def _normalize_canonical(cs: str) -> str:
    """Clean a canonical statement for consistent embedding.

    The LLM may vary wording and include ticket IDs, company names, and
    dates. This strips those so the same problem always produces the
    same embedding regardless of LLM phrasing.

    Numbers are PRESERVED: counts, amounts, and percentages are often the
    distinguishing signal between incidents (\"600 pilgrims affected\" vs
    \"13 pilgrims affected\"). Only ticket/permit ID-like tokens (6+ digit
    runs) and date-like patterns are stripped.
    """
    if not cs:
        return cs
    # Remove label prefix: "Nusuk Masar Haj/contracts: " → ""
    if ":" in cs:
        cs = cs.split(":", 1)[1].strip()

    # Remove long ID-like tokens (ticket/permit/phone numbers, 6+ digits).
    # Short numbers (counts, amounts, percentages) are kept — they are
    # grouping-relevant context, not noise.
    cs = re.sub(r"\b\d{6,}\b", "", cs)
    cs = re.sub(r"\b\d{1,2}[-/]\d{1,2}\b", "", cs)  # date-like patterns

    # Remove stopwords that vary between runs — zero grouping value
    stopwords = r"\b(specific|particular|certain|respective|relevant|number|entry|percent|percentage|usage|above|exceeding)\b"
    cs = re.sub(stopwords, "", cs, flags=re.IGNORECASE)

    # Remove leading/trailing noise
    cs = cs.strip().strip("-").strip(":").strip()
    # Collapse multiple spaces
    cs = re.sub(r"\s+", " ", cs)
    return cs
