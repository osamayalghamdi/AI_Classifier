"""SMAX change poller — the loop: list_changed(since) → submit each → stamp.

Standalone replacement for the old in-process sync worker for the SMAX
case: instead of running inside the classifier process, this connector
polls SMAX for changed tickets and pushes them into the classifier through
its public API (POST /api/v1/incidents). Idempotency comes free from the
server (content-hash + source_reference dedupe); the connector only keeps
a local since-stamp so it never re-lists old tickets.

Stamp file semantics (BUG-3 lesson): the stamp is RUNTIME STATE — a
configurable path (SMAX_SYNC_STAMP_PATH, default ./.last_sync), never
committed to git.
"""

from __future__ import annotations

import logging
import queue
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .config import NotConfiguredError, Settings
from .classifier_client import ClassifierClient
from .smax_client import SmaxClient
from .smax_models import from_smax

_log = logging.getLogger(__name__)

DEFAULT_SINCE = "2020-01-01T00:00:00+00:00"


# ── Stamp file (runtime state, never committed) ───────────────────────

def read_stamp(path: str | Path) -> str:
    """Last-sync timestamp, or the epoch default when no stamp exists."""
    try:
        return Path(path).read_text().strip() or DEFAULT_SINCE
    except FileNotFoundError:
        return DEFAULT_SINCE


def write_stamp(path: str | Path, ts: str) -> None:
    Path(path).write_text(ts)


# ── One poll tick ─────────────────────────────────────────────────────

def run_once(
    smax: SmaxClient,
    classifier: ClassifierClient,
    settings: Settings,
    *,
    outbox: queue.Queue | None = None,
) -> dict:
    """One poll tick: list changed tickets → submit each → advance stamp.

    Returns stats {"since", "listed", "submitted", "advanced"}.
    `outbox` (optional) receives each submitted reference so the write-back
    loop can poll the classifier for its result.
    """
    since_raw = read_stamp(settings.smax_sync_stamp_path)
    try:
        since = datetime.fromisoformat(since_raw)
        _log.debug("Poll since=%s (parsed %s)", since_raw, since)
    except ValueError:
        _log.warning("Stamp %r is not a valid ISO timestamp — re-listing from epoch", since_raw)
        since = datetime.fromisoformat(DEFAULT_SINCE)

    try:
        payloads = smax.list_changed(since.isoformat())
    except NotConfiguredError:
        raise
    except Exception as exc:  # noqa: BLE001 — poll tick must not die on upstream errors
        _log.error("Poll failed: %s", exc)
        return {"since": since_raw, "listed": 0, "submitted": 0, "advanced": None, "error": str(exc)}

    submitted = 0
    newest_submitted: str | None = None   # max(updated_at) of the OK submits
    oldest_failed: str | None = None      # min(updated_at) of the failures
    failed_unstamped = False              # a failure we cannot pin a time to
    for payload in payloads:
        incident = from_smax(payload)
        if not incident.source_reference:
            # Log KEY NAMES only — a whole payload dumps ticket text.
            _log.warning("SMAX payload without a source_reference — skipped (keys: %s)",
                         sorted(payload.keys()))
            continue
        try:
            ref = classifier.submit(incident)
        except Exception as exc:  # noqa: BLE001
            _log.error("Submit failed for %s: %s", incident.source_reference, exc)
            iso = _incident_changed_iso(incident)
            if iso is None:
                failed_unstamped = True
            elif oldest_failed is None or iso < oldest_failed:
                oldest_failed = iso
            continue
        submitted += 1
        if outbox is not None:
            outbox.put(ref)
        iso = _incident_changed_iso(incident)
        if iso is not None and (newest_submitted is None or iso > newest_submitted):
            newest_submitted = iso

    latest = newest_submitted or since_raw
    if failed_unstamped:
        # No timestamp to re-list this failure precisely — hold the stamp.
        _log.warning("Poll: a failed ticket carried no usable timestamp — "
                     "stamp held at %s so the whole batch is re-listed", since_raw[:19])
        latest = since_raw
    elif oldest_failed is not None and oldest_failed <= latest:
        # Never advance PAST a failed ticket: stamp to just before it, so it
        # is re-listed next tick even when a newer ticket submitted fine.
        latest = _just_before(oldest_failed)
    advanced = latest > since_raw
    if advanced:
        write_stamp(settings.smax_sync_stamp_path, latest)
        _log.info("Poll: %d/%d tickets submitted (since %s, stamp now %s)",
                  submitted, len(payloads), since_raw[:19], latest[:19])
    else:
        _log.info("Poll: %d/%d tickets submitted (since %s, no stamp advance)",
                  submitted, len(payloads), since_raw[:19])
    return {"since": since_raw, "listed": len(payloads), "submitted": submitted,
            "advanced": latest if advanced else None}


def _incident_changed_iso(incident) -> str | None:
    """The ticket's change time as a normalized UTC ISO string (updated_at
    wins over created_at), or None when the payload carried neither."""
    changed = incident.updated_at or incident.created_at
    return _as_utc_iso(changed) if changed is not None else None


def _just_before(iso: str) -> str:
    """One microsecond before an ISO timestamp — the stamp floor under a
    failed submit (the poll query is `updated_at > since`)."""
    return (datetime.fromisoformat(iso) - timedelta(microseconds=1)).isoformat()


def _as_utc_iso(dt: datetime) -> str:
    """ISO string in UTC — datetime.fromisoformat accepts offset forms;
    normalizing keeps lexicographic comparison correct."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat()


# ── Background loop ───────────────────────────────────────────────────

def start_poller(
    smax: SmaxClient,
    classifier: ClassifierClient,
    settings: Settings,
    *,
    outbox: queue.Queue | None = None,
    stop_event: threading.Event | None = None,
) -> threading.Thread:
    """Daemon poll loop every SMAX_POLL_S seconds. Graceful on
    KeyboardInterrupt via the stop_event."""
    stop = stop_event or threading.Event()

    def _run() -> None:
        _log.info("Poller started — every %ss (stamp=%s)",
                  settings.smax_poll_s, settings.smax_sync_stamp_path)
        while not stop.is_set():
            run_once(smax, classifier, settings, outbox=outbox)
            stop.wait(settings.smax_poll_s)

    thread = threading.Thread(target=_run, name="smax-poller", daemon=True)
    thread.start()
    return thread
