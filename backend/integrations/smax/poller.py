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
    limit: int | None = None,
) -> dict:
    """One poll tick: list changed tickets → submit each → advance stamp.

    Returns stats {"since", "listed", "submitted", "advanced"}.
    `outbox` (optional) receives each submitted reference so the write-back
    loop can poll the classifier for its result. `limit` caps how many tickets
    this tick submits (the first live run sets it small) — and, because a cap
    that only limited the submissions would still pull a whole backlog off
    SMAX, it caps the read too: a capped tick lists exactly one ticket more
    than it may submit (see `read_limit` below).

    Robustness: a malformed entity is skipped, a submit failure holds the stamp
    under that ticket, and a listing failure never advances past what was seen —
    so the worst case is re-listing (the classifier dedupes by reference), never
    a silently dropped ticket.
    """
    since_ms = read_stamp(settings.smax_sync_stamp_path)
    cap = limit if limit is not None else (settings.smax_max_incidents_per_tick or None)

    entities: list[dict] = []
    listing_error: str | None = None
    # A submission cap bounds the READ as well. One ticket past the cap is
    # enough to submit the cap and still learn the change time of the first
    # ticket held back (the stamp is rewound to just before it, below), so a
    # capped tick reads one page instead of every page of the change list.
    # Without a cap the whole list is read, exactly as before.
    read_limit = None if cap is None else cap + 1
    try:
        for entity in smax.list_changed(since_ms):
            entities.append(entity)
            if read_limit is not None and len(entities) >= read_limit:
                break
    except NotConfiguredError:
        raise
    except Exception as exc:  # noqa: BLE001 — a poll tick must not die on upstream errors
        listing_error = str(exc)
        _log.error("Listing failed after %d tickets: %s — the stamp will not advance", len(entities), exc)

    if not entities and listing_error:
        return {"since": since_ms, "listed": 0, "submitted": 0, "advanced": None,
                "error": listing_error}

    submitted = 0
    skipped_invalid = 0
    newest_submitted: int | None = None   # max(changed) of the OK submits
    oldest_failed: int | None = None      # min(changed) of the failures
    failed_unstamped = False              # a failure we cannot pin a time to
    limited = False
    remaining: list[int] = []             # changed times of the entities the cap held back
    for index, entity in enumerate(entities):
        if cap is not None and submitted >= cap:
            limited = True
            # With a bounded read this is normally the single extra ticket the
            # tick fetched on purpose: its time is where the next tick resumes.
            remaining = [ms for ms in (_entity_changed_ms(e) for e in entities[index:]) if ms is not None]
            break
        try:
            incident = from_smax(entity)
        except Exception as exc:  # noqa: BLE001 — one malformed entity must not kill the tick
            skipped_invalid += 1
            _log.warning("Entity %d could not be mapped (%s) — skipped", index, exc)
            continue
        if not incident.source_reference:
            # Log KEY NAMES only — a whole payload dumps ticket text.
            _log.warning("SMAX entity without an Id — skipped (keys: %s)",
                         sorted((entity.get("properties") or entity).keys()))
            skipped_invalid += 1
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
    if limited or listing_error:
        # Only part of the list was handled. Never advance past a ticket we did
        # not look at: rewind to just before the earliest one we held back (the
        # tickets already sent may be listed again — the classifier dedupes by
        # source_reference, so that costs a request, not a duplicate).
        if remaining:
            latest = min(remaining) - 1
            if latest < since_ms:
                latest = since_ms
        else:
            latest = since_ms
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
    if limited:
        _log.warning("Poll: submission cap reached (%s) — %d ticket(s) were read but "
                     "not submitted; the stamp was held before them and the next tick "
                     "resumes there (the read stopped at the cap, so more may exist)",
                     cap, max(len(entities) - submitted, 0))
    return {"since": since_ms, "listed": len(entities), "submitted": submitted,
            "skipped": skipped_invalid, "limited": limited, "read_cap": read_limit,
            "advanced": latest if advanced else None,
            **({"error": listing_error} if listing_error else {})}


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
            if not raw and size != 0:
                # An empty body for a file that should have content is a failure,
                # not a zero-byte attachment — send the name only.
                raise ValueError("empty body (expected content)")
            entry["content_base64"] = base64.b64encode(raw).decode("ascii")
        except Exception as exc:  # noqa: BLE001 — download failure keeps the ticket
            _log.warning("Attachment %s (%s) not downloaded: %s",
                         meta["file_name"], meta["file_id"], exc)
        entries.append(entry)
    return entries


def _entity_changed_ms(entity: dict) -> int | None:
    """Change time of a raw SMAX entity, tolerantly.

    Used to rewind the stamp to the earliest ticket a submission cap held back,
    so an unmappable entity simply has no time to honour rather than raising.
    """
    try:
        return _changed_ms(from_smax(entity))
    except Exception:  # noqa: BLE001
        return None


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
