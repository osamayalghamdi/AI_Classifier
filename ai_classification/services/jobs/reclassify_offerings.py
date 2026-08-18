"""Re-classify incidents whose stored offering is invalid or a known semantic mispick.

Targets:
  1. Stored service values that are NOT valid taxonomy values (invented offerings,
     echoed service names) — the hard violations.
  2. Stored incident_type == "Spike" with no error/spike in the ticket text.

Re-classifies via the LIVE cascade (with the hardened v2 prompt + validator),
then updates classification_json in place (store.update_classification — identity,
status, occurrence bookkeeping untouched). Never mints, never touches pools.

Usage (inside the API container, which has LLM keys + DB access):
    docker exec ai_classifier-api-1 python -m scripts.reclassify_offerings [--dry-run]
"""
from __future__ import annotations

import json
import logging
import sys
from datetime import datetime, timezone

_log = logging.getLogger(__name__)

# Hard violations: stored services that are not valid taxonomy values.
# (audit_offerings.py computes this generically; here we keep the explicit
# predicate so the script stays self-documenting.)
def _is_valid_service(service: str) -> bool:
    from ai_classification.domain.taxonomy import SERVICES_BY_SYSTEM

    if not service:
        return False
    if "." not in service:
        return service in {
            s for svcs in SERVICES_BY_SYSTEM.values() for s in svcs
        }
    for svcs in SERVICES_BY_SYSTEM.values():
        for key, offerings in svcs.items():
            if service == key:
                return True
            if service.startswith(key + "."):
                offering = service[len(key) + 1:]
                return offering in offerings
    return False


def _targets() -> list[dict]:
    from ai_classification.core.store import store

    out = []
    for inc in store.list_incidents():
        cj = inc.get("classification_dict") or {}
        service = cj.get("service") or ""
        itype = cj.get("incident_type") or ""
        if not _is_valid_service(service):
            out.append((inc, "invalid-offering"))
        elif itype == "Spike":
            out.append((inc, "spike-without-error"))
        elif service.endswith(".Error Spikes"):
            # Semantic mispick (user-reported): "Error Spikes" attached to
            # tickets that mention NO spike of errors (e.g. missing UI
            # elements). Valid taxonomy value, wrong meaning — reclassify so
            # the hardened v2 prompt picks a truthful offering.
            out.append((inc, "error-spikes-semantic"))
    return out


def run_reclassify(*, dry_run: bool = False) -> dict:
    from ai_classification.core.classifier import PROMPT_VERSION, classify
    from ai_classification.core.store import store
    from ai_classification.config import settings

    targets = _targets()
    stats = {"candidates": len(targets), "reclassified": 0, "failed": 0,
             "dry_run": dry_run}
    for inc, reason in targets:
        title = inc.get("title", "") or ""
        description = inc.get("description", "") or ""
        before = (inc.get("classification_dict") or {}).get("service", "?")
        if dry_run:
            _log.info("[dry-run] would reclassify %s (%s) service=%s",
                      inc["id"], reason, before)
            continue
        try:
            cls = classify(title, description)
            cls.model_version = settings.llm_model
            cls.prompt_version = PROMPT_VERSION
        except Exception as exc:  # noqa: BLE001
            _log.warning("reclassify failed for %s: %s", inc["id"], exc)
            stats["failed"] += 1
            continue
        store.update_classification(inc["id"], cls.model_dump_json())
        stats["reclassified"] += 1
        _log.info("reclassified %s (%s) %s -> %s", inc["id"], reason,
                  before, cls.service)
    return stats


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    from ai_classification.core.store import store

    store.setup()
    stats = run_reclassify(dry_run="--dry-run" in sys.argv)
    print(f"reclassify: {stats}")
    print(f"finished at {datetime.now(timezone.utc).isoformat()}")
