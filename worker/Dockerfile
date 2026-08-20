# Two tested profiles share one Dockerfile:
#   quickstart (default): Python, Node.js, shell, Git, and common utilities
#   full: quickstart plus Go, Rust, Java, Ruby, PHP, databases, and Docker CLI
ARG WORKER_PROFILE=quickstart
FROM ghcr.io/blaxel-ai/sandbox:latest AS blaxel-sandbox
FROM python:3.12-bookworm AS python-runtime
FROM golang:1.22-bookworm AS go-runtime
FROM rust:1.77-bookworm AS rust-runtime
FROM eclipse-temurin:21-jdk-jammy AS java-runtime
FROM gradle:8-jdk21-jammy AS gradle-runtime
FROM ruby:3.3-bookworm AS ruby-runtime
FROM php:8.3-cli-bookworm AS php-runtime
FROM composer:2 AS composer-runtime
FROM gcc:13-bookworm AS gcc-runtime

FROM node:22-bookworm-slim AS common

COPY --from=blaxel-sandbox /sandbox-api /usr/local/bin/sandbox-api
COPY --from=python-runtime /usr/local /usr/local

RUN apt-get update && apt-get install -y --no-install-recommends \
      bash ca-certificates curl wget git openssh-client \
      tar zip unzip jq patch diffutils sed gawk grep ripgrep tree \
      build-essential make sqlite3 netcat-openbsd \
      libffi8 libssl3 libsqlite3-0 libxml2 libyaml-0-2 libzstd1 \
  && python3 -m pip install --no-cache-dir uv \
  && npm install -g pnpm \
  && rm -rf /var/lib/apt/lists/*

COPY requirements.lock /worker/requirements.lock
RUN python3 -m pip install --no-cache-dir --require-hashes -r /worker/requirements.lock

ENV PATH="/usr/local/bin:${PATH}" \
    CMA_WORKER_PROFILE=quickstart

FROM common AS quickstart

FROM common AS full
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
    PATH="/opt/gradle/bin:/usr/local/go/bin:/usr/local/cargo/bin:/opt/java/openjdk/bin:${PATH}" \
    CMA_WORKER_PROFILE=full

RUN apt-get update && apt-get install -y --no-install-recommends \
      vim nano tmux screen htop \
      maven cmake \
      libargon2-1 libbrotli1 libcurl4 libgmp10 libgnutls30 \
      libgssapi-krb5-2 libhogweed6 libicu72 libidn2-0 libkeyutils1 \
      libldap-2.5-0 liblzma5 libnettle8 libonig5 libp11-kit0 libpsl5 \
      librtmp1 libsasl2-2 libssh2-1 libstdc++6 libtasn1-6 libunistring2 \
      postgresql-client redis-tools docker.io \
  && gem install --no-document bundler \
  && rm -rf /var/lib/apt/lists/*

RUN set -eux; \
    printf '%s\n' '#!/bin/sh' 'exec /usr/local/go/bin/go "$@"' > /usr/local/bin/go; \
    printf '%s\n' '#!/bin/sh' 'exec /usr/local/go/bin/gofmt "$@"' > /usr/local/bin/gofmt; \
    chmod +x /usr/local/bin/go /usr/local/bin/gofmt

FROM ${WORKER_PROFILE} AS final

COPY entrypoint.sh /entrypoint.sh
COPY smoke.sh worker.py /worker/
RUN chmod +x /entrypoint.sh /worker/smoke.sh /worker/worker.py \
  && mkdir -p /workspace /mnt/memory

WORKDIR /workspace
ENV HOME=/workspace

ENTRYPOINT ["/entrypoint.sh"]
