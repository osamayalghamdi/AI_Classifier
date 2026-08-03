"""Background sync thread — polls the external ticketing system for status updates.
Pipeline position: 60_sync — external ticketing poller."""

import json
import logging
import threading
import time
from pathlib import Path
from urllib.request import Request, urlopen

from .config import settings

_log = logging.getLogger(__name__)

SYNC_STAMP = Path(__file__).parent.parent / ".last_sync"

# Read the last sync timestamp from file, default to epoch
def _read_last_sync() -> str:
    try:
        return SYNC_STAMP.read_text().strip()
    except FileNotFoundError:
        return "2020-01-01T00:00:00+00:00"


# Save the latest sync timestamp to file
def _write_last_sync(ts: str) -> None:
    SYNC_STAMP.write_text(ts)


def _process_external_incident(store, inc: dict, since: str) -> None:
    """Ingest one synced ticket.

    Content-hash is the 'already seen' key (never fake ticket IDs):

    - NEW content (hash miss) → route through classify_and_store() so the
      ticket gets a real classification (system/severity/service) and a row
      with non-empty classification_json. The content-hash gate inside
      classify_and_store still applies (race-safe, occurrence_count=1).
    - Seen content (hash hit) → dedupe: increment occurrence_count, never
      create a second row, never re-classify. If the external status changed,
      propagate it with a status-only UPDATE (no LLM call).
    """
    # Lazy import — sync.py is imported by store.py and classifier.py imports
    # store.py, so a top-level import would be circular.
    from .core.classifier import classify_and_store, content_hash

    title = inc.get("title", "") or ""
    description = inc.get("description", "") or ""
    status = inc.get("status", "open")

    h = content_hash(title, description)
    existing = store.get_incident_by_hash(h)

    if existing is None:
        _log.info("Sync: new ticket '%s' — routing through classify_and_store", title[:60])
        classify_and_store(
            title,
            description,
            assign_group=inc.get("assign_group", ""),
            assignee=inc.get("assignee", ""),
        )
        return

    # Already seen → content-hash gate semantics: +1 occurrence, no second row.
    _log.info("Sync: ticket '%s' already known (incident %s) — incrementing occurrence_count",
              title[:60], existing["id"][:8])
    store.increment_occurrence(existing["id"])

    # Status-only propagation — never re-classify a known ticket.
    local_status = "active" if status in ("open", "in_progress", "third_party") else "resolved"
    current = store.get_incident(existing["id"])
    if current and current.get("status") != local_status:
        _log.info("Sync: status change for incident %s — %s → %s (no LLM call)",
                  existing["id"][:8], current.get("status"), local_status)
        store.set_status(existing["id"], status)


# Start a daemon thread that polls the external ticketing system every N seconds
def start_sync_worker(store) -> None:
    api = settings.ticketing_api_url.rstrip("/")
    interval = settings.sync_interval_seconds
    _log.info("Sync worker started — every %ss from %s", interval, api)

    def _run():
        while True:
            try:
                since = _read_last_sync()
                url = f"{api}/incidents/sync?since={since.replace('+', '%2B')}"
                req = Request(url)
                resp = urlopen(req, timeout=10)
                data = json.loads(resp.read().decode())

                updated = data.get("updated", [])
                if updated:
                    latest = since
                    for inc in updated:
                        _process_external_incident(store, inc, since)
                        updated_at = inc.get("updated_at", since)
                        if updated_at > latest:
                            latest = updated_at

                    if latest > since:
                        _write_last_sync(latest)

                    _log.info("Synced %d incidents (since %s)", len(updated), since[:19])
                else:
                    _log.debug("Sync — no changes (since %s)", since[:19])

            except Exception as exc:
                _log.error("Sync failed: %s", exc)

            time.sleep(interval)

    thread = threading.Thread(target=_run, daemon=True)
    thread.start()
