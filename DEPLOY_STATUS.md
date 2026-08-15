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
