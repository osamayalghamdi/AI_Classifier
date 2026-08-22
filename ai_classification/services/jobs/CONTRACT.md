# Jobs Service — CONTRACT
Recurring sweeps + background workers. No HTTP surface (endpoints live in `api/`).

## sync — jobs/sync.py · entry: `start_sync_worker(store)`
- Input: changed tickets from the configured `TicketSource` since the `.last_sync` stamp (`SYNC_STAMP_PATH`-configurable, defaults to repo root).
- Output: processed via seams pipeline + `persist_result`; `.last_sync` advanced to the newest `updated_at` (count logged — BUG-5 fix).
- Depends on: `config.settings`, `seams` (`get_ticket_source`, `process_incident`, `persist_result`, `NotConfiguredError`) · Called by: daemon in `app.lifespan` (interval = `sync_interval_seconds`).
- Key invariants: no payload translation here (all in source adapters); idles (no error spam) when source unconfigured; dry-run skips persistence.

## heal — jobs/heal.py · entry: `reclassify_fallback_incidents(limit=None)` / `start_heal_worker()`
- Input: rows whose stored reasoning carries the fallback marker ("Classification failed after ...").
- Output: re-classified in place (`store.reclassify_incident`); still-fallback rows left for next tick.
- Depends on: `shared.store.find_fallback_incidents`, `classify.classifier.classify`, `config.settings` · Called by: daemon in `app.lifespan` via `start_heal_worker` (interval = `reclassify_interval_s`, `reclassify_enabled`-gated — default OFF in compose to conserve LLM tokens).
- Key invariants: only fallback-marked rows touched; fails open when LLM down (bounded per tick); re-embeds the ticket's own text.

## integration — jobs/integration/ · entry: `start_integration_worker()` / `ensure_jobs_table()`
- Input: `ingestion_jobs` rows (async ingest E1-E9: pending → retrying → done/flagged).
- Output: classifies + persists each job; retry with backoff on LLM failure; writes `result_json`/`error_code`.
- Depends on: `classify.classifier`, `shared.store`, `config.settings` · Called by: daemon in `app.lifespan` (`integration_worker_enabled`-gated; tests drive synchronously with the gate off).
- Key invariants: fail-closed bearer auth on the HTTP surface (`api/integration.py`); idempotent per `source_reference`; `_connect()` private, `ping()` public (diagnostics).

## Removed from this service (Phase 1)
- `recovery.py` / `repool.py` / `reclassify_offerings.py` — the sub-offering engine is dormant; all three moved to `legacy/suboffering_engine/` (see its README).
