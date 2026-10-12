"""SMAX REST surface — the ONLY place this connector talks to SMAX.

Add a new SMAX API call by adding one method below that uses
`self.get_json(...)` / `self.post_json(...)`; auth, timeouts and retries come
from http_client.JsonHttpClient. Payload shaping lives in smax_models.py,
the poll loop in poller.py.
"""

from __future__ import annotations

from .http_client import JsonHttpClient


class SmaxClient(JsonHttpClient):
    """Minimal SMAX REST client. Configured by env, never by code."""

    label = "SMAX"
    subject = "SMAX source"
    credential_env = "SMAX_API_TOKEN"
    url_env = "SMAX_API_URL"

    # ── endpoint surface (one method per upstream call) ───────────────
    def get_ticket(self, ticket_id: str) -> dict:
        """Fetch one ticket. Returns the raw SMAX payload (see smax_models.py)."""
        return self.get_json(f"/tickets/{ticket_id}")

    def list_changed(self, since_iso: str) -> list[dict]:
        """Tickets updated since an ISO timestamp. Raw SMAX payloads."""
        return self.get_json(f"/tickets?changed_since={since_iso}").get("tickets", [])

    def get_attachments(self, ticket_id: str) -> list[dict]:
        """Attachments of one ticket (raw SMAX entries)."""
        return self.get_json(f"/tickets/{ticket_id}/attachments").get("attachments", [])

    def write_suggestion(self, ticket_id: str, suggestion: dict) -> None:
        """Write-back (safest mode): suggestions to a side channel, never
        into ticket fields directly."""
        self.post_json(f"/tickets/{ticket_id}/suggestions", suggestion)
