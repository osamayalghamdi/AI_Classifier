# AI Incident Classifier

Structured incident classification via LLM. Feed it a title and description, get back validated categories plus a daily/weekly report of clustered incidents.

**How the LLM is used** — classification (always), cluster summarization (once per cluster). Everything else is embedding cosine similarity + SQL. Reports return in < 10ms with zero LLM calls.

## Architecture

```
Incident submitted (+ optional image/PDF → OCR text)
       │
       ▼
  Qwen2.5:7b ──→ Structured labels (system, service, type, severity…)
       │
       ├── Save to SQLite (embedding = title + description + OCR text + labels)
       ├── Check cluster centroids ──→ matched? → join that cluster
       │     (fast: O(clusters), no member scan)
       ├── Show top 5 similar incidents as "related"
       └── Cluster has ≥2 members? ──→ LLM writes/updates summary
                                          centroid = mean of all member embeddings

Report requested
       │
       ▼
  SQL join: clusters + members ──→ sorted by count → < 10ms response
```

**Key insight**: clusters are built incrementally at classification time. The report is just a read.

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

# Reports
curl http://localhost:8000/reports/daily
curl http://localhost:8000/reports/weekly

# Frontend (tabs: Classify + Reports)
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
  "related_incidents": [
    { "id": "...", "title": "PayPal checkout errors", "similarity": 0.72, "classification": { ... } }
  ]
}
```

### GET /classify

Same shape, query-parameter variant: `?title=...&description=...`

### POST /ocr

Proxied through the frontend at `/api/ocr`. Accepts an image or PDF (`multipart/form-data`,
field name `file`), with an optional `?lang=en|ar` query param. Returns extracted text for use
as `extracted_text` in `/classify`.

```json
{ "text": "...", "has_low_confidence": false, "low_confidence_words": [] }
```

### GET /reports/daily | /reports/weekly

```json
{
  "period": "Today",
  "total_incidents": 9,
  "clusters": [
    {
      "summary": "Widespread payment gateway failures affecting multiple providers.",
      "affected_system": "Payment Gateway",
      "affected_service": "Checkout",
      "count": 5,
      "worst_severity": "Critical",
      "incidents": [
        { "id": "...", "title": "PayPal errors", "severity": "Major", "created_at": "..." }
      ]
    }
  ]
}
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

This makes "PayPal errors" and "Stripe timeouts" cluster together via their shared fingerprint, even with different wording.

**Same-system filter.** Incidents only cluster with others that share the same `affected_system`. A VPN incident can't leak into a Payment Gateway cluster.

**Member-average centroid.** Each cluster's centroid is the normalized mean of all member incident embeddings, recalculated whenever the cluster summary updates. New incidents check centroids first — O(clusters) instead of O(incidents) — and fall back to per-incident similarity if no centroid matches.

**Incremental clustering.** Clusters are built at classification time, not query time. Reports are fast SQL reads with no LLM calls.

**Strict validation + graceful fallback.** LLM output goes through Pydantic validation. If the LLM fails twice, a low-confidence fallback is returned instead of an error.

## Configuration

| Variable | Default | Purpose |
|----------|---------|---------|
| `LLM_MODEL` | `ollama/qwen2.5:7b` | Model name |
| `LLM_API_KEY` | — | API key (optional for Ollama) |
| `LLM_API_BASE` | — | Self-hosted LLM endpoint |
| `DB_PATH` | `/data/incidents.db` | SQLite path (persistent volume) |
| `SIMILARITY_THRESHOLD` | `0.35` | Cosine similarity threshold |
| `EMBEDDING_MODEL` | `all-MiniLM-L6-v2` | Sentence-transformer model |

## Files

```
ai_classification/
├── main.py           → Routes: classify, reports, health
├── classifier.py     → LLM prompts, retry logic, cluster summarization
├── incident_store.py → SQLite, embeddings, incremental clustering
├── models.py         → Pydantic request/response schemas
├── schemas.py        → Taxonomy enums
└── config.py         → Env-based settings

ocr/
├── ocr_server.py     → EasyOCR microservice (English + Arabic)
└── Dockerfile

frontend/
├── index.html        → Single-page UI (Classify + Reports tabs, file upload for OCR)
├── app.js            → API calls, rendering, history
├── style.css
├── nginx.conf        → Static files + proxy to api/ocr
└── Dockerfile

tests/
├── test_classifier.py    — Unit tests for prompt building & validation (mocked LLM)
├── test_incident_store.py — Unit tests for embeddings, similarity, clustering, reports
└── e2e_check.py          — End-to-end with real Ollama
```
