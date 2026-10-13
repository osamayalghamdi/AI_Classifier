#!/usr/bin/env python3
"""Attachment extraction — per-field accuracy against a hand-labeled sample.

Truth CSV columns (header must match exactly):

    file,page,error_code,object_ids,has_signal

  * `file`            path relative to the sample folder
  * `page`            the expected page name (Arabic or English); empty = no page
  * `error_code`      one expected error/reference code; empty = none
  * `object_ids`      expected object ids, separated by `;`; empty = none
  * `has_signal`      1 or 0 — whether ANY signal is expected

    python evaluation/attachments_eval.py --folder ./attachments --truth ./attachments_truth.csv

All comparisons are exact after normalization (the same normal form the
grounding check uses), so `TX-778201` matches `tx 778201`.
"""

from __future__ import annotations

import argparse
import csv
import sys
from pathlib import Path

# Allow `python evaluation/attachments_eval.py` from the backend/ directory.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ai_classification.services.attachments.extract import extract_attachment
from ai_classification.services.attachments.normalize import normalize

_EXT_MIME = {
    ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
    ".gif": "image/gif", ".webp": "image/webp", ".bmp": "image/bmp",
    ".pdf": "application/pdf",
}


def _matches_page(layers, expected: str) -> bool:
    want = normalize(expected)
    if not want:
        return True
    return want in {normalize(layers.where.page or ""), normalize(layers.where.page_en or "")}


def _matches_code(layers, expected: str) -> bool:
    want = normalize(expected)
    if not want:
        return not any(s.code for s in layers.signals)
    return any(s.code and normalize(s.code) == want for s in layers.signals)


def _object_ids_match(layers, expected: str) -> bool:
    want = {normalize(x) for x in expected.split(";") if x.strip()}
    got = {normalize(o.id) for o in layers.objects if o.id}
    return got == want


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--folder", required=True, help="folder of the sample files")
    parser.add_argument("--truth", required=True, help="the hand-labeled CSV")
    args = parser.parse_args()

    root = Path(args.folder)
    rows = list(csv.DictReader(open(args.truth, encoding="utf-8-sig")))
    required = {"file", "page", "error_code", "object_ids", "has_signal"}
    if not rows or not required <= set(rows[0].keys()):
        print(f"ERROR: truth CSV must have header columns {sorted(required)}.")
        return 1

    fields = {"page": 0, "error_code": 0, "object_ids": 0, "has_signal": 0}
    correct = {k: 0 for k in fields}
    total = 0
    mismatches: list[str] = []

    for row in rows:
        path = root / row["file"].strip()
        if not path.exists():
            mismatches.append(f"{row['file']}: file not found")
            continue
        layers = extract_attachment(path.read_bytes(), _EXT_MIME[path.suffix.lower()])
        total += 1
        ok = {
            "page": _matches_page(layers, row["page"]),
            "error_code": _matches_code(layers, row["error_code"]),
            "object_ids": _object_ids_match(layers, row["object_ids"]),
            "has_signal": (len(layers.signals) > 0) == (str(row["has_signal"]).strip() == "1"),
        }
        for name, passed in ok.items():
            if passed:
                correct[name] += 1
        if not all(ok.values()):
            got = f"page={layers.where.page!r} codes={[s.code for s in layers.signals]} " \
                  f"ids={[o.id for o in layers.objects]} signals={len(layers.signals)}"
            mismatches.append(f"{row['file']}: got {got}")

    print(f"evaluated {total} files")
    for name in fields:
        print(f"  {name:<12} accuracy: {correct[name]}/{total} "
              f"({round(100 * correct[name] / max(total, 1), 1)}%)")
    if mismatches:
        print(f"\n{len(mismatches)} files missed at least one field (first 20):")
        for line in mismatches[:20]:
            print(f"  {line}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
