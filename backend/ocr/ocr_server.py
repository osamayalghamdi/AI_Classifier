"""OCR server — extracts text from images and PDFs.

Deployment choice: the **company vision model** (ELM provider) over its
OpenAI-compatible chat-completions endpoint. `OCR_USE_ELM=1` (the default)
plus `ELM_API_URL` / `ELM_MODEL` / `ELM_API_KEY` — nothing is transcribed
locally and this image ships **no local OCR model**.

A legacy local EasyOCR path is kept in this file for environments that
install `easyocr` themselves (`OCR_USE_ELM=0`); it is not part of the image,
and selecting it without the package fails with a clear message.
"""

import asyncio
import base64
import io
import json
import os
import logging
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager

# Silence EasyOCR progress bars (they write to stderr via tqdm)
os.environ["EASYOCR_VERBOSE"] = "False"
logging.getLogger("easyocr").setLevel(logging.WARNING)

# Optional: the local engine is NOT in the image (external vision model is
# the supported path). Import guarded so the service runs without it.
try:
    import easyocr
except ModuleNotFoundError:  # pragma: no cover — expected in the shipped image
    easyocr = None

from fastapi import FastAPI, File, Query, UploadFile
from pdf2image import convert_from_bytes
from PIL import Image

log = logging.getLogger("ocr")

_executor = ThreadPoolExecutor(max_workers=2)

# ── ELM vision model config ───────────────────────────────────────────
# The company provider is the DEFAULT: OCR_USE_ELM=1 unless explicitly
# turned off. This container has no local OCR model installed.
OCR_USE_ELM = os.getenv("OCR_USE_ELM", "1").strip().lower() not in ("0", "false", "no", "off")
ELM_API_URL = os.getenv("ELM_API_URL", "https://llms.elm.sa/v1/chat/completions")
ELM_MODEL = os.getenv("ELM_MODEL", "openai/qwen3-vl:32b")
ELM_API_KEY = os.getenv("ELM_API_KEY", "").strip()
OCR_ELM_TIMEOUT_S = float(os.getenv("OCR_ELM_TIMEOUT_S", "60"))
# PDFs are rendered at 300 dpi (~26 MB RAM per page) — cap the page count so
# one long scan cannot OOM the container.
OCR_MAX_PDF_PAGES = int(os.getenv("OCR_MAX_PDF_PAGES", "10"))

_ELM_SYSTEM_PROMPT = (
    "You are a document OCR engine. Transcribe ALL visible text from the "
    "image exactly as written, preserving layout and line breaks. Return only "
    "the transcription — no commentary, no markdown."
)


