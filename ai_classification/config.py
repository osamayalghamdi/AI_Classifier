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

    embedding_model_name: str = getenv("EMBEDDING_MODEL", "BAAI/bge-m3")
    similarity_threshold: float = float(getenv("SIMILARITY_THRESHOLD", "0.80"))

    # ── Ticketing system sync ────────────────────────────────────────────
    ticketing_api_url: str = getenv("TICKETING_API_URL", "http://localhost:8002")
    sync_interval_seconds: int = int(getenv("SYNC_INTERVAL", "60"))

    # ── PostgreSQL ───────────────────────────────────────────────────────
    pg_host: str = getenv("PG_HOST", "localhost")
    pg_port: int = int(getenv("PG_PORT", "5432"))
    pg_user: str = getenv("PG_USER", "aiuser")
    pg_password: str = getenv("PG_PASSWORD", "aipass")
    pg_database: str = getenv("PG_DATABASE", "ai_incidents")


settings = Settings()
