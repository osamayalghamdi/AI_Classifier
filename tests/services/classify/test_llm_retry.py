"""call_llm resilience tests — retry/backoff/timeout semantics.

Verifies the STEP-1 contract:
- 429 (rate limit) → retried with backoff, eventually succeeds; attempts
  bounded by LLM_MAX_RETRIES (total = 1 + max_retries).
- 401/403 (auth), 404 (unknown model), 400 (bad request), 402 (credits)
  → raised IMMEDIATELY, zero retries (config/account error, not transient).
- 500/502/503/504 → retried (transient upstream).
- connection/read timeout → retried.
- Retry-After header on 429 is honoured.
- The provider's error text is preserved in the raised ValueError.
"""

from __future__ import annotations

import types

import httpx
import pytest

import ai_classification.services.classify.llm as mod_llm
from ai_classification.shared.config import settings


class _FakeChoice:
    class _FakeMessage:
        def __init__(self, content):
            self.content = content

    def __init__(self, content):
        self.message = self._FakeMessage(content)


class _FakeResponse:
    def __init__(self, content="ok"):
        self.choices = [_FakeChoice(content)]


def _settings_with(**overrides) -> types.SimpleNamespace:
    """A copy of Settings with resilience knobs pinned (frozen dataclass)."""
    d = {k: v for k, v in settings.__dict__.items() if not k.startswith("_")}
    d.update({
        "llm_max_retries": 3,
        "llm_retry_base_s": 0.1,  # tiny — real sleep is replaced below anyway
        "llm_timeout_s": 60,
        **overrides,
    })
    return types.SimpleNamespace(**d)


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    """Never sleep for real — record delays instead."""
    sleeps = []
    monkeypatch.setattr(mod_llm.time, "sleep", sleeps.append)
    return sleeps


def _litellm_error(cls, message, status_code):
    """Build a litellm exception; status_code comes from the httpx response."""
    req = httpx.Request("POST", "http://llm")
    resp = httpx.Response(status_code, request=req, headers={"Retry-After": "1"})
    return cls(message=message, llm_provider="openrouter", model="m", response=resp)


@pytest.fixture
def fake_completion(monkeypatch):
    """Scripted completion: queue of (exception | response) consumed per call."""
    queue = []
    calls = []

    def fake_completion(**kwargs):
        calls.append(kwargs)
        if not queue:
            return _FakeResponse()
        item = queue.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    monkeypatch.setattr(mod_llm, "completion", fake_completion)
    monkeypatch.setattr(mod_llm, "settings", _settings_with())
    return queue, calls


# ── 429: retried, then succeeds; Retry-After honoured ─────────────────

def test_429_retried_then_succeeds(fake_completion, _no_sleep):
    from litellm import RateLimitError
    queue, calls = fake_completion
    queue.append(_litellm_error(RateLimitError, "rate limited", 429))
    out = mod_llm.call_llm([{"role": "user", "content": "hi"}], max_tokens=10)
    assert out == "ok"
    assert len(calls) == 2          # initial + 1 retry
    assert _no_sleep[0] == 1.0      # Retry-After: 1 honoured exactly


def test_429_exhausts_retries_then_raises(fake_completion, _no_sleep):
    from litellm import RateLimitError
    queue, calls = fake_completion
    for _ in range(10):
        queue.append(_litellm_error(RateLimitError, "still limited", 429))
    with pytest.raises(ValueError) as ei:
        mod_llm.call_llm([{"role": "user", "content": "hi"}], max_tokens=10)
    assert "still limited" in str(ei.value)
    assert len(calls) == 1 + 3      # initial + LLM_MAX_RETRIES
    assert len(_no_sleep) == 3      # one backoff per retry


# ── 401/403/404/400: fail fast, zero retries ─────────────────────────

@pytest.mark.parametrize("status", [401, 403, 404, 400])
def test_non_retryable_fails_fast(fake_completion, status):
    from litellm import (
        AuthenticationError, BadRequestError, NotFoundError, PermissionDeniedError,
    )
    cls = {
        401: AuthenticationError, 403: PermissionDeniedError,
        404: NotFoundError, 400: BadRequestError,
    }[status]
    queue, calls = fake_completion
    queue.append(_litellm_error(cls, f"fail {status}", status))
    with pytest.raises(ValueError) as ei:
        mod_llm.call_llm([{"role": "user", "content": "hi"}], max_tokens=10)
    assert len(calls) == 1          # zero retries
    assert f"fail {status}" in str(ei.value)


def test_402_credits_fails_fast(fake_completion):
    """The exact incident error: OpenRouter 402 wrapped in a generic
    APIError with the status embedded in the message text (no typed
    exception, no status_code attribute — only the payload string)."""
    queue, calls = fake_completion
    queue.append(RuntimeError(
        'litellm.APIError: OpenrouterException - {"error":{"message":'
        '"This request requires more credits, or fewer max_tokens",'
        '"code":402,"metadata":{"limit_source":"openrouter_credits"}}}'
    ))
    with pytest.raises(ValueError) as ei:
        mod_llm.call_llm([{"role": "user", "content": "hi"}], max_tokens=10)
    assert len(calls) == 1          # zero retries — retrying credits is pointless
    assert "requires more credits" in str(ei.value)
    assert "402" in str(ei.value)


# ── 5xx: retried ──────────────────────────────────────────────────────

@pytest.mark.parametrize("status", [500, 502, 503, 504])
def test_5xx_retried_then_succeeds(fake_completion, status):
    from litellm import APIError
    queue, calls = fake_completion
    req = httpx.Request("POST", "http://llm")
    # APIError takes status_code as the FIRST positional arg (no response).
    queue.append(APIError(status, f"upstream {status}", "openrouter", "m", request=req))
    out = mod_llm.call_llm([{"role": "user", "content": "hi"}], max_tokens=10)
    assert out == "ok"
    assert len(calls) == 2


# ── connection/read timeouts: retried ────────────────────────────────

def test_connection_timeout_retried(fake_completion):
    from litellm import APIConnectionError
    queue, calls = fake_completion
    req = httpx.Request("POST", "http://llm")
    queue.append(APIConnectionError(
        message="read timeout", llm_provider="openrouter", model="m", request=req))
    out = mod_llm.call_llm([{"role": "user", "content": "hi"}], max_tokens=10)
    assert out == "ok"
    assert len(calls) == 2


def test_timeout_retried(fake_completion):
    from litellm import Timeout
    queue, calls = fake_completion
    queue.append(Timeout(
        message="read timeout", llm_provider="openrouter", model="m"))
    out = mod_llm.call_llm([{"role": "user", "content": "hi"}], max_tokens=10)
    assert out == "ok"
    assert len(calls) == 2


# ── attempt bound ─────────────────────────────────────────────────────

def test_total_attempts_never_exceed_max_retries(fake_completion, _no_sleep, monkeypatch):
    from litellm import RateLimitError
    queue, calls = fake_completion
    for _ in range(10):
        queue.append(_litellm_error(RateLimitError, "persistent", 429))
    monkeypatch.setattr(mod_llm, "settings", _settings_with(llm_max_retries=2))
    with pytest.raises(ValueError):
        mod_llm.call_llm([{"role": "user", "content": "hi"}], max_tokens=10)
    assert len(calls) == 3          # 1 + llm_max_retries (2)


# ── timeout kwarg plumbed through ─────────────────────────────────────

def test_timeout_kwarg_passed_to_completion(fake_completion):
    queue, calls = fake_completion
    mod_llm.call_llm([{"role": "user", "content": "hi"}], max_tokens=10)
    assert calls[0]["timeout"] == 60
