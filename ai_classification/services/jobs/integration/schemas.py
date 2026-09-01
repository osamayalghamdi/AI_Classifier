"""Integration API schemas — STRICT payloads (unknown fields rejected).

`extra="forbid"` on every model: a payload field that is not part of the
contract is an error (INVALID_PAYLOAD), never silently dropped. This is
the integration contract the other team builds against.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

# ── Stable machine-readable error codes ──────────────────────────────
class Err:
    UNAUTHORIZED = "UNAUTHORIZED"
    INVALID_PAYLOAD = "INVALID_PAYLOAD"
    NOT_FOUND = "NOT_FOUND"
    DUPLICATE = "DUPLICATE"
    LLM_UNAVAILABLE = "LLM_UNAVAILABLE"
    LLM_TIMEOUT = "LLM_TIMEOUT"
    EMBEDDING_FAILED = "EMBEDDING_FAILED"
    DB_FAILURE = "DB_FAILURE"
    RETRYABLE = "RETRYABLE"
    FLAGGED = "FLAGGED"
    INTERNAL = "INTERNAL"


class IntegrationIncident(BaseModel):
    """One normalized incident accepted by the ingest/backfill endpoints.

    - source_reference: the caller's ticket id — the idempotency key and
      the end-to-end tracing reference.
    - Unknown fields are REJECTED (422 INVALID_PAYLOAD).
    """

    model_config = ConfigDict(extra="forbid")

    source_reference: str = Field(min_length=1, max_length=128)
    title: str = Field(min_length=1, max_length=300)
    description: str = Field(default="", max_length=8000)
    # DYNAMIC by design (SMAX webhook requirement): the ticketing system's
    # status is accepted verbatim — active, verified, resolved, closed, or
    # any value SMAX adds later. No fixed enum: a new upstream status must
    # never 422. The system stores it raw in incidents.source_status and
    # derives the local active/resolved view from it.
    status: str = Field(default="active", min_length=1, max_length=64)
    attachments: list[dict] = Field(default_factory=list, max_length=20)
    created_at: datetime | None = None
    updated_at: datetime | None = None


class StatusUpdate(BaseModel):
    """Status-only update for an already-ingested incident (same reference →
    same incident row; no re-classification). Status is dynamic, like E1."""

    model_config = ConfigDict(extra="forbid")

    status: str = Field(min_length=1, max_length=64)
    updated_at: datetime | None = None


class IntegrationBatch(BaseModel):
    """Batch/backfill payload — up to 200 incidents per request."""

    model_config = ConfigDict(extra="forbid")

    incidents: list[IntegrationIncident] = Field(min_length=1, max_length=200)


# ── Structured error bodies ───────────────────────────────────────────

def error_body(code: str, message: str, reference: str | None = None) -> dict:
    """Build the stable structured error envelope: {"error": {...}}."""
    body: dict = {"error": {"code": code, "message": message}}
    if reference:
        body["error"]["reference"] = reference
    return body
