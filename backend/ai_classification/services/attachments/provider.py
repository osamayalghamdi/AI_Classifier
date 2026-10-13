"""The attachment reader — the ONLY place the external provider is called.

Everything comes from the environment (see config.py): no provider, model or
URL is hardcoded and none has a default. The transport is the standard
OpenAI-compatible chat-completions format with the image as a
`data:<mime>;base64` URL, sent through `litellm` (already a dependency) so the
retry/backoff/timeout behaviour matches the classifier's LLM path.

`ATTACHMENT_VISION_MODEL` should be in litellm's `provider/model` form (e.g.
`openai/qwen3-vl:32b`) or another alias litellm recognises; `api_base` +
`api_key` are passed straight through, which is what OpenAI-compatible
endpoints expect.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import re
from typing import Literal, Protocol

from ai_classification.shared.config import settings

from .prompts import ATTACHMENT_SYSTEM_PROMPT
from .schema import AttachmentLayers, AttObject, Signal, Where

_log = logging.getLogger("attachments")

_QUALITIES: tuple[Literal["clear", "partial", "unreadable"], ...] = (
    "clear", "partial", "unreadable",
)


class AttachmentReader(Protocol):
    def read(self, data: bytes, mime_type: str,
             *, pages: list[bytes] | None = None) -> AttachmentLayers: ...


class ReaderNotConfiguredError(RuntimeError):
    """The feature is on but the reader's URL/model/key are missing."""


def _data_url(data: bytes, mime_type: str) -> str:
    mime = mime_type or "application/octet-stream"
    return f"data:{mime};base64," + base64.b64encode(data).decode("ascii")


def _extract_json(raw: str) -> dict:
    """Tolerant: the model may return bare JSON, fenced JSON, or prose around it."""
    text = (raw or "").strip()
    if not text:
        raise ValueError("empty provider response")
    fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    if fenced:
        text = fenced.group(1)
    else:
        start, end = text.find("{"), text.rfind("}")
        if start != -1 and end > start:
            text = text[start:end + 1]
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError(f"provider did not return JSON: {exc}") from exc


class VisionLLMReader:
    """One call per attachment (all rendered PDF pages in a single call)."""

    def __init__(self, *, api_url: str, model: str, api_key: str,
                 timeout_s: float, max_pdf_pages: int) -> None:
        self.api_url = api_url
        self.model = model
        self.api_key = api_key
        self.timeout_s = timeout_s
        self.max_pdf_pages = max_pdf_pages

    def read(self, data: bytes, mime_type: str,
             *, pages: list[bytes] | None = None) -> AttachmentLayers:
        import litellm

        images = pages if pages is not None else [data]
        content: list[dict] = [{"type": "text", "text": "Analyze the attached image."}]
        for img in images:
            mime = "image/png" if pages is not None else mime_type
            content.append({"type": "image_url",
                            "image_url": {"url": _data_url(img, mime)}})

        try:
            resp = litellm.completion(
                model=self.model,
                api_base=self.api_url,
                api_key=self.api_key,
                messages=[{"role": "system", "content": ATTACHMENT_SYSTEM_PROMPT},
                          {"role": "user", "content": content}],
                timeout=self.timeout_s,
                num_retries=2,
            )
        except Exception as exc:  # noqa: BLE001 — the extractor turns this into `unreadable`
            raise RuntimeError(f"vision reader call failed: {exc}") from exc

        raw = (resp.choices[0].message.content or "") if resp.choices else ""
        payload = _extract_json(raw)
        return AttachmentLayers.model_validate(payload)


class OcrThenLLMReader:
    """Clear stub — `ocr_then_llm` is out of scope for this phase.

    Config validates that vision_llm is the only implemented mode; the stub
    exists so ATTACHMENT_READER_MODE=ocr_then_llm fails loudly and explicitly.
    """

    def read(self, data: bytes, mime_type: str,
             *, pages: list[bytes] | None = None) -> AttachmentLayers:
        raise NotImplementedError(
            "ocr_then_llm is not implemented — set ATTACHMENT_READER_MODE=vision_llm "
            "(the vision reader covers the text layer)."
        )


def build_reader() -> AttachmentReader:
    """The reader for the configured mode, or a loud NotConfigured error."""
    if settings.attachment_reader_mode == "ocr_then_llm":
        return OcrThenLLMReader()
    if not settings.attachment_reader_configured():
        raise ReaderNotConfiguredError(
            "attachments are enabled but the vision reader is missing "
            "ATTACHMENT_VISION_API_URL / ATTACHMENT_VISION_MODEL / ATTACHMENT_VISION_API_KEY"
        )
    return VisionLLMReader(
        api_url=settings.attachment_vision_api_url,
        model=settings.attachment_vision_model,
        api_key=settings.attachment_vision_api_key,
        timeout_s=settings.attachment_timeout_s,
        max_pdf_pages=settings.attachment_max_pdf_pages,
    )


_MOCK_MARKER = b"ATTACHMENT_MOCK_JSON\n"


class MockReader:
    """Deterministic offline reader for `--mock` runs of the scripts.

    If the file bytes start with ``ATTACHMENT_MOCK_JSON\\n`` the remainder is
    parsed as an ``AttachmentLayers`` JSON descriptor — so mock data is
    self-describing and a truth CSV can be written to match it exactly.
    Otherwise it returns a stable, varied record keyed by the byte hash, so a
    folder of arbitrary files still exercises the full report (quality spread,
    signal codes, object types, cache hits).
    """

    def __init__(self) -> None:
        self.calls = 0

    def read(self, data: bytes, mime_type: str,
             *, pages: list[bytes] | None = None) -> AttachmentLayers:
        self.calls += 1
        if data.startswith(_MOCK_MARKER):
            return AttachmentLayers.model_validate(json.loads(data[len(_MOCK_MARKER):]))
        digest = hashlib.sha256(data).hexdigest()
        n = int(digest[:8], 16)
        code = f"{10000000 + n % 90000000}"
        oid = f"INV-{10000 + n % 90000}"
        return AttachmentLayers(
            where=Where(page=f"Page{n % 3}"),
            objects=[AttObject(type="invoice", id=oid)],
            signals=[Signal(type="error", code=code, text="demo error")],
            text=f"Error {code} on invoice {oid}. Demo transcription.",
            summary_en="mock attachment",
            quality=_QUALITIES[n % 3],
        )
