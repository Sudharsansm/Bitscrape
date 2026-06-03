# Changelog

All notable changes to Bitscrape are documented here.
Format: [Keep a Changelog](https://keepachangelog.com/en/1.0.0/).
Versioning: [Semantic Versioning](https://semver.org/).

---

## [Unreleased]

### Planned
- AutoThrottle middleware (adaptive rate limiting)
- Prometheus metrics exporter
- Scrapy-compatible contract testing (`@url`, `@returns`, `@scrapes`)
- Supabase Storage pipeline for media files
- Dashboard UI (FastAPI + htmx)
- Kubernetes Helm chart

---

## [0.1.0] — 2024-01-01

### Added

**Core**
- `Spider` base class with `start_urls`, `parse()`, `open_spider()`, `close_spider()`
- `Request` and `Response` Pydantic v2 models
- `BaseItem` — Pydantic base class for all scraped items
- `FormRequest` — convenience class for POST form submissions
- `CrawlStats` — tracks requests, items, bytes, elapsed time, RPS
- `Settings` — full configuration via pydantic-settings + env vars

**Scheduler**
- `MemoryQueue` — asyncio priority queue (single-process)
- `RedisQueue` — Redis sorted-set queue (distributed)
- `MemoryDupeFilter` — in-memory URL fingerprint deduplication
- `RedisDupeFilter` — Redis-backed deduplication
- `Scheduler` — wires queue + dupefilter with depth-limit support

**Downloader**
- Async HTTP downloader via `aiohttp`
- Per-domain concurrency semaphores
- Exponential backoff retry on configurable HTTP codes
- Download delay support
- Built-in Playwright integration for JS-rendered pages

**Parser**
- `ParsedResponse` — CSS and XPath selector wrapper
- `NodeSelector` — wraps individual elements for chained queries
- `SelectorList` — iterable list of matched nodes/strings
- `::text` and `::attr(name)` CSS pseudo-elements
- selectolax backend (fast) with parsel/lxml fallback (full XPath)

**Middleware**
- `UserAgentMiddleware` — sets/rotates User-Agent header
- `RobotsMiddleware` — downloads and caches robots.txt
- `CookieMiddleware` — per-domain cookie jar
- `MiddlewareManager` — forward/reverse middleware chain

**Pipelines**
- `LoggingPipeline` — debug-log every item
- `ValidationPipeline` — Pydantic validation + dict-to-model coercion
- `DedupPipeline` — SHA-256 fingerprint item deduplication
- `PostgresPipeline` — async upsert via asyncpg
- `PipelineManager` — runs pipeline chain, handles `DropItem`

**Exporters**
- `JSONLExporter` — JSON Lines (one object per line)
- `JSONExporter` — JSON array
- `CSVExporter` — comma-separated values
- `XMLExporter` — XML with `<items>` wrapper

**Workflow**
- LangGraph state machine: `fetch → parse → pipeline → loop`
- `CrawlState` TypedDict schema
- `build_crawl_graph()` factory

**Engine**
- Async crawl loop with configurable concurrency semaphore
- Middleware integration (process_request / process_response / process_exception)
- Stats tracking and logging

**CLI**
- `bitscrape crawl` — run a spider from file path or module
- `bitscrape startproject` — scaffold a new project
- `bitscrape genspider` — generate spider from template (basic/crawl/sitemap)
- `bitscrape list` — list spiders in a directory
- Rich terminal output with crawl stats table

**Single-import API**
- `import bitscrape` exposes 27 symbols
- `bitscrape.run()` one-liner to run any spider
- `bitscrape.Item`, `bitscrape.Field`, `bitscrape.FormRequest` aliases

**Infrastructure**
- `pyproject.toml` with optional extras: playwright, redis, postgres, workflow, speed, full, dev
- PEP 561 `py.typed` marker
- GitHub Actions CI workflow (test on Python 3.11 + 3.12)
- GitHub Actions publish workflow (PyPI Trusted Publishing on version tags)
- MIT License
