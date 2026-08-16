# DEPLOY_STATUS.md — feat/deploy-integration-ready

Manager: Agent M. Updated by manager only. All numbers verified by manager.

## Plan (written BEFORE dispatch)

Phase order: W1 (infra rebuild) → W2 (ticketing seams) → W3 (integration endpoints).
Worktree isolation: one worker per worktree, branch `feat/deploy-integration-ready` for all.

### Baseline (manager-verified, 2026-08-11)
- DB: 96 incidents, 96 with embeddings, 1 sub_offering, 8 proposals (vol: ai_classifier_pgdata, container ai_pg)
- Containers: 3 stacks coexist (ai_classifier-*, w4_demo_*, standalone ai_pg) — 11 project containers, 5 images (16GB wt_docker api image!), 6 volumes
- LLM: .env → openai/qwen3.6 @ llms.elm.sa (company). **NXDOMAIN from this dev box** (user: resolves on target server only)
- Tests: 104 pass + flaky strict canary pair (appeals boundary class, passes on re-run)
- Ticketing touchpoints: sync.py (poller), classifier.py (dedupe), config.py

### Manager findings / decisions
1. **LLM cutover risk (D4)**: canary must run against the company model — NOT possible from this box (no DNS).
   Decision: W1 wires explicit config (fail-loud if missing), runs canary against REACHABLE endpoint, reports deviation as information. No prompt tuning to compensate.
2. **Re-seed command doesn't exist yet** → W1 must build it (import test_incidents.json → embeddings regenerate; record timing).
3. **Stack sprawl**: 3 container stacks — W1 teardown must remove ALL project containers/images/volumes, rebuild from repo compose only.
4. **16GB image** from wt_docker — will be rebuilt; note as size observation.
5. Sub-offering clustering stays DISABLED throughout.

## Gates

| Phase | Gate | Status | Evidence |
|---|---|---|---|
| W1 | D0 re-seed reproducible | pending | |
| W1 | D1 single-command rebuild | pending | |
| W1 | D2 explicit config logged at startup | pending | |
| W1 | D3 missing LLM config → fail loud | pending | |
| W1 | D4 canary 22/22+12/12 or deviation | pending | |
| W1 | D5 full suite passes on rebuilt stack | pending | |
| W1 | D6 health 200 + counts match baseline | pending | |
| W2 | S1-S6 (adapter containment, result object, idempotency, provenance) | pending | |
| W3 | E1-E9 (async ingest, readiness, auth, dry-run, guide) | pending | |

## W1 — DONE (merged 3f4bca9, verified by manager)

- D0: reseed reproducible — scripts/reseed.sh, 100 tickets → 91 stored (9 dedup), 492s re-embedding. Baseline: 91/91/0/0 reproducible (96/96/1/8 included 5 manual + prior experiment rows).
- D1: single command — `docker compose up -d --build` after `down -v` (all stacks torn down, orphaned removed).
- D2: startup log: "model=openrouter/qwen/qwen3.6-35b-a3b, api_base=(provider default), db=postgres:5432/ai_incidents, embedding_model=BAAI/bge-m3" (manager-verified).
- D3: fail-loud — RuntimeError on missing LLM_MODEL and on openrouter-without-key (manager re-ran both).
- D4: company endpoint NXDOMAIN from dev box → canary on OpenRouter qwen3.6: 8p+6x+1xp (22/22 wrong→NO held). DEVIATION (env, not code).
- D5: full suite 104p+5x+2x on rebuilt stack.
- D6: health 200, 91/91 reseeded on fresh volume. sub_offerings/proposals = 0 (clustering disabled per brief).
- NOTE: W1 hit port conflict from resurrected old stack (external agent) — diagnosed, resolved; run agent stood down by manager.

## D4 — DEFERRED, NOT PASSED (manager correction)

The D4 canary ran on OpenRouter qwen3.6 — the SAME model everything was already tuned on. It proves no regression from the rebuild (worth having) but says NOTHING about the company-hosted model. The entire risk that gate existed to catch is still open.

**Hard precondition for the first run against the real endpoint (llms.elm.sa):**
1. Canary BEFORE any live traffic.
2. If it deviates: STOP, report, manager decides. No prompt tuning to compensate.
This is recorded so D4 is never quietly remembered as green.

