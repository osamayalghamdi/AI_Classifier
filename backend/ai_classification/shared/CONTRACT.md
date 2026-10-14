# Shared Infrastructure Contract

`shared/` is NOT a service — it is infrastructure every service imports.
Consumers: services/classify, services/ingest, services/jobs, services/cluster, seams, sync.

## store.py — PostgreSQL + pgvector persistence
- Threaded connection pool; `incidents` table (HNSW ANN index, VECTOR_DIM=1024 BAAI/bge-m3)
  plus sub_offerings / sub_offering_exemplars / unmatched_pool / cluster_proposals /
  manual_review_queue (sub-offering engine, Phase 2). `CREATE EXTENSION vector` + migrations
  run idempotently in `setup()`; embedding model loads lazily (failure → similarity disabled).
- Public surface: `store` singleton, `lifespan(app)` (FastAPI lifecycle), module helpers
  `get_health / resolve_incident / get_incident / delete_all_incidents / list_incidents`.
- **lifespan wiring (background workers)** — order: D2 resolved-config log line → D3
  fail-loud LLM config checks → `store.setup()` → `start_sync_worker` (sync.py) →
  `start_repool_worker` (services.jobs.repool) → `start_rebuild_loop`
  (services.cluster.grouping) → status monitor start (services.ingest.status_monitor) →
  integration worker start (services.jobs.integration, gated by INTEGRATION_WORKER_ENABLED).
- Lazy imports inside lifespan/`_invalidate_cluster_caches` avoid import cycles.

## config.py — env-based settings, fail-loud
- Frozen dataclass `Settings`; the environment is the chart's `backend.env` (`values.yaml`) or
  `scripts/env-from-chart.sh` for a host run. This module loads **no `.env`** — a stray file in a
  parent directory used to change behaviour silently. All knobs env-driven (LLM, PG, integration
  E1-E9, heal, repool).
- Fail-loud on the pieces that must be right: an empty `integration_token` → 401 on every protected endpoint
  (never a default secret).
- **A missing model is NOT fatal** (revised): the lifespan logs a
  model-configuration report — every gap with the env vars that fix it and what degrades meanwhile — and hosts
  anyway; `GET /ready` serves the same list as `models.issues`. Refusing to start on an unset `LLM_MODEL` turned
  "set one variable" into an exit loop whose single line scrolled away with the pod. What must still be loud:
  classification jobs end `LLM_UNAVAILABLE` quoting that report, so the reason is on the job and in the log.

## Note for integration
`sync.py` (package root) still imports `ai_classification.config` — update to
`ai_classification.shared.config` when merging.
