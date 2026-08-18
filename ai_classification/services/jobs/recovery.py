"""Job 1 — RECOVERY (one-time, MANUAL): fix tickets whose classification FAILED.

Triggered by a person after the LLM endpoint is fixed (python -m
ai_classification.services.jobs.recovery). Never automatic.

- Selects ONLY tickets with a real classification ERROR (failed reasoning),
  NOT offering-less or generic ones (those are Repool's domain).
- Tries to classify them again.
- Success  -> real offering -> the incident joins the engine's pool
              (Repool picks it up).
- Fail     -> queued to manual review (manual_review_queue) — "don't try
              again" — a human decides.
"""

from __future__ import annotations

import logging
import sys
from datetime import datetime, timezone

_log = logging.getLogger(__name__)


def recovery_candidates() -> list[dict]:
    """Incidents whose classification actually FAILED (error reasoning)."""
    from ai_classification.shared.store import store

    out = []
    queued = {q["incident_id"] for q in store.queue_list()}
    for inc in store.list_incidents():
        if inc["id"] in queued:
            continue  # exhausted — manual review owns it now
        cj = inc.get("classification_dict") or {}
        reason = cj.get("reasoning") or ""
        if (not cj) or ("Classification failed" in reason) or ("failed after" in reason):
            out.append(inc)
    return out


def run_recovery(*, dry_run: bool = False) -> dict:
    """Re-classify failed tickets. Exhausted tickets go to the manual queue."""
    from ai_classification.services.classify.classifier import PROMPT_VERSION, classify
    from ai_classification.shared.store import store
    from ai_classification.shared.config import settings

    candidates = recovery_candidates()
    stats = {"candidates": len(candidates), "recovered": 0, "failed": 0,
             "queued": 0, "dry_run": dry_run}
    if dry_run:
        return stats
    for inc in candidates:
        title = inc.get("title", "") or ""
        description = inc.get("description", "") or ""
        try:
            cls = classify(title, description)
            cls.model_version = settings.llm_model
            cls.prompt_version = PROMPT_VERSION
        except Exception as exc:  # noqa: BLE001
            _log.warning("Recovery: classify failed for %s: %s", inc.get("id"), exc)
            store.queue_add(inc["id"], reason=f"recovery retry failed: {exc}"[:200])
            stats["failed"] += 1
            stats["queued"] += 1
            continue
        store.update_classification(inc["id"], cls.model_dump_json())
        stats["recovered"] += 1
        _log.info("Recovery: recovered %s → service=%s", inc.get("id"),
                  (cls.service or "?")[:60])
    return stats


if __name__ == "__main__":
    import logging as _l
    _l.basicConfig(level=_l.INFO)

    from ai_classification.shared.store import store

    store.setup()
    stats = run_recovery(dry_run="--dry-run" in sys.argv)
    print(f"recovery: {stats}")
    print(f"finished at {datetime.now(timezone.utc).isoformat()}")
