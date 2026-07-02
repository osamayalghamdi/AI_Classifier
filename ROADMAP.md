# Enterprise Roadmap

This is the long-range plan for evolving the classifier from a working prototype into
a system employees can rely on. It's organized by concern, not by sprint — pull items
into [TODO.md](TODO.md) when they're actually being worked.

The core design instinct — LLM only at the edges (classification + cluster
summarization), everything else deterministic and cheap (embeddings + SQL) — is right
and should survive all of this. The work below is about wrapping that core in
identity, durability, feedback, and operational visibility.

---

## Sequencing

1. **Auth + audit + Postgres/pgvector migration**
2. **Async pipeline + observability**
3. **Correction UI + golden-set eval + Phase-1 accuracy measurement on real data**
4. **Model swap (Arabic embeddings/OCR) with re-embed backfill machinery**
5. **Lifecycle + duplicate detection + notifications**
6. **Periodic re-clustering + integrations**

Steps 1–3 are the "safe to put in front of employees" bar. Everything after is what
makes them keep using it.

---

## Architecture

- **Split the write path into stages.** `/classify` currently does everything
  synchronously: LLM classify → embed → similarity → cluster → LLM summarize. Move to
  an ingestion pipeline: the API persists the incident immediately with status
  `pending` and returns an ID. A worker (Celery, ARQ, or a simple Redis/RabbitMQ
  queue) does classification, clustering, and summarization asynchronously. The
  frontend polls or uses SSE/WebSocket for the result. Gets you: instant response
  times, retry-with-backoff on LLM failures instead of a silent low-confidence
  fallback, graceful degradation when the GPU box is down (incidents queue instead of
  failing), and batching.

- **Decouple summarization entirely.** Cluster summaries don't need to update on
  every insert — debounce them (e.g., at most once per 5 minutes, or on a schedule).
  Removes the most expensive LLM call from the hot path.

- **Make classification a versioned, replayable operation.** Store the model name,
  prompt version, and taxonomy version alongside every classification. When the model
  or taxonomy changes — which will happen repeatedly — you can re-run classification
  over historical incidents and diff the results. Without this, every prompt tweak
  silently invalidates your history.

- **Abstract the OCR and embedding providers behind interfaces now.** Define a small
  `Embedder` protocol (`embed(texts) -> vectors`, plus `model_id` and `dimension`) and
  an `OcrProvider` protocol. Critically: store the embedding model ID and dimension
  with every vector. When you swap to the Arabic-optimized model, all old embeddings
  are garbage — you need to know which rows to re-embed, and the system should refuse
  to compare vectors from different models. Build the re-embedding backfill job as a
  first-class feature, not a one-off script.

## Data layer & scalability

- **SQLite → PostgreSQL + pgvector.** Current similarity search loads every embedding
  into Python and does brute-force cosine per query — O(incidents) per classify,
  under a Python lock, single-writer database. Fine for a demo, dies at a few
  thousand incidents with concurrent users. Postgres gives concurrent writers and
  real migrations (Alembic); pgvector gives indexed ANN search (HNSW) so similarity
  becomes a single SQL query. Keeps the "reports are just SQL" property the system
  was designed around.

- **Fix the clustering model.** Incremental nearest-centroid clustering is
  order-dependent and drifts — early misclassifications seed bad clusters that
  attract everything after. Keep incremental assignment for real-time UX, but add a
  periodic re-clustering job (nightly) that rebuilds clusters from scratch (HDBSCAN
  or agglomerative over the vectors), and track cluster lineage so IDs referenced in
  reports stay resolvable. Add a **manual override**: humans must be able to split
  clusters, merge clusters, and move an incident — and those corrections must be
  sticky (not undone by the next re-cluster).

## Security (the biggest gap)

There is currently no authentication, authorization, audit trail, or input hardening.
Minimum bar for internal company use:

