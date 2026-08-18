"""Polling caller — a thin wrapper over the SEAMS pipeline.

The pipeline talks only to the TicketSource port; this module just drives
the poll loop: list changed tickets → process each → persist. All external
payload translation lives inside the source adapters (ai_classification/
seams/), never here.
"""

import logging
import threading
import time
from datetime import datetime
from pathlib import Path

from .config import settings
from .seams import NotConfiguredError, get_ticket_source, persist_result, process_incident

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


def start_sync_worker(store) -> None:
    """Daemon poll loop — one tick: list_changed → process → persist.

    If the selected ticket source isn't configured (e.g. TICKETING_SOURCE
    is the real SMAX source but TICKETING_API_TOKEN is unset), log once at
    startup and idle — do NOT spam the log with an error every poll tick
    on a fresh deployment.
    """
    interval = settings.sync_interval_seconds
    dry_run = settings.ticketing_dry_run
    try:
        source = get_ticket_source()
    except NotConfiguredError as exc:
        _log.info(
            "Sync worker NOT started — %s. Set TICKETING_API_TOKEN (+ "
            "TICKETING_SOURCE=real) to enable polling.", exc
        )
        return
    _log.info("Sync worker started — every %ss via source=%s (dry_run=%s)",
              interval, settings.ticketing_source, dry_run)

    def _run():
        while True:
            try:
                since_raw = _read_last_sync()
                try:
                    since = datetime.fromisoformat(since_raw)
                except ValueError:
                    since = None
                latest = since_raw
                advanced = False
                for incident in source.list_changed(since):
                    result = process_incident(incident)
                    outcome = persist_result(result, dry_run=dry_run)
                    if outcome.get("action") == "skipped":
                        _log.error("Sync: ticket %s skipped — %s",
                                   incident.source_reference, outcome.get("reason"))
                    changed = incident.updated_at or incident.created_at
                    if changed is not None:
                        iso = changed.isoformat()
                        if iso > latest:
                            latest = iso
                            advanced = True
                if advanced:
                    _write_last_sync(latest)
                    _log.info("Synced %s changes (since %s)", "1+" , since_raw[:19])
                else:
                    _log.debug("Sync — no changes (since %s)", since_raw[:19])
            except NotConfiguredError as exc:
                # Same cadence/severity as the legacy "Sync failed" path.
                _log.error("Sync failed: %s", exc)
            except Exception as exc:
                _log.error("Sync failed: %s", exc)
            time.sleep(interval)

    thread = threading.Thread(target=_run, daemon=True)
    thread.start()
