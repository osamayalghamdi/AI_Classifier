"""Re-classify stored incidents through the classifier v3 pipeline.

v3 adds stage-0 triage (ticket_kind) and honest failure tracking
(classification_status) on top of the v2 cascade. This sweep re-runs
tickets through classify() — passing its incident id as incident_ref so
every LLM decision lands in classification_log — and persists the v3 result
in place via store.update_classification (identity, status, occurrence
bookkeeping untouched). Never mints, never touches pools.

Safety guards (added after the 52-row LLM-outage pollution incident):
  --only-failed           Sweep ONLY classification_status='failed' rows
                          (SQL-filtered, never loads all rows). Without it
                          the sweep would re-run GOOD classifications at
                          real cost. Default OFF for backward compat; the
                          recommended invocation always passes it.
  --sleep N               Seconds between tickets (default 1.0 when
                          --only-failed is used, else 0). Serial + paced —
                          never hammers the LLM endpoint.
  --stop-after-failures N CIRCUIT BREAKER (default 5): abort the sweep with
                          exit code 1 after N CONSECUTIVE failures. This is
                          the single most important guard — it prevents
                          repeating the event where 44 bad rows were written
                          in one minute.

--dry-run prints the per-ticket old→new diff (ticket_kind, service,
incident_type) and writes NOTHING. --limit N caps the sweep for testing.

Usage (inside the API container, which has LLM keys + DB access):
    docker exec ai_classifier-api-1 python -m scripts.reclassify_v3 \
        --only-failed --dry-run
    docker exec ai_classifier-api-1 python -m scripts.reclassify_v3 \
        --only-failed --sleep 1
"""
from __future__ import annotations

import logging
import sys
import time
from datetime import datetime, timezone

_log = logging.getLogger(__name__)

_DEFAULT_SLEEP = 1.0          # when --only-failed
_DEFAULT_STOP_AFTER = 5       # consecutive failures → abort


def _parse_args(argv: list[str]) -> dict:
    opts = {
        "dry_run": "--dry-run" in argv,
        "only_failed": "--only-failed" in argv,
        "limit": None,
        "offset": 0,
        "sleep": None,          # None → derived from only_failed
        "stop_after": _DEFAULT_STOP_AFTER,
    }
    for i, arg in enumerate(argv):
        if arg == "--limit" and i + 1 < len(argv):
            opts["limit"] = int(argv[i + 1])
        elif arg.startswith("--limit="):
            opts["limit"] = int(arg.split("=", 1)[1])
        elif arg == "--offset" and i + 1 < len(argv):
            opts["offset"] = int(argv[i + 1])
        elif arg.startswith("--offset="):
            opts["offset"] = int(arg.split("=", 1)[1])
        elif arg == "--sleep" and i + 1 < len(argv):
            opts["sleep"] = float(argv[i + 1])
        elif arg.startswith("--sleep="):
            opts["sleep"] = float(arg.split("=", 1)[1])
        elif arg == "--stop-after-failures" and i + 1 < len(argv):
            opts["stop_after"] = int(argv[i + 1])
        elif arg.startswith("--stop-after-failures="):
            opts["stop_after"] = int(arg.split("=", 1)[1])
    return opts


def _kind_value(cls) -> str:
    """ticket_kind as a plain string (StrEnum or bare str)."""
    return cls.ticket_kind.value if hasattr(cls.ticket_kind, "value") else cls.ticket_kind


def run_reclassify(*, dry_run: bool = False, only_failed: bool = False,
                   limit: int | None = None, offset: int = 0,
                   sleep_s: float | None = None,
                   stop_after_failures: int = _DEFAULT_STOP_AFTER) -> dict:
    from ai_classification.services.classify.classifier import classify
    from ai_classification.shared.store import store

    # SQL-level filter — never pull every row just to keep a few.
    incidents = store.list_incidents(
        classification_status="failed" if only_failed else None)
    if offset:
        incidents = incidents[offset:]
    if limit is not None:
        incidents = incidents[:limit]

    if sleep_s is None:
        sleep_s = _DEFAULT_SLEEP if only_failed else 0.0

    stats = {"candidates": len(incidents), "reclassified": 0, "failed": 0,
             "unchanged": 0, "dry_run": dry_run, "aborted": False,
             "consecutive_failures": 0}
    consecutive = 0
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
            consecutive += 1
            if consecutive >= stop_after_failures:
                stats["aborted"] = True
                _log.error("CIRCUIT BREAKER: %d consecutive failures — aborting sweep",
                           consecutive)
                break
            continue
        # classify() NEVER raises on LLM failure — it returns an honest
        # classification_status="failed" fallback. Count those as failures
        # for the circuit breaker too: a run where the endpoint is down
        # must abort, not keep writing fallback rows (the 44-row incident).
        if cls.classification_status != "ok":
            stats["failed"] += 1
            consecutive += 1
            if dry_run:
                print(f"[dry-run] {inc['id']}: status={before.get('classification_status') or '?'} "
                      f"FAILED-> {cls.classification_status} — aborting if consecutive")
            if consecutive >= stop_after_failures:
                stats["aborted"] = True
                _log.error("CIRCUIT BREAKER: %d consecutive failures — aborting sweep",
                           consecutive)
                break
            continue
        consecutive = 0
        new_kind = _kind_value(cls)
        if dry_run:
            print(f"[dry-run] {inc['id']}: "
                  f"status={before.get('classification_status') or '?'} -> {cls.classification_status}, "
                  f"ticket_kind={before.get('ticket_kind') or '?'} -> {new_kind}, "
                  f"service={before.get('service') or '?'} -> {cls.service}, "
                  f"system={before.get('affected_system') or '?'} -> {cls.affected_system}, "
                  f"incident_type={before.get('incident_type') or '?'} -> {cls.incident_type}")
            continue
        store.update_classification(
            inc["id"], cls.model_dump_json(),
            ticket_kind=new_kind,
            classification_status=cls.classification_status,
        )
        stats["reclassified"] += 1
        _log.info("reclassified %s — kind=%s, service=%s, status=%s",
                  inc["id"], new_kind, cls.service, cls.classification_status)
        if sleep_s:
            time.sleep(sleep_s)
    return stats


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    from ai_classification.shared.store import store

    opts = _parse_args(sys.argv[1:])
    store.setup()
    stats = run_reclassify(
        dry_run=opts["dry_run"], only_failed=opts["only_failed"],
        limit=opts["limit"], offset=opts["offset"],
        sleep_s=opts["sleep"], stop_after_failures=opts["stop_after"],
    )
    attempted = stats["candidates"]
    print(f"reclassify_v3: {stats}")
    print(f"  attempted={attempted} now_ok={stats['reclassified']} "
          f"still_failed={stats['failed']} aborted={stats['aborted']}")
    print(f"finished at {datetime.now(timezone.utc).isoformat()}")
    if stats["aborted"]:
        sys.exit(1)
