"""SMAX HTTP client — the only place this connector talks to SMAX's REST API.

Moved from the classifier app's old seams/smax package (Phase 4
restructure) and made standalone: the transport logic (auth header,
timeouts, retry on 429/5xx) is unchanged, but NotConfiguredError is now
defined locally in this package (config.py) instead of being imported from
the classifier's port module. Payload shapes live in smax_models.py; the
poll loop lives in poller.py.

NotConfiguredError is raised before any network I/O when the token is
missing, so a misconfigured connector fails loudly instead of polling a
dead endpoint.
"""

from __future__ import annotations

import logging
import random
import time
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from .config import NotConfiguredError

_log = logging.getLogger(__name__)

DEFAULT_TIMEOUT_S = 10.0
MAX_RETRIES = 3
# Backoff between retries: exponential (base * 2^n) with jitter, and a cap
# that also bounds a server-provided Retry-After.
BACKOFF_BASE_S = 0.5
RETRY_AFTER_CAP_S = 30.0

# 429 means "not processed" — retrying is safe for any method. 5xx may have
# been processed by the server, so only idempotent methods may be retried.
_RETRY_STATUS = (429, 500, 502, 503, 504)


def _retry_after_seconds(value: str | None) -> float | None:
    """Numeric Retry-After header in seconds, or None when absent/unusable."""
    if not value:
        return None
    try:
        return max(0.0, float(str(value).strip()))
    except ValueError:
        return None


def _retry_delay(attempt: int, exc: HTTPError | None = None) -> float:
    """Backoff before retry #`attempt`: exponential with jitter, capped at
    RETRY_AFTER_CAP_S. A numeric Retry-After header on `exc` wins (the
    upstream says when to come back) but is capped too."""
    headers = getattr(exc, "headers", None) if exc is not None else None
    server_delay = _retry_after_seconds(headers.get("Retry-After") if headers else None)
    if server_delay is not None:
        return min(server_delay, RETRY_AFTER_CAP_S)
    delay = BACKOFF_BASE_S * (2 ** (attempt - 1))
    delay += random.uniform(0.0, delay / 2)  # jitter: desync parallel clients
    return min(delay, RETRY_AFTER_CAP_S)


def _should_retry(status: int, idempotent: bool) -> bool:
    if status == 429:
        return True
    return idempotent and status in _RETRY_STATUS


class SmaxClient:
    """Minimal SMAX REST client. Configured by env, never by code."""

    def __init__(self, api_url: str, token: str, timeout: float = DEFAULT_TIMEOUT_S) -> None:
        self._api_url = api_url.rstrip("/")
        self._token = token
        self._timeout = timeout

    # ── transport ─────────────────────────────────────────────────────
    def _request(self, method: str, path: str, body: bytes | None = None) -> bytes:
        self._require_configured()
        req = Request(
            f"{self._api_url}{path}",
            data=body,
            method=method,
            headers={
                "Authorization": f"Bearer {self._token}",
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
        )
        idempotent = method.upper() in ("GET", "HEAD")
        last_err: Exception | None = None
        attempts_made = 0
        for attempt in range(1, MAX_RETRIES + 1):
            attempts_made = attempt
            try:
                with urlopen(req, timeout=self._timeout) as resp:
                    return resp.read()
            except HTTPError as exc:
                if _should_retry(exc.code, idempotent) and attempt < MAX_RETRIES:
                    delay = _retry_delay(attempt, exc)
                    _log.warning("SMAX %s on %s — retry %d/%d in %.1fs",
                                 exc.code, path, attempt, MAX_RETRIES, delay)
                    time.sleep(delay)
                    continue
                raise
            except URLError as exc:
                last_err = exc
                if idempotent and attempt < MAX_RETRIES:
                    delay = _retry_delay(attempt, None)
                    _log.warning("SMAX unreachable on %s — retry %d/%d in %.1fs",
                                 path, attempt, MAX_RETRIES, delay)
                    time.sleep(delay)
                    continue
                # Non-idempotent: the request may have reached SMAX — never
                # repeat it (a duplicate suggestion is worse than an error).
                break
        raise RuntimeError(
            f"SMAX unreachable after {attempts_made} of {MAX_RETRIES} attempts: {last_err}"
        ) from last_err

    # ── endpoint surface (one method per upstream call) ───────────────
    def get_ticket(self, ticket_id: str) -> dict:
        """Fetch one ticket. Returns the raw SMAX payload (see smax_models.py)."""
        raw = self._request("GET", f"/tickets/{ticket_id}")
        return _json(raw)

    def list_changed(self, since_iso: str) -> list[dict]:
        """Tickets updated since an ISO timestamp. Raw SMAX payloads."""
        raw = self._request("GET", f"/tickets?changed_since={since_iso}")
        return _json(raw).get("tickets", [])

    def get_attachments(self, ticket_id: str) -> list[dict]:
        raw = self._request("GET", f"/tickets/{ticket_id}/attachments")
        return _json(raw).get("attachments", [])

    def write_suggestion(self, ticket_id: str, suggestion: dict) -> None:
        """Write-back (safest mode): suggestions to a side channel, never
        into ticket fields directly."""
        self._request("POST", f"/tickets/{ticket_id}/suggestions", body=_dumps(suggestion))

    def _require_configured(self) -> None:
        if not self._token:
            raise NotConfiguredError(
                "SMAX source is not configured: set SMAX_API_TOKEN "
                f"(SMAX_API_URL={self._api_url!r} present but no credentials)"
            )


def _json(raw: bytes) -> dict:
    import json

    return json.loads(raw.decode("utf-8"))


def _dumps(payload: dict) -> bytes:
    import json

    return json.dumps(payload).encode("utf-8")
