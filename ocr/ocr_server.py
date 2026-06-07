"""OCR server — extracts text from images and PDFs using PaddleOCR.

Runs OCR in English and Arabic modes, returns each word with its
confidence score so the frontend can highlight low-confidence text.
"""

import io

import numpy as np
from fastapi import FastAPI, UploadFile, File
from pdf2image import convert_from_bytes
from paddleocr import PaddleOCR
from PIL import Image

app = FastAPI(title="OCR Service")

_ocr_en: PaddleOCR | None = None
_ocr_ar: PaddleOCR | None = None


def _get_ocr(lang: str) -> PaddleOCR:
    global _ocr_en, _ocr_ar
    if lang == "en":
        if _ocr_en is None:
            _ocr_en = PaddleOCR(use_angle_cls=True, lang="en")
        return _ocr_en
    else:
        if _ocr_ar is None:
            _ocr_ar = PaddleOCR(use_angle_cls=True, lang="ar")
        return _ocr_ar


def _img_to_array(img: Image.Image) -> np.ndarray:
    return np.array(img)


@app.post("/ocr")
async def ocr(file: UploadFile = File(...)) -> dict:
    """Accept an image or PDF, return extracted text with confidence."""
    raw = await file.read()
    filename = (file.filename or "").lower()

    if filename.endswith(".pdf"):
        images = convert_from_bytes(raw, dpi=300)
        arrays = [_img_to_array(p) for p in images]
    else:
        img = Image.open(io.BytesIO(raw)).convert("RGB")
        arrays = [_img_to_array(img)]

    # Collect words with confidence from both language models
    seen_text: dict[str, float] = {}  # text → best confidence

    for lang in ("en", "ar"):
        ocr = _get_ocr(lang)
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
                        if cleaned:
                            if cleaned not in seen_text or score > seen_text[cleaned]:
                                seen_text[cleaned] = float(score)
                    continue
                if isinstance(page, list):
                    for line in page:
                        if line and len(line) >= 2:
                            text = line[1][0].strip()
                            score = float(line[1][1]) if len(line[1]) >= 2 else 0.0
                            if text:
                                if text not in seen_text or score > seen_text[text]:
                                    seen_text[text] = score

    # Build response with confidence
    words = [{"text": t, "confidence": round(s, 4)} for t, s in seen_text.items()]
    full_text = "\n".join(t for t in seen_text)
    low_conf = [w for w in words if w["confidence"] < 0.6]

    return {
        "text": full_text,
        "words": words,
        "low_confidence_words": low_conf,
        "has_low_confidence": len(low_conf) > 0,
    }


@app.get("/health")
def health():
    return {"status": "ok"}
