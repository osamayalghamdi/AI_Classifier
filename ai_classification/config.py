"""Application configuration loaded from environment."""

from os import getenv
from dataclasses import dataclass
from dotenv import load_dotenv


load_dotenv()


@dataclass(frozen=True)
class Settings:
    llm_model: str = getenv("LLM_MODEL", "gpt-4o-mini")
    llm_provider: str | None = getenv("LLM_PROVIDER")       # e.g. "openai", "anthropic"
    llm_api_key: str | None = getenv("LLM_API_KEY")
    llm_api_base: str | None = getenv("LLM_API_BASE")       # for self-hosted / Ollama
    host: str = getenv("HOST", "0.0.0.0")
    port: int = int(getenv("PORT", "8000"))

    # ── Incident store (semantic similarity) ───────────────────────────
    db_path: str = getenv("DB_PATH", "incidents.db")
    embedding_model_name: str = getenv(
        "EMBEDDING_MODEL", "all-MiniLM-L6-v2"
    )
    similarity_threshold: float = float(
        getenv("SIMILARITY_THRESHOLD", "0.80")
    )


settings = Settings()
