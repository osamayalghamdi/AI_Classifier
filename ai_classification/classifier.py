"""LLM-based incident classifier.

Builds a prompt from the incident payload + the predefined taxonomies,
then asks the LLM to return a structured JSON classification.

Switching providers / models is a one-line config change — no code to
touch.
"""

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


# ── Few‑shot examples ────────────────────────────────────────────────
# Small models benefit heavily from concrete examples. These cover the
# most common incident patterns so the model has a strong reference.


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
            "Site‑to‑site VPN between HQ and Dubai office drops and "
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
]


# ── Prompt template ──────────────────────────────────────────────────


def _build_examples_block() -> str:
    """Render few‑shot examples as a prompt block."""
    blocks = []
    for i, ex in enumerate(FEW_SHOT_EXAMPLES, 1):
        output_json = json.dumps(ex["output"], indent=2)
        blocks.append(
            f"Example {i}:\n"
            f'Title: "{ex["title"]}"\n'
            f'Description: "{ex["description"]}"\n'
            f"Output:\n{output_json}"
        )
    return "\n\n".join(blocks)


def _build_system_prompt() -> str:
    systems = "\n".join(f"  - {s.value}" for s in AffectedSystem)
    types = "\n".join(f"  - {t.value}" for t in IncidentType)
    severities = "\n".join(f"  - {s.value}" for s in Severity)
    urgencies = "\n".join(f"  - {u.value}" for u in Urgency)
    categories = "\n".join(f"  - {c.value}" for c in Category)
    services_by_system = {
        s.value: svcs for s, svcs in SERVICES_BY_SYSTEM.items()
    }

    examples_block = _build_examples_block()

    return f"""You classify IT incident tickets into fixed categories.

Return ONLY valid JSON with NO extra text. Each field value must be a SINGLE STRING (not a list).

{examples_block}

---

Now classify the user's incident using the same JSON format.

Pick exactly one from each list below.

CRITICAL: "service" must be a SINGLE STRING. Pick ONE service name from the list
shown for your chosen affected_system. Do NOT return a list.

affected_system (pick ONE of these):
{systems}

service (pick ONE SERVICE STRING from the relevant list below):
{json.dumps(services_by_system, indent=2)}

incident_type (pick ONE of these):
{types}

severity (pick ONE of these):
{severities}

urgency (pick ONE of these):
{urgencies}

category (pick ONE of these):
{categories}

confidence: "low", "medium", or "high"
reasoning: short explanation (optional)

Rules:
- Pick the single best label per field.
- "service" must be a SINGLE STRING, never a list.
- If nothing fits well, pick the closest and set confidence "low".
- Respond with JSON only — no commentary before or after."""


def _build_user_prompt(title: str, description: str) -> str:
    return f"## Title\n{title}\n\n## Description\n{description}"


# ── Minimal fence-stripper (not a JSON parser) ────────────────────────


def _extract_json_str(raw: str) -> str:
    """Strip optional markdown code fences wrapping the JSON payload.

    Does NOT attempt to parse, fix, or coerce the JSON itself — that is
    left to ``json.loads`` and Pydantic. This is only here because some
    local models habitually wrap JSON in ```json … ``` fences.
    """
    text = raw.strip()
    if text.startswith("```"):
        if text.startswith("```json"):
            text = text[7:]
        elif text.startswith("```"):
            text = text[3:]
        if text.endswith("```"):
            text = text[:-3]
    return text.strip()


# ── Classification ───────────────────────────────────────────────────


def _parse_and_validate(raw: str) -> ClassificationResult:
    """Parse fence‑stripped JSON and validate with Pydantic.

    Returns the validated result or raises an exception (either
    ``json.JSONDecodeError`` or a Pydantic ``ValidationError``).
    """
    raw = _extract_json_str(raw)
    data = json.loads(raw)
    return ClassificationResult.model_validate(data)


def _build_retry_prompt(user_prompt: str, last_error: str) -> str:
    """Build a retry hint that includes the actual error message.

    This is far more effective than a generic "fix your JSON" because
    the LLM sees exactly what went wrong.
    """
    return (
        f"{user_prompt}\n\n"
        f"---\n"
        f"Your previous response was invalid. Error:\n"
        f"{last_error}\n\n"
        f"Fix ONLY the JSON. Use exactly the field names and allowed "
        f"values shown in the system prompt. Return valid JSON with no "
        f"extra text."
    )


def _call_llm(messages: list[dict]) -> str:
    """Single LLM call returning the raw response text.

    Lifts API/network errors into ``ValueError`` so callers don't
    need to know about litellm internals.
    """
    kwargs: dict = dict(
        model=settings.llm_model,
        temperature=0.0,
        max_tokens=500,
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


# ── Public API ────────────────────────────────────────────────────────


def classify(title: str, description: str) -> ClassificationResult:
    """Send the incident to the LLM and return a structured result.

    Handles all error paths internally:
      - API / network failures
      - Non-JSON / malformed responses
      - Schema validation failures (wrong fields, bad types,
        service/system mismatch)

    If the first attempt fails, a single retry is made with a
    corrective hint that includes the actual error message.  If the
    retry also fails a fallback ``ClassificationResult`` is returned
    with ``reasoning`` describing the failure — the caller never
    sees an exception.
    """
    system_prompt = _build_system_prompt()
    user_prompt = _build_user_prompt(title, description)

    # ── Attempt 1 ──────────────────────────────────────────────
    try:
        raw = _call_llm([
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ])
        return _parse_and_validate(raw)
    except Exception as e:
        last_error = str(e)

    # ── Retry with corrective hint ─────────────────────────────
    try:
        raw = _call_llm([
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": _build_retry_prompt(user_prompt, last_error)},
        ])
        return _parse_and_validate(raw)
    except Exception as e:
        last_error = str(e)

    # ── Fallback — API or LLM persistently broken ──────────────
    return ClassificationResult(
        affected_system=AffectedSystem.other,
        service="General / Unspecified",
        incident_type=IncidentType.degradation,
        severity=Severity.minor,
        urgency=Urgency.low,
        category=Category.other,
        confidence="low",
        reasoning=(
            f"Classification failed after 2 attempts. Last error: {last_error}"
        ),
    )


# ── Report summarization ──────────────────────────────────────────────


def summarize_cluster(incidents: list[dict]) -> str:
    """Summarize a cluster of related incidents into 2–3 sentences.

    Parameters
    ----------
    incidents:
        List of dicts with keys ``title``, ``description``, ``classification``
        (a ``ClassificationResult``), and ``created_at``.
    """
    lines = []
    for i, inc in enumerate(incidents, 1):
        c = inc["classification"]
        lines.append(
            f'{i}. "{inc["title"]}" — {c.affected_system} / {c.service} / '
            f'{c.incident_type} / {c.severity} — "{inc.get("description", "")}"'
        )

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
                "content": (
                    "Related incidents (same problem):\n\n"
                    f"{chr(10).join(lines)}\n\n"
                    "Summary:"
                ),
            },
        ])
        return raw.strip().strip('"')
    except Exception:
        c = incidents[0]["classification"]
        sevs = [i["classification"].severity for i in incidents]
        return (
            f"{len(incidents)} related incidents affecting "
            f"{c.affected_system} / {c.service}. "
            f"Worst severity: {max(sevs)}."
        )
