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
        "title": "Checkout timeouts for 20% of users",
        "description": (
            "Users in the EU region see 504 errors when completing "
            "purchases. Payment provider reports no outage."
        ),
        "output": {
            "affected_system": "Payment Gateway",
            "service": "Checkout",
            "incident_type": "Degradation",
            "severity": "Major",
            "urgency": "High",
            "category": "Performance",
            "confidence": "high",
            "reasoning": "Partial degradation of checkout, not a full outage.",
            "canonical_statement": "Payment checkout: EU users receive 504 errors; payment provider operational.",
        },
    },
    {
        "title": "VPN tunnel to branch office flapping",
        "description": (
            "Site-to-site VPN between HQ and Dubai office drops and "
            "reconnects every 5 minutes. Affects 50 users."
        ),
        "output": {
            "affected_system": "Network",
            "service": "VPN",
            "incident_type": "Degradation",
            "severity": "Major",
            "urgency": "High",
            "category": "Network Issue",
            "confidence": "high",
            "reasoning": "VPN flapping degrades connectivity for a branch office.",
            "canonical_statement": "VPN tunnel: Connection between HQ and Dubai drops and reconnects every 5 minutes, affecting 50 users.",
        },
    },
    {
        "title": "Expired SSL certificate on customer portal",
        "description": (
            "Users see a security warning when visiting the portal. "
            "Certificate expired 2 days ago."
        ),
        "output": {
            "affected_system": "CRM",
            "service": "Customer Portal",
            "incident_type": "Unavailability",
            "severity": "Major",
            "urgency": "High",
            "category": "Configuration",
            "confidence": "medium",
            "reasoning": "Expired cert blocks HTTPS access, but portal backend is healthy.",
            "canonical_statement": "Customer portal: HTTPS blocked by SSL certificate expired 2 days ago, backend healthy.",
        },
    },
    {
        "title": "Production database disk is 98% full",
        "description": (
            "The PostgreSQL primary server /data partition is at 98% "
            "capacity. Auto-vacuum may fail soon."
        ),
        "output": {
            "affected_system": "Infrastructure",
            "service": "Storage",
            "incident_type": "Degradation",
            "severity": "Major",
            "urgency": "High",
            "category": "Hardware",
            "confidence": "high",
            "reasoning": "Database disk near capacity may cause autovacuum failures.",
            "canonical_statement": "Database storage: PostgreSQL primary server /data at 98% capacity, autovacuum may fail.",
        },
    },
    {
        "title": "Suspicious login attempts on admin panel",
        "description": (
            "5000+ failed login attempts from 12 different IPs. "
            "Possible brute-force attack targeting the admin interface."
        ),
        "output": {
            "affected_system": "Security",
            "service": "IAM",
            "incident_type": "Spike",
            "severity": "Critical",
            "urgency": "Immediate",
            "category": "Security",
            "confidence": "high",
            "reasoning": "High-volume brute-force attack on admin panel requires immediate response.",
            "canonical_statement": "Admin login: 5000+ failed attempts from 12 distinct IPs, possible brute-force attack.",
        },
    },
    {
        "title": "Payment gateway completely down — all transactions failing",
        "description": (
            "503 errors on every payment attempt since 14:32 UTC. "
            "Our payment provider's status page shows a full outage."
        ),
        "output": {
            "affected_system": "Payment Gateway",
            "service": "Checkout",
            "incident_type": "Outage",
            "severity": "Critical",
            "urgency": "Immediate",
            "category": "External / Third Party",
            "confidence": "high",
            "reasoning": (
                "Full outage with 100% failure rate caused by third-party provider failure. "
                "incident_type=Outage (what happened), category=External / Third Party (why it happened)."
            ),
            "canonical_statement": "Payment checkout: All transactions failing with 503 errors since 14:32 UTC, provider outage confirmed.",
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

    return f"""You classify IT incident tickets into fixed categories.

Return ONLY valid JSON with NO extra text. Each field value must be a SINGLE STRING (not a list).

{_build_examples_block()}

---

Now classify the user's incident using the same JSON format.

Pick exactly one from each list below.

CRITICAL — TWO DIFFERENT FIELDS, TWO DIFFERENT LISTS:
  • incident_type = WHAT HAPPENED (the symptom): Spike | Degradation | Unavailability | Outage
  • category      = WHY IT HAPPENED (the root cause type): Hardware | Software | Network Issue | Security | Performance | Configuration | Human Error | External / Third Party | Other
  NEVER put an incident_type value (Spike/Degradation/Unavailability/Outage) into the category field.

CRITICAL: "service" must be a SINGLE STRING. Pick ONE service name from the list
shown for your chosen affected_system. Do NOT return a list.

affected_system (pick ONE of these):
{systems}

service (pick ONE SERVICE STRING from the relevant list below):
{json.dumps(services_by_system, indent=2)}

incident_type — WHAT HAPPENED (the symptom type, pick ONE of these):
{types}

severity (pick ONE of these):
{severities}

urgency (pick ONE of these):
{urgencies}

category — WHY IT HAPPENED (the root cause type, pick ONE of these):
{categories}

confidence: "low", "medium", or "high"
reasoning: short explanation (optional)
canonical_statement: one dense sentence in English stating observable
  symptoms and the affected component. Include: what happened, which
  component/system is affected (e.g. "notification delivery", "login
  authentication", "payment checkout"), scope (which users/environments),
  and inconsistency if mentioned. Start with the component name to anchor
  similar tickets together. Never guess root cause. Never add details not
  present. Write in English regardless of ticket language. Optimized for
  embedding similarity.

Rules:
- Pick the single best label per field.
- "service" must be a SINGLE STRING, never a list.
- The category field must always be a root cause type (Hardware/Software/etc.), never a symptom type.
- If nothing fits well, pick the closest and set confidence "low".
- Respond with JSON only — no commentary before or after."""


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
