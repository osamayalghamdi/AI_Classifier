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
