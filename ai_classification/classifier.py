"""LLM-based incident classifier.

Builds a prompt from the incident payload + the predefined taxonomies,
then asks the LLM to return a structured JSON classification.

Switching providers / models is a one-line config change — no code to
touch.
"""

import json
import re

import litellm
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


# ── Prompt template ──────────────────────────────────────────────────


def _build_system_prompt() -> str:
    systems = "\n".join(f"  - {s.value}" for s in AffectedSystem)
    types = "\n".join(f"  - {t.value}" for t in IncidentType)
    severities = "\n".join(f"  - {s.value}" for s in Severity)
    urgencies = "\n".join(f"  - {u.value}" for u in Urgency)
    categories = "\n".join(f"  - {c.value}" for c in Category)
    services_json = json.dumps(
        {s.value: svcs for s, svcs in SERVICES_BY_SYSTEM.items()},
        indent=2,
    )

    return f"""You classify IT incident tickets into fixed categories.

Return ONLY valid JSON with NO extra text. Example output:

{{
  "affected_system": "Payment Gateway",
  "service": "Checkout",
  "incident_type": "Degradation",
  "severity": "Major",
  "urgency": "High",
  "category": "Software",
  "confidence": "high",
  "reasoning": "Brief explanation here."
}}

Pick exactly one from each list below:

affected_system → one of:
{systems}

service → depends on affected_system:
{services_json}

incident_type → one of:
{types}

severity → one of:
{severities}

urgency → one of:
{urgencies}

category → one of:
{categories}

confidence → "low", "medium", or "high"
reasoning → short explanation (optional)

Rules:
- Pick the single best label per field.
- service must match the chosen affected_system's list.
- If nothing fits well, pick the closest and set confidence "low".
- Respond with JSON only — no commentary before or after."""


def _build_user_prompt(title: str, description: str) -> str:
    return f"## Title\n{title}\n\n## Description\n{description}"


# ── JSON extraction with fallbacks ────────────────────────────────────


def _extract_json(raw: str) -> dict:
    """Try to parse JSON from the LLM response, with fallbacks for
    markdown-wrapped, prefix/suffix-laden, or partially broken output
    that small local models sometimes produce.
    """
    text = raw.strip()

    # 1. Direct parse
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    # 2. Strip markdown code fences (```json ... ``` or ``` ... ```)
    fences = re.search(
        r"```(?:json)?\s*\n?(.*?)\n?```", text, re.DOTALL
    )
    if fences:
        try:
            return json.loads(fences.group(1).strip())
        except json.JSONDecodeError:
            pass

    # 3. Find the outermost { … } block
    brace_start = text.find("{")
    if brace_start != -1:
        depth = 0
        for i in range(brace_start, len(text)):
            if text[i] == "{":
                depth += 1
            elif text[i] == "}":
                depth -= 1
                if depth == 0:
                    candidate = text[brace_start : i + 1]
                    try:
                        return json.loads(candidate)
                    except json.JSONDecodeError:
                        # Last resort: try to fix single quotes / trailing commas
                        # that some small models produce
                        fixed = (
                            candidate
                            .replace("'", '"')
                            .replace(",}", "}")
                            .replace(",]", "]")
                        )
                        return json.loads(fixed)

    raise RuntimeError(
        f"Could not extract valid JSON from LLM response:\n{raw[:500]}"
    )


# ── Classification ───────────────────────────────────────────────────


def classify(title: str, description: str) -> ClassificationResult:
    """Send the incident to the LLM and return a structured result."""

    kwargs: dict = dict(
        model=settings.llm_model,
        messages=[
            {"role": "system", "content": _build_system_prompt()},
            {"role": "user", "content": _build_user_prompt(title, description)},
        ],
        temperature=0.1,
        max_tokens=500,
    )

    if settings.llm_api_base:
        kwargs["api_base"] = settings.llm_api_base
    if settings.llm_api_key:
        kwargs["api_key"] = settings.llm_api_key

    resp = completion(**kwargs)

    raw = resp.choices[0].message.content
    data = _extract_json(raw)

    # Fallback: if the LLM used a value outside our enums, map it to
    # "Other" rather than crashing with a 502.
    try:
        return ClassificationResult(**data)
    except Exception:
        _coerce_enums(data)
        return ClassificationResult(**data)


def _coerce_enums(data: dict) -> None:
    """Replace enum values that don't match with 'Other'."""
    valid_systems = {s.value for s in AffectedSystem}
    valid_types = {t.value for t in IncidentType}
    valid_severities = {s.value for s in Severity}
    valid_urgencies = {u.value for u in Urgency}
    valid_categories = {c.value for c in Category}

    if data.get("affected_system") not in valid_systems:
        data["affected_system"] = "Other"
    if data.get("incident_type") not in valid_types:
        data["incident_type"] = "Degradation"
    if data.get("severity") not in valid_severities:
        data["severity"] = "Minor"
    if data.get("urgency") not in valid_urgencies:
        data["urgency"] = "Medium"
    if data.get("category") not in valid_categories:
        data["category"] = "Other"
    if isinstance(data.get("service"), list):
        data["service"] = data["service"][0] if data["service"] else "General / Unspecified"
    if data.get("service") not in SERVICES_BY_SYSTEM.get(data.get("affected_system"), []):
        data["service"] = "General / Unspecified"
