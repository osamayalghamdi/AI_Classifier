"""SMAX external payload models — the boundary translation layer.

Moved from the classifier app's old seams/smax package (Phase 4
restructure) and made standalone: the `Incident` shape it translated into
is now defined locally in this package (a copy of the classifier's
normalized incident shape) instead of being imported from the classifier's
port module — the connector never imports the classifier's internals.

The BUG-1 fix is preserved: `to_smax_suggestion` reads classification
fields with `getattr(cls, ...)` (attribute access — works for Pydantic
models and plain objects), NOT `.get()` (dict-only).

No SMAX field name may appear anywhere outside this package (enforced by
the containment grep).
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

_log = logging.getLogger(__name__)


# ── Local normalized shapes (standalone copies — never imported) ──────

@dataclass
class Incident:
    """Normalized incident — the shape this connector submits to the
    classifier's public API. Field list mirrors the classifier's port
    Incident (source_reference is the idempotency key)."""

    source_reference: str
    title: str
    description: str
    attachments: list[dict] = field(default_factory=list)
    created_at: datetime | None = None
    updated_at: datetime | None = None
    status: str = "active"
    # Affected system supplied by the ticketing system (when available) —
    # the classifier validates and pins it, skipping LLM system resolution.
    affected_system: str = ""


@dataclass
class PipelineResult:
    """Result-shaped object consumed by `to_smax_suggestion`.

    Standalone copy of the classifier's PipelineResult surface — only the
    attributes the write-back needs. Anything with these attributes works
    (the serializer uses attribute access, never the concrete type).
    """

    source_reference: str
    classification: Any | None = None  # object with affected_system/service/severity
    similar_tickets: list[dict] = field(default_factory=list)
    suggestions: list[str] = field(default_factory=list)
    confidence: str = ""
    model_version: str = ""
    prompt_version: str = ""
    processed_at: datetime | None = None


# ── SMAX entity → our Incident field ──────────────────────────────────
# The ONLY place in the codebase where SMAX's payload keys are named. SMAX
# sends entities as {"properties": {...}} (SMAX_INTEGRATION.md §4); the first
# name in each tuple is the documented field, the rest are tolerant fallbacks
# so a tenant that renames one degrades instead of crashing.
_ID_KEYS = ("Id", "id", "ticket_id")
_TITLE_KEYS = ("DisplayLabel", "displayLabel", "title", "Name")
_DESC_KEYS = ("Description", "description", "details")
_STATUS_KEYS = ("Status", "status")
_CREATED_KEYS = ("EmsCreationTime", "emsCreationTime", "created_at")
_UPDATED_KEYS = ("LastUpdateTime", "lastUpdateTime", "updated_at")


def properties_of(payload: dict) -> dict:
    """The fields of an SMAX entity live under `properties`; a bare properties
    dict is accepted too, so callers can pass either shape."""
    props = payload.get("properties") if isinstance(payload, dict) else None
    return props if isinstance(props, dict) else (payload or {})


def _first(payload: dict, keys: tuple[str, ...], default: str = "") -> str:
    for k in keys:
        v = payload.get(k)
        if v is not None and str(v).strip():
            return str(v).strip()
    return default


def _timestamp(payload: dict, keys: tuple[str, ...]) -> datetime | None:
    """SMAX sends epoch milliseconds; ISO strings are accepted as a fallback
    (the webhook side, and any tenant that formats differently)."""
    raw = _first(payload, keys)
    if not raw:
        return None
    if re.fullmatch(r"\d{10,17}", raw):
        seconds = int(raw) / 1000.0
        try:
            return datetime.fromtimestamp(seconds, tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            _log.warning("SMAX payload has out-of-range timestamp %r — ignored", raw)
            return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00")).astimezone(timezone.utc)
    except ValueError:
        _log.warning("SMAX payload has unparseable timestamp %r — ignored", raw)
        return None


def from_smax(payload: dict) -> Incident:
    """Translate a raw SMAX entity into the normalized Incident shape.

    Unknown fields are ignored (SMAX returns many we don't consume) and a
    malformed value degrades to an empty string, so upstream schema drift
    never takes the connector down.

    `affected_system` is deliberately left empty: SMAX's `Entity_c` is a
    queue/entity id, not one of our taxonomy systems, and sending a raw id
    would be pinned as if the ticketing system had named the system. The
    classifier resolves the system from the text instead.
    """
    props = properties_of(payload)
    return Incident(
        source_reference=_first(props, _ID_KEYS),
        title=_first(props, _TITLE_KEYS),
        description=_first(props, _DESC_KEYS),
        status=_first(props, _STATUS_KEYS, "active") or "active",
        affected_system="",
        attachments=[],  # filled by the poller from client.iter_attachments
        created_at=_timestamp(props, _CREATED_KEYS),
        updated_at=_timestamp(props, _UPDATED_KEYS),
    )


def to_smax_suggestion(result) -> dict:
    """Serialize a PipelineResult into the SMAX suggestion payload
    (write-back in the safest mode — a side channel, not ticket fields).

    BUG-1 (preserved fix): classification fields are read with
    `getattr(cls, ...)` so Pydantic models and plain attribute objects
    work; the old `.get()` only worked on dicts and silently produced None
    for real classification objects.
    """
    cls = result.classification
    return {
        "classification": {
            "affected_system": getattr(cls, "affected_system", None) if cls else None,
            "service": getattr(cls, "service", None) if cls else None,
            "severity": getattr(cls, "severity", None) if cls else None,
        },
        "similar_ticket_ids": [t.get("id") for t in (result.similar_tickets or [])],
        "suggestions": result.suggestions,
        "confidence": result.confidence,
        "model_version": result.model_version,
        "prompt_version": result.prompt_version,
        "processed_at": result.processed_at.isoformat() if result.processed_at else None,
    }
