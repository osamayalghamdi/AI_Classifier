"""OCR server — extracts text from images and PDFs using PaddleOCR.

Runs OCR in English and/or Arabic modes. Supports a `lang` query param
to skip unnecessary model runs.
"""

import asyncio
import io
from concurrent.futures import ThreadPoolExecutor
from functools import lru_cache

import numpy as np
from fastapi import FastAPI, File, Query, UploadFile
from pdf2image import convert_from_bytes
from paddleocr import PaddleOCR
from PIL import Image

app = FastAPI(title="OCR Service")
_executor = ThreadPoolExecutor(max_workers=2)


# ── Per-language OCR models (lazy, cached) ──────────────────────────

@lru_cache(maxsize=2)
def _get_ocr(lang: str) -> PaddleOCR:
    """Return a PaddleOCR instance for the given language."""
    return PaddleOCR(use_angle_cls=True, lang=lang)


# ── Image helpers ───────────────────────────────────────────────────

def _img_to_array(img: Image.Image) -> np.ndarray:
    return np.array(img)


def _load_pages(raw: bytes, filename: str) -> list[np.ndarray]:
    """Convert uploaded file to a list of numpy arrays (one per page)."""
    if filename.endswith(".pdf"):
        images = convert_from_bytes(raw, dpi=300)
        return [_img_to_array(p) for p in images]
    img = Image.open(io.BytesIO(raw)).convert("RGB")
    return [_img_to_array(img)]


# ── Core OCR (runs in thread pool to avoid blocking FastAPI) ───────

def _ocr_single_lang(ocr: PaddleOCR, arrays: list[np.ndarray]) -> dict[str, float]:
    """Run OCR on all pages for one language. Returns {cleaned_text: best_confidence}."""
    seen: dict[str, float] = {}
    for arr in arrays:
        result = ocr.ocr(arr)
        for page in result or []:
            if page is None:
                continue
            if isinstance(page, dict):
                texts = page.get("rec_texts") or []
                scores = page.get("rec_scores") or []
                for text, score in zip(texts, scores):
                    cleaned = text.strip()
                    if cleaned and (cleaned not in seen or score > seen[cleaned]):
                        seen[cleaned] = float(score)
                continue
            if isinstance(page, list):
                for line in page:
                    if line and len(line) >= 2:
                        text = line[1][0].strip()
                        score = float(line[1][1]) if len(line[1]) >= 2 else 0.0
                        if text and (text not in seen or score > seen[text]):
                            seen[text] = score
    return seen


def _run_ocr(raw: bytes, filename: str, langs: list[str]) -> dict:
    """Synchronous OCR runner — called in thread pool."""
    arrays = _load_pages(raw, filename)

    if len(langs) == 1:
        ocr = _get_ocr(langs[0])
        seen = _ocr_single_lang(ocr, arrays)
    else:
        results = [_ocr_single_lang(_get_ocr(l), arrays) for l in langs]
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
    """Accept an image or PDF, return extracted text with confidence.

    - `lang`: 'en' (default), 'ar', or 'both' — limit to one model for speed.
    """
    raw = await file.read()
    filename = (file.filename or "").lower()

    langs = {"en": ["en"], "ar": ["ar"], "both": ["en", "ar"]}.get(lang, ["en"])

    loop = asyncio.get_running_loop()
    result = await loop.run_in_executor(_executor, _run_ocr, raw, filename, langs)
    return result


@app.get("/health")
def health():
    return {"status": "ok"}
