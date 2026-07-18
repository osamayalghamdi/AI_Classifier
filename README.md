# AI Incident Classifier

![Uploading architecture-flow.png…]()


Structured incident classification via LLM, with live duplicate detection. Feed it a
title and description, get back validated categories plus a list of similar *open*
incidents so a call center doesn't escalate the same issue twice.

**Phase 1 scope.** Clustering, reports, and LLM re-ranking exist on the `phases-2-3`
branch, paused. See [ROADMAP.md](ROADMAP.md) for status and the longer-range plan.

**How the LLM is used** — classification only, once per incident. Duplicate detection
is embedding cosine similarity against active incidents — no LLM call, no clustering.

## Architecture

```
Incident submitted (+ optional image/PDF → OCR text)
       │
       ▼
  Qwen2.5:7b ──→ Structured labels (system, service, type, severity…)
       │
       ├── Embed (title + description + OCR text + labels)
       ├── Cosine similarity against ACTIVE incidents only ──→ "N similar open incidents"
       └── Save to SQLite (status = active)

Incident resolved
       │
       ▼
  POST /incidents/{id}/resolve ──→ status = resolved, drops out of future duplicate checks
```

## Stack

| Component | Tech |
|-----------|------|
| API | FastAPI (Python 3.12) |
| LLM | Qwen2.5:7b via Ollama on RTX 2060 SUPER |
| Embeddings | all-MiniLM-L6-v2 (sentence-transformers) |
| OCR | EasyOCR (English + Arabic, GPU-accelerated) |
| Store | SQLite + cosine similarity |
| Validation | Pydantic v2 (strict — no silent coercion) |
| Container | Docker Compose with nvidia runtime |

## Quick Start

```bash
docker compose up --build -d

curl http://localhost:8000/health

curl -X POST http://localhost:8000/classify \
  -H "Content-Type: application/json" \
  -d '{
    "title": "Checkout timeouts for 20% of users",
    "description": "Users in EU see 504 errors during purchase."
  }'

# Mark an incident resolved (stops it from surfacing as a duplicate)
curl -X POST http://localhost:8000/incidents/<incident_id>/resolve

# Frontend
open http://localhost:8082
```

## API

### POST /classify

```json
// Request
{
  "title": "Checkout timeouts",
  "description": "504 errors for EU users.",
  "extracted_text": "" // optional — OCR text from /api/ocr, included in the embedding
}

// Response
{
  "incident_title": "Checkout timeouts",
  "classification": {
    "affected_system": "Payment Gateway",
    "service": "Checkout",
    "incident_type": "Degradation",
    "severity": "Major",
    "urgency": "High",
    "category": "Performance",
    "confidence": "high",
    "reasoning": "Partial degradation of checkout."
  },
  "incident_id": "a1b2c3d4e5f6",
  "similar_open_incidents": [
    { "id": "...", "title": "PayPal checkout errors", "similarity": 0.87, "classification": { ... } }
  ]
}
```

`similar_open_incidents` only ever contains incidents with `status = active` — once an
incident is resolved it stops counting as a potential duplicate.

### GET /classify

Same shape, query-parameter variant: `?title=...&description=...`

### POST /incidents/{incident_id}/resolve

Marks an incident resolved so it no longer surfaces in future duplicate checks.
Returns `404` if the ID is unknown.

```json
{ "incident_id": "a1b2c3d4e5f6", "status": "resolved" }
```

### POST /ocr

Proxied through the frontend at `/api/ocr`. Accepts an image or PDF (`multipart/form-data`,
field name `file`), with an optional `?lang=en|ar` query param. Returns extracted text for use
as `extracted_text` in `/classify`.

```json
{ "text": "...", "has_low_confidence": false, "low_confidence_words": [] }
```

### GET /health

```json
{ "status": "ok", "model": "ollama/qwen2.5:7b", "store_ready": true }
```

## Taxonomy

| Field | Values |
|-------|--------|
| affected_system | CRM, ERP, Payment Gateway, Infrastructure, Network, Security, Email, Data Pipeline, Other |
| incident_type | Spike, Degradation, Unavailability, Outage |
| severity | Critical, Major, Minor, Cosmetic |
| urgency | Immediate, High, Medium, Low |
| category | Hardware, Software, Network Issue, Security, Performance, Configuration, Human Error, External / Third Party, Other |

Each system has its own services (e.g. `Payment Gateway` → `Checkout`, `Refunds`, `Fraud Detection`, `Billing`, `Invoice Generation`).

## Key Design Decisions

**Classification-augmented embeddings.** The embedding text includes title, description, OCR text (if any), and the LLM's structured labels:

```
"Stripe payment timeouts | Classified as: Payment Gateway / Checkout / Degradation / Performance"
```

This makes "PayPal errors" and "Stripe timeouts" match as duplicates via their shared fingerprint, even with different wording.

**Active-only duplicate search.** Similarity search only scans incidents with `status = active`. A resolved incident stops flagging new submissions as duplicates — the goal is "is anyone currently handling this," not "has this ever happened before."

**Strict validation + graceful fallback.** LLM output goes through Pydantic validation. If the LLM fails twice, a low-confidence fallback is returned instead of an error.

## Configuration

| Variable | Default | Purpose |
|----------|---------|---------|
| `LLM_MODEL` | `ollama/qwen2.5:7b` | Model name |
| `LLM_API_KEY` | — | API key (optional for Ollama) |
| `LLM_API_BASE` | — | Self-hosted LLM endpoint |
| `DB_PATH` | `/data/incidents.db` | SQLite path (persistent volume) |
| `SIMILARITY_THRESHOLD` | `0.35` | Cosine similarity threshold for duplicate detection |
| `EMBEDDING_MODEL` | `all-MiniLM-L6-v2` | Sentence-transformer model |

## Files

```
ai_classification/
├── main.py           → Routes: classify, resolve, health
├── service.py         → Orchestration + app lifecycle
├── classifier.py     → LLM prompts, retry logic
├── incident_store.py → SQLite, embeddings, active-only similarity search
├── models.py         → Pydantic request/response schemas
├── schemas.py        → Taxonomy enums
└── config.py         → Env-based settings

ocr/
├── ocr_server.py     → EasyOCR microservice (English + Arabic)
└── Dockerfile

frontend/
├── index.html        → Single-page UI (Classify tab, file upload for OCR)
├── app.js             → API calls, rendering, history, resolve action
├── style.css
├── nginx.conf         → Static files + proxy to api/ocr
└── Dockerfile

tests/
├── test_classifier.py     — Unit tests for prompt building & validation (mocked LLM)
├── test_incident_store.py — Unit tests for embeddings, similarity, resolve
├── test_service.py        — Unit tests for orchestration
└── e2e_check.py           — End-to-end with real Ollama
```
