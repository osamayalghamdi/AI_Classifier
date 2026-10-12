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

from ai_classification.shared.config import settings
from ai_classification.seams import NotConfiguredError, get_ticket_source, persist_result, process_incident

_log = logging.getLogger(__name__)

# Runtime stamp file — where the last-sync timestamp lives. BUG-3: the
# hardcoded three-parent path resolved to ai_classification/ instead of the
# repo root, so the checked-in stamp was never read. Now configurable via
# SYNC_STAMP_PATH; default resolves four parents above this file
# (services/jobs/sync.py → jobs/ → services/ → ai_classification/ → repo root).
def _stamp_path() -> Path:
    configured = getattr(settings, "sync_stamp_path", "") or ""
    if configured:
        return Path(configured)
    return Path(__file__).resolve().parent.parent.parent.parent / ".last_sync"


SYNC_STAMP = _stamp_path()

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
    on a fresh deployment. (NotConfiguredError is raised lazily by the
    source's methods, so the check is on the config, not the constructor.)
    """
    interval = settings.sync_interval_seconds
    dry_run = settings.ticketing_dry_run
    if settings.ticketing_source != "local":
        # Phase 4: the real SMAX source moved out of the app into the
        # standalone connector (integrations/smax, python -m
        # integrations.smax.main), which talks to the classifier through
        # its public HTTP API. In-process, only the local fake source is
        # available; selecting "real" just logs the deprecation note and
        # the worker idles instead of polling a dead endpoint.
        _log.warning(
            "Sync worker NOT started — TICKETING_SOURCE=%r is deprecated "
            "in-process: SMAX connectivity moved to the standalone connector "
            "`python -m integrations.smax.main` (integrations/smax/). "
            "Set TICKETING_SOURCE=local to run the in-process worker.",
            settings.ticketing_source,
        )
        return
    source = get_ticket_source()
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
                processed = 0
                for incident in source.list_changed(since):
                    result = process_incident(incident)
                    outcome = persist_result(result, dry_run=dry_run)
                    if outcome.get("action") == "skipped":
                        _log.error("Sync: ticket %s skipped — %s",
                                   incident.source_reference, outcome.get("reason"))
                        continue
                    processed += 1
                    changed = incident.updated_at or incident.created_at
                    if changed is not None:
                        iso = changed.isoformat()
                        if iso > latest:
                            latest = iso
                            advanced = True
                if advanced:
                    _write_last_sync(latest)
                    _log.info("Synced %s changes (since %s)", processed, since_raw[:19])
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
