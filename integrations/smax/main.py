"""SMAX connector entrypoint — python -m integrations.smax.main.

Runs the poller (SMAX → classifier API) and the write-back loop
(classifier API → SMAX suggestions) as two daemon threads, with a clean
Ctrl-C exit. Also supports one-shot modes:

- --check        print the resolved config (tokens masked) and exit — no
                 network, no credentials required.
- --once         run exactly one poll tick + one write-back sweep, print
                 stats, exit.
- --backfill F   one-shot historical ingest from a JSON file of incidents
                 via POST /api/v1/backfill (chunked <=200). Optional
                 --since ISO filters the file by updated_at/created_at.

Startup fails loudly (exit 1, message on stderr) when a required token is
missing: SMAX_API_TOKEN for the SMAX side, CLASSIFIER_API_TOKEN for the
classifier API side. This package imports NOTHING from the classifier app.
"""

from __future__ import annotations

import argparse
import json
import logging
import queue
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

from .classifier_client import ClassifierClient
from .config import NotConfiguredError, settings
from .smax_client import SmaxClient
from .smax_models import Incident, from_smax
from . import poller, writeback

_log = logging.getLogger(__name__)


def _parse_dt(value):
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


def _incident_from_backfill_item(item: dict) -> Incident:
    """Accept a normalized incident (source_reference) or a raw SMAX
    payload (translated via from_smax)."""
    if "source_reference" in item:
        return Incident(
            source_reference=str(item["source_reference"]),
            title=str(item.get("title", "") or ""),
            description=str(item.get("description", "") or ""),
            status=str(item.get("status", "active") or "active"),
            created_at=_parse_dt(item.get("created_at")),
            updated_at=_parse_dt(item.get("updated_at")),
        )
    return from_smax(item)


def _load_backfill_incidents(path: str, since: str | None) -> list[Incident]:
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    items = raw if isinstance(raw, list) else raw.get("incidents", [])
    incidents = []
    for item in items:
        if not isinstance(item, dict):
            continue
        inc = _incident_from_backfill_item(item)
        if not inc.source_reference:
            continue
        if since is not None:
            since_dt = datetime.fromisoformat(since.replace("Z", "+00:00"))
            changed = inc.updated_at or inc.created_at
            if changed is not None and changed < since_dt:
                continue
        incidents.append(inc)
    return incidents


def _drain(outbox: queue.Queue) -> list[str]:
    refs: list[str] = []
    while True:
        try:
            refs.append(outbox.get_nowait())
        except queue.Empty:
            return refs


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="integrations.smax.main",
        description="SMAX ticketing connector — polls SMAX for changed tickets, "
                    "submits them to the classifier's public API, and writes "
                    "classifier suggestions back to SMAX.",
    )
    parser.add_argument(
        "--check", action="store_true",
        help="print the resolved configuration (tokens masked) and exit — no network, no credentials required",
    )
    parser.add_argument(
        "--once", action="store_true",
        help="run exactly one poll tick + one write-back sweep, print stats, and exit",
    )
    parser.add_argument(
        "--backfill", metavar="FILE",
        help="one-shot backfill from a JSON file of incidents "
             "(a list, or {\"incidents\": [...]}); each item is a normalized "
             "incident (source_reference/title/description/status) or a raw SMAX payload",
    )
    parser.add_argument(
        "--since", metavar="ISO",
        help="with --backfill: only incidents whose updated_at/created_at is at or after this timestamp",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    if args.check:
        print(json.dumps(settings.summary(), indent=2, sort_keys=True))
        return 0

    if args.backfill:
        return _run_backfill(args.backfill, args.since)

    try:
        settings.require_smax()
        settings.require_classifier()
    except NotConfiguredError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    smax = SmaxClient(api_url=settings.smax_api_url, token=settings.smax_api_token)
    classifier = ClassifierClient(
        api_url=settings.classifier_api_url, token=settings.classifier_api_token
    )

    if args.once:
        outbox: queue.Queue = queue.Queue()
        poll_stats = poller.run_once(smax, classifier, settings, outbox=outbox)
        refs = _drain(outbox)
        wb_stats = writeback.run_once(smax, classifier, settings, refs)
        print(json.dumps({"poll": poll_stats, "writeback": wb_stats}, indent=2, sort_keys=True))
        return 0

    stop = threading.Event()
    outbox = queue.Queue()
    try:
        poller.start_poller(smax, classifier, settings, outbox=outbox, stop_event=stop)
        writeback.start_writeback(smax, classifier, settings, inbox=outbox, stop_event=stop)
        print(
            f"SMAX connector running — poll every {settings.smax_poll_s}s, "
            f"write-back mode={settings.smax_write_back}, dry_run={settings.smax_dry_run}. "
            "Ctrl-C to stop."
        )
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        stop.set()
        print("\nStopping SMAX connector...", file=sys.stderr)
    return 0


def _run_backfill(path: str, since: str | None) -> int:
    try:
        settings.require_classifier()
    except NotConfiguredError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    try:
        incidents = _load_backfill_incidents(path, since)
    except Exception as exc:  # noqa: BLE001 — file/parse errors are user-facing
        print(f"ERROR: cannot read backfill file {path!r}: {exc}", file=sys.stderr)
        return 1
    if not incidents:
        print("No incidents to backfill (empty file or all filtered by --since).")
        return 0
    classifier = ClassifierClient(
        api_url=settings.classifier_api_url, token=settings.classifier_api_token
    )
    try:
        refs = classifier.backfill(incidents)
    except Exception as exc:  # noqa: BLE001 — network/API errors are user-facing
        print(f"ERROR: backfill failed: {exc}", file=sys.stderr)
        return 1
    print(f"Backfilled {len(refs)} references:")
    for ref in refs:
        print(f"  {ref}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
