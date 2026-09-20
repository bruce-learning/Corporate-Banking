ARG PYTHON_IMAGE=python:3.10-slim-bookworm
FROM ${PYTHON_IMAGE}

LABEL org.opencontainers.image.title="EchoMind Bank Customer Service"
LABEL org.opencontainers.image.description="FastAPI multi-agent banking customer-service demo"

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ca-certificates \
        libgomp1 \
        poppler-utils \
        tesseract-ocr \
        tesseract-ocr-chi-sim \
        tesseract-ocr-eng \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.lock.txt requirements.txt ./
RUN python -m pip install --upgrade pip \
    && python -m pip install -r requirements.lock.txt

COPY . .

RUN groupadd --gid 10001 bankapp \
    && useradd --uid 10001 --gid bankapp --create-home --home-dir /home/bankapp bankapp \
    && mkdir -p /app/var/data /app/var/logs /app/var/chroma /chroma \
    && chown -R bankapp:bankapp /app /chroma

USER bankapp

EXPOSE 8000

CMD ["python", "scripts/run_api.py"]
