# AI_Classifier — Cleanup, Restructure & SMAX Integration Plan

**Audience:** implementation agent. Execute phases in order. Every claim below was verified by reading the actual code and tracing imports/callers — evidence is cited with file:line so you can re-verify before touching anything.

**Ground rules for the agent:**
- Run the test suite after every phase: `PG_PORT=5432 uv run pytest tests/ -q` (suite forces `ai_incidents_test` DB).
- Never change classifier prompts, thresholds, or taxonomy values — those are frozen (see STATUS.md "Frozen parameters"). This plan is structure + dead code + one new integration package only.
- When deleting anything, delete its mirrored tests/scripts in the same commit so the tree never half-references a module.
- One phase = one commit (or small commit series). Do not mix phases.

---

## Phase 0 — Bug fixes (do these FIRST, before moving any files)

These are real defects found during the audit. Fix them before restructuring so `git blame`/diffs stay readable.

### BUG-1: `to_smax_suggestion` crashes on first real write-back
`ai_classification/seams/smax/models.py` (`to_smax_suggestion`):
```python
"affected_system": result.classification.get("affected_system") ...
```
`result.classification` is a `ClassificationResult` **Pydantic model**, not a dict (set in `seams/pipeline.py: process_incident` from `classify()`). `.get()` will raise `AttributeError` the first time `write_back` runs against real SMAX. This was never caught because write-back has **zero production callers** (see DEAD-4). Fix: use `getattr(result.classification, "affected_system", None)` or `result.classification.model_dump()` — and add a unit test that feeds a real `PipelineResult` through it.

### BUG-2: `persist_result` writes the wrong status value
`ai_classification/seams/pipeline.py`, "seen" branch:
```python
local_status = ("active" if result.status in ("open", "in_progress", "third_party") else "resolved")
current = store.get_incident(result.incident_id)
if current and current.get("status") != local_status:
    store.set_status(result.incident_id, result.status)   # ← passes result.status, not local_status
```
It computes the mapped local status, compares against it, then persists the **raw external** status ("open", "in_progress", …) into a column whose domain is active/resolved. Every dedupe hit with a changed upstream status corrupts local status. Fix: `store.set_status(result.incident_id, local_status)`. Add a test.

