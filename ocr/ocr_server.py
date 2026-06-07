"""OCR server — extracts text from images and PDFs using EasyOCR.

Supports English and Arabic with GPU acceleration.
"""

import asyncio
import io
import os
import logging
from concurrent.futures import ThreadPoolExecutor

# Silence EasyOCR progress bars (they write to stderr via tqdm)
os.environ["EASYOCR_VERBOSE"] = "False"
logging.getLogger("easyocr").setLevel(logging.WARNING)

import easyocr
from fastapi import FastAPI, File, Query, UploadFile
from pdf2image import convert_from_bytes
from PIL import Image

app = FastAPI(title="OCR Service (EasyOCR)")
_executor = ThreadPoolExecutor(max_workers=2)

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


def _ocr_single_lang(reader: easyocr.Reader, images: list[Image.Image]) -> dict[str, float]:
    """Run OCR on all pages for one language. Returns {cleaned_text: best_confidence}."""
    seen: dict[str, float] = {}
    for img in images:
        results = _run_easyocr(reader, img)
        for bbox, text, conf in results:
            cleaned = text.strip()
            if cleaned and (cleaned not in seen or conf > seen[cleaned]):
                seen[cleaned] = float(conf)
    return seen


def _run_ocr(raw: bytes, filename: str, langs: list[str]) -> dict:
    """Synchronous OCR runner — called in thread pool."""
    images = _load_pages(raw, filename)

    if len(langs) == 1:
        reader = _get_reader(langs[0])
        seen = _ocr_single_lang(reader, images)
    else:
        results = [_ocr_single_lang(_get_reader(l), images) for l in langs]
        seen: dict[str, float] = {}
        for r in results:
            for text, score in r.items():
                if text not in seen or score > seen[text]:
                    seen[text] = score

    words = [{"text": t, "confidence": round(s, 4)} for t, s in seen.items()]
    full_text = "\n".join(t for t in seen)
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
    raw = await file.read()
    filename = (file.filename or "").lower()
    langs = {"en": ["en"], "ar": ["ar"], "both": ["en", "ar"]}.get(lang, ["en"])

    loop = asyncio.get_running_loop()
    result = await loop.run_in_executor(_executor, _run_ocr, raw, filename, langs)
    return result


@app.get("/health")
def health():
    return {"status": "ok"}
