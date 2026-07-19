"""LLM-based incident classifier — provider-agnostic via LiteLLM.

Plus classify-and-persist orchestration: calls the classifier, checks for
duplicates via the store, saves the result.
"""

import hashlib
import json
import logging
import re

from datetime import datetime, timezone

from litellm import completion

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


# Strip markdown code fences from LLM response
def _extract_json_str(raw: str) -> str:
    """Strip optional markdown code fences that some local models add."""
    text = raw.strip()
    if text.startswith("```"):
        if text.startswith("```json"):
            text = text[7:]
        elif text.startswith("```"):
            text = text[3:]
        if text.endswith("```"):
            text = text[:-3]
    return text.strip()


# Parse JSON and validate against ClassificationResult schema
def _parse_and_validate(raw: str) -> ClassificationResult:
    return ClassificationResult.model_validate(json.loads(_extract_json_str(raw)))


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


# Call the LLM via LiteLLM, handle errors and Qwen3 reasoning
def _call_llm(messages: list[dict]) -> str:
    kwargs: dict = dict(
        model=settings.llm_model,
        temperature=0.0,
        seed=42,
        max_tokens=600,
        messages=messages,
    )
    if settings.llm_api_base:
        kwargs["api_base"] = settings.llm_api_base

    if settings.llm_api_key:
        kwargs["api_key"] = settings.llm_api_key

    # Qwen3 thinks by default — disable for structured JSON output
    if "qwen3" in settings.llm_model.lower():
        kwargs["extra_body"] = {"reasoning": {"enabled": False}}

    try:
        resp = completion(**kwargs)
    except Exception as e:
        raise ValueError(f"LLM API call failed: {e}") from e
    content = resp.choices[0].message.content
    if not content or not content.strip():
        raise ValueError("LLM returned empty response")
    return content


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
        result = _parse_and_validate(_call_llm([
            {"role": "system", "content": _SYSTEM_PROMPT},
            {"role": "user", "content": user_prompt},
        ]))
        result.signature = _normalize_canonical(result.signature)
        _log.info("Classification succeeded — system=%s, severity=%s, confidence=%s",
                  result.affected_system, result.severity, result.confidence)
        return result
    except Exception as e:
        last_error = str(e)
        _log.warning("First classification attempt failed: %s", last_error)

    try:
        result = _parse_and_validate(_call_llm([
            {"role": "system", "content": _SYSTEM_PROMPT},
            {"role": "user", "content": _build_retry_prompt(user_prompt, last_error)},
        ]))
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


# ── Content-hash dedupe gate ──────────────────────────────────────────────
# Runs BEFORE classification. Exact duplicates increment occurrence_count
# instead of creating a new incident. Digit blanking catches alerts that
# differ only by percentage/threshold value (88.4% → #, 81.9% → #).


def content_hash(title: str, description: str) -> str:
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
) -> ClassifyResponse:
    _log.info("Classifying incident — title='%s', group='%s', priority=%s", title[:60], assign_group, priority)

    # ── Content-hash dedupe (DB-backed, digit-blanked) ──
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
                incident_id=store.generate_id(), incident_title=title, classification=result,
                similar_open_incidents=matches,
            )

    result = classify(title, description)

    # ── Severity→priority mapping ──
    _priority_map = {"Critical": "critical", "Major": "high", "Minor": "medium", "Cosmetic": "low"}
    priority = _priority_map.get(result.severity.value if hasattr(result.severity, 'value') else result.severity, priority)

    embed_text = result.canonical_statement or f"{title} {description}"

    matches = store.find_similar(embed_text, extracted_text=extracted_text, classification=result)

    incident_id = store.generate_id()
    _log.info("Classify result — id=%s, system=%s, service=%s, severity=%s, confidence=%s, dupes=%d",
              incident_id, result.affected_system, result.service,
              result.severity, result.confidence, len(matches))
    _log.debug("Canonical: %s", result.canonical_statement[:120] if result.canonical_statement else "(none)")
    if matches:
        _log.info("Duplicates found — %d similar open incidents", len(matches))
        for m in matches:
            _log.debug("  Dupe: %s — %.1f%% — %s", m.id, m.similarity * 100, m.title[:60])
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
        source_ticket_ids=[incident_id],
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
