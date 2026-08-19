"""LLM-based incident classifier — provider-agnostic via LiteLLM.

Plus classify-and-persist orchestration: calls the classifier, checks for
duplicates by ticket ID (not text), saves the result.

Pipeline position: 20_classify — classification + persistence orchestration.

Classifier v3 (PROMPT_VERSION 2026-08-v3): stage-0 triage routes every
ticket by kind — incident/service_request get the full cascade
(system → service → offering) plus a stage-4 verification audit; every other
kind (administrative, inquiry, feature_request, test, content_thin) is
classified for system+service only, with incident fields null and no
verification. Stage 3 may abstain (NONE_OF_THE_ABOVE → ".OFFERING-GAP"
sentinel + taxonomy_gap row) and never fakes an offering pick on failure.
Every LLM decision is recorded to classification_log; genuine LLM failure
marks classification_status="failed" with the E5 reasoning marker intact.
"""

import inspect
import json
import logging
import re

from collections import Counter
from datetime import datetime, timezone

from pydantic import TypeAdapter

from ai_classification.shared.config import settings
from ai_classification.domain.models import (
    ClassificationResult,
    SimilarOpenIncident,
    OFFERING_GAP_SENTINEL,
)
from ai_classification.domain.taxonomy import (
    AffectedSystem,
    IncidentType,
    Severity,
    Urgency,
    Category,
    TicketKind,
    SERVICES_BY_SYSTEM,
    flatten_services,
)
from ai_classification.api.schemas import ClassifyResponse, ClassifyBatchResponse
from ai_classification.services.classify.llm import call_llm, strip_json_fences
from ai_classification.shared.store import store

_log = logging.getLogger(__name__)


# ── Few-shot examples ─────────────────────────────────────────────────

FEW_SHOT_EXAMPLES = [
    {
        "title": "Login SMS code not arriving — user waiting 20+ min",
        "description": "SMS OTP not delivered after 5 resend attempts. Cannot complete 2FA login via Nusuk Application.",
        "output": {
            "affected_system": "Nusuk Application",
            "service": "Login",
            "incident_type": "Unavailability",
            "severity": "Critical",
            "urgency": "Immediate",
            "category": "Software",
            "confidence": "high",
            "reasoning": "SMS OTP delivery failure across multiple attempts suggests an issue with the Login service's SMS integration.",
            "canonical_statement": "SMS OTP not delivered after 5 resend attempts; 2FA login blocked on Nusuk Application.",
            "signature": "User can't receive SMS OTP for 2FA login",
        },
    },
    {
        "title": "Payment checkout failing — transactions timing out",
        "description": "Checkout process times out at payment step in Nusuk Application. Users in KSA region affected.",
        "output": {
            "affected_system": "Nusuk Application",
            "service": "Umrah Services",
            "incident_type": "Degradation",
            "severity": "Major",
            "urgency": "High",
            "category": "Performance",
            "confidence": "medium",
            "reasoning": "Regional checkout timeouts point to a performance degradation in the payment integration.",
            "canonical_statement": "KSA users experiencing checkout timeouts at payment step in Nusuk Application.",
            "signature": "User can't complete checkout due to timeout at payment step",
        },
    },
    {
        "title": "Hajj package registration not loading for service providers",
        "description": "Nusuk Masar Haj registration page returns blank screen. Service providers unable to submit pilgrim groups.",
        "output": {
            "affected_system": "Nusuk Masar Haj",
            "service": "Registration",
            "incident_type": "Unavailability",
            "severity": "Major",
            "urgency": "High",
            "category": "Software",
            "confidence": "high",
            "reasoning": "Blank page on registration suggests a front-end deployment issue affecting the Registration service.",
            "canonical_statement": "Service providers see blank page; pilgrim group submission blocked in Nusuk Masar Haj Registration.",
            "signature": "Provider can't submit pilgrim groups due to blank registration page",
        },
    },
    {
        "title": "Inspector App crashing on Android after update",
        "description": "Compliance and Monitoring Inspector App crashing on launch. All field inspectors affected since v2.4 update.",
        "output": {
            "affected_system": "Compliance and Monitoring",
            "service": "Inspector App - Compliance and Monitoring",
            "incident_type": "Outage",
            "severity": "Critical",
            "urgency": "Immediate",
            "category": "Software",
            "confidence": "high",
            "reasoning": "Post-update crash on all devices indicates a software regression in the Inspector App.",
            "canonical_statement": "Inspector App crash on launch for all field inspectors after v2.4 update.",
            "signature": "Inspector app crashing on launch after update",
        },
    },
    {
        "title": "Nusuk Card not scanning at entry gates",
        "description": "Hajj Nusuk Card QR code not recognized by gate scanners at multiple entry points. Cards issued this week affected.",
        "output": {
            "affected_system": "Nusuk Card",
            "service": "Hajj Nusuk Card",
            "incident_type": "Degradation",
            "severity": "Major",
            "urgency": "High",
            "category": "Configuration",
            "confidence": "medium",
            "reasoning": "New batch of cards failing at scanners suggests an encoding or configuration mismatch.",
            "canonical_statement": "QR codes not scanning at entry gates for newly issued Hajj Nusuk Cards.",
            "signature": "Nusuk Card QR code not scanning at entry gate",
        },
    },
    {
        "title": "Umrah visa inquiry returning no results",
        "description": "Inquiry portal returns empty results for Umrah Companies inquiries. Started after database migration.",
        "output": {
            "affected_system": "Inquiries",
            "service": "Umrah Companies Inquiry",
            "incident_type": "Degradation",
            "severity": "Major",
            "urgency": "High",
            "category": "Data Issue",
            "confidence": "high",
            "reasoning": "Post-migration empty results suggest data not properly migrated or query broken.",
            "canonical_statement": "Empty results since database migration in Umrah Companies Inquiry portal.",
            "signature": "Inquiry returning empty results after database migration",
        },
    },
]


