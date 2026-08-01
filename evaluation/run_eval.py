#!/usr/bin/env python3
"""Ground-truth evaluation harness for the AI Incident Classifier.

Runs tickets from a human-labeled CSV through the LIVE classifier and scores
predicted vs user-selected vs truth at three granularities: system, service,
offering.

CSV columns (exactly):
    ticket_id, title, description,
    user_selected_system, user_selected_service,
    true_system, true_service

Truth columns MUST be filled by a human. The harness never fabricates labels:
rows with empty truth are skipped with a warning (they are reported, not
guessed). Lines starting with '#' are treated as comments and ignored.

Scoring rules (per row, at each level):
  - system match      : predicted affected_system == true_system (strip/compare)
  - service match     : the SERVICE PART of the value matches the service part
                        of true_service. Dot-path values ('Service.Offering')
                        are split on the longest known taxonomy service key —
                        service names themselves may contain dots
                        ('7.1 Invoicing and Billing - Nusuk Masar Haj'),
                        offerings never do.
  - offering match    : STRICTER — full 'Service.Offering' string equals
                        true_service exactly. Only counted for rows where the
                        truth value actually has an offering part.

Aggregates: overall + per true_system, for LLM and for the user, with a
head-to-head delta ('users X% correct, LLM Y% correct').

Usage:
    PYTHONPATH=. uv run python evaluation/run_eval.py --csv evaluation/ground_truth.csv
    PYTHONPATH=. uv run python evaluation/run_eval.py --csv labels.csv --out results.csv
    PYTHONPATH=. uv run python evaluation/run_eval.py --csv labels.csv --dry-run   # 0 LLM calls

The classifier runs with CASCADE_CLASSIFICATION as configured (default true —
coarse-to-fine system -> service -> offering cascade). The header of every
run states which mode was used.
"""

from __future__ import annotations

import argparse
import csv
import logging
import sys
from collections import defaultdict

from ai_classification.config import settings
from ai_classification.core.classifier import classify
from ai_classification.domain.taxonomy import SERVICES_BY_SYSTEM

REQUIRED_COLS = [
    "ticket_id",
    "title",
    "description",
    "user_selected_system",
    "user_selected_service",
    "true_system",
    "true_service",
]

_log = logging.getLogger("eval")


# ── CSV helpers ─────────────────────────────────────────────────────────


def read_rows(path: str) -> tuple[list[dict], list[str]]:
    """Read a CSV, skipping '#' comment lines. Returns (rows, missing_cols)."""
    with open(path, newline="", encoding="utf-8-sig") as fh:
        reader = csv.reader(fh)
        raw_rows = [row for row in reader if row and not row[0].strip().startswith("#")]
    if not raw_rows:
        return [], REQUIRED_COLS
    header = [h.strip() for h in raw_rows[0]]
    missing = [c for c in REQUIRED_COLS if c not in header]
    if missing:
        return [], missing
    idx = {c: header.index(c) for c in REQUIRED_COLS}
    rows = [
        {c: (row[idx[c]].strip() if idx[c] < len(row) else "") for c in REQUIRED_COLS}
        for row in raw_rows[1:]
    ]
    return rows, []


def norm(value: str | None) -> str:
    return (value or "").strip()


# ── Dot-path splitting ──────────────────────────────────────────────────


def split_service(value: str) -> tuple[str, str | None]:
    """Split 'Service.Offering' into (service_part, offering_part | None).

    Uses the longest known taxonomy service key as a prefix (the same rule as
    ClassificationResult's validator) so service names containing dots
    ('7.1 Invoicing and Billing - Nusuk Masar Haj.Bill Payment' ->
    ('7.1 Invoicing and Billing - Nusuk Masar Haj', 'Bill Payment')) split
    correctly. Unknown values are returned whole with no offering part.
    """
    value = norm(value)
    if not value:
        return "", None
    best: str | None = None
    for services in SERVICES_BY_SYSTEM.values():
        for svc in services:
            if value == svc:
                return svc, None
            if value.startswith(svc + "."):
                if best is None or len(svc) > len(best):
                    best = svc
    if best is not None:
        return best, value[len(best) + 1:]
    return value, None


def has_offering(value: str) -> bool:
    _, offering = split_service(value)
    return offering is not None


# ── Per-row scoring ─────────────────────────────────────────────────────


