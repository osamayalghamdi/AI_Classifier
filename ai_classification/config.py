"""Application configuration loaded from environment.
Pipeline position: 00_config — settings/env, read by every stage."""

from os import getenv
from dataclasses import dataclass, field
from dotenv import load_dotenv


load_dotenv()


def _split_csv(value: str) -> list[str]:
    """Split a comma-separated env value; strip whitespace; drop empties."""
    return [part.strip() for part in value.split(",") if part.strip()]


def _is_truthy(value: str) -> bool:
    """True for 1/true/yes/on (case-insensitive), else False."""
    return value.strip().lower() in {"1", "true", "yes", "on"}


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

    # ── Classifier ───────────────────────────────────────────────────────
    # CASCADE_CLASSIFICATION=true (default) → coarse-to-fine system→service→
    # offering cascade; false → legacy single-shot prompt (byte-identical to
    # the pre-cascade behavior).
    cascade_classification: bool = getenv(
        "CASCADE_CLASSIFICATION", "true"
    ).lower() in ("1", "true", "yes", "on")

    # ── Intake field mapping ─────────────────────────────────────────────
    # Payload keys tried in order when mapping a raw ticket to title /
    # description. Comma-separated env vars; whitespace stripped; empties dropped.
    ticket_title_fields: list[str] = field(
        default_factory=lambda: _split_csv(
            getenv("TICKET_TITLE_FIELDS", "DisplayLabel,display_label,title,Title")
        )
    )
    ticket_description_fields: list[str] = field(
        default_factory=lambda: _split_csv(
            getenv("TICKET_DESCRIPTION_FIELDS", "Description,description,desc")
        )
    )

    # ── Ticketing system sync ────────────────────────────────────────────
    ticketing_api_url: str = getenv("TICKETING_API_URL", "http://localhost:8002")
    sync_interval_seconds: int = int(getenv("SYNC_INTERVAL", "60"))
    # SEAMS: which ticket source the pipeline talks to — "real" (default,
    # raises not-configured until TICKETING_API_TOKEN exists) or "local"
    # (fake source backed by the incident store; tests + offline runs).
    ticketing_source: str = getenv("TICKETING_SOURCE", "real")
    ticketing_api_token: str = getenv("TICKETING_API_TOKEN", "")
    ticketing_dry_run: bool = _is_truthy(getenv("TICKETING_DRY_RUN", "false"))

    # ── PostgreSQL ───────────────────────────────────────────────────────
    pg_host: str = getenv("PG_HOST", "localhost")
    pg_port: int = int(getenv("PG_PORT", "5432"))
    pg_user: str = getenv("PG_USER", "aiuser")
    pg_password: str = getenv("PG_PASSWORD", "aipass")
    pg_database: str = getenv("PG_DATABASE", "ai_incidents")


settings = Settings()
