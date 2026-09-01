"""SMAX push adapter — the receive side of the SMAX webhook.

SMAX (or an automation step on top of its REST API) POSTs incident events
to us: a NEW incident (create) and, whenever the status changes (active,
verified, resolved, …), the SAME payload again with the new status. This
module is the ONLY place in the app where SMAX's field names appear (the
port.py rule: external field names live only inside source adapters).

Contract of this module:
  - Translation is TOLERANT: unknown keys are ignored and missing values
    degrade to "" — schema drift upstream never takes the endpoint down.
  - Dispatch is by SOURCE REFERENCE (the SMAX ticket id), the idempotency
    key:
        unknown reference  → treat as a NEW incident → enqueue for async
                             classification (E1 path)
        known reference    → STATUS-ONLY update of the existing incident
                             row (no re-classification, no new row)
  - Statuses are DYNAMIC: whatever SMAX reports is stored verbatim in
    incidents.source_status; only the local active/resolved view is
    derived (to_local_status). A status SMAX adds next week must not 422.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime

_log = logging.getLogger(__name__)

# SMAX field-name aliases — the only place these keys are named in the app.
_ID_KEYS = ("ticket_id", "id", "incident_id", "number", "record_id", "reference")
_TITLE_KEYS = ("title", "summary", "subject", "name", "display_label")
_DESC_KEYS = ("description", "details", "body", "notes")
_STATUS_KEYS = ("status", "state", "lifecycle_status", "status_label")
_CREATED_KEYS = ("created_at", "created", "created_date", "opened_at",
                 "creation_time", "create_time")
_UPDATED_KEYS = ("updated_at", "updated", "last_modified", "last_update_time",
                 "modified_time", "update_time")
# Wrapper keys some notification formats nest the record inside.
_WRAPPER_KEYS = ("event", "data", "incident", "ticket", "record", "payload")


@dataclass
class SmaxWebhookEvent:
    """A normalized event translated from one SMAX push payload."""

    source_reference: str = ""
    title: str = ""
    description: str = ""
    status: str = ""
    created_at: datetime | None = None
    updated_at: datetime | None = None


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
        return datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        _log.warning("SMAX webhook has unparseable timestamp %r — ignored", raw)
        return None


def _unwrap(payload: dict) -> dict:
    """Dig through notification wrappers to find the actual incident object
    (e.g. {"event": {"record": {...}}}). Bounded so a pathological nesting
    can't loop forever."""
    for _ in range(5):
        for k in _WRAPPER_KEYS:
            v = payload.get(k)
            if isinstance(v, dict):
                payload = v
                break
        else:
            return payload
    return payload


def parse_smax_event(payload: dict) -> SmaxWebhookEvent:
    """Translate a raw SMAX webhook payload into a normalized event."""
    obj = _unwrap(payload)
    return SmaxWebhookEvent(
        source_reference=_first(obj, _ID_KEYS),
        title=_first(obj, _TITLE_KEYS),
        description=_first(obj, _DESC_KEYS),
        status=_first(obj, _STATUS_KEYS),
        created_at=_iso(obj, _CREATED_KEYS),
        updated_at=_iso(obj, _UPDATED_KEYS),
    )


def handle_smax_event(payload: dict) -> tuple[int, dict]:
    """Dispatch ONE SMAX push. Returns (status_code, response_body).

    Never raises for payload issues — errors come back as the structured
    {"error": {...}} envelope (same shape as the E1-E9 contract):
        unknown reference  → 202 {action: "created", ...}  (async classify)
        known reference    → 200 {action: "updated", ...} (status only)
        unusable payload   → 400 INVALID_PAYLOAD
    """
    from ai_classification.services.jobs.integration import (
        enqueue,
        get_job,
        update_pending_job_status,
    )
    from ai_classification.services.jobs.integration.schemas import Err, error_body
    from ai_classification.shared.store import store

    event = parse_smax_event(payload)
    if not event.source_reference:
        return 400, error_body(
            Err.INVALID_PAYLOAD, "no ticket id found in the SMAX webhook payload"
        )

    status = event.status or "active"

    # ── Known reference → status-only update (the "same ID → change the
    #    status" rule — keeps the work smooth: cheap, synchronous, no LLM).
    existing = store.get_incident_by_source_ticket_id(event.source_reference)
    if existing is not None:
        updated = store.update_status_by_reference(event.source_reference, status)
        row = updated if updated is not None else existing
        _log.info("SMAX webhook — %s status → %r (local %s)",
                  event.source_reference, row.get("source_status"), row.get("status"))
        return 200, {
            "action": "updated",
            "reference": event.source_reference,
            "incident_id": row["id"],
            "status": row.get("status"),
            "source_status": row.get("source_status"),
        }

    # ── Unknown reference → new incident, async classify (E1 path).
    payload_dict = {
        "source_reference": event.source_reference,
        "title": event.title,
        "description": event.description,
        "status": status,
        "created_at": event.created_at.isoformat() if event.created_at else None,
        "updated_at": event.updated_at.isoformat() if event.updated_at else None,
    }
    job = enqueue(payload_dict)
    if job is not None and job["status"] in ("pending", "processing", "retryable"):
        # Race: SMAX pushed "created" then immediately "status changed"
        # before the worker classified — the incident row doesn't exist
        # yet, so keep the LATEST status in the queued payload.
        update_pending_job_status(event.source_reference, status)
        job = get_job(event.source_reference)
    job_status = job["status"] if job is not None else "pending"
    _log.info("SMAX webhook — %s enqueued for classification (job=%s)",
              event.source_reference, job_status)
    return 202, {
        "action": "created",
        "reference": event.source_reference,
        "job_status": job_status,
        "location": f"/api/v1/incidents/{event.source_reference}",
    }
