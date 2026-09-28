"""Write-back — poll classifier results and post suggestions to SMAX.

For every submitted reference, poll the classifier's public API
(GET /api/v1/incidents/{ref}) until a result is ready, then translate it
with to_smax_suggestion and post it to the SMAX side channel.

Modes (SMAX_WRITE_BACK):
- "none"        — never post anything (results are still fetched/logged).
- "suggestions" (default) — post to the SMAX suggestions side channel.
- "full"        — same write path as suggestions (the connector only ever
                  writes to the side channel; it never mutates ticket
                  fields directly — that stays SMAX-side policy).

Dry-run gate (SMAX_DRY_RUN=true, the default): the suggestion payload is
LOGGED, never posted. Flip SMAX_DRY_RUN=false only after the payload
shape has been verified against the real SMAX instance.
"""

from __future__ import annotations

import logging
import queue
import threading
from datetime import datetime
from types import SimpleNamespace

from .config import Settings
from .classifier_client import ClassifierClient
from .smax_client import SmaxClient
from .smax_models import PipelineResult, to_smax_suggestion

_log = logging.getLogger(__name__)

# A reference whose result is still not ready after a full poll window is
# put back in the inbox instead of being dropped; after this many re-queues
# the write-back gives up loudly (WARNING) so the ticket is not lost silent.
REQUEUE_LIMIT = 5


def result_from_job(job: dict) -> PipelineResult:
    """Build a PipelineResult from the classifier's E2 job payload.

    The API returns classification as a plain dict; to_smax_suggestion
    needs attribute access (BUG-1), so the dict is wrapped in a
    SimpleNamespace — exactly the Pydantic-model-like shape the regression
    test exercises.
    """
    result = job.get("result") or {}
    cls = result.get("classification")
    classification = SimpleNamespace(**cls) if isinstance(cls, dict) else cls
    processed_raw = result.get("processed_at")
    processed_at = None
    if processed_raw:
        try:
            processed_at = datetime.fromisoformat(str(processed_raw).replace("Z", "+00:00"))
        except ValueError:
            _log.warning("Unparseable processed_at %r — ignored", processed_raw)
    return PipelineResult(
        source_reference=job.get("source_reference", ""),
        classification=classification,
        similar_tickets=result.get("similar_tickets") or [],
        suggestions=result.get("suggestions") or [],
        confidence=result.get("confidence", ""),
        model_version=result.get("model_version", ""),
        prompt_version=result.get("prompt_version", ""),
        processed_at=processed_at,
    )


def run_once(
    smax: SmaxClient,
    classifier: ClassifierClient,
    settings: Settings,
    references: list[str],
    *,
    max_attempts: int = 20,
    poll_interval: float = 2.0,
    inbox: queue.Queue | None = None,
    requeues: dict[str, int] | None = None,
) -> dict:
    """Poll each reference; write back (or log) ready results.

    A reference whose result is still not ready after `max_attempts` is
    re-queued into `inbox` (bounded by REQUEUE_LIMIT per reference, counted
    in `requeues`) — it used to be counted as "pending" and dropped forever.
    Without an inbox there is nowhere to re-queue to, so the old
    count-and-log behaviour stands.

    Returns stats {"checked": n, "ready": n, "written": n, "dry_run": n,
    "skipped": n, "pending": n, "requeued": n, "abandoned": n}.
    """
    checked = ready = written = dry_run = skipped = pending = 0
    requeued = abandoned = 0
    for ref in references:
        checked += 1
        try:
            job = classifier.result(ref, max_attempts=max_attempts, poll_interval=poll_interval)
        except Exception as exc:  # noqa: BLE001 — one bad ticket must not kill the sweep
            _log.warning("Write-back: result fetch failed for %s: %s", ref, exc)
            skipped += 1
            continue
        if job is None:
            pending += 1
            if inbox is None:
                _log.debug("Write-back: %s still pending after %d attempts", ref, max_attempts)
                continue
            seen = (requeues or {}).get(ref, 0)
            if seen >= REQUEUE_LIMIT:
                abandoned += 1
                _log.warning(
                    "Write-back: GIVING UP on %s — result still not ready after %d re-queues "
                    "(%d attempts each); no suggestion will be posted",
                    ref, seen, max_attempts,
                )
                continue
            if requeues is not None:
                requeues[ref] = seen + 1
            requeued += 1
            inbox.put(ref)
            _log.debug("Write-back: %s still pending after %d attempts — re-queued (%d/%d)",
                       ref, max_attempts, seen + 1, REQUEUE_LIMIT)
            continue
        ready += 1
        if requeues is not None:
            requeues.pop(ref, None)  # terminal: the counter is done with it
        if settings.smax_write_back == "none":
            _log.info("Write-back mode=none — skipping suggestion for %s", ref)
            skipped += 1
            continue
        result = result_from_job(job)
        payload = to_smax_suggestion(result)
        if settings.smax_dry_run:
            dry_run += 1
            _log.info("DRY-RUN write-back for %s — would post: %s", ref, payload)
            continue
        try:
            smax.write_suggestion(ref, payload)
            written += 1
            _log.info("Write-back: suggestion posted for %s", ref)
        except Exception as exc:  # noqa: BLE001
            _log.error("Write-back POST failed for %s: %s", ref, exc)
            skipped += 1
    _log.info("Write-back sweep: checked=%d ready=%d written=%d dry_run=%d pending=%d "
              "requeued=%d abandoned=%d skipped=%d",
              checked, ready, written, dry_run, pending, requeued, abandoned, skipped)
    return {
        "checked": checked, "ready": ready, "written": written,
        "dry_run": dry_run, "pending": pending, "skipped": skipped,
        "requeued": requeued, "abandoned": abandoned,
    }


# ── Background loop ───────────────────────────────────────────────────

def start_writeback(
    smax: SmaxClient,
    classifier: ClassifierClient,
    settings: Settings,
    *,
    inbox: queue.Queue | None = None,
    stop_event: threading.Event | None = None,
) -> threading.Thread:
    """Daemon loop: drain submitted references from `inbox` and run a
    write-back sweep over whatever is available. Graceful on
    KeyboardInterrupt via the stop_event.

    `requeues` keeps the per-reference re-queue count across sweeps, so a
    reference whose result never became ready is retried a bounded number
    of times and then dropped with a WARNING instead of silently."""
    stop = stop_event or threading.Event()
    inbox = inbox or queue.Queue()
    requeues: dict[str, int] = {}

    def _run() -> None:
        _log.info("Write-back started — mode=%s dry_run=%s",
                  settings.smax_write_back, settings.smax_dry_run)
        while not stop.is_set():
            batch: list[str] = []
            try:
                # Block briefly for the first ref, then drain whatever else
                # arrived while we were idle.
                first = inbox.get(timeout=settings.smax_poll_s)
                batch.append(first)
                while True:
                    try:
                        batch.append(inbox.get_nowait())
                    except queue.Empty:
                        break
            except queue.Empty:
                pass
            if batch:
                run_once(smax, classifier, settings, batch,
                         inbox=inbox, requeues=requeues)

    thread = threading.Thread(target=_run, name="smax-writeback", daemon=True)
    thread.start()
    return thread
