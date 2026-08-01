# Load Test Results — 60-ticket burst (2026-08-01)

Task 3 of the sync-classify workstream. Measure only — no fixes in this branch.
Cascade classifier (CASCADE_CLASSIFICATION=true), OpenRouter qwen3.6-35b-a3b,
bge-m3 embeddings on CPU, Postgres 15 + pgvector. Throwaway DB `ai_loadtest`
(created for the run, dropped after — dev `ai_incidents` untouched, 115 rows).

Corpus: 60 realistic Hajj/Nusuk tickets, `{title, description}` only,
60 distinct content_hashes (digit-blanking verified not to collide).

## Scenario A — 60 concurrent POST /classify

```
total wall time: 21.79s
latency min/median/p95/max: 494/15246/18158/21770 ms
ok: 52 | errors: 8 | timeouts: 0
```
- 8 errors = `psycopg2.pool.PoolError: connection pool exhausted`
  (pool maxconn=25 vs 60 concurrent). Failures occurred AFTER the LLM calls
  (at find_similar/save) — those 8 tickets burned ~2 LLM calls each and were
  lost (no row saved).
- LLM calls: 60 service-stage + 53 offering-stage + 0 system-stage
  (all tickets system-deterministic) = 113 calls / 60 tickets = **1.88/ticket**.

## Scenario B — /classify/batch, split 50+10 (schema max 50)

Clean store (rows flushed before the run):
```
batch50: wall=265.37s | total=50 failed=0 | per-ticket≈5307ms
batch10: wall=38.66s  | total=10 failed=0 | per-ticket≈3866ms
```
- Sequential processing in classify_batch — 0.2 tickets/s end-to-end.
- LLM calls: 60 service + 54 offering = 114 / 60 = **1.9/ticket**.
- (Re-run against the scenario-A store: 22.8s / 1.4s — batch10's 166ms/ticket
  were content-hash-gate hits returning the existing incident, 0 LLM.)

## Scenario C — 60 tickets staged behind the sync worker (SYNC_INTERVAL=20)

```
worker started          17:52:40,673
first ticket seen       17:52:40,684
last incident saved     17:57:38,845
Synced 60 incidents     17:57:38,845
```
- One poll pulled all 60; the worker classifies sequentially → drain = 298s
  (~5s/ticket). DB after: 60 rows, 60 distinct hashes, all classification_json
  non-empty, occurrence_count=1 everywhere, 0 errors.

## Race probe — 5 identical concurrent POSTs (same content)

```
incident_ids: 5531394f92f0, 12dd25adda55, 02e133da4e59, f7466e749105, d3db9ef8b210
DB: 5 rows, 5 distinct ids, 1 distinct content_hash, occurrence_count=1 each
```
- The content-hash gate is check-then-insert (non-atomic): under concurrency,
  identical duplicates create N rows instead of 1 row with occurrence_count=N.
- Corroborated earlier: a 60-ticket corpus whose digit-blanking collapsed to 15
  hashes produced 52 rows under 60-way concurrency.

## Comparison table

| Scenario | Wall (60 tickets) | Per-ticket | Errors | LLM calls/ticket | Data integrity |
|---|---|---|---|---|---|
| A: 60 concurrent POST | 21.8s | med 15.2s / p95 18.2s | **8×500 (pool exhausted)** | 1.88 | 8 tickets lost |
| B: batch 50+10 sequential | 304s | ~5.3s | 0 | 1.9 | all saved |
| C: sync worker one poll | 298s drain | ~5.0s | 0 | ~1.9 | all saved |
| Race: 5 identical concurrent | — | — | 0 | — | **5 rows for 1 incident** |

## Verdict (plain language)

- **Pull-sync does NOT absorb spikes without cost.** Scenario C absorbed the
  60-ticket burst without a single error (sequential = no pool contention), but
  took ~5 minutes to drain — a 5-minute classification lag behind the ticketing
  feed. That lag is LLM-throughput-bound (~2 calls/ticket, ~2–3s each), not
  fixable by queueing alone; a bounded-concurrency worker (pool-sized) would
  cut drain to ~30s at the cost of pool pressure.
- **A queue/limit is needed at the API layer.** Scenario A's 13% failure rate is
  connection-pool exhaustion (maxconn=25), and the failures happen AFTER the
  expensive LLM work — the worst possible place to lose a ticket. A bounded
  request queue (or pool maxconn ≥ concurrent clients) prevents silent loss.
- **The dedupe gate is racy.** Identical content arriving concurrently creates
  duplicate rows (5/5 in the probe). If dedupe matters under burst, the
  check-then-insert must become atomic (unique index on content_hash +
  ON CONFLICT, or SELECT ... FOR UPDATE) — otherwise occurrence_count
  under-counts and duplicate incidents leak into grouping.
- **Latency profile is LLM-bound, not IO-bound:** batch and sync both sit at
  ~5s/ticket; the cascade's 2-call path dominates. Increasing concurrency
  without raising the pool ceiling just moves the 500s earlier.

## Artifacts / methodology

- Drivers: /tmp/scenario_a.py, /tmp/scenario_b.py, /tmp/stub_ticketing.py,
  corpus /tmp/loadtest_tickets.json (60 unique).
- Server logs: /tmp/uvicorn_loadtest2.log (scenarios A+B, debug), 
  /tmp/uvicorn_scenarioc.log (scenario C).
- Isolation: throwaway DB ai_loadtest created → row-flushed between scenarios →
  dropped after; .last_sync restored to HEAD value after scenario C.
