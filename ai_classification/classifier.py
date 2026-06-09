"""LLM-based incident classifier — provider-agnostic via LiteLLM."""

import json

from litellm import completion

from .config import settings
from .models import ClassificationResult
from .schemas import (
    AffectedSystem,
    IncidentType,
    Severity,
    Urgency,
    Category,
    SERVICES_BY_SYSTEM,
)


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

Rules:
- Pick the single best label per field.
- "service" must be a SINGLE STRING, never a list.
- The category field must always be a root cause type (Hardware/Software/etc.), never a symptom type.
- If nothing fits well, pick the closest and set confidence "low".
- Respond with JSON only — no commentary before or after."""


def _build_user_prompt(title: str, description: str) -> str:
    return f"## Title\n{title}\n\n## Description\n{description}"


# ── JSON parsing ──────────────────────────────────────────────────────


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


def _parse_and_validate(raw: str) -> ClassificationResult:
    return ClassificationResult.model_validate(json.loads(_extract_json_str(raw)))


# ── Cached prompt (built once at import time) ─────────────────────────

_SYSTEM_PROMPT = _build_system_prompt()


# ── LLM calls ────────────────────────────────────────────────────────


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
    try:
        resp = completion(**kwargs)
    except Exception as e:
        raise ValueError(f"LLM API call failed: {e}") from e
    content = resp.choices[0].message.content
    if not content or not content.strip():
        raise ValueError("LLM returned empty response")
    return content


def _build_retry_prompt(user_prompt: str, last_error: str) -> str:
    return (
        f"{user_prompt}\n\n---\n"
        f"Your previous response was invalid. Error:\n{last_error}\n\n"
        f"Fix ONLY the JSON. Use exactly the field names and allowed "
        f"values shown in the system prompt. Return valid JSON with no extra text."
    )


# ── Public API ────────────────────────────────────────────────────────


def classify(title: str, description: str) -> ClassificationResult:
    """Classify an incident. Always returns — falls back to low-confidence on LLM failure."""
    user_prompt = _build_user_prompt(title, description)

    try:
        return _parse_and_validate(_call_llm([
            {"role": "system", "content": _SYSTEM_PROMPT},
            {"role": "user", "content": user_prompt},
        ]))
    except Exception as e:
        last_error = str(e)

    try:
        return _parse_and_validate(_call_llm([
            {"role": "system", "content": _SYSTEM_PROMPT},
            {"role": "user", "content": _build_retry_prompt(user_prompt, last_error)},
        ]))
    except Exception as e:
        last_error = str(e)

    return ClassificationResult(
        affected_system=AffectedSystem.other,
        service="General / Unspecified",
        incident_type=IncidentType.degradation,
        severity=Severity.minor,
        urgency=Urgency.low,
        category=Category.other,
        confidence="low",
        reasoning=f"Classification failed after 2 attempts. Last error: {last_error}",
    )


def summarize_cluster(incidents: list[dict]) -> str:
    """Summarize a cluster of related incidents into 2–3 sentences."""
    lines = [
        f'{i}. "{inc["title"]}" — {c.affected_system} / {c.service} / '
        f'{c.incident_type} / {c.severity} — "{inc.get("description", "")}"'
        for i, inc in enumerate(incidents, 1)
        for c in [inc["classification"]]
    ]
    try:
        raw = _call_llm([
            {
                "role": "system",
                "content": (
                    "You are an incident analyst. Write 2–3 sentences "
                    "covering the underlying issue, which system is affected, "
                    "and the overall impact. No formatting, no JSON, no labels."
                ),
            },
            {
                "role": "user",
                "content": f"Related incidents (same problem):\n\n{chr(10).join(lines)}\n\nSummary:",
            },
        ])
        return raw.strip().strip('"')
    except Exception:
        c = incidents[0]["classification"]
        worst = max(i["classification"].severity for i in incidents)
        return (
            f"{len(incidents)} related incidents affecting {c.affected_system} / {c.service}. "
            f"Worst severity: {worst}."
        )
