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
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from .config import NotConfiguredError

_log = logging.getLogger(__name__)

DEFAULT_TIMEOUT_S = 10.0
MAX_RETRIES = 3


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
        last_err: Exception | None = None
        for attempt in range(1, MAX_RETRIES + 1):
            try:
                with urlopen(req, timeout=self._timeout) as resp:
                    return resp.read()
            except HTTPError as exc:
                if exc.code in (429, 500, 502, 503, 504) and attempt < MAX_RETRIES:
                    _log.warning("SMAX %s on %s — retry %d/%d", exc.code, path, attempt, MAX_RETRIES)
                    continue
                raise
            except URLError as exc:
                last_err = exc
                if attempt < MAX_RETRIES:
                    _log.warning("SMAX unreachable on %s — retry %d/%d", path, attempt, MAX_RETRIES)
                    continue
        raise RuntimeError(f"SMAX unreachable after {MAX_RETRIES} attempts: {last_err}") from last_err

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
