"""OCR server — extracts text from images and PDFs using EasyOCR.

Supports English and Arabic with GPU acceleration.
"""

import asyncio
import io
import os
import logging
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager

# Silence EasyOCR progress bars (they write to stderr via tqdm)
os.environ["EASYOCR_VERBOSE"] = "False"
logging.getLogger("easyocr").setLevel(logging.WARNING)

import easyocr
from fastapi import FastAPI, File, Query, UploadFile
from pdf2image import convert_from_bytes
from PIL import Image

_executor = ThreadPoolExecutor(max_workers=2)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Eagerly load both language models so the first request doesn't time out."""
    loop = asyncio.get_running_loop()
    await loop.run_in_executor(_executor, _get_reader, "en")
    await loop.run_in_executor(_executor, _get_reader, "ar")
    yield


app = FastAPI(title="OCR Service (EasyOCR)", lifespan=lifespan)

# ── Lazy-loaded readers per language ───────────────────────────────

_reader_en: easyocr.Reader | None = None
_reader_ar: easyocr.Reader | None = None


def _get_reader(lang: str) -> easyocr.Reader:
    global _reader_en, _reader_ar
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
        return convert_from_bytes(raw, dpi=300)
    return [Image.open(io.BytesIO(raw)).convert("RGB")]


def _run_easyocr(reader: easyocr.Reader, pil_img: Image.Image) -> list[tuple]:
    """Run EasyOCR on a single PIL Image."""
    import numpy as np
    return reader.readtext(np.array(pil_img))


_MAX_UPLOAD_BYTES = 20 * 1024 * 1024  # 20 MB


def _ocr_single_lang(
    reader: easyocr.Reader, images: list[Image.Image]
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
    raw = await file.read()
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
