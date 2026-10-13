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

import base64
import logging
import queue
import re
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .config import NotConfiguredError, Settings
from .classifier_client import ClassifierClient
from .smax_client import SmaxClient
from .smax_models import from_smax

_log = logging.getLogger(__name__)

# 2020-01-01T00:00:00Z — the epoch floor the first run lists from.
DEFAULT_SINCE_MS = 1577836800000


# ── Stamp file (runtime state, never committed) ───────────────────────
# The stamp is epoch MILLISECONDS because that is what the SMAX update-time
# filter compares against (LastUpdateTime). An older ISO stamp is converted
# once, so upgrading the connector does not re-list history.

def read_stamp(path: str | Path) -> int:
    """Last-sync time in epoch ms, or the epoch floor when no stamp exists."""
    try:
        raw = Path(path).read_text().strip()
    except FileNotFoundError:
        return DEFAULT_SINCE_MS
    if not raw:
        return DEFAULT_SINCE_MS
    if re.fullmatch(r"\d{10,17}", raw):
        return int(raw)
    try:  # legacy ISO stamp
        return int(datetime.fromisoformat(raw).timestamp() * 1000)
    except ValueError:
        _log.warning("Stamp %r is neither epoch ms nor ISO — re-listing from epoch", raw)
        return DEFAULT_SINCE_MS


def write_stamp(path: str | Path, ts_ms: int) -> None:
    Path(path).write_text(str(int(ts_ms)))


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
    since_ms = read_stamp(settings.smax_sync_stamp_path)

    try:
        entities = list(smax.list_changed(since_ms))
    except NotConfiguredError:
        raise
    except Exception as exc:  # noqa: BLE001 — a poll tick must not die on upstream errors
        _log.error("Poll failed: %s", exc)
        return {"since": since_ms, "listed": 0, "submitted": 0, "advanced": None, "error": str(exc)}

    submitted = 0
    newest_submitted: int | None = None   # max(changed) of the OK submits
    oldest_failed: int | None = None      # min(changed) of the failures
    failed_unstamped = False              # a failure we cannot pin a time to
    for entity in entities:
        incident = from_smax(entity)
        if not incident.source_reference:
            # Log KEY NAMES only — a whole payload dumps ticket text.
            _log.warning("SMAX entity without an Id — skipped (keys: %s)",
                         sorted((entity.get("properties") or entity).keys()))
            continue
        incident.attachments = _ingest_attachments(smax, entity, settings)
        try:
            ref = classifier.submit(incident)
        except Exception as exc:  # noqa: BLE001
            _log.error("Submit failed for %s: %s", incident.source_reference, exc)
            changed = _changed_ms(incident)
            if changed is None:
                failed_unstamped = True
            elif oldest_failed is None or changed < oldest_failed:
                oldest_failed = changed
            continue
        submitted += 1
        if outbox is not None:
            outbox.put(ref)
        changed = _changed_ms(incident)
        if changed is not None and (newest_submitted is None or changed > newest_submitted):
            newest_submitted = changed

    latest = newest_submitted if newest_submitted is not None else since_ms
    if failed_unstamped:
        # No timestamp to re-list this failure precisely — hold the stamp.
        _log.warning("Poll: a failed ticket carried no usable timestamp — "
                     "stamp held at %s so the whole batch is re-listed", since_ms)
        latest = since_ms
    elif oldest_failed is not None and oldest_failed <= latest:
        # Never advance PAST a failed ticket: stamp to just before it, so it
        # is re-listed next tick even when a newer ticket submitted fine.
        latest = oldest_failed - 1
    elif latest > since_ms:
        # The SMAX filter is `LastUpdateTime >= since`, so stamping the newest
        # ticket's own time would re-list it on EVERY tick, for ever. Advance
        # one millisecond past it instead.
        latest = latest + 1
    advanced = latest > since_ms
    if advanced:
        write_stamp(settings.smax_sync_stamp_path, latest)
        _log.info("Poll: %d/%d tickets submitted (since %s, stamp now %s)",
                  submitted, len(entities), since_ms, latest)
    else:
        _log.info("Poll: %d/%d tickets submitted (since %s, no stamp advance)",
                  submitted, len(entities), since_ms)
    return {"since": since_ms, "listed": len(entities), "submitted": submitted,
            "advanced": latest if advanced else None}


def _ingest_attachments(smax: SmaxClient, entity: dict, settings: Settings) -> list[dict]:
    """Download this incident's visible attachments for the ingest payload.

    One entry per attachment: `name` always, `content_base64` + `content_type`
    when the bytes came through. A file that is too large or fails to download
    is still listed by name — the classifier records it, it just cannot be
    transcribed by the OCR service. One bad attachment never fails the ticket.
    """
    entries: list[dict] = []
    limit = int(settings.smax_max_attachment_mb * 1024 * 1024)
    try:
        metadata = list(smax.iter_attachments(entity))
    except Exception as exc:  # noqa: BLE001 — metadata errors must not lose the ticket
        _log.warning("Attachment metadata unreadable (%s) — incident sent without files", exc)
        return entries
    for meta in metadata:
        entry: dict = {"name": meta["file_name"]}
        if meta.get("mime_type"):
            entry["content_type"] = meta["mime_type"]
        size = meta.get("expected_size")
        if size is not None and size > limit:
            _log.warning("Attachment %s is %s bytes (> %s limit) — sent by name only",
                         meta["file_name"], size, limit)
            entries.append(entry)
            continue
        try:
            raw = smax.download_file(meta["file_id"], max_bytes=limit)
            entry["content_base64"] = base64.b64encode(raw).decode("ascii")
        except Exception as exc:  # noqa: BLE001 — download failure keeps the ticket
            _log.warning("Attachment %s (%s) not downloaded: %s",
                         meta["file_name"], meta["file_id"], exc)
        entries.append(entry)
    return entries


def _changed_ms(incident) -> int | None:
    """The ticket's change time in epoch ms (updated_at wins over created_at),
    or None when the entity carried neither."""
    changed = incident.updated_at or incident.created_at
    if changed is None:
        return None
    if changed.tzinfo is None:
        changed = changed.replace(tzinfo=timezone.utc)
    return int(changed.timestamp() * 1000)


def _as_utc_iso(dt: datetime) -> str:
    """ISO string in UTC — kept for logs and for older callers."""
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
