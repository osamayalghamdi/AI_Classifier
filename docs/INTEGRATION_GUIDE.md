# AI Incident Classifier — Integration Guide (v1)

Everything the other team needs to call the system **without reading code**.
Base URL: `https://<host>/` (nginx proxies `/api/*` to the API service;
local dev: `http://localhost:8000`).

---

## 1. Authentication

Every endpoint **except** `GET /health` and `GET /ready` requires a bearer
token:

```
Authorization: Bearer <INTEGRATION_TOKEN>
```

The token is set server-side via the `INTEGRATION_TOKEN` environment
variable (from `.env`). There is **no default** — if it is not configured,
all integration endpoints reject requests with `401 UNAUTHORIZED`.

```
curl -s -X POST http://localhost:8000/api/v1/incidents \
  -H "Authorization: Bearer $INTEGRATION_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"source_reference":"TKT-1001","title":"Rawdah permit error","description":"date selection fails"}'
```

## 2. Endpoints

| Method | Path | Auth | Purpose |
|---|---|---|---|
| POST | `/api/v1/incidents` | yes | **Ingest ONE incident** (async) — returns immediately |
| GET | `/api/v1/incidents/{reference}` | yes | Fetch result/status by reference |
| POST | `/api/v1/incidents/{reference}/status` | yes | **Status-only update** of an ingested incident (same reference → same row) |
| POST | `/api/v1/incidents/dry-run` | yes | Same contract, synchronous, writes nothing |
| POST | `/api/v1/backfill` | yes | Batch ingest (≤200) / one-time historical run |
| POST | `/api/v1/smax/webhook` | yes | **SMAX push receiver** — new incident OR status change in one URL |
| GET | `/api/v1/jobs?limit=20` | yes | Ops view of the queue |
| POST | `/api/v1/worker/tick?limit=10` | yes | Advance the queue manually |
| GET | `/health` | **no** | Liveness |
| GET | `/ready` | **no** | Readiness — DB / embedding / LLM reported individually |

> **Bulk-ingest rule:** loads of **>20 tickets go through this API**
> (`/api/v1/backfill` or repeated `/api/v1/incidents`), NOT through
> `/classify` / `/classify/batch`. The integration API is async: every
> ticket is a job row, the retry worker processes the queue with linear
> backoff (`INTEGRATION_RETRY_BASE_S`, `INTEGRATION_MAX_ATTEMPTS`), and no
> HTTP connection is held open for the whole run. `/classify` is for single
> interactive tickets; `/classify/batch` is serial by design and has no
> retry worker (only the optional `CLASSIFY_BATCH_SLEEP_S` pacing delay).

### 2.1 E1 — Ingest one incident (async)

Request payload — **strict** (unknown fields are rejected, see §4):

```json
{
  "source_reference": "TKT-1001",
  "title": "Rawdah permit date error",
  "description": "Error when selecting a date for the pilgrim group",
  "status": "active",
  "attachments": [],
  "created_at": null,
  "updated_at": null
}
```

> **`status` is DYNAMIC by design** (SMAX webhook requirement). Any value is
> accepted — `active`, `verified`, `resolved`, `closed`, or a status SMAX
> adds next week; a new value never causes a 422. The raw value is stored
> verbatim in the incident's `source_status`; the system derives its local
> `active`/`resolved` view from it (resolved-like values: resolved, closed,
> verified, cancelled, rejected, duplicate, …; everything else stays
> active).

Response `202 Accepted` — the caller never waits on the LLM:

```json
{
  "reference": "TKT-1001",
  "status": "pending",
  "location": "/api/v1/incidents/TKT-1001"
}
```

The reference is the idempotency key AND the tracing id (every log line
for this incident carries it). Processing happens in the background.

### 2.2 E2 — Fetch result by reference

```
GET /api/v1/incidents/TKT-1001
```

```json
{
  "source_reference": "TKT-1001",
  "status": "succeeded",
  "attempts": 0,
  "error": null,
  "result": {
    "source_reference": "TKT-1001",
    "is_new": true,
    "incident_id": "a5523fbf2872",
    "classification": { "affected_system": "Nusuk Masar Haj", "...": "..." },
    "similar_tickets": [],
    "suggestions": [],
    "confidence": "high",
    "model_version": "openrouter/qwen/qwen3.6-35b-a3b",
    "prompt_version": "v3",
    "persist": { "action": "new", "incident_id": "a5523fbf2872" },
    "write_back": { "mode": "suggestions", "applied": false }
  }
}
```

Status values: `pending` → `processing` → `succeeded` | `retryable` → `flagged`.
Unknown reference → `404 NOT_FOUND`.

### 2.3 E3 — Batch ingest / backfill (one-time historical run)

