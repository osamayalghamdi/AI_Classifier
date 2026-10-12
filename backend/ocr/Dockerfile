FROM python:3.12-slim

WORKDIR /app

# System deps: poppler for PDF page→image (the vision model does the OCR).
RUN apt-get update -qq && \
    apt-get install -y -qq --no-install-recommends \
        poppler-utils \
    && rm -rf /var/lib/apt/lists/*

# Python deps — deliberately NO local OCR/ML stack (no torch, no easyocr,
# no baked models). This service transcribes through the company's
# OpenAI-compatible vision endpoint: OCR_USE_ELM=1 (default) + ELM_API_URL /
# ELM_MODEL / ELM_API_KEY. See ocr_server.py for the legacy local path.
RUN pip install --no-cache-dir \
        pdf2image \
        Pillow \
        fastapi \
        uvicorn[standard] \
        python-multipart

COPY ocr_server.py .

EXPOSE 8001

CMD ["uvicorn", "ocr_server:app", "--host", "0.0.0.0", "--port", "8001"]
