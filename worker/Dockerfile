FROM node:22-alpine

COPY --from=ghcr.io/blaxel-ai/sandbox:latest /sandbox-api /usr/local/bin/sandbox-api

# bash: the agent_toolset `bash` tool requires /bin/bash at that exact path.
# unzip + tar: required by the ant SDK helpers for skill download.
RUN apk update && apk add --no-cache \
	git curl tar bash unzip \
  && rm -rf /var/cache/apk/*

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
RUN chmod +x /entrypoint.sh

RUN mkdir -p /workspace /mnt/session/outputs

WORKDIR /workspace
ENV HOME=/workspace

ENTRYPOINT ["/entrypoint.sh"]
