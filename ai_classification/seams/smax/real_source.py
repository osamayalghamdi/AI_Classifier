"""SMAX ticket source — the TicketSource implementation over the SMAX client.

Everything SMAX-specific is contained in this package (client.py,
models.py, real_source.py); the rest of the codebase sees only the
TicketSource interface from seams.port.

Selection = config (TICKETING_SOURCE=real, the default). When
TICKETING_API_TOKEN is set, this becomes the live SMAX integration;
until then every port method raises NotConfiguredError so the pipeline
fails loudly instead of silently polling a dead endpoint.
"""

from __future__ import annotations

import logging

from ..port import Incident, PipelineResult
from .client import SmaxClient
from .models import from_smax, to_smax_suggestion

_log = logging.getLogger(__name__)


class RealTicketingSource:
    """SMAX-backed TicketSource. Configured by env, never by code."""

    def __init__(self, api_url: str, token: str) -> None:
        self._client = SmaxClient(api_url=api_url, token=token)

    # ── Port ───────────────────────────────────────────────────────────
    def fetch_ticket(self, ref: str) -> Incident:
        return from_smax(self._client.get_ticket(ref))

    def fetch_attachments(self, ref: str) -> list[dict]:
        return self._client.get_attachments(ref)

    def list_changed(self, since=None) -> list[Incident]:
        since_iso = since.isoformat() if since is not None else ""
        return [from_smax(p) for p in self._client.list_changed(since_iso)]

    def write_back(self, result: PipelineResult) -> None:
        # Safest mode: suggestions go to a side channel, never into ticket
        # fields. Mirrors the TICKETING_DRY_RUN gate one level down.
        self._client.write_suggestion(
            result.source_reference, to_smax_suggestion(result)
        )