def score_row(row: dict, llm_system: str, llm_service: str) -> dict:
    """Compute match flags for one row.

    Returns a dict with llm_*/user_* match flags. Offering flags are None
    (excluded from the offering denominator) when the truth value has no
    offering part.
    """
    truth_system = norm(row["true_system"])
    truth_service = norm(row["true_service"])
    user_system = norm(row["user_selected_system"])
    user_service = norm(row["user_selected_service"])

    truth_svc_part, _ = split_service(truth_service)
    llm_svc_part, _ = split_service(llm_service)
    user_svc_part, _ = split_service(user_service)

    truth_offering = has_offering(truth_service)

    return {
        "llm_system_match": 1 if llm_system == truth_system else 0,
        "llm_service_match": 1 if llm_svc_part and llm_svc_part == truth_svc_part else 0,
        "llm_offering_match": (
            1 if llm_service == truth_service else 0
        ) if truth_offering else None,
        "user_system_match": 1 if user_system == truth_system else 0,
        "user_service_match": 1 if user_svc_part and user_svc_part == truth_svc_part else 0,
        "user_offering_match": (
            1 if user_service == truth_service else 0
        ) if truth_offering else None,
    }


# ── Aggregation ─────────────────────────────────────────────────────────


def pct(n: int, d: int) -> float:
    return (n / d * 100.0) if d else 0.0


def aggregate(rows: list[tuple[dict, dict]]) -> dict:
    """rows = [(row, flags), ...]. Returns overall + per-system metrics."""
    def acc(flags: list[dict], key: str) -> tuple[int, int]:
        vals = [f[key] for f in flags if f[key] is not None]
        return sum(vals), len(vals)

    def metrics(flags: list[dict]) -> dict:
        out = {}
        for key, label in (
            ("llm_system_match", "llm_system"),
            ("llm_service_match", "llm_service"),
            ("llm_offering_match", "llm_offering"),
            ("user_system_match", "user_system"),
            ("user_service_match", "user_service"),
            ("user_offering_match", "user_offering"),
        ):
            n, d = acc(flags, key)
            out[label] = {"n": n, "d": d, "pct": pct(n, d)}
        return out

    all_flags = [flags for _, flags in rows]
    overall = metrics(all_flags)

    by_system: dict[str, list[dict]] = defaultdict(list)
    for row, flags in rows:
        by_system[norm(row["true_system"])].append(flags)

    per_system = {
        system: metrics(flags)
        for system, flags in sorted(by_system.items())
    }
    return {"overall": overall, "per_system": per_system, "total": len(rows)}


# ── Reporting ───────────────────────────────────────────────────────────


def fmt_cell(m: dict) -> str:
    if m["d"] == 0:
        return "   n/a  "
    return f"{m['pct']:5.1f}% ({m['n']}/{m['d']})"


def print_summary(stats: dict, csv_path: str) -> None:
    o = stats["overall"]
    print("=" * 78)
    print(f"EVAL SUMMARY — {csv_path}")
    print(f"  cascade_classification: {'ON' if settings.cascade_classification else 'OFF'}")
    print(f"  rows scored: {stats['total']}")
    print()
    print("OVERALL")
    print(f"  LLM : system {fmt_cell(o['llm_system'])} | "
          f"service {fmt_cell(o['llm_service'])} | "
          f"offering {fmt_cell(o['llm_offering'])}")
    print(f"  User: system {fmt_cell(o['user_system'])} | "
          f"service {fmt_cell(o['user_service'])} | "
          f"offering {fmt_cell(o['user_offering'])}")
    print()
    print("HEAD-TO-HEAD")
    print(f"  system : users {pct(o['user_system']['n'], o['user_system']['d']):.1f}% correct, "
          f"LLM {pct(o['llm_system']['n'], o['llm_system']['d']):.1f}% correct "
          f"(Δ {pct(o['llm_system']['n'], o['llm_system']['d']) - pct(o['user_system']['n'], o['user_system']['d']):+.1f})")
    print(f"  service: users {pct(o['user_service']['n'], o['user_service']['d']):.1f}% correct, "
          f"LLM {pct(o['llm_service']['n'], o['llm_service']['d']):.1f}% correct "
          f"(Δ {pct(o['llm_service']['n'], o['llm_service']['d']) - pct(o['user_service']['n'], o['user_service']['d']):+.1f})")
    print()
    print("PER-SYSTEM (grouped by true_system)")
    hdr = (f"{'system':<32} {'N':>3}  {'LLM sys':>12} {'LLM svc':>12} {'LLM off':>12} "
           f"{'User sys':>12} {'User svc':>12} {'User off':>12}")
    print(hdr)
    print("-" * len(hdr))
    for system, m in stats["per_system"].items():
        print(f"{system[:32]:<32} {m['llm_system']['d']:>3}  "
              f"{fmt_cell(m['llm_system']):>12} {fmt_cell(m['llm_service']):>12} {fmt_cell(m['llm_offering']):>12} "
              f"{fmt_cell(m['user_system']):>12} {fmt_cell(m['user_service']):>12} {fmt_cell(m['user_offering']):>12}")
    print("=" * 78)


