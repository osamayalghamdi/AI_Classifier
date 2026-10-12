"""SEAMS — the ticket-source port for the ingestion pipeline.

The pipeline (pipeline.py) talks ONLY to the TicketSource interface; all
external payload translation happens inside the source adapters.

IMPORTANT (Phase 4): the real SMAX adapter moved OUT of this package into
the standalone `integrations/smax` connector, which talks to the
classifier through its public HTTP API. In-process, `get_ticket_source()`
now returns ONLY the local fake source; selecting TICKETING_SOURCE=real
logs a deprecation note pointing at the connector.
"""

import logging

from .local_source import LocalFakeTicketSource
from .pipeline import (
    process_batch,
    process_incident,
    persist_result,
    manual_process,
)
from .port import (
    Incident,
    NotConfiguredError,
    PipelineResult,
    TicketSource,
)

_log = logging.getLogger(__name__)


def get_ticket_source() -> TicketSource:
    """Config-selected ticket source — LOCAL ONLY since Phase 4.

    - "local": fake source backed by the incident store (tests + offline).
    - anything else (legacy "real"): the SMAX connector now lives in
      `integrations/smax` (python -m integrations.smax.main); we log a
      deprecation note and fall back to the local fake source so the
      process keeps working.
    """
    from ai_classification.shared.config import settings
    from ai_classification.shared.store import store

    if settings.ticketing_source != "local":
        _log.warning(
            "TICKETING_SOURCE=%r is deprecated in-process — SMAX connectivity "
            "moved to the standalone connector: `python -m integrations.smax.main` "
            "(see integrations/smax/). Falling back to the local fake source.",
            settings.ticketing_source,
        )
    return LocalFakeTicketSource(store)


__all__ = [
    "Incident",
    "NotConfiguredError",
    "PipelineResult",
    "TicketSource",
    "LocalFakeTicketSource",
    "get_ticket_source",
    "process_incident",
    "process_batch",
    "persist_result",
    "manual_process",
]