- **AuthN via the company IdP** (OIDC/SAML — Azure AD, Okta, whatever's in use).
  Don't build passwords.
- **AuthZ/RBAC**: at least viewer / reporter / triager / admin. Cluster overrides and
  re-classification should be role-gated.
- **Audit log**: who submitted, who edited, who overrode a cluster, when. Incident
  descriptions may contain sensitive data (bank refs, phone numbers, names) —
  auditors will ask who saw what.
- **PII handling**: incident descriptions contain passport numbers, transaction refs,
  phone numbers. Decide a policy: redact before sending to the LLM? Encrypt at rest?
  Retention period? If the LLM ever moves off-prem (API calls leaving the network),
  this becomes a compliance question, not a technical one — get data residency and
  DPA sign-off before any pilgrim data leaves KSA infrastructure.
- **Hardening**: rate limiting per user, request size limits on the API (not just
  OCR), CSRF story for the frontend, secrets in a vault (not `.env` in compose),
  containers running as non-root, TLS everywhere, and file-type validation on OCR
  uploads (magic bytes, not filename — a malicious PDF hitting poppler is a classic
  attack surface; run OCR in a locked-down sandbox with no network egress).

## Reliability & operations

- **Observability**: structured JSON logging with request IDs, Prometheus metrics
  (classify latency p50/p95, LLM retry rate, parse-failure rate, queue depth, cluster
  count), traces (OpenTelemetry) across API → worker → LLM. TODO.md's Phase-1
  "minimal logging" item is the seed of this — make it structured from day one so it
  doesn't need redoing.
- **LLM-specific monitoring**: track confidence distribution, fallback rate, and
  per-field agreement drift over time. If the fallback rate creeps from 1% to 8%,
  that should be an alert, not a user complaint.
- **Health checks that mean something**: `/health` should verify DB connectivity,
  queue reachability, and LLM responsiveness (cached probe), and expose readiness vs
  liveness separately for orchestration.
- **Backups & DR**: automated Postgres backups with tested restore. Right now the
  incidents data is one docker volume away from total loss.
- **CI/CD**: lint (ruff) + type-check (mypy/pyright) + tests on every PR, image
  scanning, migrations run automatically, staged deploys. Add contract tests for the
  LLM prompt (golden-set: N labeled incidents that must classify correctly before a
  prompt/model change ships).

## Quality & the human loop

This is what determines whether the tool survives contact with users:

- **Correction UI**: let triagers fix a wrong classification in one click. Store
  corrections as first-class data (`predicted` vs `corrected` vs `who`/`when`).
- **Accuracy dashboard**: correction rate per field, per language, per system — this
  is the Phase-1 measurement, made continuous instead of a one-time report.
- **Feedback → improvement loop**: corrected examples become the eval set, then the
  few-shot pool, then eventually fine-tuning data. The 10k historical incidents are a
  real asset — design the schema so corrections accumulate cleanly.
- **Golden-set gating**: no model/prompt/taxonomy change deploys unless it meets or
  beats current accuracy on the eval set.

## UX & missing features

- **Duplicate detection at submission time**: before creating an incident, show "3
  similar open incidents — is this one of them?" Single highest-value UX feature for
  an internal tool — cuts noise at the source.
- **Incident lifecycle**: status (open/investigating/resolved), assignee, resolution
  notes, timestamps. Already in TODO.md — not optional for company use; a classifier
  without lifecycle is a labeling toy.
- **Full Arabic UX**: RTL layout, Arabic UI strings, Arabic-language cluster
  summaries (prompt the summarizer in the reporter's language or both). Users are
  handling Arabic tickets — an English-only UI undermines the Arabic-optimized models
  being planned.
- **Search & filters**: full-text + semantic search over incidents, filter by
  system/severity/date/status. Reports alone aren't enough once there are thousands
  of records.
- **Notifications**: alert a channel (Slack/Teams/email) when a cluster crosses a
  threshold (e.g., 5 incidents in an hour, or worst severity hits Critical) — turns
  clustering from a report feature into an early-warning system, which is the real
  business value.
- **Integrations**: webhook/API to create or link tickets in whatever the company
  already uses (Jira/ServiceNow). If people must double-enter, adoption dies.
  Server-side history instead of localStorage, obviously.
