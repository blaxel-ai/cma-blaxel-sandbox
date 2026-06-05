FROM python:3.12-slim

# Blaxel sandbox runtime API (required for every Blaxel sandbox).
COPY --from=ghcr.io/blaxel-ai/sandbox:latest /sandbox-api /usr/local/bin/sandbox-api

RUN apt-get update \
  && apt-get install -y --no-install-recommends curl ca-certificates \
  && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app.py .
COPY blaxel_features.py .

ENV HOME=/app

# sandbox-api is the sandbox runtime/init. The FastAPI webhook server
# (uvicorn app:app) is started by setup.py via the sandbox process API after
# the sandbox is created, and persists across standby/resume.
ENTRYPOINT ["/usr/local/bin/sandbox-api"]
