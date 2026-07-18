# AI Incident Classifier

<img width="2600" height="1235" alt="architecture-flow" src="https://github.com/user-attachments/assets/7a7826db-2b64-4f22-8de9-6c2108c67a12" />

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

## Project Structure

```
AI_Classifier/
│
├── ai_classification/              # Backend (FastAPI + LLM)
│   ├── main.py                     # FastAPI entry point
│   ├── config.py                   # Env-based settings
│   ├── sync.py                     # Background ticketing sync
│   ├── api/
│   │   ├── routes.py               # HTTP endpoints: classify, incidents, reports
│   │   └── schemas.py              # Request/response schemas
│   ├── core/
│   │   ├── classifier.py           # LLM classification + retry
│   │   ├── store.py                # SQLite + cosine similarity
│   │   └── grouping.py             # Graph clustering + LLM validator
│   └── domain/
│       ├── models.py               # Pydantic models
│       └── taxonomy.py             # Enums: system, type, severity, services
│
├── frontend/                       # Web UIs
│   ├── index.html                  # Old classify form (port 8082)
│   └── dashboard/                  # ⬅ New three-lens incident dashboard
│       ├── index.html              #   UI shell (dark theme, bilingual, RTL-ready)
│       ├── app.js                  #   All logic: role switching, filtering, clustering
│       └── data.js                 #   Mock data: 150+ tickets, 16 clusters, 4 teams
│
├── ocr/
│   └── ocr_server.py              # EasyOCR microservice (AR/EN, port 8003)
│
├── ticketing_simulator/           # Mock ticketing system
│   ├── main.py                    # FastAPI (port 8002), 276 bilingual incidents
│   ├── generated_nusuk_data.json  # Synthetic seed data
│   └── pyproject.toml
│
├── tests/                         # 60 unit + e2e tests
│   ├── test_classifier.py         # 21 tests — LLM parsing, retry
│   ├── test_incident_store.py     # 33 tests — embeddings, similarity, resolve
│   ├── test_service.py            # 6 tests — orchestration
│   ├── e2e_check.py               # End-to-end with real Ollama
│   └── conftest.py                # Test fixtures
│
├── docs/
│   ├── NOTES.md                   # Design review + build plan (authoritative)
│   ├── ROADMAP.md                 # Enterprise plan
│   ├── TODO.md                    # Phase tracking
│   └── CLAUDE.md                  # Agent instructions
│
├── scripts/
│   ├── generate_nusuk_data.py     # Synthetic bilingual ticket generator
│   ├── find_threshold.py          # Similarity threshold finder
│   └── reset_db.py                # Reset incidents database
│
├── docker-compose.yml             # Full stack (backend + frontend + ocr + simulator)
├── pyproject.toml                 # Python deps (pip install -e .)
└── README.md                      # This file

