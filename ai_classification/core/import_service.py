"""Bulk import service — file validation, JSON parsing, field remapping."""
import json
import logging
from pathlib import Path

from fastapi import HTTPException
from ..api.schemas import ClassifyBatchResponse
from .classifier import classify_batch

_log = logging.getLogger(__name__)


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
        title = (
            inc.get("title", "") or inc.get("Title", "") or
            inc.get("DisplayLabel", "") or inc.get("display_label", "")
        )
        if isinstance(title, str):
            title = title.strip()
        if not title:
            continue
        desc = (
            inc.get("description", "") or inc.get("Description", "") or
            inc.get("desc", "") or ""
        )
        if isinstance(desc, str):
            desc = desc.strip()
        incidents.append({"title": title, "description": desc})

    if not incidents:
        raise HTTPException(status_code=400, detail="No incidents with a non-empty title found")

    result = classify_batch(incidents)
    _log.info("Imported %d incidents from %s (%d/%d classified)",
              len(incidents), filename, result.total - result.failed, result.total)
    return result
