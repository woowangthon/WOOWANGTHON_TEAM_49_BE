# syntax=docker/dockerfile:1
FROM python:3.14-slim-bookworm AS base

FROM base AS dependencies
COPY --from=ghcr.io/astral-sh/uv:0.13.7 /uv /usr/local/bin/uv
ENV UV_PYTHON_DOWNLOADS=never UV_LINK_MODE=copy UV_COMPILE_BYTECODE=1
WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN uv sync --locked --no-dev

FROM base AS runtime
RUN apt-get update \
    && apt-get install -y --no-install-recommends tesseract-ocr tesseract-ocr-eng tesseract-ocr-kor \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --gid 10001 app \
    && useradd --uid 10001 --gid app --create-home app \
    && mkdir -p /var/lib/wolgeup \
    && chown app:app /var/lib/wolgeup
WORKDIR /app
COPY --from=dependencies /app/.venv /app/.venv
COPY app ./app
COPY agent ./agent
COPY assets ./assets
ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 \
    HOST=0.0.0.0 PORT=8000 DATA_DIR=/var/lib/wolgeup \
    AGENT_FONT_PATH=/app/assets/fonts/NotoSansKR-Regular.ttf \
    AGENT_OCR_COMMAND=/usr/bin/tesseract \
    AGENT_OCR_DATA_PATH=/usr/share/tesseract-ocr/5/tessdata \
    WORKER_CONCURRENCY=1 WORKERS_ENABLED=true PASSWORD_RESET_DELIVERY=disabled
USER app
EXPOSE 8000
HEALTHCHECK --interval=15s --timeout=5s --start-period=30s --retries=3 \
    CMD ["python", "-c", "import json, urllib.request; r=json.load(urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=4)); assert r['data']['status']=='ok'"]
CMD ["python", "-m", "app"]
