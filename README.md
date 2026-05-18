# AI Classifier

Structured incident classification via LLM. Feed it an incident title and description, get back clean, validated categories — no guesswork.

**New in v0.2**: semantic similarity memory. Every incident is stored and compared to past incidents by meaning — if a new checkout slowdown looks 93% like last week's, the API tells you.

## Quick Start

```bash
# Install
uv sync

# Run (set your LLM_API_KEY first)
cp .env.example .env
# edit .env with your model & key
uv run uvicorn ai_classification.main:app

# Test
uv run pytest
```

## API

### `POST /classify`

```json
{
  "title": "Checkout timeouts for 20% of users",
  "description": "Users in EU region see 504 errors during purchase."
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
    "reasoning": "Partial degradation of checkout."
  },
  "incident_id": "a1b2c3d4e5f6",
  "related_incidents": [
    {
      "id": "b12f4a1e...",
      "title": "Checkout slow for EU users",
      "similarity": 0.93,
      "classification": { "... same shape ..." }
    }
  ]
}
```

A `GET /classify?title=...&description=...` variant is also available.

### `GET /health`

Returns `{"status": "ok", "model": "...", "store_ready": true}`.

## Configuration

All via environment variables (see `.env.example`):

| Variable | Default | Purpose |
|----------|---------|---------|
| `LLM_MODEL` | `gpt-4o-mini` | Model name (OpenAI, Anthropic, or `ollama/...`) |
| `LLM_API_KEY` | — | API key for the provider |
| `LLM_API_BASE` | — | For self-hosted models (Ollama, etc.) |
| `LLM_PROVIDER` | — | Provider name (auto-detected from model) |
| `DB_PATH` | `incidents.db` | SQLite database path |
| `EMBEDDING_MODEL` | `all-MiniLM-L6-v2` | Sentence-transformer model for embeddings |
| `SIMILARITY_THRESHOLD` | `0.80` | Minimum cosine similarity (0‑1) for a match |
| `HOST` | `0.0.0.0` | Server bind address |
| `PORT` | `8000` | Server port |

## Docker (with local Ollama)

```bash
docker compose up
```

Pulls a local model (qwen2.5:1.5b) via Ollama and runs the API. The embedding model is pre-downloaded in the build stage — no network on first request.

## Design

`classifier.py` sends the incident to an LLM with a structured prompt. The response goes through:

1. `_extract_json_str` — strips markdown code fences only
2. `json.loads` — parses JSON
3. `ClassificationResult.model_validate` — Pydantic enforces every field

No silent fallbacks, no regex fixing, no enum coercion. If the LLM returns bad data, you get a `RuntimeError`.

### Semantic similarity

`IncidentStore` (`incident_store.py`) wraps SQLite + `all-MiniLM-L6-v2` (22MB, runs locally, no API key). On each classification:

1. Embed title + description into a 384‑dimension vector
2. Compare against all past incidents using cosine similarity
3. Return matches above the threshold (default 80%)
4. Save the new incident + its embedding to SQLite

The embedding model failing to load is non-fatal — the API continues to classify incidents, just without similarity search.
