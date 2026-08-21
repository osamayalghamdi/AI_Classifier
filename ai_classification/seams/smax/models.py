"""SMAX external payload models — the boundary translation layer.

SMAX wire formats are SMAX's business; the pipeline's incident model is
ours. This module owns the mapping between them. No SMAX field name may
appear anywhere outside this package (enforced by the containment grep).
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from ..port import Incident

_log = logging.getLogger(__name__)

# ── SMAX field name → our Incident field ──────────────────────────────
# Only place in the codebase where SMAX's payload keys are named.
_TITLE_KEYS = ("title", "summary", "subject", "name")
_DESC_KEYS = ("description", "details", "body", "notes")
_ID_KEYS = ("ticket_id", "id", "incident_id", "number")
_CREATED_KEYS = ("created_at", "created", "created_date", "opened_at")
_UPDATED_KEYS = ("updated_at", "updated", "last_modified")


def _first(payload: dict, keys: tuple[str, ...], default: str = "") -> str:
    for k in keys:
        v = payload.get(k)
        if v is not None and str(v).strip():
            return str(v).strip()
    return default


def _iso(payload: dict, keys: tuple[str, ...]) -> datetime | None:
    raw = _first(payload, keys)
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00")).astimezone(timezone.utc)
    except ValueError:
        _log.warning("SMAX payload has unparseable timestamp %r — ignored", raw)
        return None


def from_smax(payload: dict) -> Incident:
    """Translate a raw SMAX payload into the pipeline's Incident model.

    Unknown keys are ignored (SMAX returns many fields we don't consume);
    malformed payloads degrade to empty strings rather than raising, so a
    schema drift in upstream never takes the pipeline down.
    """
    return Incident(
        source_reference=_first(payload, _ID_KEYS),
        title=_first(payload, _TITLE_KEYS),
        description=_first(payload, _DESC_KEYS),
        attachments=[],  # fetched separately via client.get_attachments
        created_at=_iso(payload, _CREATED_KEYS),
        updated_at=_iso(payload, _UPDATED_KEYS),
    )


def to_smax_suggestion(result) -> dict:
    """Serialize a PipelineResult into the SMAX suggestion payload
    (write-back in the safest mode — a side channel, not ticket fields)."""
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
