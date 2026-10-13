"""SMAX connector configuration — the ONLY settings this package reads.

The connector is a standalone process: it talks to SMAX (upstream) and to
the classifier's public HTTP API, and it imports NOTHING from the
classifier app. Every setting comes from the environment with its own
prefixes:

- SMAX_*            — the upstream ticketing system (this connector's source).
- CLASSIFIER_API_*  — the classifier's public integration API (E1-E9).

All of it belongs to this folder: a `.env` beside this file is loaded when
present (see below), and nothing SMAX-specific needs to live in the app's
environment. The one shared value is the classifier token, which must equal
the server's INTEGRATION_API_TOKEN.

Fail-loud rule: a side that is enabled must be fully configured. A missing
token raises NotConfiguredError before any network I/O (mirrors the
not-configured pattern the in-process source used).
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

# Config lives WITH the connector. Loading the sibling .env keeps the connector
# self-contained: run it straight from this folder without exporting anything.
# Real environment variables win (dotenv does not override), so
# `SMAX_API_TOKEN=… python -m integrations.smax.main` still beats the file.
# python-dotenv is already a repo dependency; if it is missing the connector
# still runs from the environment alone.
try:
    from dotenv import load_dotenv
except ModuleNotFoundError:  # pragma: no cover - connector stays usable stdlib-only
    load_dotenv = None

if load_dotenv is not None:
    load_dotenv(Path(__file__).with_name(".env"))


class NotConfiguredError(RuntimeError):
    """Raised when an enabled side is missing its required credentials."""


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def _truthy(value: str) -> bool:
    """True for 1/true/yes/on (case-insensitive), else False."""
    return value.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Settings:
    # ── SMAX (upstream ticketing system) ───────────────────────────────
    # Auth is a login exchange, not a static token: the connector POSTs
    # user/password to /auth/authentication-endpoint/authenticate/token and
    # carries the returned JWT in a Cookie (see smax_client.py). SMAX_API_URL
    # is accepted as a legacy alias for SMAX_BASE.
    smax_base: str = _env("SMAX_BASE") or _env("SMAX_API_URL", "")
    smax_tenant: str = _env("SMAX_TENANT", "")
    smax_user: str = _env("SMAX_USER", "")
    smax_pass: str = _env("SMAX_PASS", "")
    # Client-side token refresh interval. The handover's example uses 25 min;
    # it is a refresh hint, not a promise about the server's token lifetime —
    # a 401/403 always forces a refresh.
    smax_token_ttl_s: float = float(_env("SMAX_TOKEN_TTL_S", "1500"))
    # Search: page size (the API caps at 250), the custom entity field, and the
    # fixed UTC offset used to convert date boundaries (3 = Riyadh).
    smax_page_size: int = int(_env("SMAX_PAGE_SIZE", "250"))
    smax_entity_field: str = _env("SMAX_ENTITY_FIELD", "Entity_c")
    smax_utc_offset_hours: int = int(_env("SMAX_UTC_OFFSET_HOURS", "3"))
    # Attachments: only the bytes we are willing to carry through the ingest
    # API (base64) are downloaded; larger ones are recorded by name.
    smax_max_attachment_mb: float = float(_env("SMAX_MAX_ATTACHMENT_MB", "15"))
    # Read timeout for one SMAX request (seconds). A big search page can be slow;
    # the handover's example allows 120s.
    smax_timeout_s: float = float(_env("SMAX_TIMEOUT_S", "60"))
    # Safety cap on submissions per poll tick. 0 = no cap (default). Set it for a
    # first live run: the connector then advances its stamp only to the last
    # ticket it actually sent, so the remainder is picked up next tick — nothing
    # is dropped, and a backlog cannot hit the classifier all at once. It bounds
    # the READ as well: a capped tick lists one ticket past the cap rather than
    # pulling the whole change list off SMAX to hold it back.
    smax_max_incidents_per_tick: int = int(_env("SMAX_MAX_INCIDENTS_PER_TICK", "0"))
    # Write-back gate: when true (DEFAULT), the connector only LOGS the
    # suggestion payload it would post to SMAX — nothing is written.
    smax_dry_run: bool = _truthy(_env("SMAX_DRY_RUN", "true"))
    # Poll interval for the SMAX change poller (seconds).
    smax_poll_s: float = float(_env("SMAX_POLL_S", "60"))
    # Local since-stamp file — runtime state, never committed to git.
    smax_sync_stamp_path: str = _env("SMAX_SYNC_STAMP_PATH", "./.last_sync")
    # Write-back mode: none | suggestions (default) | full. NOTE: the SMAX
    # handover documents no write endpoint, so write-back stays dry-run only.
    smax_write_back: str = _env("SMAX_WRITE_BACK", "suggestions")

    # ── Classifier public API (E1-E9 integration contract) ────────────
    classifier_api_url: str = _env("CLASSIFIER_API_URL", "http://localhost:8000")
    classifier_api_token: str = _env("CLASSIFIER_API_TOKEN", "")

    # ── Startup guards ─────────────────────────────────────────────────
    def require_smax(self) -> None:
        """Fail loudly when the SMAX side is enabled but not configured."""
        missing = [
            name for name, value in (
                ("SMAX_BASE", self.smax_base),
                ("SMAX_TENANT", self.smax_tenant),
                ("SMAX_USER", self.smax_user),
                ("SMAX_PASS", self.smax_pass),
            ) if not value
        ]
        if missing:
            raise NotConfiguredError(
                "SMAX side enabled but not configured — missing "
                + ", ".join(missing)
                + ". The connector authenticates with the integration account "
                "(POST /auth/authentication-endpoint/authenticate/token)."
            )

    def require_classifier(self) -> None:
        """Fail loudly when the classifier-API token is missing.

        Every non-health endpoint of the E1-E9 API requires
        Authorization: Bearer <token>; an empty token means every request
        would be rejected with 401, so we refuse to start.
        """
        if not self.classifier_api_token:
            raise NotConfiguredError(
                "CLASSIFIER_API_TOKEN is unset — the classifier API requires "
                "a Bearer token on every endpoint (POST /api/v1/incidents, "
                "GET /api/v1/incidents/{ref}, POST /api/v1/backfill)."
            )

    def summary(self) -> dict:
        """Resolved config for --check / logs. Secrets are masked."""
        return {
            "smax_base": self.smax_base or "(unset)",
            "smax_tenant": self.smax_tenant or "(unset)",
            "smax_user": self.smax_user or "(unset)",
            "smax_pass": "<set>" if self.smax_pass else "(unset)",
            "smax_token_ttl_s": self.smax_token_ttl_s,
            "smax_page_size": self.smax_page_size,
            "smax_entity_field": self.smax_entity_field,
            "smax_utc_offset_hours": self.smax_utc_offset_hours,
            "smax_max_attachment_mb": self.smax_max_attachment_mb,
            "smax_timeout_s": self.smax_timeout_s,
            "smax_max_incidents_per_tick": self.smax_max_incidents_per_tick,
            "smax_dry_run": self.smax_dry_run,
            "smax_poll_s": self.smax_poll_s,
            "smax_sync_stamp_path": self.smax_sync_stamp_path,
            "smax_write_back": self.smax_write_back,
            "classifier_api_url": self.classifier_api_url,
            "classifier_api_token": "<set>" if self.classifier_api_token else "(unset)",
        }


settings = Settings()
