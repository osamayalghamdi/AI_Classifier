"""SMAX ticketing connector — standalone package (Phase 4).

This package is the connector between the SMAX ticketing system and the
AI classifier. It is a CLIENT of the classifier's public HTTP API
(E1-E9: POST /api/v1/incidents, GET /api/v1/incidents/{ref},
POST /api/v1/backfill) and runs as its own process — it imports NOTHING
from the classifier app and can be deployed on a machine that only has
network access to the classifier API.

Layout:
- config.py            — SMAX_* / CLASSIFIER_API_* settings + NotConfiguredError
- smax_client.py       — SMAX HTTP transport (auth, retry, timeouts)
- smax_models.py       — payload translation (from_smax / to_smax_suggestion)
- classifier_client.py — classifier public-API client (submit/result/backfill)
- poller.py            — SMAX list_changed → submit → since-stamp loop
- writeback.py         — poll results → SMAX suggestions (dry-run gated)
- main.py              — entrypoint (python -m integrations.smax.main)
"""

from .config import NotConfiguredError, Settings, settings
from .smax_models import Incident, PipelineResult

__all__ = [
    "NotConfiguredError",
    "Settings",
    "settings",
    "Incident",
    "PipelineResult",
]