# ── Prompt builders ───────────────────────────────────────────────────

def _build_examples_block() -> str:
    blocks = []
    for i, ex in enumerate(FEW_SHOT_EXAMPLES, 1):
        inp = json.dumps({"title": ex["title"], "description": ex["description"]}, ensure_ascii=False)
        blocks.append(
            f"Example {i}:\n"
            f"Input:\n{inp}\n"
            f"Output:\n{json.dumps(ex['output'], indent=2, ensure_ascii=False)}"
        )
    return "\n\n".join(blocks)


# Build the full system prompt with taxonomy and examples
def _build_system_prompt() -> str:
    systems = "\n".join(f"  - {s.value}" for s in AffectedSystem)
    types = "\n".join(f"  - {t.value}" for t in IncidentType)
    severities = "\n".join(f"  - {s.value}" for s in Severity)
    urgencies = "\n".join(f"  - {u.value}" for u in Urgency)
    categories = "\n".join(f"  - {c.value}" for c in Category)
    services_by_system = {s.value: svcs for s, svcs in flatten_services().items()}

    return f"""You classify IT support tickets into structured categories. Return ONLY valid JSON.

## JSON Schema
{{
  "affected_system": "string — one from the list below",
  "service": "string — one service from the chosen system's list",
  "incident_type": "Spike | Degradation | Unavailability | Outage — the symptom/what happened. Spike = a sudden increase in errors/alerts/traffic (monitoring-style); use ONLY when the ticket explicitly mentions a spike/surge of errors. Feature requests, test tickets, and tickets describing a missing UI element are NOT Spikes — use Degradation or Unavailability instead.",
  "severity": "Critical | Major | Minor | Cosmetic",
  "urgency": "Immediate | High | Medium | Low",
  "category": "Software | Performance | Configuration | Security | Network Issue | Integration | Data Issue | Human Error | External / Third Party | Other — the root cause type/why it happened",
  "confidence": "low | medium | high",
  "reasoning": "short explanation of your choices",
  "canonical_statement": "detailed description for human reading. Include component, symptoms, scope.",
  "signature": "short problem signature for grouping: 5-8 words, start with the failing action (not actor), ban error message as the head phrase. No names, IDs, dates, or numbers.",
}}

## Key Rules
- incident_type = WHAT HAPPENED (symptom). category = WHY IT HAPPENED (root cause type). Never mix them.
- service must be a single string from the chosen system's service list.
- If unsure, pick the closest match and set confidence "low".
- Respond with JSON only — no markdown, no commentary.

## Examples
{_build_examples_block()}

## Allowed Values

affected_system (pick one):
{systems}

services per system (pick one service from your chosen system):
{json.dumps(services_by_system, indent=2)}

incident_type — WHAT HAPPENED (symptom, pick one):
{types}

severity (pick one):
{severities}

urgency (pick one):
{urgencies}

category — WHY IT HAPPENED (root cause type, pick one):
{categories}

- If unsure, pick the closest match and set confidence "low".
"""


