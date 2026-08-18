# FULL PROJECT REVIEW & DEPLOY-READINESS — REPORT

Branch: `refactor/deploy-ready` (from main @ 5206195). NOT pushed (per brief).
All numbers below are real command output from this session.

## 1. What was reviewed

Read end-to-end: api/routes.py, api/integration_routes.py, core/{classifier,
store,grouping,llm,suboffering,suboffering_cluster,status_monitor,
import_service}, domain/{models,taxonomy}, seams/{port,pipeline,local_source,
smax/*}, integration/, tests/, compose, nginx, dashboard JS, README, both docs.

## 2. Dead code / cleanup found & fixed

- **6 unused imports** (pyflakes): json in integration/__init__ + core/llm.py,
  ValidationError in integration_routes.py, timezone in local_source.py,
  TicketSource in smax/real_source.py, settings in grouping.py → removed,
  package is pyflakes-clean.
- **FM_AR_LABELS + failure_modes import** in grouping.py → removed (see §4).
- **FM-000 naming** throughout grouping logs/docstrings → OFFERING-000.

## 3. Real-behavior findings (things a test suite would miss)

1. **The data-vanishing bug** (prior session, already fixed): tests ran
   without PG_DATABASE=ai_incidents_test and wiped the main DB; conftest
   now refuses to run against ai_incidents. Re-verified: suite green with
   main DB untouched (92 incidents).
2. **offering_of() split on the FIRST dot** — "7.1 Invoicing and Billing -
   Nusuk Masar Haj.Bill Generation" → "7". The separator is the LAST dot.
   Fixed with rsplit; the old test literally codified the bug
   (assert == "7") — corrected. Live: cluster now named
   "7.1 Invoicing and Billing - Nusuk Masar Haj" (7 tickets).
3. **Verdict-cache leak across tests** — module-global cache keyed by
   incident-ID fingerprint; a coherent verdict cached by one test made a
   later incoherent-verdict test pass for the wrong reason. Autouse
   fixture now clears it.
4. **sim-matrix shadowing (live crash)** — the W2 worker's live sub-offering
   edit did `sub_id, sim = match_against_exemplars(...)`, rebinding the
   92x92 cosine matrix `sim` to a float → rebuild crashed with
   "'float' object is not subscriptable" (04:30 logs, real error). Renamed
   to sub_sim + regression test. Rebuild verified clean live.
5. **E1-E9 flow re-verified live**: ingest → pending → retryable (transient)
   → succeeded; duplicate reference replay-safe (no double ingest); no/
   wrong token → 401; /health + /status open; /ready + /status independent
   per-service; unknown payload field → 422 (contract tests).

## 4. The product-direction work (per user directive)

**Failure-mode layer deleted from the clustering path.** Phase-1 now keys on
OFFERING (service string before the last dot) via offering_of():
- classification_dict.service "X.Bill Generation" → offering "X"
- OFFERING-000 (unresolvable) falls through to Phase-2 embedding
- FM_AR_LABELS + FAILURE_MODES import removed from grouping
- Cluster name/failure_mode_desc = the offering name (human-readable)
- Remaining FM usage (NOT in grouping): the frozen classifier prompt still
  emits failure_mode internally (can't change — prompts frozen per rules),
  and the gated sub-offering engine's compute_member_flags uses FM as an
  internal needs_review signal (manual-script only, kept gated).

**Sub-offering split (W2 worker's live edit, fixed + kept):** Phase-1 now
splits each offering into sub-offering groups via read-only exemplar
matching (match_against_exemplars, MATCH_THRESHOLD 0.60) with a residual
offering cluster for unmatched members — coarse + fine, exactly the product
shape. Uses committed store APIs (list_sub_offerings/list_exemplars).

## 5. Volume-adaptive clustering sensitivity (Section 3)

Design: `_sensitivity_params(active_count)` — pure, deterministic function
of the active incident count:
- <= 20 incidents: threshold 0.40, min cluster 2 (LOOSE — related tickets group)
- >= 150 incidents: threshold 0.60, min cluster 4 (TIGHT — precision)
- between: linear interpolation (verified: 91 → 0.509 / 3)
- Emission floor scales with threshold: loose 0.50, mid 0.70 (= old behavior
  exactly), tight 0.80; float-safe comparison (1e-9).
- Wired through _build_clusters (all MIN_CLUSTER_SIZE / SIMILARITY_THRESHOLD
  references are adaptive now). _cluster_pass takes min_cluster_size.
- README section added (plain language).
- 14 tests: both extremes, interpolation monotonicity/bounds, determinism,
  mid-volume = 0.50/3, empty/single-incident, incoherent rejection,
  regression for the sim-shadowing bug.

Before/after at live volume (92 incidents): 11 FM clusters → 8 offering
clusters:
pilgrim groups and issue permit 19 | inquiry 11 | System/Application 11 |
Between cities 10 | contracts 7 | suggestion 7 | 7.1 Invoicing 7 |
Registration 3. (Matches the historical offering-vs-FM shape: ~88/91
grouped in ~10 buckets.)

## 6. Scenario checklist (all run)

- [x] Empty DB → app boots, /ready reports, build returns empty (test)
- [x] 1 incident → no crash, no cluster (test)
- [x] 5-20 incidents → LOOSE regime: pairs group (test, threshold 0.40/min 2)
- [x] 100+ incidents → TIGHT regime: weak pairs rejected, strong groups kept (test)
- [x] LLM unreachable → E5 real-endpoint test: retryable → flagged, no
      half-write (28.5s, real NXDOMAIN llms.elm.sa)
- [x] Duplicate source_reference → one job, replay safe (live)
- [x] Unknown payload field → 422 INVALID_PAYLOAD (contract tests)
- [x] No/wrong token → 401 everywhere; /health /ready /status open (live)
- [x] Postgres restart → 92 incidents survive, job survives (succeeded),
      API recovers (live, this session)
- [x] Full suite green: **145 passed + 3 xfailed + 4 xpassed** (67.6s)
      (baseline was 131; +14 new adaptive tests, counts shifted by the
      documented flaky canary pairs)

## 7. Deploy-readiness pass

- Config env-only: LLM_MODEL/LLM_API_KEY/LLM_API_BASE/INTEGRATION_API_TOKEN/
  POSTGRES_PASSWORD all from .env; compose fails loud without the password.
- Startup logs resolved config; status monitor logs SERVICE LLM: UNREACHABLE
  loudly + /status endpoint + dashboard red indicator (already shipped).
- No hardcoded secrets in repo (gitignored .env, .env.example documents).
- Docs match endpoints (README /test/all + /test/llm + /status; INTEGRATION
  guide untouched — contract unchanged).
- api image ~16GB (CUDA torch wheel): **recommendation only, not changed** —
  CPU-index torch rebuild is a separate experiment; the build works as-is
  and a failed image swap would block deploy. Flagged for later.

## 8. Not changed (deliberately)

- Frozen classifier + canary prompts (rules).
- compute_member_flags FM usage in the gated sub-offering engine (out of
  scope, manual scripts only; noted for the offering migration follow-up).
- failure_mode field name "failure_mode_desc" in the report (dashboard
  contract; value is now the offering name — renaming is cosmetic-only and
  would touch the frontend; noted).
- OCR service, dashboard, integration contract, seams pipeline shape.

## 9. Commit history (branch refactor/deploy-ready, NOT pushed)

- a5db72e feat(clustering): offering-based Phase-1 + volume-adaptive sensitivity
- 5aa8e57 chore: remove 6 unused imports
- bfe6096 fix(grouping): sub-offering loop shadowed the sim matrix (worker's
  live edit kept + fixed; regression test added)

Per the brief: pushed only after the user says so.
