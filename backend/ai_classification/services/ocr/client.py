"""OCR client — transcribe an incident's attachments through the OCR service.

The OCR service is EXTERNAL to this app: its own endpoint, its own
deployment, its own model (see `ocr/`, and the chart's `ocr.*` values).
Nothing about it is hard-coded here — the endpoint is `OCR_SERVICE_URL`
(compose service, in-cluster Service, or a company-hosted URL).

Contract (the service's `POST /ocr`):
    multipart/form-data, field `file`  →  200 {
        "engine": "elm", "model": "openai/qwen3-vl:32b",
        "text": "<page 1>\\n…", "low_confidence_words": [...] }

Attachment dicts come from the integration contract and are free-form; we
accept the usual variants:
    name|filename|file_name, content_type|mime_type|type,
    content_base64|content|data|base64  (inline, optional data: prefix),
    url|download_url|link|href          (fetched with a size cap)

Enrichment is BEST-EFFORT by design: a missing/unreadable/unreachable
attachment is recorded on the outcome and never fails the incident — the
ticket still has to land in the record.
"""

from __future__ import annotations

import base64
import io
import json
import logging
import os
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass, field
from typing import Any

from ai_classification.shared.config import settings

_log = logging.getLogger(__name__)

_NAME_KEYS = ("name", "filename", "file_name", "title")
_MIME_KEYS = ("content_type", "mime_type", "mimetype", "type")
_URL_KEYS = ("url", "download_url", "link", "href")
_INLINE_KEYS = ("content_base64", "content", "data", "base64", "file_content")

_MIME_BY_SUFFIX = {
    ".pdf": "application/pdf",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
    ".tif": "image/tiff",
    ".tiff": "image/tiff",
    ".bmp": "image/bmp",
}


@dataclass
class OcrOutcome:
    """Result for ONE attachment — text when it worked, error when it did not."""

    name: str
    text: str = ""
    model: str = ""
    engine: str = ""
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.error is None


@dataclass
class OcrReport:
    """All attachment outcomes for one incident (order preserved)."""

    outcomes: list[OcrOutcome] = field(default_factory=list)

    @property
    def succeeded(self) -> list[OcrOutcome]:
        return [o for o in self.outcomes if o.ok and o.text.strip()]

    @property
    def failed(self) -> list[OcrOutcome]:
        return [o for o in self.outcomes if o.error]

    @property
    def documents(self) -> list[str]:
        """Attachment file names — the incident record's `documents` value."""
        seen: list[str] = []
        for o in self.outcomes:
            if o.name and o.name not in seen:
                seen.append(o.name)
        return seen

    def text(self, *, limit: int | None = None) -> str:
        """Combined transcription, each file marked with its name.

        Pages inside one file are already marked `<page N>` by the service.
        """
        parts = []
        for o in self.succeeded:
            parts.append(f"[{o.name}]\n{o.text.strip()}")
        joined = "\n\n".join(parts).strip()
        cap = limit if limit is not None else settings.ocr_max_text_chars
        if cap and len(joined) > cap:
            joined = joined[:cap]
        return joined


# ── Attachment field extraction ───────────────────────────────────────

def attachment_name(att: dict) -> str:
    for key in _NAME_KEYS:
        value = (att or {}).get(key)
        if value:
            return os.path.basename(str(value).strip().strip('"'))
    return "attachment"


def attachment_mime(att: dict, name: str) -> str:
    for key in _MIME_KEYS:
        value = (att or {}).get(key)
        if value and "/" in str(value):
            return str(value)
    return _MIME_BY_SUFFIX.get(os.path.splitext(name)[1].lower(), "application/octet-stream")


def _read_url(url: str, max_bytes: int) -> bytes:
    with urllib.request.urlopen(url, timeout=settings.ocr_timeout_s) as resp:  # noqa: S310 — operator-provided URL
        blob = resp.read(max_bytes + 1)
    if len(blob) > max_bytes:
        raise ValueError(f"attachment exceeds the {settings.ocr_max_attachment_mb:g} MB cap")
    return blob


