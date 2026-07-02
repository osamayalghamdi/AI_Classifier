# AI Incident Classifier

## Project
LLM-powered structured incident classification with live duplicate detection. Feed it
a title/description, get validated categories + a list of similar *open* incidents so
a call center doesn't escalate a duplicate.

**Phase 1 scope only.** Clustering, reports, and LLM re-ranking were paused by
management and live on the `phases-2-3` branch, untouched. Don't reintroduce them on
`main` without an explicit ask — see [ROADMAP.md](ROADMAP.md) for status.

## Stack
- FastAPI backend + Uvicorn
- Pydantic for validation
- LiteLLM for LLM calls (provider-agnostic — switch via config)
- Sentence-Transformers for embeddings
- Pillow for image processing
- SQLite backend for storage (cosine similarity over active incidents only)

## Key Architecture
- LLM used only for: classification (every incident)
- Everything else: embedding cosine similarity against `status = active` incidents
- No clustering, no reports, no re-ranking (Phase 2/3, paused)
- Classification-augmented embeddings (title + description + OCR text + LLM labels)
- Taxonomy enums: IncidentType, Severity, Urgency, Category, AffectedSystem
- Incidents carry a `status` (`active` / `resolved`); resolving one removes it from
  future duplicate checks

## Running Locally
- `uvicorn ai_classification.main:app --reload` — dev server
- `pytest tests/` — unit tests (mocked LLM)
- `python tests/e2e_check.py` — end-to-end with real Ollama

## Dependencies
- See pyproject.toml (no requirements.txt — pip install -e .)
- Ollama for local LLM (Qwen2.5:7b on RTX 2060 SUPER)
- Mixed Arabic/English OCR via easyocr/surya-ocr

## Conventions
- Clean, simple code preferred
- Type hints on all public functions
- No committing/pushing without explicit permission
