"""SEAMS port: the ticket-source interface and normalized incident model.

Pipeline code consumes ONLY these shapes. External field names (whatever
the upstream system calls them) may appear ONLY inside source adapters.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Protocol


class NotConfiguredError(RuntimeError):
    """Raised when a ticket source is selected but not configured."""


@dataclass
class Incident:
    """Normalized incident — the only shape pipeline code consumes.

    - source_reference: the external ticket id (idempotency key).
    - id: internal incident id when already persisted, else "".
    """

    source_reference: str
    title: str
    description: str
    id: str = ""
    attachments: list[dict] = field(default_factory=list)
    created_at: datetime | None = None
    updated_at: datetime | None = None
    status: str = "active"


@dataclass
class PipelineResult:
    """Result OBJECT returned by the pipeline — the pipeline never writes.

    Write-back is a separate, skippable, dry-run-capable step
    (persist_result); writing results back to the ticket source itself is
    the source adapter's write_back().
    """

    source_reference: str
    title: str
    description: str
    is_new: bool
    incident_id: str | None
    classification: Any | None  # ClassificationResult when classified
    similar_tickets: list[dict]
    suggestions: list[str]
    confidence: str
    model_version: str
    prompt_version: str
    processed_at: datetime
    status: str
    error: str | None = None


class TicketSource(Protocol):
    """Port the pipeline depends on. Implementations translate external
    payloads to/from the normalized Incident model."""

    def fetch_ticket(self, ref: str) -> Incident:
        """Fetch one ticket by its external reference."""
        ...

    def fetch_attachments(self, ref: str) -> list[dict]:
        """Fetch attachments for a ticket reference."""
        ...

    def list_changed(self, since: datetime | None = None) -> list[Incident]:
        """List tickets changed since `since` (None = all)."""
        ...

    def write_back(self, result: PipelineResult) -> None:
        """Write a pipeline result back to the ticket source itself
        (e.g. post a reply on the ticket). Optional per source."""
        ...
