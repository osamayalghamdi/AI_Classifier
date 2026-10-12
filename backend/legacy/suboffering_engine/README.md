# Sub-Offering Engine (Quarantined)

The **sub-offering engine** (Phase-2/W2–W3 work: exemplar matching, unmatched
pools, batch proposals, human review gate) has been **superseded** by the v2
LLM-first **persistent clustering** path (`ai_classification/services/cluster/
persistent.py`, wired into the app `lifespan` via `start_sweep_worker`).

This folder is the dormant engine, preserved for resurrection. **Nothing in the
running app imports it** — the `proposal_router` was unmounted, the repool /
recovery workers were never started in `lifespan`, and the store surface the
engine used was extracted out of `ai_classification/shared/store.py`.

## What lives here

| File | Original location | Role |
|---|---|---|
| `suboffering.py` | `services/match/suboffering.py` (dormant half) | exemplar matcher + pool feed (engine-only) |
| `suboffering_cluster.py` | `services/cluster/suboffering_cluster.py` | batch clustering → proposals |
| `verifier.py` | `services/cluster/verifier.py` | strict pairwise LLM verifier (prompt v3) |
| `repool.py` | `services/jobs/repool.py` | cross-offering re-match worker |
| `recovery.py` | `services/jobs/recovery.py` | failed-classification retry |
| `proposal_routes.py` | `services/review/proposal_routes.py` | `/proposals` human-gate API (unmounted) |
| `store_suboffering.py` | extracted from `shared/store.py` | sub-offering/pool/proposal store methods (mixin) |
| `scripts/` | `scripts/run_engine.py` etc. | manual engine drivers |
| `tests/` | `tests/services/{match,cluster,jobs}/…` | engine tests (run with `-o pythonpath=.`) |

## What stayed live (do not touch)

- `ai_classification/services/match/suboffering.py` keeps **only** the helpers
  live clustering uses: `OFFERING_000`, `offering_of`, `embed_pure`.
- `shared/store.py` keeps the **persistent-cluster** surface
  (`create_cluster`…`audit_cluster`), plus `queue_add`/`queue_list` (the live
  `/review-queue` endpoint) and `find_fallback_incidents` (heal).

## How to run the quarantine tests

```bash
PG_PORT=5432 uv run pytest legacy/suboffering_engine/tests/ -q -o pythonpath=.
```

The `-o pythonpath=.` is required because the legacy package lives at repo root
and imports `ai_classification.*` and `legacy.*` (bootstrap in
`tests/conftest.py`).

## How to resurrect the engine

1. Point the moved modules' imports back at `ai_classification.*` (or keep the
   `legacy.` paths and re-import them from the app).
2. Re-mount `proposal_router` in the FastAPI app (removed in the quarantine
   commit) and re-add `start_repool_worker` / recovery to `lifespan` if wanted.
3. Restore the extracted store methods by inheriting
   `LegacySubOfferingStore` or copying the mixin into `IncidentStore`.

## Hard-delete decision

If the owner confirms permanent removal, delete this folder and the extracted
store mixin in one commit. The DB tables (`sub_offerings`,
`sub_offering_exemplars`, `unmatched_pool`, `cluster_proposals`,
`manual_review_queue`) are left in place — dropping them is a separate ops
decision.