# Build user message with title and description as JSON
def _build_user_prompt(title: str, description: str) -> str:
    return json.dumps({"title": title, "description": description}, ensure_ascii=False)


# ── Stage-0 triage ────────────────────────────────────────────────────
# One LLM call per ticket, always first. Decides the ticket KIND; only
# incident/service_request proceed through the full cascade + verification.

# Real-ticket examples drawn from the live ai_incidents database (read-only
# pull, 2026-08-19) — exact titles and faithful description excerpts.
TRIAGE_EXAMPLES = [
    {
        "kind": "administrative",
        "title": "إغلاق بلاغ",
        "description": "الموضوع: طلب إغلاق البلاغ رقم (202620734484). نود إفادتكم بأنه بخصوص البلاغ رقم (202620734484) المسجل باسم الحاج/ بن راشد المري.",
        "reason": "Closing an existing report — housekeeping, not a system problem.",
    },
    {
        "kind": "test",
        "title": "Final gate test",
        "description": "verify ingest after restructure",
        "reason": "Engineer verification ticket.",
    },
    {
        "kind": "test",
        "title": "Service restructure test",
        "description": "verifying ingest after the move",
        "reason": "Engineer verification ticket.",
    },
    {
        "kind": "test",
        "title": "Review test ticket",
        "description": "testing E1-E9 end to end",
        "reason": "Engineer verification ticket.",
    },
    {
        "kind": "content_thin",
        "title": "x",
        "description": "y",
        "reason": "No meaningful content — placeholder text only.",
    },
    {
        "kind": "feature_request",
        "title": "مقترح إضافة مؤشر أداء للوكلاء الخارجيين ضمن شاشة المؤشرات التشغيلية",
        "description": "نقترح إضافة مؤشر أداء خاص بالوكلاء الخارجيين ضمن شاشة المؤشرات التشغيلية في منصة الشركة السعودية، بما يتيح لشركات العمرة متابعة أداء الوكلاء بشكل دوري.",
        "reason": "Proposes a new KPI to an existing dashboard — an enhancement.",
    },
    {
        "kind": "incident",
        "title": "Rawdah permit fails on date selection",
        "description": "error on done button",
        "reason": "A broken flow — the permit cannot be issued.",
    },
    {
        "kind": "incident",
        "title": "ERROR (10036015) IN ISSUING RAWDAH PERMITS",
        "description": "PLEASE FIND ENCLOSED SCREENSHOT OF ERROR WHILE SAVING PILGRIMS IN RAWDAH PERMIT. IT IS SHOWING ERROR NUMBER 10036015.",
        "reason": "An error blocks permit issuance — something is broken.",
    },
]


def _build_triage_system_prompt() -> str:
    examples = []
    for ex in TRIAGE_EXAMPLES:
        inp = json.dumps({"title": ex["title"], "description": ex["description"]}, ensure_ascii=False)
        examples.append(f"Input:\n{inp}\nKind: \"{ex['kind']}\" — {ex['reason']}")
    examples_block = "\n\n".join(examples)
    return f"""You triage IT support tickets into exactly one of 7 kinds. Return ONLY valid JSON: {{"kind": "...", "reason": "one sentence"}}.

## Kinds
- "incident": something is BROKEN — a system, service, or feature is failing, erroring, unavailable, or degrading. The ticket reports a problem happening NOW.
- "service_request": the ticket asks for something to be provisioned or changed (access, a new user, a modification) and the system is NOT broken. If a system failure is BLOCKING the request, choose "incident" instead.
- "administrative": housekeeping — closing an existing report, approvals, user administration, non-technical follow-up.
- "inquiry": a question about a service, process, or status. No failure, no action beyond an answer.
- "feature_request": proposes a NEW feature or enhancement to an existing system.
- "test": verification/testing of the system or pipeline — created by engineers to validate behavior, not a real user problem.
- "content_thin": the ticket has no meaningful content — placeholder titles/descriptions, gibberish, or near-empty text.

## Examples (drawn from real tickets)
{examples_block}

## Rules
- If the ticket describes anything broken, choose "incident" even if it also asks for something.
- Respond with JSON only: {{"kind": "<one of the 7 kinds>", "reason": "<one sentence>"}}.
"""


