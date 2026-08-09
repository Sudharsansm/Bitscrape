# Changelog

The full, canonical release history lives in the root
[`CHANGELOG.md`](../../CHANGELOG.md) -- this page is a short summary/index so
it's reachable from within `docs/`.

## Release summary

| Version | Highlights |
|---|---|
| **0.8.0** | Fixed 4 real bugs found via QA-driven testing: `self.follow()` now resolves relative URLs (concurrency-safe via `contextvars`), `XMLExporter` no longer produces invalid XML with special characters, `parsel` dependency was missing (broke `.xpath()`), parsel-native XPath text extraction was broken. `PostgresStorageBackend`/`PostgresPipeline` now live-server-verified; `MongoStorageBackend` now a real implementation. Coverage 80%→93%, 440 tests. |
| **0.7.0** | Simplified top-level API: `@bitscrape.spider()` decorator, `bitscrape.run()` fixed to delegate to `build_engine()` (was missing `MetaRobotsMiddleware`), dynamic `__version__`, full feature set importable from top-level `bitscrape`. |
| **0.6.0** | `build_engine()` factory: one function wiring middleware/plugins/observability from `Settings`. Declined a proposed 5-pattern architecture rewrite (event bus, actor runtime) in favor of this. |
| **0.5.0** | Hyperscale search infrastructure: `Frontier`, `LinkGraph` (PageRank/HITS), `RecrawlScheduler`, canonicalization + SimHash dedup, `BM25Index`/`VectorIndex`/RRF, `EntityResolver`, real Prometheus/OpenTelemetry observability, Docker/Kubernetes manifests. |
| **0.4.0** | Distributed crawling (`DistributedThrottleMiddleware`), `AutoThrottle`, `SessionPoolMiddleware`, `BrowserPool`, pluggable storage backends (SQLite/S3/Postgres/stubs), plugin system, `StatsMonitor`. |
| **0.3.0** | Crawler-etiquette features matching Googlebot/ClaudeBot/PerplexityBot-class crawlers: conditional GET, Retry-After-aware backoff, meta-robots (`noindex`/`nofollow`) handling. |
| **0.2.1** | Diagnostic for "0 items scraped" (the Engine now logs why). CLI `--version` fixed to read real installed version. |
| **0.2.0** | Full packaging: `pyproject.toml`, `LICENSE`, `CHANGELOG.md`, examples, pinned dev tooling. |
| **0.1.6 -> fixes** | Root-caused and fixed: `MemoryQueue` priority-queue crash on same-priority requests, `RobotsMiddleware` silently disabled by a `parser.feed()` vs `.parse()` bug (robots.txt enforcement was a complete no-op), fail-open -> fail-safe robots.txt error handling, `ProxyMiddleware`, `scroll_to_bottom()`. |

## Versioning

This project follows the version number in `pyproject.toml`'s
`[project] version` field. Both `bitscrape --version` and
`python -c "import bitscrape; print(bitscrape.__version__)"` read this
dynamically from installed package metadata -- neither is a hardcoded
string (both previously were, at different points, and both were fixed;
see the 0.2.1 and 0.7.0 entries above).

## Full history

-> [`CHANGELOG.md`](../../CHANGELOG.md)
