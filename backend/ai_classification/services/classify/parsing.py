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


def _loads_lenient(raw: str) -> object:
    """json.loads with recovery for prose-wrapped replies.

    Providers ignore "respond with JSON only" often enough to matter: the
    reply arrives as "Sure, here it is:\\n```json\\n{...}\\n```".
    strip_json_fences only strips a LEADING fence, so json.loads died on the
    leading sentence and the stage was thrown away. On failure, scan for the
    first BALANCED JSON object with raw_decode starting at each '{'.

    Valid input keeps the exact old behaviour (straight json.loads); when no
    balanced object exists the ORIGINAL JSONDecodeError is re-raised — the
    stage-1 truncation recovery keys off json.JSONDecodeError.
    """
    text = strip_json_fences(raw)
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        decoder = json.JSONDecoder()
        for i, ch in enumerate(text):
            if ch != "{":
                continue
            try:
                return decoder.raw_decode(text, i)[0]
            except json.JSONDecodeError:
                continue
        raise exc


def _parse_and_validate(raw: str) -> ClassificationResult:
    return ClassificationResult.model_validate(_loads_lenient(raw))


# ── Service-name snapping (the near-miss repair) ───────────────────────
# The stage-2/3 option lists are exact strings, and the model regularly
# writes a near-miss of the system suffix instead of copying the option:
# prod's classification_log carries "licensing - Nusuk Hajj",
# "contracts - Nusuk Hajj Services" and even the concatenation
# "licensing - Nusuk Hajj services - Nusuk Masar Haj" where the taxonomy has
# "licensing - Nusuk Masar Haj". The strict validator rejected those, the
# stage retried, and on a second miss the WHOLE classification was thrown
# away — the ticket was stored as Other / "General / Unspecified" with
# classification_status="failed". 41 of 1505 logged stage-2/3 responses
# (2.7%) were near-misses of this kind, every one of them a lost ticket.
#
# Snapping compares the SERVICE CORE: the system-name part is removed from
# both sides, then case/punctuation/whitespace are normalized. Only an
# unambiguous match is accepted (a core shared by two services is skipped),
# so a wrong pick is impossible — the worst case stays "no snap".

_SYSTEM_NAME_RE = re.compile(
    r"nusuk\s+(?:masar\s+)?(?:hajj?|umrah)(?:\s+services)?",
    re.IGNORECASE,
)


def _service_core(text: str) -> str:
    """Service name minus the system suffix, normalized for comparison."""
    core = _SYSTEM_NAME_RE.sub(" ", text or "")
    core = core.lower()
    core = re.sub(r"[^a-z0-9\u0600-\u06FF]+", " ", core)
    return re.sub(r"\s+", " ", core).strip()


def _service_core_index(system_value: str) -> dict[str, str]:
    """{core: service key} for one system — ambiguous cores are dropped."""
    services = SERVICES_BY_SYSTEM.get(system_value, {})
    index: dict[str, str] = {}
    ambiguous: set[str] = set()
    for key in services:
        core = _service_core(key)
        if not core:
            continue
        if core in index and index[core] != key:
            ambiguous.add(core)
        index[core] = key
    for core in ambiguous:
        index.pop(core, None)
    return index


def snap_service(system_value: str, service: str) -> str | None:
    """Map a model-written service string onto a taxonomy service key.

    Returns the canonical key, or None when nothing matches unambiguously
    (the caller then keeps the system and uses its generic service rather
    than discarding the classification). Accepts dot-paths and the
    OFFERING-GAP sentinel by looking at the key part only.
    """
    if not service or not isinstance(service, str):
        return None
    services = SERVICES_BY_SYSTEM.get(system_value, {})
    if not services:
        return None
    raw = service.strip()
    if raw in services:
        return raw
    key_part = raw.split(".")[0] if "." in raw else raw
    if key_part in services:
        return key_part
    # A dot-path of a real key ("<key>.<offering>") — keep the key.
    for k in services:
        if raw.startswith(k + "."):
            return k
    candidates = {raw, key_part}
    index = _service_core_index(system_value)
    for cand in candidates:
        hit = index.get(_service_core(cand))
        if hit:
            return hit
    return None


def generic_service(system_value: str) -> str:
    """The system's catch-all service — used when only the system is known."""
    services = SERVICES_BY_SYSTEM.get(system_value, {})
    for key in services:
        if key.lower().startswith("system/application"):
            return key
    return next(iter(services), "General / Unspecified")


def _parse_stage_service(raw: str, system_value: str) -> ClassificationResult | None:
    """Lenient stage-2 parse — the system is already resolved, so never lose it.

    Strict validation first (the happy path is unchanged); on an off-taxonomy
    service the value is snapped to the closest real option; if it cannot be
    snapped, the system is KEPT and the ticket is stored on the system's
    generic service instead of collapsing to Other / "General / Unspecified".
    Returns None only when the response is not usable JSON at all (the caller
    then takes the old failure path).
    """
    try:
        data = json.loads(strip_json_fences(raw))
    except Exception:
        return None
    if not isinstance(data, dict):
        return None
    # Stage 2 may only choose a service WITHIN the resolved system.
    data["affected_system"] = system_value
    service = str(data.get("service") or "")
    snapped = snap_service(system_value, service)
    if snapped is None:
        snapped = generic_service(system_value)
        data["confidence"] = "low"
    data["service"] = snapped
    try:
        return ClassificationResult.model_validate(data)
    except Exception:
        return None


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
# Strip ONLY the label prefix the model sometimes prepends. Everything the
# model wrote about the incident is kept verbatim.


# "Nusuk Masar Haj/contracts: …" / "Nusuk Masar Haj.Issue Permits: …" — a
# SYSTEM[/SERVICE]-style label, not prose. Anchored to the known system names
# so a colon inside real text is never mistaken for a label.
_LABEL_PREFIX_RE = re.compile(
    r"^\s*(?:Nusuk Masar Haj|Nusuk Masar Umrah|CRM|OldSM|Other)"
    r"\s*(?:[/.|\-–]\s*[^:]{0,60})?:\s+",
    re.IGNORECASE,
)


def _normalize_canonical(cs: str) -> str:
    """Clean a canonical statement WITHOUT destroying what it says.

    HISTORY (fixed 2026-09): this function used to split on the first colon
    ("Bus departs at 14:00…" -> "00…"), delete every 6+ digit run ("Error
    number 10036015" -> "Error )"), delete date-like tokens ("4/6/2026" ->
    "/2026") and delete the words 'number'/'entry'/'percent'. The rationale
    was embedding stability, but the canonical is NOT embedded anywhere (the
    embedding is title+description) and it IS shown to operators and fed to
    the clustering prompts — so the mangling only cost information: prod had
    'Inter-city transport requests (IDs , ) are stuck in a pending approval
    state.' and 'Recurring technical error (Error ) occurs…'.

    Ids, error codes, dates and counts are now preserved: they are often the
    only thing distinguishing two incidents of the same kind. Only a leading
    "System[/service]:" label is removed, plus whitespace normalization.
    """
    if not cs:
        return cs
    cs = _LABEL_PREFIX_RE.sub("", cs)
    cs = re.sub(r"\s+", " ", cs).strip()
    return cs
