# SERVICE-STRUCTURE RESTRUCTURE — DELIVERABLE REPORT

Task: move-and-document restructure into service blocks. ZERO logic changes.
Branch: `refactor/deploy-ready` (base d9c05cb). Not pushed.

## Final test counts (the invariant)

- Baseline: **172 passed + 3 xfailed + 4 xpassed** (measured before any move)
- Final: **172 passed + 4 xfailed + 3 xpassed** — same 172 pass count; xf/xp
  flap is the documented flaky canary class (expected, unchanged).
- Full suite runs after EVERY merge step (not per-move — parallel workers
  made per-move suite runs impossible; verification happened at integration).

## What moved where (all `git mv`, history preserved, one commit per move)

| Service | Files (from → to) |
|---|---|
| jobs/ | seams/recovery.py, seams/repool.py, seams/reclassify_offerings.py, core/heal.py, sync.py, integration/ (worker + schemas) → services/jobs/ |
| review/ | api/proposal_routes.py → services/review/ |
| cluster/ | core/grouping.py, core/suboffering_cluster.py, core/verifier.py → services/cluster/ |
| match/ | core/suboffering.py → services/match/ |
| classify/ | core/classifier.py, core/llm.py → services/classify/ |
| ingest/ | api/routes.py, api/integration_routes.py, core/import_service.py, core/status_monitor.py → services/ingest/ |
| shared/ | core/store.py, config.py → shared/ |
| seams/ | UNTOUCHED (port, local_source, smax/*, pipeline) — deliberate isolation |
| domain/ | UNTOUCHED (models, taxonomy) |
| core/ | only failure_modes.py remains (intentional legacy FM taxonomy) |

7 CONTRACT.md files written (6 services + shared), each <30 lines
(input/output/depends-on/called-by/invariants/entry-point). ARCHITECTURE.md
added: mermaid flow, service table, "where do I look" table.

Tests mirrored: tests/services/{classify,ingest,match,cluster,jobs}/ +
tests/shared/ with __init__.py + path-depth fixes (dirname chains,
fixture paths, cross-test imports).

## How it was done (3 parallel workers + manager integration)

- 3 worktrees (wt_svc1/2/3) on branches svc/jobs-review, svc/cluster-match,
  svc/classify-ingest-shared — disjoint file ownership, absolute-import
  contract, per-move commits, no docker/container access.
- Manager merged in dependency order, resolved 3 add/add __init__ conflicts,
  ran a 29-file import rewrite (absolute + relative forms), fixed 14
  module-import test forms, updated entrypoint.sh uvicorn target (baked
  into image — rebuilt).

## Shims / intentional leftovers

- `ai_classification.core.failure_modes` — the ONE remaining core import
  (classifier.py:146). Intentional: frozen legacy FM taxonomy, not a
  product surface. Listed per the prompt's shim clause.

## Bugs / dead code NOTICED (not touched, per rules)

1. services/jobs/repool.py:152 — worker thread still named "seams-repool"
   (cosmetic stale location reference).
2. services/cluster/grouping.py:223 — fingerprint eviction uses substring
   match, so ID "123" also evicts "1234" (over-eviction → extra LLM calls,
   never wrong results). Pre-existing.
3. services/jobs/sync.py — SYNC_STAMP writes `.last_sync` to repo root
   (environment-quirky). Pre-existing.
4. `_AR_NAME_TTL` in grouping.py was never enforced pre-move; the run
   agent's later TTL commit (ccb180d) superseded it — now enforced.

## Live verification (final gate, all passed)

- docker compose up -d --force-recreate (clean) → all healthy
- /health 200, /status rollup ok (db ok, llm ok)
- Dashboard :8082 → 200
- Live classify → Nusuk Masar Haj / FM-018 / Critical
- E1-E9: POST /api/v1/incidents → 202 pending → processing (ingest works)
- Data preserved across recreate (98 incidents), 9 clusters rebuilt with
  LLM Arabic names (فشل إصدار تصريح الروضة, ...)
- grep "from ai_classification.core" → only the intentional failure_modes line

## Commit history (refactor/deploy-ready)

3 worker merges (b604825..c88aea4 chain) → e17698d, 59fbe90 merge commits
→ 734f3f9 integration import pass → 26359ed test mirror → ARCHITECTURE.md.
Tree clean, nothing pushed (awaiting user's word).
