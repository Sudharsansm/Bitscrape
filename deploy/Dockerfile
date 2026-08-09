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

# Playwright's own browser binaries are a separate, much larger layer --
# only install them if you actually use use_playwright=True. Uncomment if
# needed:
# RUN pip install playwright && playwright install --with-deps chromium

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
EXPOSE 8765 9100

ENTRYPOINT ["bitscrape"]
CMD ["--help"]
