# Debian (glibc) base, not Alpine (musl): the worker image is where the agent
# executes code, and on musl a routine `pip install numpy/pandas` falls back to
# slow/failing source builds. glibc gets real manylinux wheels. node comes from
# the base image; bash is already present (Alpine would lack it).
# Keep the image linux/amd64 so it matches the Blaxel sandbox API image copied
# below and builds consistently on Apple Silicon Docker hosts.
FROM --platform=linux/amd64 ghcr.io/blaxel-ai/sandbox:latest AS blaxel-sandbox
FROM --platform=linux/amd64 python:3.12-bookworm AS python-runtime
FROM --platform=linux/amd64 golang:1.22-bookworm AS go-runtime
FROM --platform=linux/amd64 rust:1.77-bookworm AS rust-runtime
FROM --platform=linux/amd64 eclipse-temurin:21-jdk-jammy AS java-runtime
FROM --platform=linux/amd64 gradle:8-jdk21-jammy AS gradle-runtime
FROM --platform=linux/amd64 ruby:3.3-bookworm AS ruby-runtime
FROM --platform=linux/amd64 php:8.3-cli-bookworm AS php-runtime
FROM --platform=linux/amd64 composer:2 AS composer-runtime
FROM --platform=linux/amd64 gcc:13-bookworm AS gcc-runtime
FROM --platform=linux/amd64 node:22-bookworm-slim

COPY --from=blaxel-sandbox /sandbox-api /usr/local/bin/sandbox-api
COPY --from=python-runtime /usr/local /usr/local
COPY --from=go-runtime /usr/local/go /usr/local/go
COPY --from=rust-runtime /usr/local/cargo /usr/local/cargo
COPY --from=rust-runtime /usr/local/rustup /usr/local/rustup
COPY --from=java-runtime /opt/java/openjdk /opt/java/openjdk
COPY --from=gradle-runtime /opt/gradle /opt/gradle
COPY --from=ruby-runtime /usr/local /usr/local
COPY --from=php-runtime /usr/local /usr/local
COPY --from=composer-runtime /usr/bin/composer /usr/local/bin/composer
COPY --from=gcc-runtime /usr/local /usr/local

ENV JAVA_HOME=/opt/java/openjdk \
    RUSTUP_HOME=/usr/local/rustup \
    CARGO_HOME=/usr/local/cargo \
    PATH="/opt/gradle/bin:/usr/local/go/bin:/usr/local/cargo/bin:/opt/java/openjdk/bin:${PATH}"

# The worker image is the agent's execution environment. Keep its available
# toolchain compatible with Anthropic's cloud sandbox reference so self-hosted
# sessions see the same documented commands and runtime floors. This remains a
# Blaxel-built Debian image, not Anthropic's managed Ubuntu image.
RUN apt-get update && apt-get install -y --no-install-recommends \
      bash ca-certificates curl wget git openssh-client \
      tar zip unzip jq patch diffutils \
      sed gawk grep vim nano \
      tmux screen tree htop ripgrep \
      build-essential gcc g++ make \
      maven \
      libargon2-1 libbrotli1 libcurl4 libffi8 libgmp10 libgnutls30 \
      libgssapi-krb5-2 libhogweed6 libicu72 libidn2-0 libkeyutils1 \
      libldap-2.5-0 liblzma5 libnettle8 libonig5 libp11-kit0 libpsl5 \
      librtmp1 libsasl2-2 libsqlite3-0 libssh2-1 libssl3 libstdc++6 \
      libtasn1-6 libunistring2 libxml2 libyaml-0-2 libzstd1 \
      sqlite3 postgresql-client redis-tools \
      docker.io netcat-openbsd \
  && python3 -m pip install --no-cache-dir uv cmake \
  && cp /usr/bin/make /usr/local/bin/make \
  && npm install -g pnpm \
  && rm -rf /var/lib/apt/lists/*

RUN set -eux; \
    printf '%s\n' '#!/bin/sh' 'exec /usr/local/go/bin/go "$@"' > /usr/local/bin/go; \
    printf '%s\n' '#!/bin/sh' 'exec /usr/local/go/bin/gofmt "$@"' > /usr/local/bin/gofmt; \
    chmod +x /usr/local/bin/go /usr/local/bin/gofmt

RUN gem install --no-document bundler

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
