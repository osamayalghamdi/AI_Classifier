"""Local/fake ticket source — backed by the incident store.

Used by tests and offline runs (TICKETING_SOURCE=local). Every method
translates store rows to/from the normalized Incident model; no external
field names leak past this module.
"""

from __future__ import annotations

import logging
from datetime import datetime

from .port import Incident, PipelineResult

_log = logging.getLogger(__name__)


class LocalFakeTicketSource:
    """Fake source: incidents live in the store itself."""

    def __init__(self, store) -> None:
        self._store = store

    # ── Port: reads ────────────────────────────────────────────────────
    def fetch_ticket(self, ref: str) -> Incident:
        row = self._store.get_incident_by_source_ticket_id(ref)
        if row is None:
            raise KeyError(f"local ticket source: no incident for reference {ref!r}")
        return self._to_incident(row, ref)

    def fetch_attachments(self, ref: str) -> list[dict]:
        try:
            return list(self.fetch_ticket(ref).attachments)
        except KeyError:
            return []

    def list_changed(self, since: datetime | None = None) -> list[Incident]:
        incidents = self._store.list_incidents()
        out = []
        for row in incidents:
            changed = _parse_dt(row.get("last_seen") or row.get("created_at"))
            if since is not None and changed is not None and changed < since:
                continue
            ref = (row.get("source_ticket_ids") or [""])[0] or row["id"]
            out.append(self._to_incident(row, ref))
        return out

    # ── Port: write_back to the source ─────────────────────────────────
    def write_back(self, result: PipelineResult) -> None:
        # Fake/local source: nothing to write back to.
        _log.debug("LocalFakeTicketSource.write_back no-op for %s", result.source_reference)

    # ── Translation (external ↔ normalized) ────────────────────────────
    @staticmethod
    def _to_incident(row: dict, ref: str) -> Incident:
        return Incident(
            source_reference=ref,
            title=row.get("title", "") or "",
            description=row.get("description", "") or "",
            id=row.get("id", ""),
            attachments=[
                {"name": d, "type": "document"}
                for d in (row.get("documents") or [])
            ],
            created_at=_parse_dt(row.get("created_at")),
            updated_at=_parse_dt(row.get("last_seen")),
            status=row.get("status", "active"),
        )


def _parse_dt(value):
    if value is None:
        return None
    if isinstance(value, datetime):
        return value
    try:
        return datetime.fromisoformat(str(value))
    except ValueError:
        return None
