# Jobs Service — CONTRACT
Recurring sweeps + manual one-time operations. No HTTP surface.
## recovery — jobs/recovery.py · entry: `run_recovery(dry_run=...)`
- Input: incidents whose classification actually FAILED (failed-reasoning marker), not already in `manual_review_queue`.
- Output: re-classified via `store.update_classification`; failures/exhausted → queue (`store.queue_add`), human decides.
- Depends on: `core.store`, `core.classifier.classify`, `config.settings` · Called by: human manually (`python -m ai_classification.services.jobs.recovery [--dry-run]`) — never automatic.
- Key invariants: ERROR-only tickets; queued = "don't try again"; dry-run writes nothing.
## repool — jobs/repool.py · entry: `repool_once(dry_run=...)` / `start_repool_worker()`
- Input: `unmatched_pool` tickets (have offering, no sub-offering).
- Output: re-matched to ACTIVE sub-offerings (own offering @0.60, cross-offering @0.75); leftovers clustered per offering → pending PROPOSALS.
- Depends on: `core.store`, `core.suboffering`, `core.suboffering_cluster.run_all_pools`, `core.verifier.Verifier`, `config.settings` · Called by: daemon every `repool_interval_seconds` (900s) via `core.store.lifespan`; or manual run.
- Key invariants: re-match only — NEVER re-classifies; proposals NEVER auto-mint (human review gate); dry-run does no LLM/embedding/writes.
## reclassify_offerings — jobs/reclassify_offerings.py · entry: `run_reclassify(dry_run=...)`
- Input: invalid/invented stored offering, `Spike` incident_type without error text, "Error Spikes" semantic mispicks.
- Output: `classification_json` updated in place via live cascade (hardened v2 prompt + validator).
- Depends on: `core.store`, `core.classifier`, `config.settings`, `domain.taxonomy.SERVICES_BY_SYSTEM` · Called by: human manually (`python -m ai_classification.services.jobs.reclassify_offerings [--dry-run]`).
- Key invariants: never mints, never touches pools; identity/status/occurrence bookkeeping untouched.
## heal — jobs/heal.py · entry: `reclassify_fallback_incidents(limit=None)`
- Input: rows whose stored reasoning carries the fallback marker ("Classification failed after ...").
- Output: re-classified in place (`store.reclassify_incident`); still-fallback rows left for next tick.
- Depends on: `core.store.find_fallback_incidents`, `core.classifier.classify`, `config.settings` · Called by: `core.grouping._maybe_heal` on `reclassify_interval_s` cadence (`reclassify_enabled`-gated).
- Key invariants: only fallback-marked rows touched; fails open when LLM down (bounded per tick); re-embeds the ticket's own text.
## sync — jobs/sync.py · entry: `start_sync_worker(store)`
- Input: changed tickets from the configured `TicketSource` since `.last_sync` stamp (repo root).
- Output: processed via seams pipeline + `persist_result`; `.last_sync` advanced to newest `updated_at`.
- Depends on: `config.settings`; `seams` (`get_ticket_source`, `process_incident`, `persist_result`, `NotConfiguredError`) · Called by: daemon in `core.store.lifespan` (interval = `sync_interval_seconds`).
- Key invariants: no payload translation here (all in source adapters); idles (no error spam) when source unconfigured; dry-run skips persistence.
