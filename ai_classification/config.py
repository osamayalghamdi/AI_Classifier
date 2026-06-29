"""Application configuration loaded from environment."""

from os import getenv
from dataclasses import dataclass
from dotenv import load_dotenv


load_dotenv()


@dataclass(frozen=True)
class Settings:
    # ── LLM ───────────────────────────────────────────────────────────────
    # Local:      LLM_MODEL=ollama/qwen2.5:7b  +  LLM_API_BASE=http://localhost:11434
    # API:        LLM_MODEL=openrouter/qwen/qwen3.6-35b-a3b  +  LLM_API_KEY=sk-or-v1-...
    llm_model: str = getenv("LLM_MODEL", "ollama/qwen2.5:7b")
    llm_api_key: str | None = getenv("LLM_API_KEY")
    llm_api_base: str | None = getenv("LLM_API_BASE")

    host: str = getenv("HOST", "0.0.0.0")
    port: int = int(getenv("PORT", "8000"))

    db_path: str = getenv("DB_PATH", "incidents.db")
    embedding_model_name: str = getenv("EMBEDDING_MODEL", "all-MiniLM-L6-v2")
    similarity_threshold: float = float(getenv("SIMILARITY_THRESHOLD", "0.80"))


settings = Settings()