```
POST /api/v1/backfill
{"incidents": [ { ...same schema as E1... }, ... ]}   // 1..200
```

→ `202 {"total": N, "references": [...], "location_prefix": "/api/v1/incidents/"}`
Each reference is pollable via E2. Replays of the same reference are safe
(idempotent — one job per reference).

### 2.4 E5 — Dry-run (writes nothing, persists nothing)

```
POST /api/v1/incidents/dry-run   (same payload as E1)
```

Synchronous: returns the classification + exactly what would be written:

```json
{
  "reference": "TKT-1001",
  "is_new": true,
  "classification": { "...": "..." },
  "suggestions": [],
  "would_write": { "dry_run": true, "action": "new" },
  "write_back": { "mode": "suggestions", "applied": false }
}
```

No job row, no incident row, no write-back. Use it to preview before
ingesting.

### 2.5 E4 — Health & readiness

```
GET /health     -> 200 {"status":"ok","model":"...","store_ready":true}
GET /ready      -> 200/503 {"status":"ok|degraded|unhealthy","checks":{...}}
```

`/ready` reports **each dependency individually**:

```json
{
  "status": "degraded",
  "checks": {
    "db": "ok",
    "embedding": "ok",
    "llm": "unreachable: ConnectionError"
  }
}
```

- `db` = live `SELECT 1`
- `embedding` = embedding model loaded
- `llm` = real 1-token probe against the configured LLM endpoint
  (a company endpoint that is unreachable from the caller's network shows
  as `unreachable` while db/embedding stay `ok` — degraded, not down)

### 2.6 E10 — Status-only update by reference

The **"same ID → change the status"** rule as a normalized endpoint: an
incident already ingested under a `source_reference` gets a **status-only
update** — the same incident row is updated, nothing is re-classified and
no new row/duplicate is created.

```
POST /api/v1/incidents/{reference}/status
Authorization: Bearer <INTEGRATION_TOKEN>
```

```json
{
  "status": "verified",
  "updated_at": "2025-01-11T12:00:00Z"
}
```

- `status` is dynamic (same rule as E1 — any value accepted, stored raw).
- Response `200`:
  ```json
  {
    "action": "updated",
    "reference": "TKT-1001",
    "incident_id": "8fe666825145",
    "status": "resolved",
    "source_status": "verified",
    "updated_at": "2025-01-11T12:00:00+00:00"
  }
  ```
- `404 NOT_FOUND` when the reference was never ingested — ingest it first
  via E1 (or the webhook below).

### 2.7 SMAX webhook — one URL for new incidents AND status changes

Configure SMAX's outbound notification to POST to:

```
POST /api/v1/smax/webhook
Authorization: Bearer <INTEGRATION_TOKEN>
Content-Type: application/json
```

The endpoint accepts SMAX's **raw payload** — field-name translation is
tolerant (id/title/description/status/timestamp aliases are recognized,
unknown fields ignored, wrappers like `{"event": {"record": {...}}}`
unwrapped). Dispatch is by **source reference** (the SMAX ticket id):

| Situation | Action | Response |
|---|---|---|
| reference **unknown** | enqueue for **async classification** (E1 path) | `202` `{"action":"created", ...}` |
| reference **known** | **status-only update** of the existing row | `200` `{"action":"updated", ...}` |
| no usable id in payload | — | `400 INVALID_PAYLOAD` |

Example push (new incident):

```json
{
  "ticket_id": "SMAX-1001",
  "title": "Rawdah permit date error",
  "description": "Error when selecting a date for the pilgrim group",
  "status": "Active",
  "created": "2025-01-10T08:00:00Z"
}
```

Example push (status change — same id, new status):

```json
{ "ticket_id": "SMAX-1001", "status": "Verified" }
```

Notes:

- **Statuses are dynamic**: `Active`, `Verified`, `Resolved`, `Closed` or
  any future value are all accepted and stored verbatim in
  `source_status`; only the local `active`/`resolved` view is derived.
- A status change for a reference still sitting in the job queue (pushed
  "created" then immediately "status changed" before classification ran)
  refreshes the queued payload, so the worker persists the **latest**
  status.
- The raw status is visible wherever incidents are served
  (`source_status` field on incident payloads).

## 3. Write-back default (SAFEST)

`INTEGRATION_WRITE_BACK` (default **`suggestions`**): processed results and
suggestions land in the job's result area — **never written into ticket
fields**. `none` = no write-back at all; `full` = write back to the ticket
source (needs a configured source adapter). Every response carries the
effective mode in `write_back.mode`.

## 4. Error codes (stable, machine-readable)

All errors are `{"error": {"code": <code>, "message": ..., "reference": ...}}`.

