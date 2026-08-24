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


def _map_write_back(value: str) -> str:
    """Normalise INTEGRATION_WRITE_BACK to the mode domain.

    Accepts the documented modes (none/suggestions/full) plus the legacy
    numeric aliases 0/1 that older compose/.env files set.
    """
    v = (value or "").strip().lower()
    if v in {"0", "none"}:
        return "none"
    if v in {"1", "suggestions"}:
        return "suggestions"
    if v == "full":
        return "full"
    # Unknown value — fall back to the SAFEST mode rather than pass an
    # invalid string downstream (the worker records mode in job results).
    return "suggestions"


@dataclass(frozen=True)
class Settings:
    # ── LLM ───────────────────────────────────────────────────────────────
    # Local:      LLM_MODEL=ollama/qwen2.5:7b  +  LLM_API_BASE=http://localhost:11434
    # API:        LLM_MODEL=openrouter/qwen/qwen3.6-35b-a3b  +  LLM_API_KEY=sk-or-v1-...
    llm_model: str = getenv("LLM_MODEL", "ollama/qwen2.5:7b")
    llm_api_key: str | None = getenv("LLM_API_KEY")
    llm_api_base: str | None = getenv("LLM_API_BASE")

    # ── LLM resilience (call_llm) ────────────────────────────────────────
    # Request timeout per LLM call, seconds (0 = provider default).
    llm_timeout_s: float = float(getenv("LLM_TIMEOUT_S", "60"))
    # Retries for TRANSIENT failures only (429 / 500 / 502 / 503 / 504 /
    # connection & read timeouts) — exponential backoff + jitter, honouring
    # Retry-After. Auth (401/403), credits (402), unknown model (404), and
    # bad request (400) fail FAST (zero retries — retrying masks a config
    # error). Total attempts = 1 + llm_max_retries.
    llm_max_retries: int = int(getenv("LLM_MAX_RETRIES", "3"))
    llm_retry_base_s: float = float(getenv("LLM_RETRY_BASE_S", "2"))

    host: str = getenv("HOST", "0.0.0.0")
    port: int = int(getenv("PORT", "8000"))

    embedding_model_name: str = getenv("EMBEDDING_MODEL", "BAAI/bge-m3")
    similarity_threshold: float = float(getenv("SIMILARITY_THRESHOLD", "0.80"))

    # ── Classifier ───────────────────────────────────────────────────────
    # CASCADE_CLASSIFICATION=true (default) → coarse-to-fine system→service→
    # offering cascade; false → legacy single-shot prompt (byte-identical to
    # the pre-cascade behavior).
    cascade_classification: bool = _is_truthy(getenv("CASCADE_CLASSIFICATION", "true"))

    # Classifier v3 self-consistency pass (DEFAULT OFF — measure before
    # enabling): when true, tickets that end confidence=low are re-run 3× at
    # temperature 0.7 and majority-voted per field; no majority → the
    # low-confidence result is kept and flagged needs_review=true.
    classify_self_consistency: bool = _is_truthy(getenv("CLASSIFY_SELF_CONSISTENCY", "false"))

    # Inter-ticket delay in classify_batch (seconds). 0 = unchanged (no
    # sleep). Lets operators pace a synchronous bulk run to stay under
    # provider rate limits without code edits. For real bulk ingest use the
    # async integration API (/api/v1/backfill, /api/v1/incidents) — see
    # docs/INTEGRATION_GUIDE.md.
    classify_batch_sleep_s: float = float(getenv("CLASSIFY_BATCH_SLEEP_S", "0"))

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
    # Runtime stamp file path — where the last-sync timestamp lives. Defaults
    # to the repo root (four parents above this file: shared/config.py →
    # ai_classification/ → repo root); overridable so deployments can keep it
    # outside the repo (BUG-3: the old hardcoded path resolved one level short).
    sync_stamp_path: str = getenv("SYNC_STAMP_PATH", "")
    # Repool worker sweep interval (seconds) — re-match-only, never re-classifies
    repool_interval_seconds: int = int(getenv("REPOOL_INTERVAL", "900"))
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

    # ── Integration API (E1-E9) ─────────────────────────────────────────
    # Bearer token required by EVERY non-health endpoint. Empty => all
    # integration requests rejected with UNAUTHORIZED (never a default).
    # INTEGRATION_API_TOKEN (ops convention) takes precedence over
    # INTEGRATION_TOKEN (older alias) — both are accepted.
    integration_token: str = getenv("INTEGRATION_API_TOKEN") or getenv("INTEGRATION_TOKEN", "")
    # Admin console: env-credential writes go to this file (default .env in
    # the CWD). Settings is frozen at import, so changes need a restart.
    admin_env_file: str = getenv("ADMIN_ENV_FILE", ".env")
    # Write-back mode for processed results:
    #   "suggestions" (default — SAFEST): results/suggestions land in the
    #     job result area, never written into ticket fields.
    #   "none": no write-back at all.  "full": write back to the ticket
    #     source (requires a configured source adapter).
    # Legacy numeric values are mapped: "0" -> "none", "1" -> "suggestions"
    # (older compose/.env files set INTEGRATION_WRITE_BACK=0).
    integration_write_back: str = _map_write_back(getenv("INTEGRATION_WRITE_BACK", "suggestions"))
    integration_max_attempts: int = int(getenv("INTEGRATION_MAX_ATTEMPTS", "5"))
    integration_retry_base_s: int = int(getenv("INTEGRATION_RETRY_BASE_S", "5"))
    integration_poll_s: float = float(getenv("INTEGRATION_POLL_S", "2.0"))
    # 0 disables the background worker (tests / manual ticking).
    integration_worker_enabled: bool = _is_truthy(getenv("INTEGRATION_WORKER_ENABLED", "1"))

    # ── Re-classification sweep (heal) ──────────────────────────────────
    # Periodically re-classify incidents whose stored classification is the
    # LLM-failure fallback (low confidence + "Classification failed after"
    # reasoning), so an LLM outage self-heals once the endpoint is reachable
    # again. Only fallback-marked rows are touched — good classifications
    # are never re-run.
    reclassify_enabled: bool = _is_truthy(getenv("RECLASSIFY_ENABLED", "1"))
    reclassify_interval_s: int = int(getenv("RECLASSIFY_INTERVAL_S", "600"))
    reclassify_max_per_tick: int = int(getenv("RECLASSIFY_MAX_PER_TICK", "10"))

    # ── Persistent clustering (v2 LLM-first) ────────────────────────────
    # Flow A runs in the classify path's BACKGROUND task; slow inference
    # must not delay the classify response. Flow C audit cadence (nightly).
    cluster_assign_on_arrival: bool = _is_truthy(getenv("CLUSTER_ASSIGN_ON_ARRIVAL", "1"))
    cluster_audit_interval_s: int = int(getenv("CLUSTER_AUDIT_INTERVAL_S", "86400"))
    # Human gate for new clusters: proposals -> review -> activate. User
    # asked to SKIP the gate (zero-friction NOC demo): default ON mints
    # sweep groups as ACTIVE clusters directly. The nightly audit is the
    # backstop (removes wrong members; verified live: 11 in one pass).
    # Set CLUSTER_AUTO_ACTIVATE=0 to restore the human approval gate.
    cluster_auto_activate: bool = _is_truthy(getenv("CLUSTER_AUTO_ACTIVATE", "1"))


settings = Settings()
