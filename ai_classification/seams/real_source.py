"""Real ticketing source — satisfies the port, but raises a clear
not-configured error until credentials exist.

Selection = config (TICKETING_SOURCE=real, the default). When
TICKETING_API_TOKEN is set, this becomes the base for the real HTTP
integration; until then every port method raises NotConfiguredError so the
pipeline fails loudly instead of silently polling a dead endpoint.
"""

from __future__ import annotations

import logging

from .port import Incident, NotConfiguredError, PipelineResult

_log = logging.getLogger(__name__)


class RealTicketingSource:
    """Real upstream adapter stub. Configured by env, never by code."""

    def __init__(self, api_url: str, token: str) -> None:
        self._api_url = api_url
        self._token = token

    def _require_configured(self) -> None:
        if not self._token:
            raise NotConfiguredError(
                "real ticketing source is not configured: set "
                "TICKETING_API_TOKEN (TICKETING_API_URL="
                f"{self._api_url!r} is present but no credentials exist)"
            )

    # ── Port ───────────────────────────────────────────────────────────
    def fetch_ticket(self, ref: str) -> Incident:
        self._require_configured()
        raise NotImplementedError("real ticketing fetch_ticket: implement against upstream API")

    def fetch_attachments(self, ref: str) -> list[dict]:
        self._require_configured()
        raise NotImplementedError("real ticketing fetch_attachments: implement against upstream API")

    def list_changed(self, since=None) -> list[Incident]:
        self._require_configured()
        raise NotImplementedError("real ticketing list_changed: implement against upstream API")

    def write_back(self, result: PipelineResult) -> None:
        self._require_configured()
        raise NotImplementedError("real ticketing write_back: implement against upstream API")