| Code | HTTP | Meaning |
|---|---|---|
| `UNAUTHORIZED` | 401 | Missing/invalid/misconfigured token |
| `INVALID_PAYLOAD` | 422 | Schema violation — incl. **unknown fields** (`fields` list details each) |
| `NOT_FOUND` | 404 | No job with that reference |
| `DUPLICATE` | 409 | Reserved — duplicate content detected |
| `LLM_UNAVAILABLE` | (job) | LLM unreachable (DNS/connection) — job retryable |
| `LLM_TIMEOUT` | (job) | LLM timed out — job retryable |
| `EMBEDDING_FAILED` | (job) | Embedding model failure — job retryable |
| `DB_FAILURE` | (job) | Persist-phase database failure — job retryable |
| `RETRYABLE` / `FLAGGED` | (job) | Job state — see §5 |

## 5. Retry semantics (async jobs)

- Any failure leaves the incident **retryable, never half-written**:
  classification is read-only; persistence is a single step that only runs
  after classification succeeded.
- Failures are classified: `LLM_UNAVAILABLE` / `LLM_TIMEOUT` /
  `EMBEDDING_FAILED` / `DB_FAILURE` — recorded on the job with the real
  error message.
- Retry: linear backoff (`INTEGRATION_RETRY_BASE_S` × attempt), up to
  `INTEGRATION_MAX_ATTEMPTS` (default 5). After exhaustion the job is
  **`flagged`** — never silently dropped. A flagged job's `error` carries
  the final code + message; `GET /api/v1/incidents/{reference}` shows it.

## 6. Idempotency

`source_reference` is the primary key of the job table. Re-posting the
same reference **does not re-enqueue and does not re-process**: the
existing job is returned unchanged (safe replays). Content-level dedupe
additionally collapses identical title+description into a single incident.

## 7. Tracing

Every request and every worker outcome is logged with the
`source_reference` — grep the API logs by reference to trace an incident
end-to-end (accept → process → persist → result).

## 8. Working examples (curl)

```bash
# token
export INTEGRATION_TOKEN=$(grep ^INTEGRATION_TOKEN .env | cut -d= -f2-)
AUTH="Authorization: Bearer $INTEGRATION_TOKEN"

# E1 ingest
curl -s -X POST http://localhost:8000/api/v1/incidents \
  -H "$AUTH" -H "Content-Type: application/json" \
  -d '{"source_reference":"TKT-2001","title":"Permit booking fails","description":"error 10036015"}'

# E2 poll the result
curl -s http://localhost:8000/api/v1/incidents/TKT-2001 -H "$AUTH"

# E5 dry-run (no side effects)
curl -s -X POST http://localhost:8000/api/v1/incidents/dry-run \
  -H "$AUTH" -H "Content-Type: application/json" \
  -d '{"source_reference":"TKT-2001","title":"Permit booking fails","description":"error 10036015"}'

# E3 backfill
curl -s -X POST http://localhost:8000/api/v1/backfill \
  -H "$AUTH" -H "Content-Type: application/json" \
  -d '{"incidents":[{"source_reference":"BF-1","title":"A","description":"a"}]}'

# E4 health/readiness (no auth)
curl -s http://localhost:8000/ready
```

## 9. Running the integration test suite

```bash
export PG_DATABASE=ai_incidents_test INTEGRATION_TOKEN=test-token \
       INTEGRATION_WORKER_ENABLED=0
.venv/bin/python -m pytest tests/test_integration_api.py -q
```

(The worker is disabled under test so the queue is driven synchronously;
the E5 test spawns a subprocess pointed at the REAL unreachable company
endpoint and asserts the retryable→flagged path with the actual
DNS/connection error.)

## 10. Reference client — integrations/smax

The canonical example consumer of this API is the **SMAX connector** in
`integrations/smax/` — a standalone process with **zero** `ai_classification`
imports that talks to the classifier only over HTTP:

- `submit(incident)` → `POST /api/v1/incidents` (Bearer, 202 + reference)
- `result(ref)` → `GET /api/v1/incidents/{ref}` (polls to a terminal state)
- `backfill(incidents)` → `POST /api/v1/backfill` (chunked ≤200)

Since the SMAX **push** flow (webhook, §2.7) is the receive path, the
connector's poller path no longer needs to handle status changes — but when
it does, the endpoint to call is E10 (§2.6): `POST /api/v1/incidents/{ref}/status`.

Run it (see `integrations/smax/README.md` for the full env table):

```bash
export CLASSIFIER_API_URL=http://localhost:8000
export CLASSIFIER_API_TOKEN=<INTEGRATION_API_TOKEN from .env>
python -m integrations.smax.main --once      # one poll + write-back pass
```

Use it as a template for any other upstream system: the E1-E9 contract here
is the only surface it needs.