### BUG-3: `SYNC_STAMP` path resolves to the wrong directory
`ai_classification/services/jobs/sync.py`:
```python
# comment says: repo root
SYNC_STAMP = Path(__file__).parent.parent.parent / ".last_sync"
```
Three `.parent` from `ai_classification/services/jobs/sync.py` = `ai_classification/`, **not** the repo root (needs four). The checked-in `.last_sync` at repo root is therefore never read after the services/ restructure — every restart re-syncs from the stamp default. Fix: four parents, or better: make the stamp path an env-configurable setting (`SYNC_STAMP_PATH`) with a sane default, and **remove `.last_sync` from git** (add to `.gitignore` — runtime state doesn't belong in the repo).

### BUG-4: unused variable in `decide_proposal`
`ai_classification/shared/store.py:1271` — `decided_by` is assigned and never used (vulture, 100% confidence). Either persist it in the UPDATE or delete the assignment.

### BUG-5: broken/pointless log line in sync
`sync.py`: `_log.info("Synced %s changes (since %s)", "1+", ...)` — hardcoded `"1+"`. Count the processed tickets in the loop and log the real number.

---

## Phase 1 — Dead code removal

### DEAD-1: The dormant "sub-offering engine" (the big one — decision required)
`shared/store.py:1727` says it explicitly: *"Flow B supersedes repool's phase logic; the sub-offering engine stays dormant."* The v2 persistent-clustering path (`services/cluster/persistent.py`, wired in `lifespan` via `start_sweep_worker`) replaced it. The dormant engine is this set — **nothing in the running app calls any of it** (verified by import tracing; only scripts and its own tests reference it):

| File | Only referenced by |
|---|---|
| `services/match/suboffering.py` | repool, proposal_routes, scripts, tests |
| `services/cluster/suboffering_cluster.py` | repool, scripts, tests |
| `services/cluster/verifier.py` | repool, suboffering_cluster, scripts |
| `services/jobs/repool.py` | its own test only (`test_cross_offering_repool.py`); worker never started in `lifespan` |
| `services/jobs/recovery.py` | its own test only; manual `python -m` entry |
| `services/review/proposal_routes.py` | mounted in the app, but the frontend never calls `/proposals` — `review.html` uses only `/cluster-proposals`, `/review-queue`, `/taxonomy-gaps` (verified by grepping frontend fetches) |
| Scripts: `run_engine.py`, `run_offering000.py`, `audit_offering000.py` | manual |
| Store surface used only by the above: `create_sub_offering`, `list_sub_offerings`, `get_sub_offering`, `set_sub_offering_status`, `add_exemplar`, `list_exemplars`, `pool_add/remove/remove_many/set_cooldown/list/clear`, `create_proposal`, `list_proposals`, `get_proposal`, `decide_proposal`, `delete_all_proposals`, `_enrich_proposal_members`, `_row_to_sub_offering`, `_row_to_proposal` | — |

**Recommendation:** do NOT silently delete — the owner invested heavily here (STATUS.md documents the whole W1–W3 program). Instead:
1. Create `legacy/suboffering_engine/` at repo root and **move** all of the above modules + their scripts + their tests into it, with a short README explaining it's superseded by persistent clustering and how to resurrect it.
2. Unmount `proposal_router` from the app (keep `cluster_proposal_router`, `taxonomy_gaps_router`, `/review-queue` — the frontend uses those).
3. Extract the store methods listed above from `store.py` into `legacy/suboffering_engine/store_suboffering.py` (a mixin or standalone functions taking a store/conn). This alone removes ~450 lines from `store.py`.
4. Leave the DB tables in place (data is harmless; dropping is a separate ops decision).

If the owner confirms full deletion later, the folder deletes cleanly in one commit.

**Caveat to verify first:** `services/review/proposal_routes.py` imports `embed_pure` from `match/suboffering.py`, and `store.py`'s `find_fallback_incidents` / `queue_add` / `queue_list` serve *both* the review-queue endpoint (live) and recovery (dormant). Keep `queue_*` and `find_fallback_incidents` in `store.py` — the live `/review-queue` endpoint and heal use them.

### DEAD-2: `heal.py` is configured but never wired
`services/jobs/heal.py` (`reclassify_fallback_incidents`) has dedicated config (`reclassify_enabled`, `reclassify_interval_s`, `reclassify_max_per_tick` in `shared/config.py`) but **no worker ever starts it** — `lifespan` starts sync, sweep, monitor, integration workers only; grep confirms zero production callers (only `tests/services/jobs/test_heal.py`). Decide one way:
- **Option A (recommended):** wire it — add a small periodic worker (gated on `reclassify_enabled`) started from `lifespan`, mirroring `start_sweep_worker`. The feature is genuinely useful (self-healing after LLM outages) and the config already promises it.
- **Option B:** delete `heal.py` + its test + the three `reclassify_*` settings.
Pick A unless the owner says otherwise; leaving config that lies about behavior is the worst state.

### DEAD-3: `services/jobs/reclassify_offerings.py`
Imported by **nobody** (verified). Its docstring even says to run it as `python -m scripts.reclassify_offerings` — a path that doesn't exist (it lives under `services/jobs/`). `scripts/reclassify_v3.py` supersedes it (v3 sweep through the live cascade). **Delete it.**

### DEAD-4: SMAX write-back path — never called
`RealTicketingSource.write_back` → `SmaxClient.write_suggestion` → `to_smax_suggestion` has zero callers: the sync worker never invokes `write_back`, and the integration worker only *reports* `write_back: {"mode": ..., "applied": False}`. `INTEGRATION_WRITE_BACK=full` is documented in config but implemented nowhere. This code moves into the new SMAX integration package (Phase 4) where it will finally get a real caller — do not delete, but do fix BUG-1 first and mark it clearly.

### DEAD-5: misc small removals
- `ai_classification/config.example.py` — stale. Its header says "Copy this file to config.py (same directory)" but real config is `shared/config.py` reading env, and `.env.example` already exists. **Delete** (or replace with a one-line pointer to `.env.example`).
- `ai_classification/core/` — contains only an `__init__.py` docstring; README still describes `core/classifier.py`, `core/store.py`, `core/failure_modes.py` which **do not exist**. Delete the folder; fix README (Phase 5).
- `classifier.py` compat shims: `getattr(settings, "cascade_classification", True)` (the field exists in Settings now) and the `inspect.signature(store.save_incident)` probe in `classify_and_store` ("fall back gracefully when this worktree snapshot predates that merge" — merged long ago). Remove both; drop the now-unused `import inspect`.
- Duplicate logos: `elm-logo.svg` / `elm-logo-white.svg` exist at repo root AND in `frontend/dashboard/`. Keep the frontend copies, delete the root ones (verify nothing serves from root first — `nginx.conf`).
- Root worklog clutter: `STATUS.md`, `W2_B1_AUDIT.md`, `DEPLOY_STATUS.md`, `fix-cascade-stage1.md` are internal work logs, not project docs. Move to `docs/worklogs/`.

---

## Phase 2 — Complexity reduction

### C-1: Split `shared/store.py` (1,781 lines — the god module)
It currently mixes: embedding model loading, connection pool, DDL, incident CRUD, similarity search, taxonomy-gap log, classification log, sub-offering tables, pools, proposals, persistent clusters, assignment log, **plus** the FastAPI `lifespan`, worker startup, and a set of module-level API helper wrappers (`get_health`, `resolve_incident`, …). Split by responsibility, keeping the public `store` singleton import path stable:

```
ai_classification/shared/
├── db.py            # pool, _getconn/_putconn, DDL/schema setup
├── embeddings.py    # SentenceTransformer load, _embed, _build_embedding_text
├── store.py         # IncidentStore facade composing the parts (public API unchanged)
├── store_incidents.py   # incident CRUD, find_similar, hashes, occurrence
├── store_clusters.py    # persistent clusters + members + assignment log
├── store_logs.py        # classification_log, taxonomy_gaps, review queue
└── config.py
```
Mechanical extraction only — no behavior changes. The row-mapper statics (`_row_to_*`) go with their table's module.

**Critically: move `lifespan` + worker startup OUT of store.py** into a new `ai_classification/app.py` (see C-3). `shared/` importing `services.jobs.sync` at module top (store.py line 24) is an inverted dependency — infrastructure must not import services. The lazy-import workarounds sprinkled across `pipeline.py`/`heal.py`/`repool.py` ("classifier imports store and store imports sync, so a top-level import here would be circular") exist *only because of this*; after the move, most of those lazy imports can become normal top-level imports.

Also move the thin module-level wrappers (`get_health`, `resolve_incident`, `get_incident`, `delete_all_incidents`, `list_incidents` at store.py:1749–1781) into the routes that call them, or just call `store.*` directly from routes — they add a layer with nothing in it.

### C-2: Split `services/classify/classifier.py` (1,690 lines)
Keep the module boundary (public `classify`, `classify_and_store`, `classify_batch`, `content_hash`, `PROMPT_VERSION`) but split internals — prompts must stay byte-identical (frozen):

```
services/classify/
├── classifier.py      # public API: classify(), orchestration only
├── prompts.py         # FEW_SHOT_EXAMPLES + all _build_*_prompt functions (frozen text)
├── cascade.py         # _run_cascade, _stage_system/service/offering_llm, _resolve_* pins
├── parsing.py         # _parse_* + _normalize_canonical + validators
├── verification.py    # stage-4 verify + corrections + _self_consistency
├── persistence.py     # classify_and_store, classify_batch, content_hash
└── llm.py             # (unchanged)
```
Add a trivial regression guard: a test asserting the SHA of the concatenated frozen prompt strings, so future refactors can't silently drift them.

### C-3: `services/ingest/routes.py` is not "ingest" — it's the whole app
It defines the FastAPI `app`, CORS, exception handlers, mounts every router, and holds cluster/report/test endpoints. Restructure:

```
ai_classification/
├── app.py                         # FastAPI app, lifespan, CORS, exception handlers, router mounting, worker startup
├── api/
│   ├── schemas.py                 # (already there)
│   ├── incidents.py               # /classify, /incidents, /import, /reset
│   ├── reports.py                 # /api/reports, /reports, /clusters, /cluster/sweep
│   ├── diagnostics.py             # /health, /status, /test/llm, /test/all
│   └── integration.py             # current integration_routes.py (E1-E9)
```
`entrypoint.sh` / Dockerfile change: `uvicorn ai_classification.app:app`. The `api/` package finally earns its name (today it holds only schemas). Routes stay endpoint-only — the logic already lives in services, keep it that way.

### C-4: Config cleanup
`shared/config.py` defines `_is_truthy` then re-implements it inline **six times** (`cascade_classification`, `classify_self_consistency`, `integration_worker_enabled`, `reclassify_enabled`, `cluster_assign_on_arrival`, `cluster_auto_activate`). Use `_is_truthy` everywhere. Also group settings into sections that match the new package layout, and delete settings for anything removed in Phase 1.

### C-5: Overlapping diagnostics endpoints
`/health`, `/ready`, `/status`, `/test/llm`, `/test/all` overlap heavily. Keep all (they serve different consumers: k8s liveness, readiness, dashboard, human smoke-test) but co-locate them in `api/diagnostics.py` with a docstring table saying which is for what — the confusion today is discoverability, not redundancy. One real change: `/test/all` reaches into private internals (`store._model`, `jobs.integration._connect`) — give it proper public accessors (`store.embedding_ready()`, a `ping()` in the jobs module).

### C-6: Model duplication
`SimilarMatch` (dataclass, `shared/store.py:42`) vs `SimilarOpenIncident` (Pydantic, `domain/models.py`) represent the same thing at two layers. Acceptable (storage vs API shape), but move `SimilarMatch` into `domain/models.py` next to its sibling so both shapes live in one file with a comment explaining the split.

---

## Phase 3 — File-structure target (end state after Phases 1–2)

```
AI_Classifier/
├── ai_classification/
│   ├── app.py                  # FastAPI wiring + lifespan (NEW)
│   ├── api/                    # endpoints only, split per concern (C-3)
│   ├── domain/                 # models.py, taxonomy.py (unchanged)
│   ├── services/
│   │   ├── classify/           # split per C-2
│   │   ├── cluster/            # persistent.py, grouping.py (dormant engine moved out)
│   │   ├── ingest/             # import_service.py, status_monitor.py ONLY
│   │   ├── jobs/               # sync.py, heal.py (wired), integration/
│   │   └── review/             # cluster_proposal_routes.py, taxonomy_gaps_routes.py
│   ├── seams/                  # port.py, pipeline.py, local_source.py  (SMAX moved out → Phase 4)
│   └── shared/                 # config.py + split store (C-1)
├── integrations/
│   └── smax/                   # NEW — Phase 4
├── legacy/
│   └── suboffering_engine/     # DEAD-1 quarantine
├── docs/  (+ docs/worklogs/)
├── frontend/  scripts/  tests/  evaluation/  simulator/  ocr/
```

Test tree mirrors the moves 1:1. Update `ARCHITECTURE.md` diagrams/tables in the same commit as each move.

---

## Phase 4 — SMAX integration package (the actual feature)

**Requirement (from owner):** SMAX connectivity lives in its **own separate folder**, and it talks to the classifier through the system's **own HTTP endpoints** (e.g. `/classify`, `/api/v1/incidents`) — so the classifier's endpoint surface stays clean and SMAX-specific code never touches the app's internals.

This is a genuinely better design than the current in-process seams/sync approach, and the system is already prepared for it: the E1–E9 integration API (`POST /api/v1/incidents` → 202 + reference, `GET /api/v1/incidents/{ref}`, bearer auth, retry worker) was built exactly as the external ingestion contract. The SMAX connector should be **a client of that API, running as its own process**.

### 4.1 New package layout
```
integrations/smax/
├── README.md            # what it is, how to run, env vars, sequence diagram
├── __init__.py
├── config.py            # ONLY SMAX + classifier-API settings (own env prefix: SMAX_*, CLASSIFIER_API_*)
├── smax_client.py       # moved from ai_classification/seams/smax/client.py (transport: auth, retry, timeouts)
├── smax_models.py       # moved from seams/smax/models.py (field mapping, from_smax / to_smax_suggestion — with BUG-1 fixed)
├── classifier_client.py # NEW — thin HTTP client for the classifier's public API:
│                        #   submit(incident) -> POST /api/v1/incidents (Bearer INTEGRATION_API_TOKEN)
│                        #   result(ref)      -> GET  /api/v1/incidents/{ref}
│                        #   (optional sync mode: POST /classify for small/manual runs)
├── poller.py            # the loop: SMAX list_changed(since) -> submit each -> stamp file
│                        #   (replaces services/jobs/sync.py's in-process loop for the SMAX case)
├── writeback.py         # poll classifier results -> smax_client.write_suggestion (modes: none|suggestions|full)
├── main.py              # entrypoint: python -m integrations.smax.main  (runs poller + writeback)
└── tests/               # unit tests with a fake SMAX server + fake classifier API (httpserver or respx)
```

### 4.2 Design rules for the agent
1. **Zero imports from `ai_classification.*`** inside `integrations/smax/` except *nothing at all* — it must be runnable on a machine that only has network access to the API. This is the whole point: enforce it with a CI grep (there's precedent — the repo already had a "containment grep" for SMAX field names).
2. **Idempotency** comes free from the server side (content-hash + `source_reference` dedupe in the pipeline). The poller still keeps a local `since` stamp (file or small sqlite) — configurable path, not committed to git (BUG-3 lesson).
3. **Auth:** the connector holds two secrets: `SMAX_API_TOKEN` (upstream) and `CLASSIFIER_API_TOKEN` (the existing `INTEGRATION_API_TOKEN`). Fail loudly at startup if either is missing when its side is enabled — reuse the `NotConfiguredError` pattern.
4. **Write-back** defaults to `suggestions` (side channel), `none` and `full` selectable — this finally gives `INTEGRATION_WRITE_BACK` semantics a real implementation (DEAD-4). Since it was never live-tested, gate it behind `SMAX_DRY_RUN=true` default: log the payload it *would* post.
5. **Deployment:** add a `smax-connector` service to `docker-compose.yml` (same image or a slim one, `command: python -m integrations.smax.main`), disabled by default via profile. Document in the README.
6. **Batch/backfill:** support `--backfill --since <iso>` using `POST /api/v1/backfill` (already exists, ≤200 per call).

### 4.3 What happens to the old in-process path
- `ai_classification/seams/smax/` — **moved** to `integrations/smax/` (client + models). `seams/` keeps `port.py`, `pipeline.py`, `local_source.py`: the pipeline and the local/fake source are still used by the integration worker and tests.
- `services/jobs/sync.py` (in-process polling worker): keep it but make it **local-source only** — after the move, `TICKETING_SOURCE=real` no longer exists in-process; `get_ticket_source()` returns only the local fake (used by tests/offline). Simplify `seams/__init__.py` accordingly and delete the `RealTicketingSource` re-export. Update `.env.example`: `TICKETING_*` vars shrink to the local/test case; new `SMAX_*` / `CLASSIFIER_API_*` vars documented under the connector.
- Alternative if the owner wants to keep both paths temporarily: leave `TICKETING_SOURCE=real` working but log a deprecation warning pointing to `integrations/smax`. **Ask the owner; default to the clean removal.**

### 4.4 Acceptance criteria for Phase 4
- `python -m integrations.smax.main` against a fake SMAX + the real local API: tickets flow SMAX → `POST /api/v1/incidents` → classified → `GET .../{ref}` returns result → (dry-run) write-back payload logged.
- Restart mid-run: no duplicates (server dedupe + since-stamp).
- Kill the classifier API mid-run: connector retries with backoff, no crash, no lost tickets.
- Kill SMAX mid-run: same.
- CI grep proves no `ai_classification` import inside `integrations/`.
- BUG-1 regression test passes (real `PipelineResult`-shaped object through `to_smax_suggestion`).

---

## Phase 5 — Documentation sync (last, mandatory)

- **README.md "Project Layout" is wrong today** (describes `api/routes.py`, `core/classifier.py`, `core/store.py`, `core/failure_modes.py`, `sync.py` at package root — none exist). Rewrite it against the Phase-3 tree.
- README "SMAX integration" section: replace both options with the new single story — the `integrations/smax` connector + the E1–E9 API contract.
- `ARCHITECTURE.md`: update the mermaid diagram (SMAX box now points at the HTTP API, not into the app), the service table, and the "Where do I look?" table.
- Each moved service keeps/updates its `CONTRACT.md`. Add one for `integrations/smax`.
- `docs/INTEGRATION_GUIDE.md`: add a "reference client" section pointing at `integrations/smax` as the canonical example consumer.

---

## Known open questions for the owner (ask before Phase 1 if possible; otherwise take the recommended default)

1. **Sub-offering engine (DEAD-1):** quarantine to `legacy/` (recommended default) or hard delete?
2. **heal.py (DEAD-2):** wire it (recommended default) or delete + drop its config?
3. **In-process SMAX polling (4.3):** remove cleanly (recommended default) or keep behind a deprecation warning for one release?
4. `CLUSTER_AUTO_ACTIVATE` currently defaults to ON (skips the human review gate — config comment says it was for a NOC demo). Not in scope here, but flag it: for production SMAX go-live the owner should decide whether the gate comes back (`CLUSTER_AUTO_ACTIVATE=0`).

## Explicit do-not-touch list

- All prompt strings, few-shot examples, `PROMPT_VERSION`, thresholds (0.60 / 0.75 / 0.80 / 0.40 / 0.90), taxonomy enums/values.
- `scripts/migrate_classifier_v3.py`, `scripts/reclassify_v3.py`, `scripts/audit_offerings.py`, `evaluation/` — live tooling.
- The E1–E9 integration API contract (paths, status codes, error codes) — external consumers may depend on it; the SMAX connector is being built ON it.
- `domain/taxonomy.py` enum members flagged "unused" by static analysis — they're LLM-output validation values, used dynamically. Not dead.
- FastAPI route functions flagged "unused" by vulture — decorator-registered, not dead.

## Suggested execution order & sizing

| Phase | Content | Risk |
|---|---|---|
| 0 | 5 bug fixes + tests | low |
| 1 | dead code removal / quarantine | medium (DEAD-1 is large but mechanical) |
| 2 | store.py + classifier.py + routes split | medium — mechanical moves, prompt-SHA guard |
| 3 | final tree + test-tree mirror | low |
| 4 | `integrations/smax` package | main feature work |
| 5 | docs sync | low |

Full test suite + `smoke_test.sh` + a `docker compose up` boot check after phases 2, 3, and 4.
