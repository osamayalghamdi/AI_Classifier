"""core/heal.py — periodic re-classification of fallback-classified incidents.

When the LLM is unreachable, the classifier degrades to a low-confidence
generic fallback (reasoning starts with "Classification failed after ...")
and the incident is stored with a wrong classification. The integration
worker already retries FRESH ingest (E5); this sweep heals the legacy
sync-path backlog: once the LLM is reachable again, fallback-marked
incidents are re-classified in place.

Safety rules:
- ONLY rows whose stored classification carries the fallback marker are
  touched; good classifications are never re-run (no LLM cost, no risk).
- Fails open: if the LLM is down, the sweep logs, skips, and tries again
  next tick (bounded per tick — never hammers the endpoint).
- Re-embedding uses the TICKET'S OWN TEXT (same rule as save_incident).
"""

from __future__ import annotations

import logging

from ..config import settings

_log = logging.getLogger(__name__)


def reclassify_fallback_incidents(limit: int | None = None) -> dict:
    """Re-classify fallback-marked incidents. Returns counts.

    {"healed": n, "still_fallback": n} — healed = updated in place with a
    real classification; still_fallback = re-classification produced the
    fallback again (LLM still down or genuinely unclassifiable).
    """
    from ..core.classifier import classify
    from ..core.store import store

    limit = limit or settings.reclassify_max_per_tick
    rows = store.find_fallback_incidents(limit)
    healed = still = 0
    for row in rows:
        try:
            cls = classify(row["title"], row["description"])
        except Exception as exc:  # noqa: BLE001 — LLM down: fail open
            _log.warning("Heal: LLM unavailable — leaving %s as-is (%s)",
                         row["id"][:8], exc)
            break  # LLM down → stop the tick, don't hammer the endpoint
        reasoning = cls.reasoning or ""
        if cls.confidence == "low" and reasoning.startswith("Classification failed after"):
            still += 1
            continue
        store.reclassify_incident(
            row["id"], row["title"], row["description"],
            row.get("extracted_text", ""), cls,
        )
        healed += 1
        _log.info("Heal: reclassified %s — %s / %s (%s)",
                  row["id"][:8], cls.affected_system, cls.service, cls.confidence)
    if healed or still:
        _log.info("Heal sweep: %d healed, %d still fallback", healed, still)
    return {"healed": healed, "still_fallback": still}