OUT_COLS = [
    "ticket_id", "title",
    "llm_system", "llm_service",
    "user_selected_system", "user_selected_service",
    "true_system", "true_service",
    "llm_system_match", "llm_service_match", "llm_offering_match",
    "user_system_match", "user_service_match", "user_offering_match",
]


def write_results(rows: list[tuple[dict, dict]], path: str) -> None:
    with open(path, "w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=OUT_COLS)
        writer.writeheader()
        for row, flags in rows:
            writer.writerow({
                "ticket_id": row["ticket_id"],
                "title": row["title"],
                "llm_system": row["_llm_system"],
                "llm_service": row["_llm_service"],
                "user_selected_system": row["user_selected_system"],
                "user_selected_service": row["user_selected_service"],
                "true_system": row["true_system"],
                "true_service": row["true_service"],
                **{k: ("" if v is None else v) for k, v in flags.items()},
            })


# ── Dry-run validation ──────────────────────────────────────────────────


def dry_run(rows: list[dict], csv_path: str) -> int:
    print("=" * 78)
    print(f"DRY-RUN — {csv_path} (no LLM calls)")
    print(f"  columns: {', '.join(REQUIRED_COLS)}")
    print(f"  data rows: {len(rows)}")
    problems = 0
    for i, row in enumerate(rows, 1):
        issues = []
        if not norm(row["title"]):
            issues.append("empty title")
        if not norm(row["description"]):
            issues.append("empty description")
        if not norm(row["user_selected_system"]) or not norm(row["user_selected_service"]):
            issues.append("empty user_selected_*")
        if not norm(row["true_system"]) or not norm(row["true_service"]):
            issues.append("EMPTY TRUTH -> row will be SKIPPED in a real run")
        if issues:
            problems += 1
            print(f"  row {i} ({norm(row['ticket_id']) or '-'}): {', '.join(issues)}")
    print(f"  rows with issues: {problems}/{len(rows)}")
    print(f"  LLM calls made: 0")
    print("=" * 78)
    return 0


# ── Main ────────────────────────────────────────────────────────────────


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--csv", required=True, help="Path to the labeled CSV")
    parser.add_argument("--out", default=None, help="Output per-ticket results CSV (default: <csv stem>_results.csv)")
    parser.add_argument("--dry-run", action="store_true", help="Validate CSV shape with ZERO LLM calls")
    args = parser.parse_args(argv)

    rows, missing = read_rows(args.csv)
    if missing:
        print(f"ERROR: missing required columns: {', '.join(missing)}", file=sys.stderr)
        return 1

    if args.dry_run:
        return dry_run(rows, args.csv)

    if not rows:
        print("ERROR: no data rows in CSV", file=sys.stderr)
        return 1

    out_path = args.out or args.csv.rsplit(".", 1)[0] + "_results.csv"

    print(f"Classifying {len(rows)} rows through the LIVE classifier "
          f"(cascade={'ON' if settings.cascade_classification else 'OFF'}, "
          f"model={settings.llm_model}, temperature=0.0/seed=42) ...")

    scored: list[tuple[dict, dict]] = []
    skipped = 0
    for i, row in enumerate(rows, 1):
        if not norm(row["true_system"]) or not norm(row["true_service"]):
            _log.warning("Skipping row %s — no truth labels", norm(row["ticket_id"]) or i)
            skipped += 1
            continue
        result = classify(norm(row["title"]), norm(row["description"]))
        llm_system = norm(result.affected_system.value)
        llm_service = norm(result.service)
        row["_llm_system"] = llm_system
        row["_llm_service"] = llm_service
        flags = score_row(row, llm_system, llm_service)
        scored.append((row, flags))
        print(f"  [{i}/{len(rows)}] {norm(row['ticket_id']) or '?':<10} "
              f"LLM={llm_system}/{llm_service}")

    if skipped:
        print(f"SKIPPED {skipped} row(s) without truth labels — they are NOT scored.")

    if not scored:
        print("ERROR: no rows had truth labels — nothing to score.", file=sys.stderr)
        return 2

    stats = aggregate(scored)
    print_summary(stats, args.csv)
    write_results(scored, out_path)
    print(f"Per-ticket results: {out_path}")
    return 0


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    sys.exit(main())
