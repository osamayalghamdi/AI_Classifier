"""Retry worker: periodic re-classification of fallback/unassigned incidents.

WHY: incidents classified under a broken LLM env (or transient failures) land
with a generic fallback (no offering → unassigned → OFFERING-000). Once the
LLM env is reachable, this worker re-runs them so they get a real offering
and become clusterable/assignable.

GATE NOTE: this feeds the offering engine's pools. Minting (proposals →
ACTIVE sub-offerings) stays behind the HUMAN review gate — proposals are
reviewed in frontend/dashboard/review.html, never auto-minted.
"""

from __future__ import annotations

import logging
import threading
import time
from datetime import datetime, timezone

_log = logging.getLogger(__name__)

# Fallback markers that mean "classification failed, worth retrying":
_FALLBACK_SERVICES = {"", "General / Unspecified", "Other", "general / unspecified", "other"}


def retry_candidates(limit: int | None = None) -> list[dict]:
    """Incidents whose stored classification is a failure fallback."""
    from ..core.store import store

    out = []
    for inc in store.list_incidents():
        cj = inc.get("classification_dict") or {}
        reason = cj.get("reasoning") or ""
        svc = (cj.get("service") or "").strip()
        failed = (not cj) or ("Classification failed" in reason) or ("failed after" in reason)
        generic = svc in _FALLBACK_SERVICES
        # Offering-less = "unassigned": a real service but no offering dot-path
        # (no offering → no assignable group). Re-classification gives the
        # cascade the chance to emit the full service.offering path.
        offering_less = bool(svc) and "." not in svc
        if failed or generic or offering_less:
            out.append(inc)
            if limit is not None and len(out) >= limit:
                break
    return out


def retry_unassigned(limit: int | None = None, *, dry_run: bool = False) -> dict:
    """Re-classify fallback incidents and update their stored classification.

    Idempotent per row (update, never duplicate). Returns stats. Dry-run
    mode reports what WOULD be re-classified without writing.
    """
    from ..core.classifier import PROMPT_VERSION, classify
    from ..core.store import store
    from ..config import settings

    candidates = retry_candidates(limit)
    stats = {"scanned": len(store.list_incidents()), "candidates": len(candidates),
             "reclassified": 0, "failed": 0, "dry_run": dry_run}
    if dry_run:
        # Pure counting — no LLM calls, nothing written.
        return stats
    for inc in candidates:
        title = inc.get("title", "") or ""
        description = inc.get("description", "") or ""
        try:
            cls = classify(title, description)
            cls.model_version = settings.llm_model
            cls.prompt_version = PROMPT_VERSION
        except Exception as exc:  # noqa: BLE001 — worker keeps sweeping
            _log.warning("Retry: classify failed for %s: %s", inc.get("id"), exc)
            stats["failed"] += 1
            continue
        new_cj = cls.model_dump_json()
        if not dry_run:
            store.update_classification(inc["id"], new_cj)
        stats["reclassified"] += 1
        _log.info("Retry: re-classified %s → service=%s", inc.get("id"),
                  (cls.service or "?")[:60])
    return stats


def start_retry_worker(interval: float | None = None) -> threading.Thread:
    """Background daemon: periodic retry sweep. interval seconds (default:
    settings.retry_interval, 900s). Runs once immediately at startup."""
    from ..config import settings

    interval = interval if interval is not None else float(getattr(settings, "retry_interval", 900))

    def _loop() -> None:
        _log.info("Retry worker started (interval=%ss)", interval)
        while True:
            try:
                stats = retry_unassigned()
                _log.info("Retry sweep: %s", stats)
            except Exception as exc:  # noqa: BLE001
                _log.error("Retry sweep failed: %s", exc)
            time.sleep(interval)

    t = threading.Thread(target=_loop, name="seams-retry", daemon=True)
    t.start()
    return t


# Manual one-shot entry (scripts/cron): python -m ai_classification.seams.retry
if __name__ == "__main__":
    import sys

    from ..core.store import store

    store.setup()
    args = [a for a in sys.argv[1:] if not a.startswith("-")]
    stats = retry_unassigned(limit=int(args[0]) if args else None,
                             dry_run="--dry-run" in sys.argv)
    print(f"retry: {stats}")
    print(f"finished at {datetime.now(timezone.utc).isoformat()}")