def _ocr_via_elm(pil_img: Image.Image) -> str:
    """Send one image to the ELM vision endpoint and return its text."""
    if not ELM_API_KEY:
        raise RuntimeError("OCR_USE_ELM=1 but ELM_API_KEY is not set")
    buf = io.BytesIO()
    pil_img.save(buf, format="PNG")
    b64 = base64.b64encode(buf.getvalue()).decode("ascii")
    payload = {
        "model": ELM_MODEL,
        "messages": [
            {"role": "system", "content": _ELM_SYSTEM_PROMPT},
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "Transcribe this image:"},
                    {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b64}"}},
                ],
            },
        ],
        "temperature": 0.0,
        "max_tokens": 2048,
        "stream": False,
    }
    req = urllib.request.Request(
        ELM_API_URL,
        data=json.dumps(payload).encode("utf-8"),
        method="POST",
        headers={
            "Authorization": f"Bearer {ELM_API_KEY}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
    )
    with urllib.request.urlopen(req, timeout=OCR_ELM_TIMEOUT_S) as resp:  # noqa: S310 — configured ELM URL
        data = json.loads(resp.read().decode("utf-8"))
    return data["choices"][0]["message"]["content"].strip()


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Fail loud on a misconfigured engine, then preload only if the legacy
    local engine is actually in use (no local model in this image)."""
    if OCR_USE_ELM:
        if not ELM_API_KEY:
            raise RuntimeError(
                "OCR_USE_ELM is on (default) but ELM_API_KEY is not set — export the "
                "company vision-model key, or set OCR_USE_ELM=0 to use a locally "
                "installed easyocr (not part of this image)."
            )
        log.info("OCR engine: ELM vision model %s @ %s", ELM_MODEL, ELM_API_URL)
    else:
        if easyocr is None:
            raise RuntimeError(
                "OCR_USE_ELM=0 but the local engine is not installed in this image. "
                "Set OCR_USE_ELM=1 with ELM_API_URL/ELM_MODEL/ELM_API_KEY, or build an "
                "image that includes easyocr."
            )
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(_executor, _get_reader, "en")
        await loop.run_in_executor(_executor, _get_reader, "ar")
    yield


app = FastAPI(title="OCR Service (ELM vision model)", lifespan=lifespan)

# ── Lazy-loaded readers per language (legacy local engine only) ──────

_reader_en = None
_reader_ar = None


def _get_reader(lang: str):
    global _reader_en, _reader_ar
    if easyocr is None:
        raise RuntimeError(
            "local OCR engine is not installed in this image — use OCR_USE_ELM=1 "
            "(company vision model) instead"
        )
    gpu = True  # uses GPU if torch detects CUDA, falls back to CPU otherwise
    if lang == "en":
        if _reader_en is None:
            _reader_en = easyocr.Reader(["en"], gpu=gpu)
        return _reader_en
    else:
        if _reader_ar is None:
            _reader_ar = easyocr.Reader(["ar"], gpu=gpu)
        return _reader_ar


# ── Image helpers ───────────────────────────────────────────────────


def _load_pages(raw: bytes, filename: str) -> list[Image.Image]:
    """Convert uploaded file to a list of PIL Images."""
    if filename.endswith(".pdf"):
        return convert_from_bytes(raw, dpi=300, first_page=1, last_page=OCR_MAX_PDF_PAGES)
    return [Image.open(io.BytesIO(raw)).convert("RGB")]


def _run_easyocr(reader, pil_img: Image.Image) -> list[tuple]:
    """Run EasyOCR on a single PIL Image."""
    import numpy as np
    return reader.readtext(np.array(pil_img))


_MAX_UPLOAD_BYTES = 20 * 1024 * 1024  # 20 MB


def _ocr_single_lang(
    reader, images: list[Image.Image]
) -> list[tuple[float, float, str, float]]:
    """Run OCR on all pages for one language.

    Returns [(page, y, text, confidence)] sorted top-to-bottom per page so
    the caller can reconstruct reading order after merging multiple languages.
    """
    results: list[tuple[float, float, str, float]] = []
    seen_texts: set[str] = set()
    for page_num, img in enumerate(images):
        page_results = _run_easyocr(reader, img)
        page_results.sort(key=lambda r: r[0][0][1])  # sort by top-left y-coordinate
        for bbox, text, conf in page_results:
            cleaned = text.strip()
            if cleaned and cleaned not in seen_texts:
                results.append((float(page_num), float(bbox[0][1]), cleaned, float(conf)))
                seen_texts.add(cleaned)
    return results


def _run_ocr(raw: bytes, filename: str, langs: list[str]) -> dict:
    """Synchronous OCR runner — called in thread pool."""
    images = _load_pages(raw, filename)

    # ELM vision model path — the company deployment choice (everything
    # through the ELM provider, same as the classifier).
    if OCR_USE_ELM:
        pages_text = []
        for page_num, img in enumerate(images):
            try:
                text = _ocr_via_elm(img)
            except Exception as exc:  # noqa: BLE001 — report, don't kill the request
                pages_text.append(f"<page {page_num + 1} OCR error: {exc}>")
                continue
            if text:
                pages_text.append(f"<page {page_num + 1}>\n{text}")
        full_text = "\n\n".join(pages_text)
        return {
            "engine": "elm",
            "model": ELM_MODEL,
            "text": full_text,
            "words": [],
            "low_confidence_words": [],
            "has_low_confidence": False,
        }

    if len(langs) == 1:
        raw_results = _ocr_single_lang(_get_reader(langs[0]), images)
    else:
        seen: dict[str, tuple[float, float, float]] = {}  # text -> (page, y, conf)
        for lang_code in langs:
            for page, y, text, conf in _ocr_single_lang(_get_reader(lang_code), images):
                if text not in seen or conf > seen[text][2]:
                    seen[text] = (page, y, conf)
        raw_results = sorted(
            [(p, y, t, c) for t, (p, y, c) in seen.items()],
            key=lambda r: (r[0], r[1]),
        )

    words = [{"text": t, "confidence": round(c, 4)} for _, _, t, c in raw_results]
    full_text = "\n".join(t for _, _, t, _ in raw_results)
    low_conf = [w for w in words if w["confidence"] < 0.6]

    return {
        "engine": "easyocr",
        "text": full_text,
        "words": words,
        "low_confidence_words": low_conf,
        "has_low_confidence": len(low_conf) > 0,
    }


# ── Endpoints ──────────────────────────────────────────────────────


@app.post("/ocr")
async def ocr(
    file: UploadFile = File(...),
    lang: str = Query("en", description="Language(s): 'en', 'ar', or 'both'"),
) -> dict:
    """Accept an image or PDF, return extracted text with confidence."""
    from fastapi import HTTPException
    # Read at most one byte past the cap — never buffer an oversized body.
    raw = await file.read(_MAX_UPLOAD_BYTES + 1)
    if len(raw) > _MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=413, detail="File too large (max 20 MB)")
    filename = (file.filename or "").lower()
    langs = {"en": ["en"], "ar": ["ar"], "both": ["en", "ar"]}.get(lang, ["en"])

    loop = asyncio.get_running_loop()
    result = await loop.run_in_executor(_executor, _run_ocr, raw, filename, langs)
    return result


@app.get("/health")
def health():
    return {"status": "ok"}
