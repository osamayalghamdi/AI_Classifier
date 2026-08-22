# Cluster Service Contract

v2 LLM-first **persistent clustering** — clusters are DB rows, not rebuild artifacts. Pipeline stage 30.

## Persistent clustering (`persistent.py`)
- **Flow A** — `assign_incident(incident_id)`: after a ticket lands, the LLM decides (against up to `MAX_CANDIDATES` active cluster cards) whether it belongs to an existing cluster (`assign`) or not (`none_fit`). Runs in a background thread via `assign_in_background` (gated by `cluster_assign_on_arrival` — default OFF in compose to conserve LLM tokens).
- **Flow B** — `sweep_pool()`: unassigned tickets get second chances as clusters grow; batch grouping via the strict `SWEEP_PROMPT` (same-service/same-failure rule, `clustering-v3`); groups of ≥2 become proposals (or activate directly when `cluster_auto_activate`). `start_sweep_worker()` runs it periodically from `app.lifespan`.
- **Flow C** — `audit_cluster(cluster_id)`: nightly purity audit of active clusters (LLM reads all member texts; removes outliers; safeguards: ID reconciliation + 60% pruning floor). Cadence `cluster_audit_interval_s`.
- **User rules**: 1 incident = individual (never a cluster); names/descriptions capped (9 words); single-incident clusters demoted.
- Public API: `build_clusters(period)`, `sweep_pool()`, `assign_in_background()`, `start_sweep_worker()`, `audit_cluster()`.

## Grouping (`grouping.py`)
- Legacy stateless grouping utilities (volume-adaptive sensitivity, offering-based Phase 1). Kept for reference/tests; the live dashboard reads persistent clusters via `build_clusters`.

## Removed from this service (Phase 1)
- `suboffering_cluster.py` / `verifier.py` — the sub-offering engine is dormant (superseded by persistent clustering); both moved to `legacy/suboffering_engine/` (see its README for the frozen v3 verifier prompt + pairwise canary twin).

Consumers: `app.py` (router mounting), `api/reports.py`, `api/diagnostics.py`, `shared/store_clusters.py`.
