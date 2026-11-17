# syntax=docker/dockerfile:1
# Multi-stage build: a slim final image without build toolchains baked in.

FROM python:3.11-slim AS builder

WORKDIR /build

# System deps needed only to BUILD some wheels (lxml, etc.) -- not present
# in the final image.
RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential \
        libxml2-dev \
        libxslt1-dev \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml ./
COPY src ./src

RUN pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir --prefix=/install ".[all,cli]"

# ---------------------------------------------------------------------------

FROM python:3.11-slim AS runtime

RUN apt-get update && apt-get install -y --no-install-recommends \
        libxml2 \
        libxslt1.1 \
        curl \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --create-home --uid 1000 bitscrape

COPY --from=builder /install /usr/local

WORKDIR /app
USER bitscrape

# Liveness/readiness probes can hit this once StatsMonitor or the
# Prometheus /metrics endpoint is wired up in your entrypoint script.
# 8000 is the MCP server's HTTP transport (`bitscrape mcp --transport streamable-http`).
EXPOSE 8765 9100 8000

ENTRYPOINT ["bitscrape"]
CMD ["--help"]

# ---------------------------------------------------------------------------
# `browsers` target: the same image plus Playwright's Chromium, Firefox and
# WebKit (and their system libraries), for use_playwright=True, DynamicFetcher,
# browser sessions and the MCP server's session tools. It is a much larger
# image, so it is a separate target:
#
#   docker build -f deploy/Dockerfile --target runtime  -t bitscrape:slim .
#   docker build -f deploy/Dockerfile --target browsers -t bitscrape:browsers .
#
# The LAST stage is what a plain `docker build` produces, so `browsers` is the
# default; use --target runtime for the slim, HTTP-only image.
# ---------------------------------------------------------------------------

FROM runtime AS browsers

USER root
ENV PLAYWRIGHT_BROWSERS_PATH=/ms-playwright
RUN mkdir -p /ms-playwright \
    && python -m playwright install --with-deps chromium firefox webkit \
    && chmod -R a+rX /ms-playwright \
    && rm -rf /var/lib/apt/lists/*
USER bitscrape
