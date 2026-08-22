"""Frozen prompt text and prompt builders for the incident classifier.

Everything in this module is FROZEN — copied byte-identical from the
pre-split classifier module. Rewording any string here silently changes
production prompts, so the prompt-drift guard (test_prompt_identity.py)
pins the SHA-256 of the frozen payload. PROMPT_VERSION lives in the
classifier facade; bump it there whenever prompt content changes.
"""

import json

from ai_classification.domain.taxonomy import (
    AffectedSystem,
    IncidentType,
    Severity,
    Urgency,
    Category,
    flatten_services,
)


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


def _build_stage_system_prompt(stage_rules: str, allowed_values: str) -> str:
    """Build a stage system prompt: full JSON contract + the stage's short option list."""
    return (
        "You classify IT support tickets into structured categories. Return ONLY valid JSON.\n\n"
        f"{_CASCADE_JSON_SCHEMA}\n\n"
        f"## Stage Rules\n{stage_rules}\n\n"
        f"## Allowed Values\n{allowed_values}"
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


# ── Cached prompt (built once at import time) ─────────────────────────


_SYSTEM_PROMPT = _build_system_prompt()


# Build retry prompt with the last error for a second attempt


def _build_retry_prompt(user_prompt: str, last_error: str) -> str:
    return (
        f"{user_prompt}\n\n---\n"
        f"Your previous response was invalid. Error:\n{last_error}\n\n"
        f"Fix ONLY the JSON. Use exactly the field names and allowed "
        f"values shown in the system prompt. Return valid JSON with no extra text."
    )
