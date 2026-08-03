"""Shared LLM utilities — provider-agnostic call wrapper and response parsing.

Used by classifier.py (classification) and grouping.py (cluster validation).
Both had near-identical copies of _call_llm and their own fence-stripping
parsers; this module consolidates them.

Pipeline position: 05_llm — shared LLM call wrapper (classify, grouping)."""

import json
import logging

from litellm import completion

from ..config import settings

_log = logging.getLogger(__name__)


def call_llm(
    messages: list[dict],
    *,
    max_tokens: int,
    temperature: float = 0.0,
) -> str:
    """Call the LLM via LiteLLM with shared provider wiring.

    Args:
        messages: Chat messages (system + user).
        max_tokens: Max tokens for the response.
        temperature: Sampling temperature (0.0 = deterministic).

    Returns:
        Raw response text.

    Raises:
        ValueError: If the API call fails or returns empty content.
    """
    kwargs: dict = dict(
        model=settings.llm_model,
        temperature=temperature,
        seed=42,
        max_tokens=max_tokens,
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


def strip_json_fences(raw: str) -> str:
    """Strip optional markdown code fences that some local models add.

    Handles ```json … ```, ``` … ```, and bare JSON.
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