_TRIAGE_SYSTEM_PROMPT = _build_triage_system_prompt()

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


# ── Stage prompt scaffolding ──────────────────────────────────────────


def _build_stage_system_prompt(stage_rules: str, allowed_values: str) -> str:
    """Build a stage system prompt: full JSON contract + the stage's short option list."""
    return (
        "You classify IT support tickets into structured categories. Return ONLY valid JSON.\n\n"
        f"{_CASCADE_JSON_SCHEMA}\n\n"
        f"## Stage Rules\n{stage_rules}\n\n"
        f"## Allowed Values\n{allowed_values}"
    )


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


# ── Cached prompt (built once at import time) ─────────────────────────

_SYSTEM_PROMPT = _build_system_prompt()

# Identity of _SYSTEM_PROMPT — recorded on persisted classifications by the
# seams pipeline (provenance). Bump when the prompt content changes.
PROMPT_VERSION = "2026-08-v3"  # v3: stage-0 triage + kind routing + OFFERING-GAP abstention + stage-4 verification
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

    The LLM may vary wording and include ticket IDs, company names, numbers,
    and dates. This strips those so the same problem always produces the
    same embedding regardless of LLM phrasing.
    """
    if not cs:
        return cs
    # Remove label prefix: "Nusuk Masar Haj/contracts: " → ""
    if ":" in cs:
        cs = cs.split(":", 1)[1].strip()

    # Remove ticket IDs, permit numbers, phone numbers
    cs = re.sub(r"\b\d{2,}\b", "", cs)  # 2+ digit numbers (IDs, percentages, counts)
    cs = re.sub(r"\b\d{1,2}[-/]\d{1,2}\b", "", cs)  # date-like patterns

    # Remove stopwords that vary between runs — zero grouping value
    stopwords = r"\b(specific|particular|certain|respective|relevant|number|entry|percent|percentage|usage|above|exceeding)\b"
    cs = re.sub(stopwords, "", cs, flags=re.IGNORECASE)

    # Remove leading/trailing noise
    cs = cs.strip().strip("-").strip(":").strip()
    # Collapse multiple spaces
    cs = re.sub(r"\s+", " ", cs)
    return cs


# Build retry prompt with the last error for a second attempt
def _build_retry_prompt(user_prompt: str, last_error: str) -> str:
    return (
        f"{user_prompt}\n\n---\n"
        f"Your previous response was invalid. Error:\n{last_error}\n\n"
        f"Fix ONLY the JSON. Use exactly the field names and allowed "
        f"values shown in the system prompt. Return valid JSON with no extra text."
    )


# ── classification_log / taxonomy-gap store hooks ─────────────────────
# Worker B implements the store methods in parallel (signatures fixed in the
# v3 contract). getattr-style calls keep this worktree importable until the
# merge; failures are logged, never raised.


def _log_classification(
    incident_ref: str | None,
    stage: str,
    raw: str,
    extra: dict | None = None,
) -> None:
    """Record an LLM decision to classification_log (best-effort)."""
    try:
        fn = getattr(store, "log_classification", None)
        if fn is None:
            _log.debug("store.log_classification not available — skipping log (stage=%s)", stage)
            return
        fn(incident_ref, stage, PROMPT_VERSION, settings.llm_model, raw, extra=extra)
    except Exception as e:
        _log.warning("log_classification failed (stage=%s): %s", stage, e)


def _record_taxonomy_gap(
    service: str,
    suggested_offering: str,
    incident_ref: str | None,
) -> None:
    """Record a taxonomy-gap row for a NONE_OF_THE_ABOVE abstention (best-effort)."""
    try:
        fn = getattr(store, "record_taxonomy_gap", None)
        if fn is None:
            _log.debug("store.record_taxonomy_gap not available — skipping gap record")
            return
        fn(service=service, suggested_offering=suggested_offering, incident_ref=incident_ref)
    except Exception as e:
        _log.warning("record_taxonomy_gap failed: %s", e)


# ── Public API ────────────────────────────────────────────────────────


# Public API: classify an incident, retry once on failure, fallback to low-confidence
def classify(title: str, description: str, *, incident_ref: str | None = None,
             affected_system: str | None = None) -> ClassificationResult:
    """Classify an incident. Always returns — falls back to low-confidence on LLM failure.

    incident_ref: stable ticket id recorded on every classification_log entry.
    Direct callers without one get "anon-<content-hash-prefix>".
    affected_system: supplied by the ticketing system (when present) — it is
    validated and PINNED, skipping stage-1 system resolution entirely.
    """
    _log.info("Classifying — title='%s'", title[:60])
    ref = incident_ref or f"anon-{content_hash(title, description)[:8]}"

    # CASCADE_CLASSIFICATION gate (default TRUE). The Settings field is added
    # by the config commit; getattr keeps this worktree importable until then.
    if getattr(settings, "cascade_classification", True):
        return _classify_v3(title, description, incident_ref=ref, affected_system=affected_system)

    return _classify_single_shot(title, description, incident_ref=ref)


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
    )


# Stage 1 deterministic resolution — no LLM call.
_SYSTEM_EXACT_NAMES = {
    AffectedSystem.nusuk_masar_haj: "nusuk masar haj",
    AffectedSystem.nusuk_masar_umrah: "nusuk masar umrah",
    AffectedSystem.old_sm: "oldsm",
}

_SYSTEM_ALIASES = {
    AffectedSystem.nusuk_masar_haj: ("haj", "hajj"),
    AffectedSystem.nusuk_masar_umrah: ("umrah",),
    AffectedSystem.old_sm: ("old sm", "old system"),
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
    )


# Stage prompt scaffolding — shared JSON contract + one short option list per stage.
_CASCADE_JSON_SCHEMA = """\
## JSON Schema
{
  "affected_system": "string — one from the list below",
  "service": "string — see the stage rules below",
  "incident_type": "Spike | Degradation | Unavailability | Outage — the symptom/what happened. Spike = a sudden increase in errors/alerts/traffic (monitoring-style); use ONLY when the ticket explicitly mentions a spike/surge of errors. Feature requests, test tickets, and tickets describing a missing UI element are NOT Spikes — use Degradation or Unavailability instead.",
  "severity": "Critical | Major | Minor | Cosmetic",
  "urgency": "Immediate | High | Medium | Low",
  "category": "Software | Performance | Configuration | Security | Network Issue | Integration | Data Issue | Human Error | External / Third Party | Other — the root cause type/why it happened",
  "confidence": "low | medium | high",
  "reasoning": "short explanation of your choices",
  "canonical_statement": "detailed description for human reading. Include component, symptoms, scope.",
  "signature": "short problem signature for grouping: 5-8 words, start with the failing action (not actor), ban error message as the head phrase. No names, IDs, dates, or numbers.",
}

