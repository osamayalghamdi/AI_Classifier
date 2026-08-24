"""Shared LLM utilities — provider-agnostic call wrapper and response parsing.

Used by classifier.py (classification) and grouping.py (cluster validation).
Both had near-identical copies of _call_llm and their own fence-stripping
parsers; this module consolidates them.

Resilience (v3.2): call_llm retries TRANSIENT provider failures only —
HTTP 429 / 500 / 502 / 503 / 504 and connection/read timeouts — with
exponential backoff + jitter, honouring Retry-After. Non-retryable errors
(401/403 auth, 402 credits, 404 unknown model, 400 bad request) fail FAST so
a config error surfaces immediately instead of being masked by retries.
The provider's error text is always preserved in the raised ValueError —
it is the only forensic trail (lands in classification_log via the cascade).

Pipeline position: 05_llm — shared LLM call wrapper (classify, grouping)."""

import logging
import random
import re
import time

from litellm import (
    APIConnectionError,
    AuthenticationError,
    BadRequestError,
    NotFoundError,
    PermissionDeniedError,
    RateLimitError,
    Timeout,
    completion,
)

from ai_classification.shared.config import ModelEntry, settings

_log = logging.getLogger(__name__)

# HTTP statuses worth retrying (transient upstream conditions).
_RETRYABLE_STATUSES = frozenset({429, 500, 502, 503, 504})
# Statuses where retrying is pointless and hides a config/account error.
_NON_RETRYABLE_STATUSES = frozenset({400, 401, 402, 403, 404, 422})
_MAX_BACKOFF_S = 30.0
_MAX_RETRY_AFTER_S = 60.0


def _extract_status(exc: Exception) -> int | None:
    """Best-effort HTTP status from a litellm exception.

    litellm typed errors (RateLimitError etc.) expose `status_code`; provider
    payloads wrapped in the generic APIError carry it in the message text
    (e.g. OpenRouter's `{"error":{"message":...,"code":402,...}}`).
    """
    code = getattr(exc, "status_code", None)
    if isinstance(code, int):
        return code
    m = re.search(r'"code"\s*:\s*(\d{3})', str(exc))
    if m:
        return int(m.group(1))
    return None


def _is_retryable(exc: Exception, status: int | None) -> bool:
    """True only for transient failures; auth/config errors fail fast."""
    if isinstance(exc, (AuthenticationError, PermissionDeniedError,
                        NotFoundError, BadRequestError)):
        return False
    if isinstance(exc, (RateLimitError, Timeout, APIConnectionError)):
        return True
    if status is not None:
        if status in _NON_RETRYABLE_STATUSES:
            return False
        if status in _RETRYABLE_STATUSES:
            return True
    # Unknown shape (generic APIError without a parseable status) — assume
    # transient network/upstream and retry.
    return True


def _retry_after(exc: Exception) -> float | None:
    """Honour a Retry-After header when the provider sends one."""
    resp = getattr(exc, "response", None)
    if resp is None:
        return None
    headers = getattr(resp, "headers", {}) or {}
    ra = headers.get("Retry-After") or headers.get("retry-after")
    if ra is None:
        return None
    try:
        return min(float(ra), _MAX_RETRY_AFTER_S)
    except (TypeError, ValueError):
        return None


def _retry_delay(exc: Exception, status: int | None, base: float, attempt: int) -> float:
    """Exponential backoff with jitter; Retry-After wins on 429."""
    ra = _retry_after(exc) if status == 429 else None
    if ra is not None:
        return ra
    exp = base * (2 ** (attempt - 1))
    jittered = exp * random.uniform(0.5, 1.5)
    return min(jittered, _MAX_BACKOFF_S)


def call_llm(
    messages: list[dict],
    *,
    max_tokens: int,
    temperature: float = 0.0,
    model: str | None = None,
) -> str:
    """Call the LLM via LiteLLM with shared provider wiring.

    Model resolution (model registry):
      - ``model``: explicit override (ad-hoc callers that already resolved
        a model id).
      - otherwise the ACTIVE classifier model — the first ENABLED registry
        entry with role='classifier'. If every classifier model is disabled,
        raises immediately: the enable/disable control must be explicit,
        never silently falls back to a disabled model.

    Args:
        messages: Chat messages (system + user).
        max_tokens: Max tokens for the response.
        temperature: Sampling temperature (0.0 = deterministic).
        model: Optional explicit litellm model id (overrides resolution).

    Returns:
        Raw response text.

    Raises:
        ValueError: If the API call fails (after retries on transient
            errors, immediately on auth/config errors) or returns empty,
            or when no classifier model is enabled.
    """
    if model:
        entry = ModelEntry(name="explicit", role="classifier", provider="openrouter",
                           enabled=True, model_id=model,
                           api_base=settings.llm_api_base or "",
                           api_key=settings.llm_api_key or "")
    else:
        entry = settings.active_classifier_model
        if entry is None:
            raise ValueError(
                "No classifier model is ENABLED. Enable one via "
                "MODEL_<NAME>_ENABLED=1 (admin console → Models) and restart."
            )

    kwargs: dict = dict(
        model=entry.model_id,
        temperature=temperature,
        seed=42,
        max_tokens=max_tokens,
        messages=messages,
    )
    if entry.api_base:
        kwargs["api_base"] = entry.api_base
    if entry.api_key:
        kwargs["api_key"] = entry.api_key

    if getattr(settings, "llm_timeout_s", 0):
        kwargs["timeout"] = settings.llm_timeout_s

    # Qwen3 thinks by default — disable for structured JSON output
    if "qwen3" in entry.model_id.lower():
        kwargs["extra_body"] = {"reasoning": {"enabled": False}}

    max_retries = getattr(settings, "llm_max_retries", 0)
    base = getattr(settings, "llm_retry_base_s", 2.0)

    attempt = 0
    while True:
        try:
            resp = completion(**kwargs)
            break
        except Exception as exc:  # noqa: BLE001 — classify all provider errors
            status = _extract_status(exc)
            if not _is_retryable(exc, status) or attempt >= max_retries:
                raise ValueError(f"LLM API call failed: {exc}") from exc
            attempt += 1
            delay = _retry_delay(exc, status, base, attempt)
            _log.warning(
                "LLM call attempt %d/%d failed (status=%s %s) — retrying in %.1fs",
                attempt, max_retries, status, type(exc).__name__, delay,
            )
            time.sleep(delay)

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
