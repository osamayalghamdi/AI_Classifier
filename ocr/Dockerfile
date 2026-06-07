FROM python:3.12-slim

WORKDIR /app

# System deps: poppler for PDF page→image, build tools for paddle
RUN apt-get update -qq && \
    apt-get install -y -qq --no-install-recommends \
        poppler-utils \
        build-essential \
        libgl1 \
        libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/*

# Python deps
RUN pip install --no-cache-dir \
        paddlepaddle==3.0.0 \
        paddleocr>=2.9 \
        pdf2image \
        Pillow \
        fastapi \
        uvicorn[standard] \
        python-multipart

COPY ocr_server.py .

EXPOSE 8001

CMD ["uvicorn", "ocr_server:app", "--host", "0.0.0.0", "--port", "8001"]
