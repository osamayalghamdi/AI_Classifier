# Match Service Contract

Similarity matcher — routes classified incidents into sub-offerings via exemplar matching. Pipeline stage 20.

## Sub-offering matcher (`suboffering.py`)
- **`offering_of(service)`** — first segment of the service string, split on the LAST `.` (rsplit — versioned names like "7.1 Invoicing …" are safe). No dot → None.
- **`embed_pure(title, description)`** — frozen bge-m3 embedding of pure text `title + "\n" + description`, normalized; None when the model is not loaded.
- **`match_against_exemplars(embedding, exemplar_rows)`** — best ACTIVE exemplar by cosine; returns `(sub_offering_id, sim)` or `(None, -1.0)`.
- **`feed_incident(incident, match_threshold=MATCH_THRESHOLD)`** — side-channel routing, never mutates the incident's classification:
  - no offering → `{"offering": OFFERING_000, "routed": "offering-000"}`
  - matched (≥ threshold) → attach ticket as new exemplar (grows the sub-offering)
  - unmatched → add to the offering's `unmatched_pool` (batch clustering fodder)
- **`MATCH_THRESHOLD = 0.60`** — starting value; tuning evidence pending (see STATUS.md).
- **`OFFERING_000 = "OFFERING-000"`** — sentinel for tickets with no resolvable offering (W3 scope, skipped here).

## Dependencies
- `ai_classification.core.store` (store singleton: `_model`, `pool_add`, exemplar queries) — the ONLY core dependency.
- Consumed by: `services.cluster.grouping` (Phase 2 embedding), `services.cluster.suboffering_cluster` (candidate generation), core store pool feeds.
