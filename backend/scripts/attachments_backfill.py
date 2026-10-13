#!/usr/bin/env python3
"""Attachment extraction — Phase 1 measurement tool.

Runs the reader over a folder of files (or a JSON list of SMAX ticket refs,
later) and reports: counts by quality, top pages, top signal codes, top object
types, % with any signal, cache-hit rate, and per-file latency. It touches
neither the classifier nor the database; `--out` writes `layers.jsonl` and
`report.json`.

    python scripts/attachments_backfill.py --folder ./attachments --out ./report
    python scripts/attachments_backfill.py --folder . --limit 20 --dry-run
    python scripts/attachments_backfill.py --reader-check      # one provider call

Set the reader with the ATTACHMENT_VISION_* env vars first (see .env.example).
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
from ai_classification.services.attachments.normalize import normalize
from ai_classification.services.attachments.provider import build_reader

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
    parser.add_argument("--out", metavar="DIR", help="write layers.jsonl + report.json here")
    parser.add_argument("--reader-check", action="store_true",
                        help="one provider call, then exit (proves the transport)")
    args = parser.parse_args()

    if args.reader_check:
        return _reader_check()

    if not args.folder and not args.list:
        parser.error("give --folder (or --list), or use --reader-check")

    seen: set[str] = set()
    quality: Counter = Counter()
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
    layers_fh = (out_dir / "layers.jsonl").open("w", encoding="utf-8") if out_dir else None

    for path in files[: args.limit] if args.limit else files:
        total += 1
        data = path.read_bytes()
        digest = hashlib.sha256(data).hexdigest()
        cache_hit = digest in seen
        seen.add(digest)
        start = time.monotonic()
        layers = extract_attachment(data, _EXT_MIME[path.suffix.lower()])
        latencies.append(time.monotonic() - start)
        quality[layers.quality] += 1
        if layers.signals:
            with_signal += 1
        if layers.where.page:
            pages[normalize(layers.where.page)] += 1
        for sig in layers.signals:
            if sig.code:
                codes[sig.code] += 1
        for obj in layers.objects:
            obj_types[obj.type or "(empty)"] += 1
        if layers_fh is not None:
            layers_fh.write(json.dumps({
                "file": str(path), "sha256": digest, "cache_hit": cache_hit,
                "quality": layers.quality, "layers": layers.model_dump(),
            }, ensure_ascii=False) + "\n")
    if layers_fh:
        layers_fh.close()

    report = {
        "files": total,
        "unique_files": len(seen),
        "cache_hit_rate": round(1 - len(seen) / max(total, 1), 4),
        "quality": dict(quality),
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
        print(f"\nwrote {out_dir / 'layers.jsonl'} and {out_dir / 'report.json'}")
    print("\nNOTE: ungrounded values dropped are counted by the log — "
          "`grep -c attachment_ungrounded_value` on the run output.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
