# AI Incident Classifier

Structured incident classification via LLM. Feed it an incident title and description, get back clean validated categories — plus semantically similar past incidents.

Built on **Qwen2.5:7b** with **classification-augmented embeddings** for similarity search: the LLM classifies the incident, then the structured labels (system, service, type, category) are folded into the embedding. This means "Stripe errors" and "PayPal timeout" cluster together via their shared `Payment Gateway / Checkout / Outage` fingerprint, even though the raw wording differs.

## How it works

```
Incident title + description
        │
        ▼
  ┌─ Qwen2.5:7b (via Ollama, GPU) ──┐
  │  System prompt with taxonomy +   │
  │  5 few-shot examples             │
  │  ↓ Validated by Pydantic         │
  └──────────┬───────────────────────┘
             │
        ClassificationResult
     (system, service, type, severity, urgency, category)
             │
    ┌────────┴─────────┐
    ▼                  ▼
  Save to          Semantic search
  SQLite           against past incidents
  (title + desc    (embeddings include
   + embedding      classification
   + class JSON)    fingerprint)
                    │
                    ▼
              Related incidents
              sorted by similarity
```

## Stack

| Component | Tech |
|-----------|------|
| API framework | FastAPI (Python 3.12) |
| LLM | Qwen2.5:7b via Ollama |
| Embeddings | all-MiniLM-L6-v2 (sentence-transformers) |
| Similarity store | SQLite + cosine similarity |
| Validation | Pydantic v2 (strict gatekeeper — no silent coercion) |
| LLM client | litellm (provider-agnostic) |
| Container | Docker Compose (GPU via nvidia runtime) |
| Frontend | Static HTML/CSS/JS served by nginx |

## Quick Start

```bash
# Prerequisites
# 1. NVIDIA GPU with driver installed (test with: nvidia-smi)
# 2. nvidia-container-toolkit (test with: docker run --rm --gpus all nvidia/cuda:12.4-base nvidia-smi)

# Pull and run everything
docker compose up --build -d

# Check health
curl http://localhost:8000/health

# Classify an incident
curl -X POST http://localhost:8000/classify \
  -H "Content-Type: application/json" \
  -d '{
    "title": "Checkout timeouts for 20% of users",
    "description": "Users in the EU region see 504 errors when completing purchases."
  }'

# Frontend UI
open http://localhost:8082
```

## API

### `POST /classify`

```json
{
  "title": "Checkout timeouts for 20% of users",
  "description": "Users in the EU region see 504 errors during purchase."
}
```

Returns:

```json
{
  "incident_title": "Checkout timeouts for 20% of users",
  "classification": {
    "affected_system": "Payment Gateway",
    "service": "Checkout",
    "incident_type": "Degradation",
    "severity": "Major",
    "urgency": "High",
    "category": "Performance",
    "confidence": "high",
    "reasoning": "Partial degradation of checkout, not a full outage."
  },
  "incident_id": "a1b2c3d4e5f6",
  "related_incidents": [
    {
      "id": "b12f4a1e...",
      "title": "PayPal checkout errors",
      "similarity": 0.72,
      "classification": { "...same shape..." }
    }
  ]
}
```

### `GET /classify?title=...&description=...`

Same response shape, query-parameter variant.

### `GET /health`

```json
{ "status": "ok", "model": "ollama/qwen2.5:7b", "store_ready": true }
```

## Taxonomy

| Field | Allowed values |
|-------|---------------|
| affected_system | CRM, ERP, Payment Gateway, Infrastructure, Network, Security, Email, Data Pipeline, Other |
| incident_type | Spike, Degradation, Unavailability, Outage |
| severity | Critical, Major, Minor, Cosmetic |
| urgency | Immediate, High, Medium, Low |
| category | Hardware, Software, Network Issue, Security, Performance, Configuration, Human Error, External / Third Party, Other |

Each `affected_system` has its own set of valid services (e.g. `Payment Gateway` → `Checkout`, `Refunds`, `Fraud Detection`, `Billing`, `Invoice Generation`).

## Configuration

| Variable | Default | Purpose |
|----------|---------|---------|
| `LLM_MODEL` | `ollama/qwen2.5:7b` | Model name (Ollama, OpenAI, Anthropic) |
| `LLM_API_KEY` | — | API key (not needed for local Ollama) |
| `LLM_API_BASE` | — | For self-hosted LLMs (Ollama, vLLM) |
| `DB_PATH` | `/data/incidents.db` | SQLite path (persistent via Docker volume) |
| `SIMILARITY_THRESHOLD` | `0.35` | Minimum cosine similarity for related matches |
| `EMBEDDING_MODEL` | `all-MiniLM-L6-v2` | Sentence-transformer for embeddings |

## Classification-Augmented Embeddings

The similarity search doesn't just embed raw text — it adds the LLM's structured classification:

```
Embedded text:
  "Stripe payment timeouts | Payment Gateway / Checkout / Degradation / Performance"
```

This means:
- Two payment incidents with different wording (PayPal vs Stripe) still cluster via shared classification fields
- Cross-system noise is filtered out — a DB incident won't match a payment incident
- The more incidents you add, the cleaner the clusters become

Threshold tuned at **0.35** for short incident texts with the 384-dim all-MiniLM-L6-v2 model.

## Design Decisions

**Strict validation.** The LLM output goes through `json.loads` → `Pydantic.model_validate`. No regex fixing, no enum coercion, no silent fallbacks for bad JSON. If the LLM returns invalid data on both attempts, a graceful fallback `ClassificationResult` is returned with `confidence: "low"`.

**Retry with error feedback.** If the first LLM call fails validation, the retry prompt includes the exact error message. This is far more effective than a generic "fix your JSON" hint.

**Embedding model failure is non-fatal.** If `sentence-transformers` fails to load, the API continues to classify — it just won't return similar incidents.

## Files

```
├── ai_classification/
│   ├── __init__.py
│   ├── main.py          — FastAPI app, routes
│   ├── classifier.py    — LLM prompt, retry logic, few-shot examples
│   ├── incident_store.py — SQLite store, embeddings, similarity search
│   ├── models.py        — Pydantic request/response models
│   ├── schemas.py       — StrEnum taxonomies (system, type, severity…)
│   └── config.py        — Env-based settings
├── frontend/
│   ├── index.html       — Single-page UI
│   ├── nginx.conf       — Static file + API reverse proxy
│   └── Dockerfile
├── tests/
│   ├── test_classifier.py  — Unit tests (mocked LLM)
│   └── e2e_check.py        — E2E with real Ollama
├── docker-compose.yml
├── Dockerfile
└── entrypoint.sh
```
