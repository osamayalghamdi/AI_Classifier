"""The ONE HTTP transport for this connector.

Both sides of the connector are JSON over HTTP, but they authenticate
differently, so the credentials are a hook rather than a hard-coded header:

    classifier API  →  Authorization: Bearer <token>          (default)
    SMAX            →  Cookie: SMAX_AUTH_TOKEN=<jwt>; TENANTID=<tenant>
                       (see smax_client.py — the JWT comes from a login call)

This module owns the machinery both share, so endpoint code stays trivial:

    class SmaxClient(JsonHttpClient):
        def get_ticket(self, ticket_id: str) -> dict:
            return self.get_json(f"/tickets/{ticket_id}")

Retry policy — defined once, applied to every call:

    429              → always retried (the server did not process it)
    5xx / timeouts   → retried only for idempotent methods (GET/HEAD); a POST
                       is never repeated, because the server may have applied it
    Retry-After      → honoured, capped at RETRY_AFTER_CAP_S

Redirects are never followed: the opener raises instead, so a login page or a
misconfigured host stays visible rather than silently becoming a 200.

A 401/403 (or an HTML login page that SMAX serves with a 200) calls the
`_on_unauthorized()` hook once, which is how a client with an exchangeable
credential refreshes and retries — `JsonHttpClient` itself does nothing there.

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
from urllib.request import HTTPRedirectHandler, Request, build_opener

from .config import NotConfiguredError

_log = logging.getLogger(__name__)

DEFAULT_TIMEOUT_S = 30.0
MAX_RETRIES = 3
# Backoff between retries: exponential (base * 2^n) with jitter, capped — the
# cap also bounds a server-provided Retry-After.
BACKOFF_BASE_S = 0.5
RETRY_AFTER_CAP_S = 30.0

_IDEMPOTENT_METHODS = ("GET", "HEAD")
# 429 means "not processed" (safe to retry anything); 5xx may have been
# processed, so it is only retried for idempotent methods.
_RETRY_STATUS = (429, 500, 502, 503, 504)
_UNAUTHORIZED_STATUS = (401, 403)


class _NoRedirect(HTTPRedirectHandler):
    """Surface redirects instead of following them."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D102
        raise HTTPError(req.full_url, code, f"redirected to {newurl}", headers, fp)


_opener = build_opener(_NoRedirect)


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
    upstream endpoint using `get_json` / `post_json` / `get_bytes`.
    """

    #: Log prefix, e.g. "SMAX" → "SMAX 429 on /tickets — retry 1/3 in 0.5s"
    label = "HTTP"
    #: How this side is named in the not-configured error.
    subject = "HTTP client"
    #: Env var(s) holding the credentials (named in the not-configured error).
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
        more than a bearer token (a cookie pair, an API key header, a tenant)."""
        return {"Authorization": f"Bearer {self._token}"} if self._token else {}

    def _extra_headers(self) -> dict[str, str]:
        """Static headers to add to every request (none by default)."""
        return {}

    def _on_unauthorized(self) -> bool:
        """Called once when a request comes back 401/403 (or as a login page).

        Return True to have the request retried with fresh credentials — a
        client with an exchangeable token refreshes here. False (the default)
        lets the 401 propagate.
        """
        return False

    def _is_login_page(self, raw: bytes, content_type: str) -> bool:
        """True when a 200 response is really an HTML login page.

        SMAX serves its login form with a 200, so a lazy client would treat it
        as data. Overridden by clients that can hit one.
        """
        return False

    def _require_configured(self) -> None:
        if not self._token:
            raise NotConfiguredError(
                f"{self.subject} is not configured: set {self.credential_env} "
                f"({self.url_env}={self._api_url!r} present but no credentials)"
            )

    # ── transport ─────────────────────────────────────────────────────
    def _build_request(self, method: str, path: str, body: bytes | None,
                       accept: str) -> Request:
        headers = {"Accept": accept, "Connection": "close"}
        headers.update(self._extra_headers())
        headers.update(self._auth_headers())
        if body is not None:
            headers["Content-Type"] = "application/json"
        return Request(f"{self._api_url}{path}", data=body, method=method, headers=headers)

    def _request(self, method: str, path: str, body: bytes | None = None,
                 accept: str = "application/json") -> bytes:
        """Raw request with the shared retry policy. Raises HTTPError for a
        non-retryable status and RuntimeError when the host stays unreachable."""
        self._require_configured()
        request = self._build_request(method, path, body, accept)

        idempotent = method.upper() in _IDEMPOTENT_METHODS
        last_error: URLError | None = None
        attempts_made = 0
        refreshed = False
        for attempt in range(1, MAX_RETRIES + 1):
            attempts_made = attempt
            try:
                with _opener.open(request, timeout=self._timeout) as resp:
                    raw = resp.read()
                    content_type = resp.headers.get("Content-Type", "")
                # A 200 that is really a login page (SMAX does this) counts as
                # an auth failure: refresh once, then retry.
                if (not refreshed and self._is_login_page(raw, content_type)
                        and self._on_unauthorized()):
                    refreshed = True
                    request = self._build_request(method, path, body, accept)
                    continue
                return raw
            except HTTPError as exc:
                if (exc.code in _UNAUTHORIZED_STATUS and not refreshed
                        and self._on_unauthorized()):
                    refreshed = True
                    request = self._build_request(method, path, body, accept)
                    _log.info("%s %s on %s — refreshed credentials, retrying once",
                              self.label, exc.code, path)
                    continue
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

    # ── sugar (use these in endpoint methods) ─────────────────────────
    def get_json(self, path: str) -> dict:
        return loads(self._request("GET", path))

    def post_json(self, path: str, payload: dict) -> dict:
        raw = self._request("POST", path, body=dumps(payload))
        return loads(raw) if raw.strip() else {}

    def get_bytes(self, path: str, accept: str = "*/*") -> bytes:
        """Raw bytes (file downloads). Same retry policy, no JSON decoding."""
        return self._request("GET", path, accept=accept)

    def post_text(self, path: str, payload: dict) -> str:
        """POST JSON, return the body as plain text (SMAX returns a bare JWT)."""
        return self._request("POST", path, body=dumps(payload),
                             accept="text/plain").decode("utf-8").strip()
