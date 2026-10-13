#!/usr/bin/env python3
"""Attachment extraction — measurement tool.

Runs the reader over a folder of files and reports AGGREGATES ONLY by default:
counts by quality/status, top pages, top signal codes, top object types, %
with any signal, cache-hit rate, and per-file latency. It touches neither the
classifier nor the database.

    python scripts/attachments_backfill.py --folder ./attachments --out ./report
    python scripts/attachments_backfill.py --folder . --limit 20 --dry-run
    python scripts/attachments_backfill.py --reader-check          # one provider call
    python scripts/attachments_backfill.py --folder . --out r --transcripts

`--out` writes only the aggregate `report.json`. Per-file transcription output
is opt-in behind `--transcripts` (implies `--out`) and is ALWAYS masked for
personal data (names, national ID / iqama / passport by context, phones,
emails) — error codes and technical IDs are never masked.

Set the reader with the ATTACHMENT_VISION_* env vars first (see .env.example),
or pass `--mock` for an offline deterministic run.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import sys
import time
from collections import Counter
from pathlib import Path

# Allow `python scripts/attachments_backfill.py` from the backend/ directory.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ai_classification.services.attachments.extract import extract_attachment
from ai_classification.services.attachments.masking import mask_personal_data
from ai_classification.services.attachments.normalize import normalize
from ai_classification.services.attachments.provider import MockReader, build_reader

_EXT_MIME = {
    ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
    ".gif": "image/gif", ".webp": "image/webp", ".bmp": "image/bmp",
    ".pdf": "application/pdf",
}
# A valid 1x1 transparent PNG — the reader-check sends this to prove auth+transport.
_ONE_PX_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=="
)


def _iter_files(folder: Path):
    for path in sorted(folder.rglob("*")):
        if path.is_file() and path.suffix.lower() in _EXT_MIME:
            yield path


def _reader_check() -> int:
    """One provider call against the configured endpoint, proving the transport."""
    from ai_classification.shared.config import settings
    if not settings.attachment_reader_configured():
        print("ERROR: the vision reader is not configured "
              "(ATTACHMENT_VISION_API_URL / _MODEL / _API_KEY).")
        return 1
    reader = build_reader()
    print(f"reader: {settings.attachment_vision_model} @ "
          f"{settings.attachment_vision_api_url}")
    start = time.monotonic()
    try:
        layers = reader.read(_ONE_PX_PNG, "image/png")
    except Exception as exc:  # noqa: BLE001 — this is the report the user asked for
        print(f"FAILED after {time.monotonic() - start:.1f}s: {type(exc).__name__}: {exc}")
        return 1
    print(f"OK in {time.monotonic() - start:.1f}s — quality={layers.quality}, "
          f"summary={layers.summary_en!r}")
    print("  (a 1x1 image, so an 'unreadable' answer is fine — this proves the "
          "endpoint, the key and the JSON format all work.)")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--folder", help="folder of image/pdf files to extract")
    parser.add_argument("--list", metavar="FILE", help="JSON list of ticket refs (Phase 4)")
    parser.add_argument("--limit", type=int, help="stop after N files")
    parser.add_argument("--dry-run", action="store_true", help="print what would run")
    parser.add_argument("--out", metavar="DIR", help="write aggregate report.json here")
    parser.add_argument("--transcripts", action="store_true",
                        help="also write per-file masked transcripts.jsonl (implies --out)")
    parser.add_argument("--reader-check", action="store_true",
                        help="one provider call, then exit (proves the transport)")
    parser.add_argument("--mock", action="store_true",
                        help="use the deterministic offline MockReader (no network)")
    args = parser.parse_args()

    if args.reader_check:
        return _reader_check()

    if not args.folder and not args.list:
        parser.error("give --folder (or --list), or use --reader-check")
    if args.transcripts and not args.out:
        parser.error("--transcripts requires --out")

    seen: set[str] = set()
    quality: Counter = Counter()
    status: Counter = Counter()
    pages: Counter = Counter()
    codes: Counter = Counter()
    obj_types: Counter = Counter()
    with_signal = 0
    total = 0
    latencies: list[float] = []

    files = list(_iter_files(Path(args.folder))) if args.folder else []
    if args.dry_run:
        print(f"would extract {len(files)} files:")
        for f in files[:20]:
            print(f"  {f}")
        return 0

    out_dir = Path(args.out) if args.out else None
    if out_dir:
        out_dir.mkdir(parents=True, exist_ok=True)
    transcripts_fh = None
    if args.transcripts and out_dir:
        transcripts_fh = (out_dir / "transcripts.jsonl").open("w", encoding="utf-8")
    reader = MockReader() if args.mock else None

    for path in files[: args.limit] if args.limit else files:
        total += 1
        data = path.read_bytes()
        digest = hashlib.sha256(data).hexdigest()
        cache_hit = digest in seen
        seen.add(digest)
        start = time.monotonic()
        layers = extract_attachment(data, _EXT_MIME[path.suffix.lower()], reader=reader)
        latencies.append(time.monotonic() - start)
        quality[layers.quality] += 1
        status[layers.status] += 1
        if layers.signals:
            with_signal += 1
        if layers.where.page:
            pages[normalize(layers.where.page)] += 1
        for sig in layers.signals:
            if sig.code:
                codes[sig.code] += 1
        for obj in layers.objects:
            obj_types[obj.type or "(empty)"] += 1
        if transcripts_fh is not None:
            transcripts_fh.write(json.dumps({
                "file": str(path), "sha256": digest, "cache_hit": cache_hit,
                "quality": layers.quality, "status": layers.status,
                # text is masked for personal data; codes/ids are technical and
                # stay in the clear — never mask error codes or technical IDs.
                "text": mask_personal_data(layers.text),
                "codes": [s.code for s in layers.signals if s.code],
                "ids": [o.id for o in layers.objects if o.id],
            }, ensure_ascii=False) + "\n")
    if transcripts_fh:
        transcripts_fh.close()

    report = {
        "files": total,
        "unique_files": len(seen),
        "cache_hit_rate": round(1 - len(seen) / max(total, 1), 4),
        "quality": dict(quality),
        "status": dict(status),
        "pct_with_signal": round(with_signal / max(total, 1), 4),
        "avg_latency_s": round(sum(latencies) / max(len(latencies), 1), 2),
        "top_pages": pages.most_common(50),
        "top_signal_codes": codes.most_common(50),
        "top_object_types": obj_types.most_common(50),
    }
    print(json.dumps(report, indent=2, ensure_ascii=False))
    if out_dir:
        (out_dir / "report.json").write_text(json.dumps(report, indent=2, ensure_ascii=False),
                                             encoding="utf-8")
        wrote = f"{out_dir / 'report.json'}"
        if transcripts_fh:
            wrote += f" and {out_dir / 'transcripts.jsonl'} (masked)"
        print(f"\nwrote {wrote}")
    print("\nNOTE: ungrounded values dropped are counted by the log — "
          "`grep -c attachment_ungrounded_value` on the run output.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