def attachment_bytes(att: Any) -> tuple[bytes, str, str]:
    """(content, mime, name) for an attachment dict. Raises ValueError when the
    attachment carries nothing readable (name-only entries, e.g. from the
    local seam's `documents` list, land here)."""
    if isinstance(att, dict):
        name = attachment_name(att)
        mime = attachment_mime(att, name)
        raw = next(((att.get(k) for k in _INLINE_KEYS if att.get(k))), None)
        url = next((str(att.get(k)) for k in _URL_KEYS if att.get(k)), "")
    else:
        name = os.path.basename(str(att)) or "attachment"
        mime = _MIME_BY_SUFFIX.get(os.path.splitext(name)[1].lower(), "application/octet-stream")
        raw, url = None, ""

    max_bytes = int(settings.ocr_max_attachment_mb * 1024 * 1024)
    if isinstance(raw, (bytes, bytearray)):
        blob = bytes(raw)
    elif isinstance(raw, str) and raw.strip():
        payload = raw.split(",", 1)[1] if raw.lstrip().startswith("data:") else raw
        try:
            blob = base64.b64decode(payload, validate=False)
        except Exception as exc:  # noqa: BLE001 — malformed inline content
            raise ValueError(f"inline content is not valid base64 ({exc})") from exc
    elif url:
        blob = _read_url(url, max_bytes)
    else:
        raise ValueError("no content_base64/content/url on the attachment")

    if not blob:
        raise ValueError("attachment content is empty")
    if len(blob) > max_bytes:
        raise ValueError(f"attachment exceeds the {settings.ocr_max_attachment_mb:g} MB cap")
    return blob, mime, name


# ── HTTP ──────────────────────────────────────────────────────────────

def _multipart(field_name: str, filename: str, content: bytes, mime: str) -> tuple[bytes, str]:
    boundary = f"----incidentocr{uuid.uuid4().hex}"
    safe = filename.replace('"', "").replace("\r", "").replace("\n", "")
    out = io.BytesIO()
    out.write(f"--{boundary}\r\n".encode())
    out.write(
        f'Content-Disposition: form-data; name="{field_name}"; filename="{safe}"\r\n'.encode()
    )
    out.write(f"Content-Type: {mime}\r\n\r\n".encode())
    out.write(content)
    out.write(f"\r\n--{boundary}--\r\n".encode())
    return out.getvalue(), f"multipart/form-data; boundary={boundary}"


def ocr_endpoint() -> str:
    """The configured OCR endpoint, normalized to `…/ocr`."""
    base = (settings.ocr_service_url or "").strip().rstrip("/")
    if not base:
        return ""
    return base if base.endswith("/ocr") else f"{base}/ocr"


def transcribe(att: Any, *, endpoint: str | None = None,
               timeout: float | None = None) -> OcrOutcome:
    """POST one attachment to the OCR service and return its transcription.

    Never raises: failures come back as `OcrOutcome.error` (redacted).
    """
    from ai_classification.services.classify.llm import redact_secrets

    try:
        name = attachment_name(att) if isinstance(att, dict) else attachment_name({"name": att})
    except Exception:  # noqa: BLE001 — never let naming break the pipeline
        name = "attachment"
    try:
        url = endpoint or ocr_endpoint()
        if not url:
            raise ValueError("OCR_SERVICE_URL is not configured")
        content, mime, filename = attachment_bytes(att)
        body, content_type = _multipart("file", filename, content, mime)
        req = urllib.request.Request(  # noqa: S310 — operator-provided base URL
            url,
            data=body,
            headers={"Content-Type": content_type, "Accept": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=timeout or settings.ocr_timeout_s) as resp:
            payload = json.loads(resp.read().decode("utf-8", "replace") or "{}")
        text = payload.get("text") or ""
        outcome = OcrOutcome(
            name=payload.get("filename") or filename,
            text=text,
            model=str(payload.get("model") or ""),
            engine=str(payload.get("engine") or ""),
        )
        _log.info("OCR ok — file=%s engine=%s chars=%d", outcome.name, outcome.engine, len(text))
        return outcome
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            detail = exc.read().decode("utf-8", "replace")[:200]
        except Exception:  # noqa: BLE001
            pass
        return OcrOutcome(name=name, error=redact_secrets(f"HTTP {exc.code} {detail}".strip()))
    except Exception as exc:  # noqa: BLE001 — best-effort enrichment
        return OcrOutcome(name=name, error=redact_secrets(str(exc)))


def transcribe_attachments(attachments: list[Any]) -> OcrReport:
    """Transcribe every attachment. Returns an empty report when OCR is off or
    there is nothing to read — callers always get a report object."""
    report = OcrReport()
    if not attachments or not settings.ocr_enabled:
        return report
    endpoint = ocr_endpoint()
    if not endpoint:
        _log.warning("OCR enabled but OCR_SERVICE_URL is empty — skipping %d attachment(s)",
                     len(attachments))
        return report
    for att in attachments:
        outcome = transcribe(att, endpoint=endpoint)
        report.outcomes.append(outcome)
        if outcome.error:
            _log.warning("OCR failed — file=%s error=%s", outcome.name, outcome.error[:200])
    return report
