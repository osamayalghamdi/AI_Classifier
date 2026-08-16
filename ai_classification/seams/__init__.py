"""SEAMS — the ticket-source port for the ingestion pipeline.

The pipeline (pipeline.py) talks ONLY to the TicketSource interface; all
external payload translation happens inside the source adapters. Selection
is configuration (TICKETING_SOURCE=real|local), never code.
"""

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
from .smax.real_source import RealTicketingSource


def get_ticket_source() -> TicketSource:
    """Config-selected ticket source.

    - "real" (default): satisfies the interface but raises
      NotConfiguredError until TICKETING_API_TOKEN exists.
    - "local": fake source backed by the incident store (tests + offline).
    """
    from ..config import settings

    if settings.ticketing_source == "local":
        from ..core.store import store

        return LocalFakeTicketSource(store)
    return RealTicketingSource(
        api_url=settings.ticketing_api_url,
        token=settings.ticketing_api_token,
    )


__all__ = [
    "Incident",
    "NotConfiguredError",
    "PipelineResult",
    "TicketSource",
    "LocalFakeTicketSource",
    "RealTicketingSource",
    "get_ticket_source",
    "process_incident",
    "process_batch",
    "persist_result",
    "manual_process",
]
