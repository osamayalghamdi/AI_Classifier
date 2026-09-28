# Classifier known issues — observed, not yet acted on

Logged 2026-08-01 after the stage-1 lenient-parse fix (62252fb). Both items
are logged per review; no action taken — validation stays strict.

## KI-1: "Other with high confidence" selection-quality watch

Observed: `Tax billing data update blocked` → `Other` / `General / Unspecified`
with **high** confidence (live classify + eval fixture EV-3). The stage-1 LLM
itself picked Other — the parse is working (not the fallback, which is always
low). The ticket text carries no system alias, but the FM taxonomy maps
tax-billing to `Nusuk Masar Haj / 7.1 Invoicing and Billing`. This is a
selection-quality gap, not a crash.

Watch: if "Other with high confidence" shows up in the labeled eval set more
than occasionally, the fix is prompt/few-shot work (stage-1 system hints),
NOT validation changes. Count it in run_eval.py per-system breakdown.

## KI-2: stage-3 offering separator slip (dash vs dot)

Observed once live: stage 3 returned `pilgrim groups and issue permit - Issue
Permits` (dash separator, system suffix dropped) instead of
`pilgrim groups and issue permit - Nusuk Masar Haj.Issue Permits`. The strict
validator correctly rejected it → cascade fell back to generic Other for that
run (subsequent runs formatted correctly — transient LLM variance).

Deliberately NOT fixed by loosening validation (the strict dot-path check is
correct). If the eval shows several tickets falling back this way, the fix is
a tiny output normalization (accept dash form, store dot form) in the
stage-3 parse path — revisit only if the data supports it.

**CLOSED 2026-09-24 — the data supported it, and the fallback was worse than
assumed.** Reviewing every ticket found 41 of 1505 logged stage-2/3 responses
(2.7%) were service near-misses, dominated by the system suffix written from
memory (`licensing - Nusuk Hajj`, `contracts - Nusuk Hajj Services`, even the
concatenation `licensing - Nusuk Hajj services - Nusuk Masar Haj`). Each one
did not just "fall back for that run": the stage retried, and on a second miss
the WHOLE classification was discarded (system included) and the ticket stored
as `Other / General / Unspecified` with `classification_status="failed"`.

Fix (`services/classify/parsing.py` + `cascade.py`, classifier v3.5):
* `snap_service()` maps an off-taxonomy string onto the real option by
  comparing the service CORE — system-name variants removed, punctuation/case
  normalized, ambiguous cores skipped. All 14 prod near-misses snap; an
  invented service still fails.
* `_parse_stage_service()` keeps the already-resolved SYSTEM and stores the
  ticket on that system's generic service when nothing matches (never a fake
  pick).
* `_cascade_fallback()` no longer replaces a known/pinned system with "Other".
Regression tests: `tests/services/classify/test_service_snap.py`.
