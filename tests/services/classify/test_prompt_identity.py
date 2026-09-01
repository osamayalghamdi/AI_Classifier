"""Prompt-drift regression guard (C-2 requirement).

Pins the SHA-256 of the concatenated FROZEN prompt strings so any future
refactor that silently rewords a prompt fails the suite. Imported from
ai_classification.services.classify.prompts (the module that owns the frozen
text after the classifier split).

Payload scheme (stable across runs):
  "\n".join([
      json.dumps(FEW_SHOT_EXAMPLES, ensure_ascii=False, sort_keys=True),
      json.dumps(TRIAGE_EXAMPLES, ensure_ascii=False, sort_keys=True),
      _CASCADE_JSON_SCHEMA,
      _SYSTEM_PROMPT,
      _TRIAGE_SYSTEM_PROMPT,
  ])

sort_keys + ensure_ascii=False keep the JSON serialization independent of
dict insertion order. When a prompt is intentionally changed, recompute the
hash the same way and update FROZEN_PROMPT_SHA (and bump PROMPT_VERSION in
the classifier facade).
"""

import hashlib
import json

from ai_classification.services.classify.prompts import (
    FEW_SHOT_EXAMPLES,
    TRIAGE_EXAMPLES,
    _CASCADE_JSON_SCHEMA,
    _SYSTEM_PROMPT,
    _TRIAGE_SYSTEM_PROMPT,
)

# SHA-256 of the payload above, computed from the current prompts module
# (2026-08-22, post C-2 split — byte-identical to the pre-split classifier).
# 2026-08-31: v3.2 — 'Other' is a last-resort affected_system (never for
# Nusuk-screen problems); recomputed after the prompt change.
FROZEN_PROMPT_SHA = "508e8ea0cd851bec69a78ee15b228ebbb8d9b210400e621a4686a8c838c503f2"


def _frozen_payload() -> str:
    return "\n".join([
        json.dumps(FEW_SHOT_EXAMPLES, ensure_ascii=False, sort_keys=True),
        json.dumps(TRIAGE_EXAMPLES, ensure_ascii=False, sort_keys=True),
        _CASCADE_JSON_SCHEMA,
        _SYSTEM_PROMPT,
        _TRIAGE_SYSTEM_PROMPT,
    ])


def test_frozen_prompts_unchanged():
    digest = hashlib.sha256(_frozen_payload().encode("utf-8")).hexdigest()
    assert digest == FROZEN_PROMPT_SHA, (
        "Frozen prompt text drifted from the pinned SHA. If the change is "
        "intentional, recompute FROZEN_PROMPT_SHA and bump PROMPT_VERSION."
    )
