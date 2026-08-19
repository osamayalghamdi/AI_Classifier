# CLAUDE CODE — FULL PROJECT REVIEW & DEPLOY-READINESS BRIEF

## 0. HOW TO WORK — READ THIS FIRST

You are reviewing a REAL production-bound system. Work in this order and do
NOT skip steps:

1. **UNDERSTAND before touching anything.** Read the code, the tests, the
   docs (README.md, docs/INTEGRATION_GUIDE.md), the compose file, and the
   flow end-to-end. Ask yourself: what does each layer do, who calls it,
   what would break if it changed?
2. **Check ACTUAL results, not just "tests pass".** Run the app, load the
   real test data, call the endpoints, look at real classifications and
   clusters. "Tests pass" is necessary but NOT sufficient — a test can pass
   while the system does something wrong.
3. Then and only then: simplify, restructure, improve. One change at a
   time, tests after every change.
4. Report honestly: what you found, what you changed, before/after numbers,
   anything you chose NOT to change and why. Never fabricate results.

## 1. PROJECT CONTEXT

**What it is:** An AI incident classifier for Hajj/Nusuk operations at ELM.
Support tickets (Arabic/English) from the ticketing system (SMAX) are
classified, deduplicated, similarity-matched against past incidents, and
clustered so operators see "what's happening now" instead of raw tickets.

**Stack:** FastAPI + Uvicorn · LiteLLM → Qwen3.6-35B-A3B (OpenRouter, or the
company endpoint llms.elm.sa on the target server) · BAAI/bge-m3 embeddings
(1024d) · PostgreSQL + pgvector (HNSW) · vanilla-JS dashboard (no build) ·
separate OCR microservice · Docker Compose deployment.

**Layout (main branch):**
```
ai_classification/
├── config.py            # env-based settings (frozen dataclass, loaded at import)
├── sync.py              # background ticketing sync worker
├── api/                 # routes.py (FastAPI app + endpoints), schemas.py,
│                        # integration_routes.py (E1-E9 integration API)
├── core/                # classifier.py (LLM classification), store.py
│                        # (pgvector persistence + background rebuild loops),
│                        # grouping.py (clustering), failure_modes.py (LEGACY),
│                        # import_service.py, status_monitor.py
├── domain/              # models.py, taxonomy.py
├── seams/               # THE INTEGRATION SEAM: port.py (Incident/PipelineResult),
│                        # pipeline.py (process_incident → persist_result,
│                        # read-only classify + separate write step),
│                        # smax/ (real ticketing adapter: client.py, models.py,
│                        # real_source.py), local_source.py
└── integration/         # E1-E9 async ingest: job table, retry/backoff worker,
                         # strict schemas, error codes
frontend/dashboard/      # index.html + app.js (no build step)
ocr/                     # OCR microservice (separate, on :8001)
tests/                   # 131+ tests (mocked LLM + real PG)
docs/INTEGRATION_GUIDE.md # the external integration contract (DO NOT BREAK)
```

## 2. THE PRODUCT DIRECTION (IMPORTANT — THE USER IS EMPHATIC)

- The **taxonomy is OFFERINGS** (offerings + sub-offerings). Versioned JSON
  catalog, grown from real tickets, data-driven. The LLM classifies into
  offering/sub-offering; embeddings match offering descriptions.
- **Failure modes (FM codes) are LEGACY.** Internal identifiers only. They
  are NOT the product taxonomy and must not be presented as one. Do not
  add, expand, or emphasize FM anything. If code still leans on FM where an
  offering would do the job, that is a candidate for improvement — but do
  not rip out working behavior in one shot; note it and change it carefully
  with tests.
- A sub-offering clustering engine exists (candidate pool → LLM
  verification → proposals) but is **disabled by default** — manual scripts
  only. Keep it available, keep it gated.

## 3. THE KEY DESIGN IDEA — VOLUME-ADAPTIVE CLUSTERING SENSITIVITY

**The business reality:** incident volume is NOT constant. Some periods
have a handful of tickets; others have a flood (e.g., Hajj season). The
current clustering uses fixed thresholds (e.g., a hard similarity cutoff),
which behaves badly at both extremes:

- **Few incidents (e.g., 5–20 active):** fixed thresholds are too strict —
  related incidents never group, operators see 15 separate single-ticket
  clusters instead of "2 real problems". Sensitivity should be LOOSER so
  small but real groups still form.
- **Many incidents (hundreds):** thresholds that are too loose create noise
  — unrelated tickets merge into giant meaningless clusters. Sensitivity
  should be TIGHTER for precision.

**Your task:** design and implement clustering sensitivity that ADAPTS to
the current incident volume. The exact mechanism is your design decision,
but the shape is: a sensitivity/threshold function of the active-incident
count (and/or recent volume), applied in the rebuild loop, with sensible
floor/ceiling bounds, deterministic (seed 42), and covered by tests at both
extremes (few-incident regime and flood regime). Consider also: minimum
cluster size, cluster-size caps, and how confidence interacts with merging.
Document the design in a short section of the README (one paragraph, plain
language) or in the code module docstring.

## 4. CURRENT STATE (verified facts — don't re-litigate)

