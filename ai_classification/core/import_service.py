"""Bulk import service — file validation, JSON parsing, field remapping."""
import json
import logging
from pathlib import Path

from fastapi import HTTPException
from ..api.schemas import ClassifyBatchResponse
from ..config import settings
from .classifier import classify_batch

_log = logging.getLogger(__name__)


def _first_non_empty(inc: dict, fields: list[str]) -> str:
    """Return the first field whose value is a non-empty string after .strip();

    Empty string when none of the configured keys yields one.
    """
    for key in fields:
        value = inc.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def import_incidents_from_body(incidents: list[dict]) -> ClassifyBatchResponse:
    """Classify incidents from a body payload with DisplayLabel/Description fields."""
    mapped = []
    for inc in incidents:
        title = _first_non_empty(inc, settings.ticket_title_fields)
        desc = _first_non_empty(inc, settings.ticket_description_fields)
        if not title:
            continue
        mapped.append({"title": title, "description": desc})

    if not mapped:
        raise HTTPException(status_code=400, detail="No incidents with a non-empty DisplayLabel found")

    result = classify_batch(mapped)
    _log.info("Imported %d incidents from request body (%d/%d classified)",
              len(mapped), result.total - result.failed, result.total)
    return result


def import_incidents_from_file(filename: str) -> ClassifyBatchResponse:
    """Read a JSON file of incidents and classify them in batch."""
    if not filename.endswith(".json"):
        raise HTTPException(status_code=400, detail="Only .json files allowed")

    filepath = Path(__file__).parent.parent.parent / filename
    if not filepath.exists():
        raise HTTPException(status_code=404, detail=f"File {filename} not found at {filepath}")

    try:
        with open(filepath) as f:
            data = json.load(f)
    except json.JSONDecodeError as e:
        raise HTTPException(status_code=400, detail=f"Invalid JSON: {e}")

    if not isinstance(data, list):
        raise HTTPException(status_code=400, detail="JSON must be an array of incident objects")

    incidents = []
    for inc in data:
        title = _first_non_empty(inc, settings.ticket_title_fields)
        if not title:
            continue
        desc = _first_non_empty(inc, settings.ticket_description_fields)
        incidents.append({"title": title, "description": desc})

    if not incidents:
        raise HTTPException(status_code=400, detail="No incidents with a non-empty title found")

    result = classify_batch(incidents)
    _log.info("Imported %d incidents from %s (%d/%d classified)",
              len(incidents), filename, result.total - result.failed, result.total)
    return result
