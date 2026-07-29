"""LLM-based incident classifier — provider-agnostic via LiteLLM.

Plus classify-and-persist orchestration: calls the classifier, checks for
duplicates by ticket ID (not text), saves the result.
"""

import json
import logging
import re

from datetime import datetime, timezone

from pydantic import TypeAdapter

from ..config import settings
from ..domain.models import ClassificationResult, SimilarOpenIncident
from ..domain.taxonomy import (
    AffectedSystem,
    IncidentType,
    Severity,
    Urgency,
    Category,
    flatten_services,
)
from ..api.schemas import ClassifyResponse, ClassifyBatchResponse
from .llm import call_llm, strip_json_fences
from .store import store

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
            "failure_mode": "FM-000",
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
            "failure_mode": "FM-000",
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
            "failure_mode": "FM-000",
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
            "failure_mode": "FM-000",
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
            "failure_mode": "FM-000",
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
            "failure_mode": "FM-000",
        },
    },
]


# ── Prompt builders ───────────────────────────────────────────────────

# Build examples block with failure mode codes from the taxonomy
def _build_fm_taxonomy_block() -> str:
    from .failure_modes import FAILURE_MODES
    lines = []
    for code, fm in sorted(FAILURE_MODES.items()):
        name, system, service, severity = fm[0], fm[1], fm[2], fm[3]
        lines.append(f"  {code}: {name} [{system}/{service}, {severity}]")
    return "\n".join(lines)


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
  "incident_type": "Spike | Degradation | Unavailability | Outage — the symptom/what happened",
  "severity": "Critical | Major | Minor | Cosmetic",
  "urgency": "Immediate | High | Medium | Low",
  "category": "Software | Performance | Configuration | Security | Network Issue | Integration | Data Issue | Human Error | External / Third Party | Other — the root cause type/why it happened",
  "confidence": "low | medium | high",
  "reasoning": "short explanation of your choices",
  "canonical_statement": "detailed description for human reading. Include component, symptoms, scope.",
  "signature": "short problem signature for grouping: 5-8 words, start with the failing action (not actor), ban error message as the head phrase. No names, IDs, dates, or numbers.",
  "failure_mode": "FM-XXX — pick the best matching code from the failure-mode taxonomy below. Use FM-000 if none fits."
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

canonical_statement: Include component name first, describe symptoms and scope. English only. Facts only — no inferred causes.

## Failure-Mode Taxonomy
Pick the best-matching failure_mode code. If no code fits, use FM-000.

{_build_fm_taxonomy_block()}
"""


# Build user message with title and description as JSON
def _build_user_prompt(title: str, description: str) -> str:
    return json.dumps({"title": title, "description": description}, ensure_ascii=False)


# ── JSON parsing ──────────────────────────────────────────────────────


# Parse JSON and validate against ClassificationResult schema
def _parse_and_validate(raw: str) -> ClassificationResult:
    return ClassificationResult.model_validate(json.loads(strip_json_fences(raw)))


# ── Cached prompt (built once at import time) ─────────────────────────

_SYSTEM_PROMPT = _build_system_prompt()


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


# ── Public API ────────────────────────────────────────────────────────


# Public API: classify an incident, retry once on failure, fallback to low-confidence
def classify(title: str, description: str) -> ClassificationResult:
    """Classify an incident. Always returns — falls back to low-confidence on LLM failure."""
    _log.info("Classifying — title='%s'", title[:60])
    user_prompt = _build_user_prompt(title, description)

    try:
        result = _parse_and_validate(call_llm([
            {"role": "system", "content": _SYSTEM_PROMPT},
            {"role": "user", "content": user_prompt},
        ], max_tokens=600, temperature=0.0))
        result.signature = _normalize_canonical(result.signature)
        _log.info("Classification succeeded — system=%s, severity=%s, confidence=%s",
                  result.affected_system, result.severity, result.confidence)
        return result
    except Exception as e:
        last_error = str(e)
        _log.warning("First classification attempt failed: %s", last_error)

    try:
        result = _parse_and_validate(call_llm([
            {"role": "system", "content": _SYSTEM_PROMPT},
            {"role": "user", "content": _build_retry_prompt(user_prompt, last_error)},
        ], max_tokens=600, temperature=0.0))
        result.signature = _normalize_canonical(result.signature)
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
        incident_type=IncidentType.degradation,
        severity=Severity.minor,
        urgency=Urgency.low,
        category=Category.other,
        confidence="low",
        signature="Generic/Unknown",
        reasoning=f"Classification failed after 2 attempts. Last error: {last_error}",
        canonical_statement=f"Incident reported: {title[:120]}",
    )


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
    priority: str = "medium",
    notes: str | None = None,
    discussion_history: list[dict] | None = None,
    escalation_info: str | None = None,
    completion_code: str | None = None,
    source_ticket_id: str = "",
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

    result = classify(title, description)

    # ── Severity→priority mapping ──
    _priority_map = {"Critical": "critical", "Major": "high", "Minor": "medium", "Cosmetic": "low"}
    sev = result.severity.value if hasattr(result.severity, "value") else result.severity
    priority = _priority_map.get(sev, "medium")

    embed_text = result.canonical_statement or f"{title} {description}"

    matches = store.find_similar(embed_text, extracted_text=extracted_text, classification=result)

    incident_id = store.generate_id()
    h = content_hash(title, description)

    _log.info("Classify result — id=%s, system=%s, service=%s, severity=%s, confidence=%s, dupes=%d",
              incident_id, result.affected_system, result.service,
              result.severity, result.confidence, len(matches))
    _log.debug("Canonical: %s", result.canonical_statement[:120] if result.canonical_statement else "(none)")
    if matches:
        _log.info("Similar open incidents found — %d related incidents", len(matches))
        for m in matches:
            _log.debug("  Similar: %s — %.1f%% — %s", m.id, m.similarity * 100, m.title[:60])
    store.save_incident(
        incident_id, title, description, result, extracted_text,
        documents=documents or [],
        assign_group=assign_group,
        assignee=assignee,
        priority=priority,
        notes=notes,
        discussion_history=discussion_history or [],
        escalation_info=escalation_info,
        completion_code=completion_code,
        content_hash=h,
        source_ticket_ids=[source_ticket_id] if source_ticket_id else [incident_id],
    )

    _log.info("Incident %s classified — system=%s, severity=%s, confidence=%s, dupes=%d",
              incident_id, result.affected_system, result.severity, result.confidence, len(matches))

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
                priority=inc.get("priority", "medium"),
                notes=inc.get("notes"),
                discussion_history=inc.get("discussion_history"),
                escalation_info=inc.get("escalation_info"),
                completion_code=inc.get("completion_code"),
            )
            results.append(r)
        except Exception as e:
            _log.error("Batch classify failed for '%s': %s", inc.get("title", "")[:40], e)
            failed += 1
    _log.info("Batch classify — %d/%d succeeded", len(results), len(incidents))
    return ClassifyBatchResponse(results=results, total=len(incidents), failed=failed)
