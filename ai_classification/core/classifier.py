"""LLM-based incident classifier — provider-agnostic via LiteLLM.

Plus classify-and-persist orchestration: calls the classifier, checks for
duplicates via the store, saves the result.
"""

import json
import logging

from litellm import completion

from ..config import settings
from ..domain.models import ClassificationResult, SimilarOpenIncident
from ..domain.taxonomy import (
    AffectedSystem,
    IncidentType,
    Severity,
    Urgency,
    Category,
    SERVICES_BY_SYSTEM,
)
from ..api.schemas import ClassifyResponse, ClassifyBatchResponse
from .store import store

_log = logging.getLogger(__name__)


# ── Few-shot examples ─────────────────────────────────────────────────

FEW_SHOT_EXAMPLES = [
    {
        "title": "Login page returning 500 errors for 30% of users",
        "description": "Users seeing 500 Internal Server Error when attempting to log in via the web portal. Started 20 minutes ago. Auth service health check passing.",
        "output": {
            "affected_system": "Infrastructure",
            "service": "Compute (EC2 / VMs)",
            "incident_type": "Degradation",
            "severity": "Major",
            "urgency": "High",
            "category": "Performance",
            "confidence": "high",
            "reasoning": "Partial login failure with auth service healthy suggests web server or load balancer issue.",
            "canonical_statement": "Login: 30% of users receive 500 errors on web portal; auth service is healthy.",
        },
    },
    {
        "title": "Payment checkout failing — transactions timing out",
        "description": "Checkout process times out at payment step for all users in APAC region. Payment provider status page shows no outage.",
        "output": {
            "affected_system": "Payment Gateway",
            "service": "Checkout",
            "incident_type": "Degradation",
            "severity": "Critical",
            "urgency": "Immediate",
            "category": "Performance",
            "confidence": "high",
            "reasoning": "APAC-only checkout timeouts with provider reporting no issues suggests regional routing or gateway instance problem.",
            "canonical_statement": "Payment checkout: APAC users experiencing timeouts at payment step; payment provider is operational.",
        },
    },
    {
        "title": "Email notifications not being delivered to users",
        "description": "Password reset and welcome emails not arriving since deployment v3.2.1. SMTP relay logs show messages queued but not sent.",
        "output": {
            "affected_system": "Email",
            "service": "SMTP Relay",
            "incident_type": "Unavailability",
            "severity": "Major",
            "urgency": "High",
            "category": "Software",
            "confidence": "medium",
            "reasoning": "Emails queued but not sent correlates with recent deployment, likely a software regression.",
            "canonical_statement": "Email delivery: Password reset and welcome emails queued but not sent since deployment v3.2.1.",
        },
    },
    {
        "title": "Database query performance degraded after schema change",
        "description": "Reports dashboard taking 30+ seconds to load. Slow query log shows full table scans on orders table after adding new index.",
        "output": {
            "affected_system": "Data Pipeline",
            "service": "Data Warehouse",
            "incident_type": "Degradation",
            "severity": "Major",
            "urgency": "High",
            "category": "Configuration",
            "confidence": "high",
            "reasoning": "New index causing full table scans — query planner regression after schema change.",
            "canonical_statement": "Reports dashboard: Queries on orders table use full table scans after schema change, load time exceeds 30 seconds.",
        },
    },
    {
        "title": "Admin panel brute-force attack detected",
        "description": "5000+ failed login attempts from 15 distinct IPs targeting admin accounts. Rate limiting not triggering.",
        "output": {
            "affected_system": "Security",
            "service": "IAM",
            "incident_type": "Spike",
            "severity": "Critical",
            "urgency": "Immediate",
            "category": "Security",
            "confidence": "high",
            "reasoning": "High-volume brute-force attack on admin accounts with rate limiting failure requires immediate response.",
            "canonical_statement": "Admin login: 5000+ failed attempts from 15 distinct IPs; rate limiting not engaging.",
        },
    },
    {
        "title": "API gateway returning 503 — all downstream calls failing",
        "description": "All API requests returning 503 errors since 14:32 UTC. Downstream services are healthy individually. Gateway logs show connection pool exhausted.",
        "output": {
            "affected_system": "Infrastructure",
            "service": "Load Balancer",
            "incident_type": "Outage",
            "severity": "Critical",
            "urgency": "Immediate",
            "category": "Configuration",
            "confidence": "high",
            "reasoning": "Full outage with connection pool exhaustion on gateway while downstream services are healthy points to gateway config issue.",
            "canonical_statement": "API gateway: All requests returning 503 since 14:32 UTC; connection pool exhausted, downstream services healthy.",
        },
    },
]


# ── Prompt builders ───────────────────────────────────────────────────


def _build_examples_block() -> str:
    blocks = []
    for i, ex in enumerate(FEW_SHOT_EXAMPLES, 1):
        blocks.append(
            f"Example {i}:\n"
            f'Title: "{ex["title"]}"\n'
            f'Description: "{ex["description"]}"\n'
            f"Output:\n{json.dumps(ex['output'], indent=2)}"
        )
    return "\n\n".join(blocks)


# Build the full system prompt with taxonomy and examples
def _build_system_prompt() -> str:
    systems = "\n".join(f"  - {s.value}" for s in AffectedSystem)
    types = "\n".join(f"  - {t.value}" for t in IncidentType)
    severities = "\n".join(f"  - {s.value}" for s in Severity)
    urgencies = "\n".join(f"  - {u.value}" for u in Urgency)
    categories = "\n".join(f"  - {c.value}" for c in Category)
    services_by_system = {s.value: svcs for s, svcs in SERVICES_BY_SYSTEM.items()}

    return f"""You classify IT support tickets into structured categories. Return ONLY valid JSON.

## JSON Schema
{{
  "affected_system": "string — one from the list below",
  "service": "string — one service from the chosen system's list",
  "incident_type": "Spike | Degradation | Unavailability | Outage — the symptom/what happened",
  "severity": "Critical | Major | Minor | Cosmetic",
  "urgency": "Immediate | High | Medium | Low",
  "category": "Hardware | Software | Network Issue | Security | Performance | Configuration | Human Error | External / Third Party | Other — the root cause type/why it happened",
  "confidence": "low | medium | high",
  "reasoning": "short explanation of your choices",
  "canonical_statement": "one dense English sentence: what happened, which component, scope. Never guess root cause."
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

canonical_statement: Include component name first, describe symptoms and scope. English only. Facts only — no inferred causes."""


# Build user message with title and description
def _build_user_prompt(title: str, description: str) -> str:
    return f"## Title\n{title}\n\n## Description\n{description}"


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


# ── LLM calls ────────────────────────────────────────────────────────


# Call the LLM via LiteLLM, handle errors and Qwen3 reasoning
def _call_llm(messages: list[dict]) -> str:
    kwargs: dict = dict(
        model=settings.llm_model,
        temperature=0.1,
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
        reasoning=f"Classification failed after 2 attempts. Last error: {last_error}",
        canonical_statement=f"Incident reported: {title[:120]}",
    )


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

    result = classify(title, description)

    embed_text = result.canonical_statement or f"{title} {description}"

    matches = store.find_similar(embed_text, extracted_text=extracted_text, classification=result)

    incident_id = store.generate_id()
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