Related: D2's resolved-model log (openrouter/qwen/qwen3.6-35b-a3b) is correct for the dev box. The D3 fail-loud work has NOT yet been exercised against the endpoint it was built for. Do not conflate the two.

## W2 — DONE (merged, verified by manager)

- S1 port: TicketSource (fetch_ticket/fetch_attachments/list_changed/write_back) in ai_classification/seams/port.py
- S2 impls: local_source.py (store-backed, 91 incidents) + real_source.py (NotConfiguredError until TICKETING_API_TOKEN)
- S3 boundary: Incident model (source_reference/title/description/attachments/timestamps); translation only in adapters
- S4 entry: process_incident(Incident) -> PipelineResult; sync.py/batch/manual = thin callers
- S5 result object; pipeline writes nothing; persist_result = separate skippable dry-run step
- S6 idempotency via content-hash + source_reference; provenance (model_version/prompt_version) persisted
- S7 containment grep: ticketing name only in seams/ + config selection keys + tests
- Test counts: original suite 104p+5x+2x UNCHANGED (manager re-ran); seams tests 12/12 (idempotency 2/2, provenance 1/1)

### W2 INTENTIONAL DEVIATION (manager-registered, NOT hidden by green count)
Deviation 4: LLM-failure handling changed from aborting the poll iteration to per-ticket capture in result.error. Better behavior, right direction for W3 — but it IS a behavior change in a zero-behavior-change workstream. Recorded as an intentional exception. Deviations 2, 3, 5, 6 clean.

## ORCHESTRATION LESSON (for W3 dispatch)
The run agent was stood down mid-W1 for acting on stale demo-era assumptions ("never down -v") that contradicted the current spec. W3's dispatch must state plainly: the 92 incidents are disposable and scripts/reseed.sh exists — no worker re-derives caution from history.

## W3 — DONE (merged 3f3c433, verified by manager)

- E1: POST /api/v1/incidents → 202 + reference in 1.0s (manager timed it live); processing async via worker thread
- E2: GET /api/v1/incidents/{ref} → structured result or pending/retryable/flagged
- E3: POST /api/v1/backfill → 202 + references (manager verified live)
- E4: /health liveness + /ready → {db, embedding, llm} each reported independently (manager verified: all "ok")
- E5: LLM-unreachable → retryable → FLAGGED after max attempts, real NXDOMAIN endpoint (llms.elm.sa), no mock. Test passed (manager re-ran: 1 passed, asserts real DNS-failure signature in error)
- E6: auth (Bearer token) on every non-health endpoint; unauthenticated → 401 structured UNAUTHORIZED (manager verified live)
- E7: dry-run persists nothing, writes nothing back — manager verified live: jobs list unchanged after DRY-1
- E8: docs/INTEGRATION_GUIDE.md (251 lines, curl example per endpoint)
- E9: full suite 131p+5x+2x (manager re-ran); sub-offering clustering confirmed still disabled
- Write-back default: SAFEST — suggestions to separate channel, applied=false (verified in live job result)

## FINAL STATE — all three workstreams merged on feat/deploy-integration-ready

### W1: DONE — infra rebuilt from source; reseed reproducible (91/91, 492s); fail-loud config; canary no-regression on OpenRouter. D4 DEFERRED: company-model canary is the hard precondition before live traffic on llms.elm.sa.
### W2: DONE — SMAX contained under seams/smax/ (client/models/real_source per user spec); single pipeline entry; result object; idempotent; provenance; original 104 tests unchanged. One intentional deviation registered (LLM-failure → per-ticket error capture).
### W3: DONE — integration-ready API + guide; E1-E9 verified by manager (live smoke + re-run tests).

### MODEL CUTOVER FINDING
Company-hosted model (llms.elm.sa) NOT testable from dev box (NXDOMAIN). Canary on OpenRouter qwen3.6 shows no regression from rebuild. The D4 precondition stands: run the 34-pair canary against llms.elm.sa BEFORE any live traffic on the server; if it deviates, STOP — no prompt tuning.

### OPEN ITEMS / RISKS
1. D4 canary vs company model — first thing on the server after deploy.
2. W2 deviation 4 (LLM-failure per-ticket capture) — intentional, verified better, registered.
3. SMAX client endpoints (/tickets, changed_since, suggestions) are best-guess REST shapes — must be reconciled with SMAX's actual API before live integration.
4. Integration worker starts in API lifespan (INTEGRATION_WORKER_ENABLED default?) — confirm default is safe for prod.
