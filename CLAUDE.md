# AI Incident Classifier

## Project
LLM-powered structured incident classification with semantic similarity clustering. Feed it a title/description, get validated categories + daily/weekly cluster reports.

## Stack
- FastAPI backend + Uvicorn
- Pydantic for validation
- LiteLLM for LLM calls (provider-agnostic — switch via config)
- Sentence-Transformers for embeddings
- Pillow for image processing
- PostgreSQL backend for storage (SQL-based clustering via cosine similarity)

## Key Architecture
- LLM used only for: classification (every incident) + cluster summarization (once per cluster)
- Everything else: embedding cosine similarity + SQL (reports return in <10ms with zero LLM)
- Incremental clustering at classification time
- Classification-augmented embeddings, summary-as-centroid
- Taxonomy enums: IncidentType, Severity, Urgency, Category, AffectedSystem

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
