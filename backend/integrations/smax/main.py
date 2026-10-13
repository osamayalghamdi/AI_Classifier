"""SMAX connector entrypoint — python -m integrations.smax.main.

Runs the poller (SMAX → classifier API) and the write-back loop
(classifier API → SMAX suggestions) as two daemon threads, with a clean
Ctrl-C exit. Also supports one-shot modes:

- --check                 print the resolved config (secrets masked) and exit —
                          no network, no credentials required.
- --probe REF             fetch ONE incident from SMAX and show exactly what the
                          connector would send: the mapped fields, the resolved
                          filter, and the ingest body (attachments by name).
                          Nothing is submitted, nothing is written. This is the
                          first-live-run check.
- --probe-raw             with --probe: also print the raw SMAX entity (ticket
                          text included) so field names can be verified.
- --probe-download        with --probe: actually download the attachments (proves
                          the file service and the token work) — still submits
                          nothing.
- --once                  run exactly one poll tick + one write-back sweep,
                          print stats, exit. Add --limit N to submit at most N.
- --limit N               cap submissions for this run (--once, or the daemon
                          loop). Nothing is dropped: the stamp stops before the
                          tickets that were held back and the next tick resumes.
- --backfill F            one-shot historical ingest from a JSON file of
                          incidents via POST /api/v1/backfill (chunked <=200).
                          Optional --since ISO filters the file.

Failure policy: every mode exits 1 with a one-line message on stderr — never a
traceback. A run that cannot reach SMAX or the classifier says so and stops;
nothing is half-applied, and re-running is always safe (the classifier dedupes
by source_reference). This package imports NOTHING from the classifier app.
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

from .classifier_client import ClassifierClient, incident_payload
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
            affected_system=str(item.get("affected_system", "") or ""),
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


def _build_smax_client() -> SmaxClient:
    return SmaxClient(
        base_url=settings.smax_base,
        tenant=settings.smax_tenant,
        username=settings.smax_user,
        password=settings.smax_pass,
        token_ttl=settings.smax_token_ttl_s,
        timeout=settings.smax_timeout_s,
        entity_field=settings.smax_entity_field,
        utc_offset_hours=settings.smax_utc_offset_hours,
        page_size=settings.smax_page_size,
        max_attachment_bytes=int(settings.smax_max_attachment_mb * 1024 * 1024),
    )


def _build_classifier_client() -> ClassifierClient:
    return ClassifierClient(api_url=settings.classifier_api_url,
                            token=settings.classifier_api_token)


def _redacted(value):
    """Replace every value with its shape — keys and types survive, content does not.

    Used by `--probe-raw --probe-redact` so the entity structure can be shared
    (e.g. to fix a field name) without exposing ticket text or file names.
    """
    if isinstance(value, dict):
        return {k: _redacted(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_redacted(value[0]), f"… +{len(value) - 1} more"] if value else []
    if isinstance(value, str):
        # Field NAMES matter, values do not: show the type and the length.
        return f"<str:{len(value)}>"
    if isinstance(value, bool):
        return f"<bool:{value}>"
    if isinstance(value, (int, float)):
        return f"<num:{value}>"
    return value


def _probe(reference: str, *, raw: bool, download: bool, redact: bool = False) -> int:
    """Fetch ONE incident and show what would be sent — no submission at all.

    Proves, against the real tenant: authentication, the search filter, the
    field mapping, and optionally the attachment download. The classifier is
    never contacted, so nothing is created on either side.
    """
    settings.require_smax()
    smax = _build_smax_client()

    print(f"SMAX: {settings.smax_base} (tenant {settings.smax_tenant})")
    try:
        entity = smax.get_ticket(reference)
    except KeyError:
        print(f"ERROR: SMAX returned no incident with Id={reference!r}. Check the id "
              "and that the integration account may see it.", file=sys.stderr)
        return 1
    except Exception as exc:  # noqa: BLE001 — a user-facing failure, never a traceback
        print(f"ERROR: could not fetch incident {reference!r}: {exc}", file=sys.stderr)
        return 1

    incident = from_smax(entity)
    if raw:
        title = "raw SMAX entity, VALUES HIDDEN (keys + shapes)" if redact \
            else "raw SMAX entity (contains ticket text — do not paste this publicly)"
        print(f"--- {title} ---")
        print(json.dumps(_redacted(entity) if redact else entity,
                         indent=2, ensure_ascii=False, sort_keys=True))
        if redact:
            print("(share this block: it shows every field name the tenant sends)")
    if not incident.source_reference and not raw:
        print("WARNING: no Id on this entity — a real run would SKIP it.", file=sys.stderr)

    description = incident.description or ""
    print("--- mapped ---")
    print(json.dumps({
        "source_reference": incident.source_reference,
        "title": incident.title,
        "description_chars": len(description),
        "description_preview": description[:160],
        "status": incident.status,
        "created_at": incident.created_at.isoformat() if incident.created_at else None,
        "updated_at": incident.updated_at.isoformat() if incident.updated_at else None,
        "affected_system": incident.affected_system or "(none — the classifier resolves it)",
    }, indent=2, ensure_ascii=False, sort_keys=True))
    if not incident.source_reference:
        print("WARNING: no Id on this entity — a real run would SKIP it.", file=sys.stderr)

    try:
        metadata = list(smax.iter_attachments(entity))
    except Exception as exc:  # noqa: BLE001 — metadata problems must not stop the probe
        print(f"WARNING: attachment metadata unreadable: {exc}", file=sys.stderr)
        metadata = []
    print(f"--- attachments ({len(metadata)} visible) ---")
    for meta in metadata:
        size = meta.get("expected_size")
        print(f"  {meta['file_name']}  ({meta['mime_type'] or 'unknown type'}, "
              f"{size if size is not None else 'size unknown'} bytes, id={meta['file_id']})")
        if download:
            try:
                raw_bytes = smax.download_file(meta["file_id"])
                print(f"    downloaded {len(raw_bytes)} bytes ✓ (would be sent as base64)")
            except Exception as exc:  # noqa: BLE001
                print(f"    download FAILED: {exc}", file=sys.stderr)

    # The body a real run would post — attachments by name only (no bytes here).
    preview = from_smax(entity)
    preview.attachments = [{"name": m["file_name"],
                            **({"content_type": m["mime_type"]} if m.get("mime_type") else {})}
                           for m in metadata]
    body = incident_payload(preview)
    size = len(json.dumps(body, ensure_ascii=False))
    print("--- ingest body that WOULD be sent (NOT sent) ---")
    print(f"POST {settings.classifier_api_url}/api/v1/incidents   ({size} bytes, no attachments downloaded)")
    print(json.dumps(body, indent=2, ensure_ascii=False, sort_keys=True)[:2000])
    print("\nNothing was submitted. Run with --once --limit 1 to send one real ticket.")
    return 0


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="integrations.smax.main",
        description="SMAX ticketing connector — polls SMAX for changed tickets and "
                    "submits them to the classifier's public API.",
    )
    parser.add_argument(
        "--check", action="store_true",
        help="print the resolved configuration (secrets masked) and exit — no network, no credentials required",
    )
    parser.add_argument(
        "--probe", metavar="REF",
        help="fetch ONE incident from SMAX and show what would be sent — submits nothing "
             "(the first-live-run check)",
    )
    parser.add_argument(
        "--probe-raw", action="store_true",
        help="with --probe: also print the raw SMAX entity (contains ticket text)",
    )
    parser.add_argument(
        "--probe-redact", action="store_true",
        help="with --probe: print the raw entity with VALUES HIDDEN (keys and shapes "
             "only) — safe to share when a field name needs fixing",
    )
    parser.add_argument(
        "--probe-download", action="store_true",
        help="with --probe: download the attachments too (proves the file service works)",
    )
    parser.add_argument(
        "--once", action="store_true",
        help="run exactly one poll tick + one write-back sweep, print stats, and exit",
    )
    parser.add_argument(
        "--limit", type=int, metavar="N",
        help="submit at most N tickets (with --once, or for the daemon loop). "
             "Nothing is dropped — the next tick resumes where this one stopped",
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

    try:
        return _dispatch(args)
    except NotConfiguredError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 0
    except Exception as exc:  # noqa: BLE001 — one line, never a traceback
        print(f"ERROR: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1


def _dispatch(args) -> int:
    if args.probe:
        return _probe(args.probe, raw=args.probe_raw or args.probe_redact,
                      download=args.probe_download, redact=args.probe_redact)

    if args.backfill:
        return _run_backfill(args.backfill, args.since)

    settings.require_smax()
    settings.require_classifier()

    smax = _build_smax_client()
    classifier = _build_classifier_client()

    if args.once:
        outbox: queue.Queue = queue.Queue()
        poll_stats = poller.run_once(smax, classifier, settings, outbox=outbox,
                                     limit=args.limit)
        refs = _drain(outbox)
        wb_stats = writeback.run_once(smax, classifier, settings, refs)
        print(json.dumps({"poll": poll_stats, "writeback": wb_stats}, indent=2, sort_keys=True))
        return 0

    if args.limit:
        # A CLI cap is for one-shot runs; the daemon uses SMAX_MAX_INCIDENTS_PER_TICK.
        print("NOTE: --limit only applies to --once; the daemon honours "
              "SMAX_MAX_INCIDENTS_PER_TICK.", file=sys.stderr)

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
    settings.require_classifier()
    try:
        incidents = _load_backfill_incidents(path, since)
    except Exception as exc:  # noqa: BLE001 — file/parse errors are user-facing
        print(f"ERROR: cannot read backfill file {path!r}: {exc}", file=sys.stderr)
        return 1
    if not incidents:
        print("No incidents to backfill (empty file or all filtered by --since).")
        return 0
    classifier = _build_classifier_client()
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
