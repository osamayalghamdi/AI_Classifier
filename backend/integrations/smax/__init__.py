"""SMAX ticketing connector — standalone process, one job: tickets → classifier.

It is a CLIENT of the classifier's public HTTP API (E1-E9:
POST /api/v1/incidents, GET /api/v1/incidents/{ref}, POST /api/v1/backfill) and
imports NOTHING from the classifier app, so it can run anywhere that can reach
that API.

Where things live / where your change goes:

    config.py             env settings (SMAX_* / CLASSIFIER_API_*) + fail-loud guards
    http_client.py        ONE transport for both sides (auth, retry, timeouts)
    smax_client.py   ←    add a new SMAX API call here (one method)
    classifier_client.py  add a new app call here (one method)
                          + incident_payload(): add a new FIELD here, once
    smax_models.py        SMAX payload ⇄ Incident mapping (from_smax / to_smax_suggestion)
    poller.py             pull loop: SMAX list_changed → submit → since-stamp
    writeback.py          job results → SMAX suggestions (dry-run gated)
    main.py               entrypoint: python -m integrations.smax.main
    tests/                fake SMAX + fake classifier servers (see conftest.py)

Run:    .venv/bin/python -m integrations.smax.main [--check | --once | --backfill FILE]
Tests:  .venv/bin/python -m pytest integrations/smax/tests -q
"""

from .classifier_client import ClassifierClient, ClassifierError, incident_payload
from .config import NotConfiguredError, Settings, settings
from .http_client import DEFAULT_TIMEOUT_S, MAX_RETRIES, JsonHttpClient
from .smax_client import SmaxClient
from .smax_models import Incident, PipelineResult, from_smax, to_smax_suggestion

__all__ = [
    # clients (add endpoints inside these, not here)
    "JsonHttpClient",
    "SmaxClient",
    "ClassifierClient",
    "DEFAULT_TIMEOUT_S",
    "MAX_RETRIES",
    # payload translation
    "Incident",
    "PipelineResult",
    "from_smax",
    "to_smax_suggestion",
    "incident_payload",
    # configuration + errors
    "Settings",
    "settings",
    "NotConfiguredError",
    "ClassifierError",
]
