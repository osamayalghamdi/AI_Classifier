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
- Frozen dataclass `Settings`; `load_dotenv()` is CWD-relative — env must be exported by the
  caller (compose/systemd). All knobs env-driven (LLM, PG, integration E1-E9, heal, repool).
- Fail-loud: lifespan refuses to start without explicit `LLM_MODEL`; `openrouter/*` models
  require `LLM_API_KEY`; empty `integration_token` → 401 on every protected endpoint
  (never a default secret).

## Note for integration
`sync.py` (package root) still imports `ai_classification.config` — update to
`ai_classification.shared.config` when merging.