## Key Rules
- incident_type = WHAT HAPPENED (symptom). category = WHY IT HAPPENED (root cause type). Never mix them.
- service MUST be one of the allowed values EXACTLY as written — never invent, never rephrase, never shorten a service or offering name.
- Respond with JSON only — no markdown, no commentary.
- If unsure, pick the closest match and set confidence "low".
"""


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
    services = SERVICES_BY_SYSTEM.get(system, {})
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
    services = SERVICES_BY_SYSTEM.get(system, {})
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
        services = SERVICES_BY_SYSTEM.get(system, {})
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
    pinned = _resolve_pinned_system(affected_system)
    kind = _triage(title, description, incident_ref=incident_ref)
    if kind not in (TicketKind.incident, TicketKind.service_request):
        return _classify_routed(
            title, description, kind, incident_ref=incident_ref, pinned_system=pinned
        )

    result = _run_cascade(
        title, description, kind=kind, incident_ref=incident_ref, pinned_system=pinned
    )
    if result.classification_status == "failed":
        return result  # honest failure — nothing to verify
    result = _verify_classification(title, description, result, incident_ref=incident_ref)
    if getattr(settings, "classify_self_consistency", False) and result.confidence == "low":
        result = _self_consistency(title, description, result, incident_ref=incident_ref)
    return result


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


# ── Content hash for analytics (not used for dedupe) ─────────────────────
# Stored on each incident for analytical queries but never gates creation.
# Digit blanking catches alerts that differ only by percentage/threshold value.


def content_hash(title: str, description: str) -> str:
    import hashlib
    text = f"{title} {description}".lower()
    text = re.sub(r"<[^>]+>", " ", text)       # strip HTML
    text = re.sub(r"\d+(\.\d+)?", "#", text)    # digits → placeholder
    text = re.sub(r"\s+", " ", text).strip()
    return hashlib.sha256(text.encode()).hexdigest()[:16]


# ── Classify-and-persist orchestration ──────────────────────────────────


# Classify a single incident and save to store
def classify_and_store(
    title: str,
    description: str,
    extracted_text: str = "",
    documents: list[str] | None = None,
    assign_group: str = "",
    assignee: str = "",
    priority: str | None = None,
    status: str = "active",
    notes: str | None = None,
    discussion_history: list[dict] | None = None,
    escalation_info: str | None = None,
    completion_code: str | None = None,
    source_ticket_id: str = "",
    precomputed: ClassificationResult | None = None,
    affected_system: str | None = None,
) -> ClassifyResponse:
    _log.info("Classifying incident — title='%s', group='%s', priority=%s, ticket_id='%s'",
              title[:60], assign_group, priority, source_ticket_id)

    # ── ID-based dedupe ──
    # The ONLY deduplication mechanism: exact match on the originating
    # ticket ID. If this ticket ID was already processed, return the
    # existing incident unchanged (idempotent). Text similarity is
    # surfaced as informational only (similar_open_incidents) and must
    # never suppress creation of a new incident.
    if source_ticket_id:
        existing = store.get_incident_by_source_ticket_id(source_ticket_id)
        if existing is not None:
            _log.info("Ticket ID '%s' already processed → idempotent return of incident %s",
                      source_ticket_id, existing["id"][:8])
            cls_data = existing.get("classification_dict", {})
            result = TypeAdapter(ClassificationResult).validate_python(cls_data)
            embed_text = result.canonical_statement or f"{title} {description}"
            matches = store.find_similar(embed_text, extracted_text=extracted_text, classification=result)
            return ClassifyResponse(
                incident_id=existing["id"],
                incident_title=title,
                classification=result,
                similar_open_incidents=[
                    SimilarOpenIncident(
                        id=m.id,
                        title=m.title,
                        similarity=round(m.similarity, 4),
                        classification=m.classification,
                        canonical_statement=m.classification.canonical_statement,
                    )
                    for m in matches
                ],
            )

    # ── Content-hash dedupe gate ──
    # Runs when there's NO source_ticket_id (external feed: title +
    # description only). Exact duplicates (digit-blanked) within the recency
    # window increment occurrence_count and return the existing incident
    # instead of creating a new one. ID-based dedupe above takes priority
    # when a stable ticket ID IS present.
    h = content_hash(title, description)
    existing = store.get_incident_by_hash(h)
    if existing and existing.get("last_seen"):
        age = (datetime.now(timezone.utc) - existing["last_seen"]).total_seconds()
        if age < 86400 * 7:  # 7-day window
            _log.info("Content hash match → incrementing occurrence_count for %s (seen %d×)",
                      existing["id"][:8], existing["occurrence_count"])
            store.increment_occurrence(existing["id"])
            # Return the existing classification
            cls_data = json.loads(existing["classification_json"]) if isinstance(existing["classification_json"], str) else existing["classification_json"]
            result = TypeAdapter(ClassificationResult).validate_python(cls_data)
            embed_text = result.canonical_statement or f"{title} {description}"
            matches = store.find_similar(embed_text, extracted_text=extracted_text, classification=result)
            return ClassifyResponse(
                incident_id=existing["id"],
                incident_title=title,
                classification=result,
                similar_open_incidents=[
                    SimilarOpenIncident(
                        id=m.id,
                        title=m.title,
                        similarity=round(m.similarity, 4),
                        classification=m.classification,
                        canonical_statement=m.classification.canonical_statement,
                    )
                    for m in matches
                ],
            )

    # ── v3: the incident id is generated BEFORE classifying so every
    # classification_log entry (triage, stages, verification) carries it.
    incident_id = store.generate_id()

    result = precomputed if precomputed is not None else classify(
        title, description, incident_ref=incident_id, affected_system=affected_system
    )

    # ── Severity→priority mapping (only when the caller did not supply one) ──
    _priority_map = {"Critical": "critical", "Major": "high", "Minor": "medium", "Cosmetic": "low"}
    if not priority:
        sev = result.severity.value if hasattr(result.severity, "value") else result.severity
        priority = _priority_map.get(sev, "medium")

    embed_text = result.canonical_statement or f"{title} {description}"

    matches = store.find_similar(embed_text, extracted_text=extracted_text, classification=result)

    _log.info("Classify result — id=%s, system=%s, service=%s, severity=%s, confidence=%s, dupes=%d",
              incident_id, result.affected_system, result.service,
              result.severity, result.confidence, len(matches))
    _log.debug("Canonical: %s", result.canonical_statement[:120] if result.canonical_statement else "(none)")
    if matches:
        _log.info("Similar open incidents found — %d related incidents", len(matches))
        for m in matches:
            _log.debug("  Similar: %s — %.1f%% — %s", m.id, m.similarity * 100, m.title[:60])

    _save_kwargs: dict = dict(
        documents=documents or [],
        assign_group=assign_group,
        assignee=assignee,
        priority=priority,
        status=status,
        notes=notes,
        discussion_history=discussion_history or [],
        escalation_info=escalation_info,
        completion_code=completion_code,
        content_hash=h,
        source_ticket_ids=[source_ticket_id] if source_ticket_id else [incident_id],
    )
    # v3: persist ticket_kind/classification_status to the dedicated columns
    # (worker B's save_incident accepts them; fall back gracefully when this
    # worktree snapshot predates that merge).
    try:
        _sig = inspect.signature(store.save_incident)
    except Exception:
        _sig = None
    if _sig is not None and "ticket_kind" in _sig.parameters:
        _save_kwargs["ticket_kind"] = result.ticket_kind.value
        _save_kwargs["classification_status"] = result.classification_status
    store.save_incident(
        incident_id, title, description, result, extracted_text, **_save_kwargs
    )

    _log.info("Incident %s classified — system=%s, severity=%s, confidence=%s, dupes=%d",
              incident_id, result.affected_system, result.severity, result.confidence, len(matches))

    # ── Flow A (v2 persistent clustering): LLM-decided assignment to an
    # existing active cluster, in a BACKGROUND task — slow inference must
    # never delay the classify response (ingestion stays synchronous).
    if settings.cluster_assign_on_arrival:
        try:
            from ai_classification.services.cluster.persistent import assign_in_background
            assign_in_background(incident_id)
        except Exception as exc:  # noqa: BLE001 — clustering must not break ingestion
            _log.warning("Flow A background assignment failed to start: %s", exc)

    return ClassifyResponse(
        incident_title=title,
        classification=result,
        incident_id=incident_id,
        similar_open_incidents=[
            SimilarOpenIncident(
                id=m.id,
                title=m.title,
                similarity=round(m.similarity, 4),
                classification=m.classification,
                canonical_statement=m.classification.canonical_statement,
            )
            for m in matches
        ],
    )


# Classify multiple incidents in batch
def classify_batch(incidents: list[dict]) -> ClassifyBatchResponse:
    results = []
    failed = 0
    for inc in incidents:
        try:
            r = classify_and_store(
                inc.get("title", ""),
                inc.get("description", ""),
                inc.get("extracted_text", ""),
                documents=inc.get("documents"),
                assign_group=inc.get("assign_group", ""),
                assignee=inc.get("assignee", ""),
                priority=inc.get("priority"),
                status=inc.get("status", "active"),
                notes=inc.get("notes"),
                discussion_history=inc.get("discussion_history"),
                escalation_info=inc.get("escalation_info"),
                completion_code=inc.get("completion_code"),
                affected_system=inc.get("affected_system"),
            )
            results.append(r)
        except Exception as e:
            _log.error("Batch classify failed for '%s': %s", inc.get("title", "")[:40], e)
            failed += 1
    _log.info("Batch classify — %d/%d succeeded", len(results), len(incidents))
    return ClassifyBatchResponse(results=results, total=len(incidents), failed=failed)
