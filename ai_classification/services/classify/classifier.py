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

import logging

from ai_classification.shared.config import settings
from ai_classification.domain.models import ClassificationResult
from ai_classification.api.schemas import ClassifyResponse, ClassifyBatchResponse
from ai_classification.services.classify.llm import call_llm, strip_json_fences
from ai_classification.shared.store import store

_log = logging.getLogger(__name__)


# Identity of _SYSTEM_PROMPT — recorded on persisted classifications by the
# seams pipeline (provenance). Bump when the prompt content changes.
PROMPT_VERSION = "2026-08-v3.2"  # v3.2: 'Other' is a last-resort system — never for Nusuk-screen problems


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

    # CASCADE_CLASSIFICATION gate (default TRUE).
    if settings.cascade_classification:
        return _classify_v3(title, description, incident_ref=ref, affected_system=affected_system)

    return _classify_single_shot(title, description, incident_ref=ref)


# ── Facade wiring ─────────────────────────────────────────────────────
# The internal implementation lives in responsibility-scoped modules
# (prompts / parsing / cascade / verification / persistence). The names are
# re-bound into THIS module's namespace so that tests can monkeypatch
# `classifier_mod.store`, `.settings`, `.classify`, `.PROMPT_VERSION`, etc.
# exactly as before the split — internal functions resolve those names
# through this facade at call time (never frozen at import time).

from ai_classification.services.classify.prompts import (  # noqa: E402,F401
    FEW_SHOT_EXAMPLES,
    TRIAGE_EXAMPLES,
    _CASCADE_JSON_SCHEMA,
    _SYSTEM_PROMPT,
    _TRIAGE_SYSTEM_PROMPT,
    _build_examples_block,
    _build_retry_prompt,
    _build_stage_system_prompt,
    _build_system_prompt,
    _build_triage_system_prompt,
    _build_user_prompt,
)
from ai_classification.services.classify.parsing import (  # noqa: E402,F401
    _normalize_canonical,
    _parse_and_validate,
    _parse_stage_offering,
    _parse_stage_system,
    _parse_triage,
    _stage_system_from_partial,
)
from ai_classification.services.classify.cascade import (  # noqa: E402,F401
    _cascade_fallback,
    _classify_routed,
    _classify_single_shot,
    _classify_v3,
    _resolve_pinned_system,
    _resolve_system_deterministic,
    _run_cascade,
    _stage_offering_llm,
    _stage_service_llm,
    _stage_system_llm,
    _triage,
)
from ai_classification.services.classify.verification import (  # noqa: E402,F401
    _apply_service_correction,
    _apply_verification_corrections,
    _bare_service_key,
    _build_verification_prompt,
    _enforce_confidence_honesty,
    _self_consistency,
    _verify_classification,
)
from ai_classification.services.classify.persistence import (  # noqa: E402,F401
    _log_classification,
    _record_taxonomy_gap,
    classify_and_store,
    classify_batch,
    content_hash,
)
