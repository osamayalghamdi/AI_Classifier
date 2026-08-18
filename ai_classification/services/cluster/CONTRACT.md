# Cluster Service Contract

Clustering engine — turns classified incidents into named clusters and sub-offering proposals. Pipeline stages 30–40.

## Grouping (`grouping.py`)
- Two-phase: **Phase 1** offering exact-match (same offering = guaranteed cluster, no LLM); **Phase 2** OFFERING-000 tickets via bge-m3 cosine + LLM validator (graph components ≥ MIN_DENSITY, > MAX_VALIDATOR_GROUP_SIZE dropped).
- **Volume-adaptive sensitivity**: threshold/min-size are a deterministic function of active incident count — LOOSE regime (≤20 incidents: 0.40, min 2) → TIGHT (≥150: 0.60, min 4), linear interpolation between.
- **LLM Arabic naming**: `_arabic_cluster_name()` derives a human-readable Arabic title from member tickets; cached per member-ID fingerprint (24h TTL) so 5-min rebuilds don't re-hit the LLM. English fallback on failure.
- **Verdict cache**: keyed by sorted member-ID fingerprint; `invalidate_incident(id)` evicts every cluster containing the ticket; `invalidate_cache()` clears all.
- Public API: `build_clusters(period)`, `request_rebuild()`, `start_rebuild_loop()`, `invalidate_cache()`, `invalidate_incident()`.

## Proposals (`suboffering_cluster.py`)
- Per-offering pool batch job: candidates (FLOOR 0.40, TOP_N 10) → strict v3 pairwise verification → union-find → oversize guard (>20 re-verify weakest 25%) → drift review (max_tokens 2000, chunk 25, retry-once-then-FLAG) → purity floor (mean_sim < 0.45 OR > 6 FM codes → NEEDS_REVIEW) → **PROPOSALS (never mints)**; auto-accept ≥ 0.90 cap-exempt.
- `OFFERING000_MAX_MEMBERS = 10` W3 guard. Cross-offering edges impossible (disjoint pools).

## Verifier (`verifier.py`)
- Frozen prompt v3 (verbatim twin of tests/test_pairwise_canary.py — drift-guarded), batch 8 pairs/call, retry-once-individual, UNRESOLVED→NO, temp 0.0 seed 42, cache key includes `PROMPT_VERSION`.

Consumers: api/routes.py, core/store.py (rebuild loop), core/heal.py.
