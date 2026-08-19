"""Re-classify EVERY stored incident through the classifier v3 pipeline.

v3 adds stage-0 triage (ticket_kind) and honest failure tracking
(classification_status) on top of the v2 cascade. This sweep re-runs every
stored ticket through classify() — passing its incident id as incident_ref so
every LLM decision lands in classification_log — and persists the v3 result
in place via store.update_classification (identity, status, occurrence
bookkeeping untouched). Never mints, never touches pools.

--dry-run prints the per-ticket old→new diff (ticket_kind, service,
incident_type) and writes NOTHING. --limit N caps the sweep for testing.

Usage (inside the API container, which has LLM keys + DB access):
    docker exec ai_classifier-api-1 python -m scripts.reclassify_v3 [--dry-run] [--limit N]
"""
from __future__ import annotations

import logging
import sys
from datetime import datetime, timezone

_log = logging.getLogger(__name__)


def _parse_args(argv: list[str]) -> tuple[bool, int | None, int]:
    dry_run = "--dry-run" in argv
    limit = None
    offset = 0
    for i, arg in enumerate(argv):
        if arg == "--limit" and i + 1 < len(argv):
            limit = int(argv[i + 1])
        elif arg.startswith("--limit="):
            limit = int(arg.split("=", 1)[1])
        elif arg == "--offset" and i + 1 < len(argv):
            offset = int(argv[i + 1])
        elif arg.startswith("--offset="):
            offset = int(arg.split("=", 1)[1])
    return dry_run, limit, offset


def _kind_value(cls) -> str:
    """ticket_kind as a plain string (StrEnum or bare str)."""
    return cls.ticket_kind.value if hasattr(cls.ticket_kind, "value") else cls.ticket_kind


def run_reclassify(*, dry_run: bool = False, limit: int | None = None, offset: int = 0) -> dict:
    from ai_classification.services.classify.classifier import classify
    from ai_classification.shared.store import store

    incidents = store.list_incidents()
    if offset:
        incidents = incidents[offset:]
    if limit is not None:
        incidents = incidents[:limit]

    stats = {"candidates": len(incidents), "reclassified": 0, "failed": 0,
             "dry_run": dry_run}
    for inc in incidents:
        title = inc.get("title", "") or ""
        description = inc.get("description", "") or ""
        before = inc.get("classification_dict") or {}
        try:
            try:
                cls = classify(title, description, incident_ref=inc["id"])
            except TypeError:
                # classifier not yet on the v3 signature — drop the kwarg
                cls = classify(title, description)
        except Exception as exc:  # noqa: BLE001 — never let one ticket kill the sweep
            _log.warning("reclassify_v3 failed for %s: %s", inc["id"], exc)
            stats["failed"] += 1
            continue
        new_kind = _kind_value(cls)
        if dry_run:
            print(f"[dry-run] {inc['id']}: "
                  f"ticket_kind={before.get('ticket_kind') or '?'} -> {new_kind}, "
                  f"service={before.get('service') or '?'} -> {cls.service}, "
                  f"incident_type={before.get('incident_type') or '?'} -> {cls.incident_type}")
            continue
        store.update_classification(
            inc["id"], cls.model_dump_json(),
            ticket_kind=new_kind,
            classification_status=cls.classification_status,
        )
        stats["reclassified"] += 1
        _log.info("reclassified %s — kind=%s, service=%s", inc["id"], new_kind, cls.service)
    return stats


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    from ai_classification.shared.store import store

    dry_run, limit, offset = _parse_args(sys.argv[1:])
    store.setup()
    stats = run_reclassify(dry_run=dry_run, limit=limit, offset=offset)
    print(f"reclassify_v3: {stats}")
    print(f"finished at {datetime.now(timezone.utc).isoformat()}")
