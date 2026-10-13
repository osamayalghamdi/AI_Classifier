#!/usr/bin/env python3
"""Attachment extraction — per-field accuracy against a hand-labeled sample.

Truth CSV columns (header must match exactly):

    file,page,error_code,object_ids,has_signal

  * `file`            path relative to the sample folder
  * `page`            the expected page name (Arabic or English); several
                      acceptable values separated by `|`; empty = no page
  * `error_code`      one expected error/reference code; empty = none
  * `object_ids`      expected object ids, separated by `;`; empty = none
  * `has_signal`      1 or 0 — whether ANY signal is expected

    python evaluation/attachments_eval.py --folder ./attachments --truth ./attachments_truth.csv

Codes/ids compare after `normalize` (case / space / dash / Arabic-Indic
digits); pages compare after `normalize_page` (adds diacritics + tatweel
removal, alef folding, ة→ه, ى→ي, and an optional leading ال). For `error_code`
and `object_ids` the report splits into correct / missed / invented, because
invented values (extracted but not in the truth) are the main risk.
"""

from __future__ import annotations

import argparse
import csv
import sys
from pathlib import Path

# Allow `python evaluation/attachments_eval.py` from the backend/ directory.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ai_classification.services.attachments.extract import extract_attachment
from ai_classification.services.attachments.normalize import normalize, normalize_page
from ai_classification.services.attachments.provider import MockReader

_EXT_MIME = {
    ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
    ".gif": "image/gif", ".webp": "image/webp", ".bmp": "image/bmp",
    ".pdf": "application/pdf",
}


def _matches_page(layers, expected: str) -> bool:
    options = [o for o in expected.split("|") if o.strip()] if expected.strip() else []
    if not options:
        return True
    got = {normalize_page(layers.where.page or ""),
           normalize_page(layers.where.page_en or "")}
    return any(normalize_page(o) in got for o in options)


def _code_outcome(layers, expected: str) -> tuple[bool, int, int]:
    """(correct, missed, invented) for the error_code field."""
    truth = normalize(expected) if expected.strip() else None
    extracted = {normalize(c) for c in (s.code for s in layers.signals) if c}
    if truth is None:
        return (not extracted, 0, len(extracted))
    missed = 0 if truth in extracted else 1
    invented = len(extracted - {truth})
    return (missed == 0 and invented == 0, missed, invented)


def _object_ids_outcome(layers, expected: str) -> tuple[bool, int, int]:
    """(correct, missed, invented) for the object_ids field."""
    truth = {normalize(x) for x in expected.split(";") if x.strip()}
    extracted = {normalize(o.id) for o in layers.objects if o.id}
    missed = len(truth - extracted)
    invented = len(extracted - truth)
    return (missed == 0 and invented == 0, missed, invented)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--folder", required=True, help="folder of the sample files")
    parser.add_argument("--truth", required=True, help="the hand-labeled CSV")
    parser.add_argument("--mock", action="store_true",
                        help="use the deterministic offline MockReader (no network)")
    args = parser.parse_args()

    root = Path(args.folder)
    with open(args.truth, encoding="utf-8-sig") as fh:
        rows = list(csv.DictReader(fh))
    required = {"file", "page", "error_code", "object_ids", "has_signal"}
    if not rows or not required <= set(rows[0].keys()):
        print(f"ERROR: truth CSV must have header columns {sorted(required)}.")
        return 1

    correct = {"page": 0, "error_code": 0, "object_ids": 0, "has_signal": 0}
    missed = {"error_code": 0, "object_ids": 0}
    invented = {"error_code": 0, "object_ids": 0}
    total = 0
    mismatches: list[str] = []
    reader = MockReader() if args.mock else None

    for row in rows:
        path = root / row["file"].strip()
        if not path.exists():
            mismatches.append(f"{row['file']}: file not found")
            continue
        layers = extract_attachment(path.read_bytes(), _EXT_MIME[path.suffix.lower()],
                                    reader=reader)
        total += 1

        page_ok = _matches_page(layers, row["page"])
        code_ok, code_miss, code_inv = _code_outcome(layers, row["error_code"])
        id_ok, id_miss, id_inv = _object_ids_outcome(layers, row["object_ids"])
        signal_ok = (len(layers.signals) > 0) == (str(row["has_signal"]).strip() == "1")

        correct["page"] += page_ok
        correct["error_code"] += code_ok
        correct["object_ids"] += id_ok
        correct["has_signal"] += signal_ok
        missed["error_code"] += code_miss
        missed["object_ids"] += id_miss
        invented["error_code"] += code_inv
        invented["object_ids"] += id_inv

        if not (page_ok and code_ok and id_ok and signal_ok):
            got = f"page={layers.where.page!r} codes={[s.code for s in layers.signals]} " \
                  f"ids={[o.id for o in layers.objects]} signals={len(layers.signals)}"
            mismatches.append(f"{row['file']}: got {got}")

    print(f"evaluated {total} files")
    print(f"  page         correct: {correct['page']}/{total}")
    print(f"  error_code   correct: {correct['error_code']}/{total}"
          f"   missed: {missed['error_code']}   invented: {invented['error_code']}")
    print(f"  object_ids   correct: {correct['object_ids']}/{total}"
          f"   missed: {missed['object_ids']}   invented: {invented['object_ids']}")
    print(f"  has_signal   correct: {correct['has_signal']}/{total}")
    if mismatches:
        print(f"\n{len(mismatches)} files missed at least one field (first 20):")
        for line in mismatches[:20]:
            print(f"  {line}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
