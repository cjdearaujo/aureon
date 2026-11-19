# Main service: FastAPI + Deep Agents + dashboard, with FinBERT and Whisper baked in (CPU).
FROM python:3.12-slim AS base
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 \
    HF_HOME=/models/hf WHISPER_CACHE=/models/whisper
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg && rm -rf /var/lib/apt/lists/*
WORKDIR /app

# CPU-only PyTorch keeps the image ~2 GB smaller than the default CUDA build.
RUN pip install torch --index-url https://download.pytorch.org/whl/cpu
COPY requirements.txt .
RUN pip install -r requirements.txt

# Bake the finance + voice models into the image (no downloads at runtime, faster cold starts).
ARG WHISPER_MODEL=small
RUN python -c "from transformers import pipeline; pipeline('text-classification', model='ProsusAI/finbert')" && \
    python -c "from faster_whisper import WhisperModel; WhisperModel('${WHISPER_MODEL}', device='cpu', compute_type='int8', download_root='/models/whisper')"

COPY app ./app
RUN useradd -m appuser && chown -R appuser /app /models
USER appuser
ENV PORT=8080 WHISPER_MODEL=${WHISPER_MODEL}
CMD exec uvicorn app.main:app --host 0.0.0.0 --port ${PORT} --proxy-headers --forwarded-allow-ips='*' --ws-ping-interval 20