- Integration API E1–E9 is live on main: async ingest
  (`POST /api/v1/incidents` → 202 + reference), fetch by reference,
  dry-run (writes nothing), backfill, `GET /ready` (db/embedding/llm
  individually), Bearer auth (`INTEGRATION_API_TOKEN`), strict payloads
  (unknown fields → 422 INVALID_PAYLOAD), retryable→flagged job state
  machine (never half-written), write-back default `suggestions` (safest).
  Contract: docs/INTEGRATION_GUIDE.md.
- Seams pipeline: `process_incident` (read-only classify) + `persist_result`
  (separate write step) — webhooks/batch/polling are thin callers.
- Tests: ~131 passed + a few xfail/xpass (known flaky canary pairs —
  rerun once before declaring failure). conftest auto-sets the test token,
  disables the worker, and REFUSES to run against the production DB.
- LLM env: OpenRouter in .env; company endpoint llms.elm.sa is NXDOMAIN
  from this dev box but works on the target server. `temperature=0.0`,
  `seed=42` convention. Startup must log resolved config (model, base,
  DB, embedding) and FAIL LOUD on missing LLM config — no silent fallback.
- Deployment: docker-compose (postgres + api + nginx + ocr; ollama/
  cloudflared behind profiles); healthchecks; bge-m3 baked into the image
  (HF_HUB_OFFLINE=1). Known: the api image is ~16GB (CUDA torch wheel) —
  investigate a CPU-index torch alternative as a report item, but ONLY if
  it doesn't break the build; treat as recommendation unless trivial.

## 5. SCOPE OF WORK (in order)

1. **Full review** — read everything; map the flow; list dead code, unused
   imports/functions/endpoints, duplicate logic, unreachable branches,
   half-finished experiments. Keep a list.
2. **Real-results check** — run the stack (or the API + PG locally), reseed
   the 91-incident demo data (scripts/reseed.sh exists), exercise:
   classify, similarity, clustering (few AND many incidents), the E1–E9
   flow (ingest → poll → result), dry-run, auth failures, /ready. Record
   actual outputs. This is where you find what's actually wrong.
3. **Simplify & clean** — remove dead/unused/duplicate code CAREFULLY
   (tests must catch regressions; the integration contract must not change
   shape). Organize what remains: consistent layering, naming, no
   cross-layer surprises.
4. **Volume-adaptive clustering** — implement Section 3 with tests at both
   volume extremes. Wire it into the rebuild loop with bounds.
5. **Edge cases / scenarios** — think through and TEST: empty DB,
   single incident, 3 incidents, 300 incidents, LLM down (jobs must go
   retryable→flagged, never half-written), embedding failure, duplicate
   ingest (idempotency), concurrent ingests, oversized payload, wrong
   token, SMAX unreachable (sync must idle quietly), postgres restart.
6. **Deploy-readiness pass** — config via env only (no hardcoded secrets/
   tokens); compose correctness; startup logs; healthchecks; the guide
   matches the actual endpoints. Report findings, fix what's safe.
7. **Full test run** — everything green, new tests included. Update the
   README tests section if counts changed.

## 6. GROUND RULES

- Work on a branch (create one: e.g., `refactor/deploy-ready`). Commit
  with explicit paths and clear messages. Never `git add -A`/`.`.
- NEVER touch or commit the external uncommitted edits at
  /home/osama/projects/AI_Classifier (classifier.py, import_service.py,
  app.js, .env.backup* are someone else's work-in-progress on the
  demo-ready checkout) — work from your own branch.
- Do NOT break: the integration API contract (docs/INTEGRATION_GUIDE.md),
  the seams pipeline shape, the dashboard, the OCR service, the E1–E9
  endpoints and their tests.
- Do NOT hardcode the LLM: always env-driven (LLM_MODEL/LLM_API_KEY/
  LLM_API_BASE). Never reintroduce a silent Ollama fallback.
- Do NOT change prompt texts of the classifier or the canary (they are
  frozen; behavior changes belong in taxonomy/code, not prompt-patching).
- Taxonomy = offerings. FM = legacy. No new FM surface.
- Every claim backed by real command output. If something fails, show the
  actual error and either fix it or report it — never fake a pass.
- Tests after every change; full suite green at the end.

## 7. DELIVERABLES

- A review report: what you found (dead code list, real-behavior findings,
  scenario results), what you changed and why, before/after (tests, image
  size if touched, response times if measured).
- The adaptive-clustering design decision + tests + README paragraph.
- Commit history on your branch, pushed only after the user says so.
- Final full-suite run output.

## 8. SCENARIO CHECKLIST (run these, record results)

- [ ] Empty DB → app boots, /ready reports, ingest works, rebuild idle
- [ ] 1 incident → no crash, no cluster
- [ ] 5–20 incidents → LOOSE regime: related incidents group
- [ ] 100+ incidents → TIGHT regime: no noise clusters
- [ ] LLM endpoint unreachable → job retryable (LLM_UNAVAILABLE) → flagged;
      no incident half-written; dry-run still works
- [ ] Duplicate source_reference → one job, replay safe
- [ ] Unknown payload field → 422 INVALID_PAYLOAD
- [ ] No/wrong token → 401 UNAUTHORIZED on every non-health endpoint;
      /health + /ready open
- [ ] Postgres down/restart → app fails loud or recovers, jobs not lost
- [ ] Full suite green (131+ baseline) with the new tests
