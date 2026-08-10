# TASK: Fix cascade stage-1 validation bug (root cause of generic-fallback cascade)

## The bug (verified, reproduce it yourself)

`classify()` → `_classify_cascade()` → stage 1 (`_stage_system_llm`) returns the
generic fallback (`Other/General/Unspecified/low`) for nearly every ticket that
needs LLM system resolution. Root cause is NOT the LLM:

1. `_stage_system_llm` (classifier.py:442) tells the LLM (stage-1 rules):
   "service: give your best **provisional** guess as a single string (it will be
   refined in stage 2)".
2. But it parses with `_parse_and_validate` (classifier.py:238) which runs
   `ClassificationResult.model_validate(...)` — and `ClassificationResult` has a
   `model_validator` (`_check_service_in_system`, domain/models.py:40) that
   REJECTS any `service` not in the taxonomy for the chosen `affected_system`
   (raises ValueError).
3. So the LLM's provisional service (e.g. "Rawdah Permit Issuance") fails
   validation → `_stage_system_llm` catches, returns None → caller returns
   `_cascade_fallback(title, "system resolution failed")`.

Proof (run it): `classify("Rawdah permit error", "User cannot book Rawdah
permit for 30 May")` → raw LLM response is VALID JSON with
`affected_system: Nusuk Masar Haj` (no reasoning tokens, no fence), yet stage-1
returns None and the result is Other/General/low.

The eval harness's 5-fixture run (all rows → Other/General) is the same bug.

## Fix

Stage 1 only needs `affected_system` — it must NOT run the full taxonomy
service validator. Implement a lenient parse for stage 1:

- Add a helper, e.g. `_parse_stage_system(raw: str) -> ClassificationResult | None`:
  - strip fences (strip_json_fences), json.loads
  - validate ONLY `affected_system` against the 4 AffectedSystem values
    (invalid or missing → return None)
  - build a minimal ClassificationResult: use the LLM's other fields if they
    validate, but at minimum set `affected_system`; if the LLM's `service` is
    not valid for that system, replace it with the system's first service or
    "General / Unspecified" (see below) and keep confidence from the LLM
  - NEVER let the taxonomy service validator reject the whole stage.
- The simplest robust implementation: construct `ClassificationResult` with
  `service` coerced to a valid value when the LLM's guess is not in the
  taxonomy (e.g. fall back to the first service of the chosen system, or
  "General / Unspecified" if the system is Other). Keep the LLM's
  incident_type/severity/urgency/category/reasoning/canonical/signature/failure_mode.
  If pydantic still rejects (e.g. bad enum), return None.
- Then `_stage_system_llm` returns that leniently-parsed result (stage 2 will
  replace `service` with a real taxonomy service anyway).
- Do NOT change `_parse_and_validate` or `ClassificationResult` itself — the
  strict validator is correct for stages 2/3 and single-shot.

## Constraints

- Do not touch: prompts (including stage-1 rules), taxonomy, thresholds,
  grouping.py, store, dedupe. Only the stage-1 parse path.
- temperature=0.0, seed=42 unchanged.
- classify() must never raise.

## Verify (paste ALL real output)

1. `classify("Rawdah permit error", "...")` → affected_system=Nusuk Masar Haj
   (NOT Other), confidence from LLM (high), service will be refined by stage 2.
2. Try 3-4 more tickets that need LLM system resolution (no "haj"/"umrah"/
   "oldsm" alias in text, e.g. "CRM portal slow", "Appeal submission fails").
   None should return Other/General fallback.
3. The eval fixture: `PYTHONPATH=. uv run python evaluation/run_eval.py --csv
   <your 5 fake fixture rows CSV>` → at least most rows now get a real system
   instead of Other/General. Paste the summary + one row's per-level scoring.
4. `uv run pytest tests/ -q` → paste summary. If the cascade tests
   (tests/test_cascade.py) assert stage-1 returns None for non-taxonomy
   services, they encoded the BUG — fix them to assert the lenient behavior
   and say exactly which tests you changed and why.

## Branch

Create `fix-cascade-stage1-parse` off `task3-load-test` (7f180cd). Commit:
"fix(cascade): stage-1 lenient parse — provisional service no longer fails
validation". One commit. Report files changed + all verification output.
