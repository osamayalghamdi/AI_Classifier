"""SMAX connector configuration — the ONLY settings this package reads.

The connector is a standalone process: it talks to SMAX (upstream) and to
the classifier's public HTTP API, and it imports NOTHING from the
classifier app. Every setting comes from the environment with its own
prefixes:

- SMAX_*            — the upstream ticketing system (this connector's source).
- CLASSIFIER_API_*  — the classifier's public integration API (E1-E9).

Fail-loud rule: a side that is enabled must be fully configured. A missing
token raises NotConfiguredError before any network I/O (mirrors the
not-configured pattern the in-process source used).
"""

from __future__ import annotations

import os
from dataclasses import dataclass


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
    smax_api_url: str = _env("SMAX_API_URL", "")
    smax_api_token: str = _env("SMAX_API_TOKEN", "")
    # Write-back gate: when true (DEFAULT), the connector only LOGS the
    # suggestion payload it would post to SMAX — nothing is written.
    smax_dry_run: bool = _truthy(_env("SMAX_DRY_RUN", "true"))
    # Poll interval for the SMAX change poller (seconds).
    smax_poll_s: float = float(_env("SMAX_POLL_S", "60"))
    # Local since-stamp file — runtime state, never committed to git.
    smax_sync_stamp_path: str = _env("SMAX_SYNC_STAMP_PATH", "./.last_sync")
    # Write-back mode: none | suggestions (default) | full.
    smax_write_back: str = _env("SMAX_WRITE_BACK", "suggestions")

    # ── Classifier public API (E1-E9 integration contract) ────────────
    classifier_api_url: str = _env("CLASSIFIER_API_URL", "http://localhost:8000")
    classifier_api_token: str = _env("CLASSIFIER_API_TOKEN", "")

    # ── Startup guards ─────────────────────────────────────────────────
    def require_smax(self) -> None:
        """Fail loudly when the SMAX side is enabled but not configured."""
        if not self.smax_api_url:
            raise NotConfiguredError(
                "SMAX side enabled but SMAX_API_URL is unset — the connector "
                "cannot poll the ticketing system."
            )
        if not self.smax_api_token:
            raise NotConfiguredError(
                "SMAX side enabled but SMAX_API_TOKEN is unset — set it to "
                "authenticate against SMAX (SMAX_API_URL is configured)."
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
        """Resolved config for --check / logs. Tokens are masked."""
        return {
            "smax_api_url": self.smax_api_url or "(unset)",
            "smax_api_token": "<set>" if self.smax_api_token else "(unset)",
            "smax_dry_run": self.smax_dry_run,
            "smax_poll_s": self.smax_poll_s,
            "smax_sync_stamp_path": self.smax_sync_stamp_path,
            "smax_write_back": self.smax_write_back,
            "classifier_api_url": self.classifier_api_url,
            "classifier_api_token": "<set>" if self.classifier_api_token else "(unset)",
        }


settings = Settings()
