# Debian (glibc) base, not Alpine (musl): the worker image is where the agent
# executes code, and on musl a routine `pip install numpy/pandas` falls back to
# slow/failing source builds. glibc gets real manylinux wheels. node comes from
# the base image; bash is already present (Alpine would lack it).
# Keep the image linux/amd64 so it matches the Blaxel sandbox API image copied
# below and builds consistently on Apple Silicon Docker hosts.
FROM --platform=linux/amd64 ghcr.io/blaxel-ai/sandbox:latest AS blaxel-sandbox
FROM --platform=linux/amd64 node:22-bookworm-slim

COPY --from=blaxel-sandbox /sandbox-api /usr/local/bin/sandbox-api

# The worker image is the agent's execution environment, so it ships the runtimes
# and tools the agent uses. python3 + pip is the common second runtime (node is in
# the base). netcat-openbsd backs the port wait in entrypoint.sh; unzip/tar/git/curl
# support skill download and general use. Removing EXTERNALLY-MANAGED lets the agent
# `pip install` freely in this disposable sandbox. Extend this list for your stack.
RUN apt-get update && apt-get install -y --no-install-recommends \
      git curl ca-certificates tar unzip netcat-openbsd \
      python3 python3-pip python-is-python3 \
  && rm -f /usr/lib/python3.*/EXTERNALLY-MANAGED \
  && rm -rf /var/lib/apt/lists/*

# ant CLI version (the worker binary). Override with --build-arg ANT_VERSION=...
ARG ANT_VERSION=1.10.0
ARG TARGETARCH=amd64

RUN set -eux; \
    ARCH=$([ "$TARGETARCH" = "arm64" ] && echo arm64 || echo amd64); \
    curl -fsSL \
      "https://github.com/anthropics/anthropic-cli/releases/download/v${ANT_VERSION}/ant_${ANT_VERSION}_linux_${ARCH}.tar.gz" \
      | tar -xz -C /usr/local/bin ant; \
    chmod +x /usr/local/bin/ant

COPY entrypoint.sh /entrypoint.sh
COPY smoke.sh /worker/smoke.sh
RUN chmod +x /entrypoint.sh /worker/smoke.sh

RUN mkdir -p /workspace /mnt/session/outputs

WORKDIR /workspace
ENV HOME=/workspace

ENTRYPOINT ["/entrypoint.sh"]
