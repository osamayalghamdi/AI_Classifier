"""The ONE HTTP transport for this connector.

Both sides of the connector are JSON over HTTP with a bearer token — SMAX
(upstream) and the classifier's public API (downstream). This module owns
that machinery, so endpoint code stays trivial:

    class SmaxClient(JsonHttpClient):
        label = "SMAX"

        def get_ticket(self, ticket_id: str) -> dict:
            return self.get_json(f"/tickets/{ticket_id}")

Retry policy — defined once, applied to every call:

    429              → always retried (the server did not process it)
    5xx / timeouts   → retried only for idempotent methods (GET/HEAD); a POST
                       is never repeated, because the server may have applied it
    Retry-After      → honoured, capped at RETRY_AFTER_CAP_S

Credentials: `_require_configured()` raises NotConfiguredError **before** any
network I/O, so a misconfigured connector fails immediately instead of
hammering a dead or unauthenticated endpoint.
"""

from __future__ import annotations

import json
import logging
import random
import time
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from .config import NotConfiguredError

_log = logging.getLogger(__name__)

DEFAULT_TIMEOUT_S = 10.0
MAX_RETRIES = 3
# Backoff between retries: exponential (base * 2^n) with jitter, capped — the
# cap also bounds a server-provided Retry-After.
BACKOFF_BASE_S = 0.5
RETRY_AFTER_CAP_S = 30.0

_IDEMPOTENT_METHODS = ("GET", "HEAD")
# 429 means "not processed" (safe to retry anything); 5xx may have been
# processed, so it is only retried for idempotent methods.
_RETRY_STATUS = (429, 500, 502, 503, 504)


def loads(raw: bytes) -> dict:
    return json.loads(raw.decode("utf-8"))


def dumps(payload: dict) -> bytes:
    return json.dumps(payload).encode("utf-8")


def retry_after_seconds(value: str | None) -> float | None:
    """Numeric Retry-After header in seconds, or None when absent/unusable."""
    if not value:
        return None
    try:
        return max(0.0, float(str(value).strip()))
    except ValueError:
        return None


def retry_delay(attempt: int, exc: HTTPError | None = None) -> float:
    """Seconds to wait before retry #`attempt`: exponential with jitter,
    capped at RETRY_AFTER_CAP_S. A numeric Retry-After header on `exc` wins
    (the upstream tells us when to come back) but is capped too."""
    headers = getattr(exc, "headers", None) if exc is not None else None
    server_delay = retry_after_seconds(headers.get("Retry-After") if headers else None)
    if server_delay is not None:
        return min(server_delay, RETRY_AFTER_CAP_S)
    delay = BACKOFF_BASE_S * (2 ** (attempt - 1))
    delay += random.uniform(0.0, delay / 2)  # jitter: desync parallel clients
    return min(delay, RETRY_AFTER_CAP_S)


def should_retry(status: int, idempotent: bool) -> bool:
    if status == 429:
        return True
    return idempotent and status in _RETRY_STATUS


class JsonHttpClient:
    """Base class for every HTTP client in this package.

    Subclass it, set the four class attributes below, and add one method per
    upstream endpoint using `get_json` / `post_json`.
    """

    #: Log prefix, e.g. "SMAX" → "SMAX 429 on /tickets — retry 1/3 in 0.5s"
    label = "HTTP"
    #: How this side is named in the not-configured error.
    subject = "HTTP client"
    #: Env var holding the bearer token (named in the not-configured error).
    credential_env = "API_TOKEN"
    #: Env var holding the base URL (named in the not-configured error).
    url_env = "API_URL"

    def __init__(self, api_url: str, token: str, timeout: float = DEFAULT_TIMEOUT_S) -> None:
        self._api_url = (api_url or "").rstrip("/")
        self._token = token
        self._timeout = timeout

    # ── overridable hooks ─────────────────────────────────────────────
    def _auth_headers(self) -> dict[str, str]:
        """Headers carrying the credentials. Override when a provider needs
        more than a bearer token (an API key header, a tenant id, …)."""
        return {"Authorization": f"Bearer {self._token}"} if self._token else {}

    def _require_configured(self) -> None:
        if not self._token:
            raise NotConfiguredError(
                f"{self.subject} is not configured: set {self.credential_env} "
                f"({self.url_env}={self._api_url!r} present but no credentials)"
            )

    # ── transport ─────────────────────────────────────────────────────
    def _request(self, method: str, path: str, body: bytes | None = None) -> bytes:
        """Raw request with the shared retry policy. Raises HTTPError for a
        non-retryable status and RuntimeError when the host stays unreachable."""
        self._require_configured()
        headers = {"Accept": "application/json"}
        headers.update(self._auth_headers())
        if body is not None:
            headers["Content-Type"] = "application/json"
        request = Request(f"{self._api_url}{path}", data=body, method=method, headers=headers)

        idempotent = method.upper() in _IDEMPOTENT_METHODS
        last_error: URLError | None = None
        attempts_made = 0
        for attempt in range(1, MAX_RETRIES + 1):
            attempts_made = attempt
            try:
                with urlopen(request, timeout=self._timeout) as resp:
                    return resp.read()
            except HTTPError as exc:
                if should_retry(exc.code, idempotent) and attempt < MAX_RETRIES:
                    delay = retry_delay(attempt, exc)
                    _log.warning("%s %s on %s — retry %d/%d in %.1fs",
                                 self.label, exc.code, path, attempt, MAX_RETRIES, delay)
                    time.sleep(delay)
                    continue
                raise
            except URLError as exc:
                last_error = exc
                if idempotent and attempt < MAX_RETRIES:
                    delay = retry_delay(attempt, None)
                    _log.warning("%s unreachable on %s — retry %d/%d in %.1fs",
                                 self.label, path, attempt, MAX_RETRIES, delay)
                    time.sleep(delay)
                    continue
                # Non-idempotent: the request may have reached the server —
                # never repeat it (a duplicate write is worse than an error).
                break
        raise RuntimeError(
            f"{self.label} unreachable after {attempts_made} of {MAX_RETRIES} attempts: {last_error}"
        ) from last_error

    # ── JSON sugar (use these in endpoint methods) ────────────────────
    def get_json(self, path: str) -> dict:
        return loads(self._request("GET", path))

    def post_json(self, path: str, payload: dict) -> dict:
        return loads(self._request("POST", path, body=dumps(payload)))
