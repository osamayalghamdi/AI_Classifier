# Cluster Service Contract

v2 LLM-first **persistent clustering** — clusters are DB rows, not rebuild artifacts. Pipeline stage 30.
v4 (2026-09): **system-scoped clusters** — every cluster carries ONE `affected_system` (Hajj / Umrah / …);
incidents from different systems are different teams and NEVER share a cluster (3-layer guarantee below).

## Persistent clustering (`persistent.py`)
- **Flow A** — `assign_incident(incident_id)`: after a ticket lands, the LLM decides (against up to `MAX_CANDIDATES` active cluster cards) whether it belongs to an existing cluster (`assign`) or not (`none_fit`). Runs in a background thread via `assign_in_background` (gated by `cluster_assign_on_arrival` — default OFF in compose to conserve LLM tokens). **v4: `retrieve_candidates` filters active clusters to the ticket's own system, so the LLM never sees a cross-system candidate.**
- **Flow B** — `sweep_pool()`: unassigned tickets get second chances as clusters grow; batch grouping via the strict `SWEEP_PROMPT` (same-service/same-failure rule, `clustering-v4`); groups of ≥2 become proposals (or activate directly when `cluster_auto_activate`). **v4: the pool is partitioned by `affected_system` first — every grouping batch is single-system — and minted clusters carry the partition's system.** `start_sweep_worker()` runs it periodically from `app.lifespan`.
- **Flow C** — `audit_cluster(cluster_id)`: nightly purity audit of active clusters (LLM reads all member texts; removes outliers; safeguards: ID reconciliation + pruning floor). Cadence `cluster_audit_interval_s`. **v4: the audit prompt names the cluster's system and instructs that a member from a DIFFERENT system is always removed — legacy mixed clusters are cleaned up on the next pass.**
- **System-scoping guarantee (3 layers)**:
  1. `retrieve_candidates` — Flow A candidates are same-system only.
  2. `sweep_pool` — Flow B batches and mints per system; `clusters.affected_system` is stored on the row (backfilled from dominant member labels for legacy rows at `store.setup()`).
  3. `store.add_cluster_member` — the hard invariant: an incident from a different system is REFUSED even if called directly; an unscoped cluster (`affected_system=''`) is claimed by its first member.
- **User rules**: 1 incident = individual (never a cluster); names/descriptions capped (9 words); single-incident clusters demoted.
- Public API: `build_clusters(period)`, `sweep_pool()`, `assign_in_background()`, `start_sweep_worker()`, `audit_cluster()`.

## Grouping (`grouping.py`)
- Legacy stateless grouping utilities (volume-adaptive sensitivity, offering-based Phase 1). Kept for reference/tests; the live dashboard reads persistent clusters via `build_clusters`.

## Removed from this service (Phase 1)
- `suboffering_cluster.py` / `verifier.py` — the sub-offering engine is dormant (superseded by persistent clustering); both moved to `legacy/suboffering_engine/` (see its README for the frozen v3 verifier prompt + pairwise canary twin).

Consumers: `app.py` (router mounting), `api/reports.py`, `api/diagnostics.py`, `shared/store_clusters.py`.
