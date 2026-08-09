# Changelog

All notable changes to this fork/patch are documented here.
Format loosely follows [Keep a Changelog](https://keepachangelog.com/).

## [0.8.0] — 2026-07-27

Follow-up to an external QA pass (verified independently before acting on
it — see the "How this release was produced" note below) that closed all
five flagged coverage gaps and, in the process of writing real tests for
each, found and fixed **four genuine application bugs** plus **one real
packaging bug**. As always: every fix below has a real, passing test
behind it; nothing here is a documentation-only change pretending to be a
code fix.

### Fixed — real bugs found while closing test coverage gaps

- **`self.follow()` didn't resolve relative URLs against the current
  page.** A long-standing, previously-documented limitation
  (`self.follow("/next")` enqueued the literal string `/next` instead of
  the resolved absolute URL) — now fixed for real. The Engine tracks the
  response currently being parsed via a `contextvars.ContextVar` (set for
  the duration of each callback), and `follow()` resolves relative URLs
  against it with `urljoin()`. **Deliberately not a plain instance
  attribute**: the Engine processes multiple requests *concurrently* via
  `asyncio.create_task()` against one shared `Spider` instance, so a plain
  `self._current_response = response` would let two in-flight requests
  clobber each other's resolution context at an `await` point inside a
  callback. `ContextVar` is correctly isolated per `asyncio` task. Verified
  with a dedicated concurrency-safety test: two pages with *different*
  relative links, deliberately raced against each other (one artificially
  slow), each resolve against their own URL, not the other's.
  (`bitscrape/core/spider.py`, `bitscrape/engine.py`,
  `bitscrape/workflow/graph.py` — the same fix applied to the optional
  LangGraph workflow path for consistency)
- **`XMLExporter` produced invalid, unparseable XML for any item value
  containing `&`, `<`, `>`, or `"`.** (e.g. a scraped title like "Smith &
  Sons" broke the whole output file.) Fixed with `xml.sax.saxutils.escape`.
  Also fixed: dict keys with spaces or special characters (e.g. `"2024
  price ($)"`) produced invalid XML element names — now sanitized into
  valid tag names. (`bitscrape/exporters/feed.py`)
- **`parsel` was never declared as a project dependency**, despite
  `.xpath()` requiring it, being documented throughout `docs/`, and being
  used in the bundled `examples/polite_sitemap_spider.py`. A fresh
  `pip install -e ".[all,cli]"` left `.xpath()` broken with
  `ImportError: XPath requires parsel: pip install parsel` on the default
  `selectolax` backend (which has no native XPath support). Fixed by
  adding `parsel>=1.9` to base dependencies. (`pyproject.toml`)
- **The parsel-native XPath backend (used when `selectolax` is
  unavailable) silently returned no results for `text()`/`@attr`
  queries.** `isinstance(m, str)` was checked against the wrong object —
  `parsel.Selector.xpath()` always wraps results in a `Selector`, even for
  text/attribute queries; the actual string lives at `m.root`, not `m`
  itself. Fixed by checking `m.root`. Only affects installs where
  `selectolax` is genuinely absent (not the default), but a real,
  previously-undetected bug in a documented fallback path.
  (`bitscrape/parser/selector.py`)
- **The optional LangGraph workflow path misclassified follow-up
  `Request`s as scraped items.** `parse_node`'s classification checked
  `hasattr(out, "model_dump")` to detect items, but `Request` is *also* a
  pydantic `BaseModel` (so it also has `model_dump`) — every `Request`
  yielded by a spider was being routed into `items` instead of
  `new_requests`. Fixed by checking `isinstance(out, Request)` first.
  (`bitscrape/workflow/graph.py`)

### Added — real implementations replacing documented stubs / gaps

- **`PostgresStorageBackend` and `PostgresPipeline` are now verified
  against a real, live PostgreSQL server** (previously mock-connection-
  tested only, since no live server was available in earlier build
  environments). A live server became available; added
  `tests/test_postgres_live.py` (connection pooling, real JSONB storage
  and querying, persistence across backend-instance/process restarts,
  concurrent writes, upsert-on-conflict) and expanded
  `tests/test_pipelines.py` (real inserts, real `ON CONFLICT` upserts,
  connection-failure handling).
- **`MongoStorageBackend` is now a real implementation**, no longer a
  `NotImplementedError` stub. Implemented against `motor` (MongoDB's
  official async driver) and tested against `mongomock_motor` — a genuine
  async MongoDB API emulator (the same category of tool as `moto` for
  S3), injected via a new `client=` constructor parameter. A live MongoDB
  server wasn't available (MongoDB needs its own separate apt repository,
  not present in this build environment's package mirror), so this is
  "verified against a real emulator," the same honest tier Postgres was at
  before a live server became available for it. `ElasticsearchStorageBackend`
  remains a documented stub — no comparable, easily-installable ES emulator
  exists to test a real implementation against.
- **`workflow/graph.py` (the optional LangGraph orchestration path) is now
  tested end-to-end against a real local HTTP server** — previously 0%
  coverage since it needs `langgraph` installed, and was untested even
  when the package was present. `langgraph` added as an optional
  `workflow` extra.
- **`cli/main.py`'s subcommands are now tested end-to-end via
  `click.testing.CliRunner`** — `crawl` (JSONL/CSV output, concurrency
  flag, stats summary, failure on a nonexistent spider file), `list`,
  `genspider` (all three templates, output confirmed to be valid Python),
  and `startproject` (structure, valid Python settings file, failure on an
  existing directory).

### Fixed — tooling/config

- **Bumped `[tool.mypy] python_version` from 3.11 to 3.12.** `numpy`
  (pulled in transitively via `networkx`, used by `LinkGraph`) ships inline
  type stubs using newer type-statement syntax that `mypy` can't parse
  under a 3.11 target — a hard parse error that halted the whole `mypy`
  run, not something `ignore_missing_imports` could paper over (the stub
  *is* found; it just can't be parsed at the configured version). 3.12 is
  within this project's supported range (`requires-python = ">=3.11"`) and
  matches the actual runtime used throughout this project's own testing.
- Added `numpy`, `scipy`, `motor`, `mongomock_motor` to the existing
  `ignore_missing_imports` override list, consistent with the pattern
  already used for other optional third-party dependencies.
- Added `pytest-cov` to the `dev` extra for coverage reporting, and
  `mongomock-motor` for testing the new `MongoStorageBackend`.

### Testing

- **440 tests total, all passing** (418 verified-QA baseline + 22 new in
  `tests/test_pipelines.py`), up from 332 at the last verified baseline.
  Overall coverage 93%, up from 80–81%. Per-module improvements on the
  specific gaps flagged: `cli/main.py` 36%→94%, `exporters/feed.py`
  61%→99%, `parser/selector.py` 46%→73%, `pipeline/pipelines.py`
  41%→98%, `workflow/graph.py` 0%→100%.
- ruff clean on the whole tree; mypy clean (`Success: no issues found in
  36 source files`).

### How this release was produced

An external QA report (`BITSCRAPE_QA_REPORT.md`) claimed 332 passing
tests, a mypy fix (missing `pydantic.mypy` plugin), and 5 flagged coverage
gaps. Rather than accepting the report at face value, every claim was
independently re-verified before building on it: the mypy fix was
confirmed genuine by toggling the plugin config off and back on and
watching the errors reappear/disappear; the diff was checked file-by-file
against the claimed change list; two of the nine new tests were read in
full to confirm they measured real behavior, not decorative assertions;
and the full suite was re-run from a genuinely fresh install rather than
trusted from the report's own numbers. Only after that independent
verification passed did this release build on top of it.

## [0.7.0] — 2026-07-24

Simplifying the developer-facing ecosystem: found and fixed a real
consistency bug in the existing top-level API while doing this, on top of
adding a genuinely simpler entry point.

### Fixed

- **`bitscrape.run()` had its own separate, simplified middleware wiring
  that had drifted out of sync with everything else** — most importantly,
  it never included `MetaRobotsMiddleware`, so a page marked
  `<meta name="robots" content="noindex">` would have had its item counted
  as scraped via `bitscrape.run()`, while the CLI (already on
  `build_engine()` since 0.6.0) would have correctly excluded it. Same
  bug-class as the CLI's stale hardcoded `--version` string fixed
  previously. Fixed by making `run()` delegate to `build_engine()` — the
  one function now used by both the CLI and the top-level API, so they
  can't drift apart again. Proven with a regression test: the exact
  noindex scenario, run through `bitscrape.run()`, now correctly reports
  `items_scraped=0, items_noindexed=1`.
- **`bitscrape.__version__` was hardcoded to a stale `"0.1.0"`** — the same
  bug class, at the package level this time instead of just the CLI. Fixed
  to read the real installed version via `importlib.metadata`, matching
  the CLI's existing fix.

### Added

- **`@bitscrape.spider(name=..., start_urls=[...])`** — a decorator that
  turns a plain `async def parse(response): yield ...` function into a full
  `Spider` subclass. The simplest possible entry point for the common case
  (one callback, no spider-level state): no class boilerplate, no `self`.
  For anything needing multiple callbacks or `self.follow()`, a full
  `class MySpider(Spider):` is still there and unchanged.
- **The full 0.3.0–0.6.0 feature set is now importable from the top-level
  `bitscrape` package** — `build_engine`, `Frontier`, `LinkGraph`,
  `RecrawlScheduler`, `canonicalize_url`/`resolve_redirect_chain`/
  `compute_fingerprint`, `BM25Index`/`VectorIndex`/`HybridSearcher`,
  `EntityResolver`, `KnowledgeGraph`, `CrawlMetrics`/`CrawlTracer`/
  `AlertManager`, `StatsMonitor`, `PluginManager`/`BasePlugin`, and the
  storage backends. Previously these needed knowing the exact submodule
  path (`from bitscrape.link_analysis import LinkGraph`); now
  `from bitscrape import LinkGraph` works directly, matching the
  project's existing "one import" design. None of these add a hard
  dependency on optional packages (aiosqlite, boto3, networkx, etc.) at
  `import bitscrape` time — those imports stay lazy, inside the
  functions/methods that actually need them, exactly as before.

### Testing

- 11 new tests in `tests/test_package_api.py`, including the two real
  regressions found and fixed (stale version, missing meta-robots
  middleware in `run()`), and both the function-decorator and class-based
  spider paths verified end-to-end against a real local HTTP server.
- Switched these specific tests to a plain `threading` + `http.server`
  based test server instead of `aiohttp.test_utils.TestServer`, after
  discovering the real reason a first draft of these tests hung: aiohttp's
  `TestServer` is bound to whichever asyncio event loop started it, and
  stops actually processing requests once that loop closes — which is
  exactly what happens when it's accessed from `bitscrape.run()`'s own,
  separate internal `asyncio.run()` call. A plain thread-based server has
  no such event-loop coupling.
- 323 tests total, all passing. ruff clean on the whole tree; mypy clean
  aside from the long-documented pydantic-settings/mypy false positive
  (now also appearing at the two new bare `Settings()` call sites in
  `__init__.py`/`factory.py` — same non-issue, confirmed at runtime by
  every single test in this project that calls `Settings()`).

## [0.6.0] — 2026-07-24

### Architecture: a factory, not a rewrite

A proposal came in to restructure the project around five stacked patterns
at once: Clean/Hexagonal Architecture, an internal event-driven runtime, an
actor-style concurrency model, plugin-first design, and a distributed
scheduler layer. Evaluated and declined, for concrete reasons:

- An event bus adds indirection, not speed — the actual performance here
  comes from `asyncio` + semaphores + connection pooling, already in place.
- Plugin-first and distributed scheduling were already delivered in 0.4.0
  (`PluginManager`, `RedisQueue`, `RedisDupeFilter`,
  `DistributedThrottleMiddleware`) — restating them as a new architecture
  layer would be relabeling, not new capability.
- An actor runtime (mailboxes, supervision trees) solves fault-isolation
  problems at a scale this project isn't at, and would mean maintaining two
  competing concurrency models for no demonstrated benefit over the
  existing `asyncio` task model.
- Stacking five architectural patterns simultaneously is a real risk to a
  312-test, working codebase, for terminology, not a concrete gap.

**What was real in the proposal**: "the same spider runs unchanged from a
laptop to a distributed cluster." That property already existed in pieces
(`Settings` toggles selecting backends, `Scheduler.from_settings()`) but
required hand-assembling ~6 subsystems every time — exactly what the CLI
was doing ad hoc, and what every library user would otherwise have to
copy-paste. That's a real, worth-fixing gap, and it doesn't need a new
pattern to fix — it needs a factory.

### Added

- **`bitscrape.factory.build_engine()`** — the single function that builds
  a fully-wired `Engine` from one `Settings` object: proxy rotation,
  session pooling vs. cookie jar, distributed throttling, robots.txt +
  meta-robots handling, live monitoring, and Prometheus metrics are all
  selected automatically from config toggles. No new abstraction layers —
  it's a plain factory over the dependency injection the codebase already
  uses.
- **`bitscrape.factory.build_middlewares()`** — the middleware-selection
  logic, exposed standalone so it can be inspected/extended without
  reimplementing the selection rules.
- **`Engine.stats`** — a public property for live crawl stats (previously
  only accessible via the private `_stats` attribute), needed so
  `StatsMonitor` can poll a running crawl through the factory wiring.
- New `Settings` fields powering the factory: `proxies`, `proxy_rotate`,
  `monitoring_enabled`/`monitoring_port`, `metrics_enabled`/`metrics_port`.
- **The CLI now uses `build_engine()` itself**, replacing its previous
  ad-hoc middleware assembly — the CLI is now just a caller of the same
  public API a library user gets, rather than a separate, undocumented
  wiring path that could drift from it.

### Testing

- 13 new tests in `tests/test_factory.py`, including the actual claim being
  tested end-to-end: the **same spider class**, run through
  `build_engine()` twice — once with default (in-memory) `Settings`, once
  with `scheduler_use_redis=True` + `distributed_throttle_enabled=True`
  against a real local Redis — both complete a real crawl against a real
  local server with `items_scraped=1, requests_failed=0`. Also verified the
  CLI's new `build_engine()`-based path with a real end-to-end crawl.
- 312 tests total, all passing. ruff clean on the whole tree; mypy clean
  (aside from the long-documented pydantic-settings/mypy false positive in
  `engine.py`, unrelated to this change).

## [0.5.0] — 2026-07-23

Hyperscale search infrastructure: the surrounding systems a large search
engine needs beyond the crawler itself. As throughout this project, every
algorithm claimed as working has a real test behind it. The genuinely
infrastructure-dependent pieces (live Kubernetes clusters, real embedding
models, real alerting-vendor integrations) are shipped as real, valid
artifacts with honest disclosure of what's verified vs. what needs your own
environment — not faked as "tested" when they weren't.

### Added — URL Frontier

- **`Frontier`** (`bitscrape/frontier.py`) — a proper Mercator-style
  two-level frontier: N priority tiers of front queues + one per-domain
  back queue with its own next-allowed-time, so politeness (minimum
  spacing between requests to the same domain) is enforced structurally,
  not just hoped for under concurrency. 17 tests, including real-time
  politeness enforcement and 20-domain round-robin fairness. Complements
  (doesn't replace) `bitscrape.scheduler` — use `Frontier` when you need
  real per-domain politeness guarantees at scale; combine with
  `DistributedThrottleMiddleware` (0.4.0) for the same guarantee across
  worker *processes*, not just within one.

### Added — Link Analysis

- **`LinkGraph`** (`bitscrape/link_analysis.py`) — PageRank and HITS over
  the crawl's discovered hyperlink graph, built on `networkx`'s real
  algorithms (not reimplemented). 14 tests verifying correct relative
  ordering (frequently-cited pages outrank orphans, symmetric graphs score
  symmetrically, scores sum to ~1.0).

### Added — Incremental Recrawling

- **`RecrawlScheduler`** (`bitscrape/recrawl.py`) — schedules next-visit
  time from importance (e.g. a PageRank score) and an estimated
  change-frequency (Laplace-smoothed observed-change-rate, in the spirit of
  Cho & Garcia-Molina's freshness estimation), clamped to configurable
  min/max intervals. 17 tests confirming important and frequently-changing
  pages get shorter intervals than unimportant/static ones.

### Added — Canonicalization

- **`canonicalize_url()`, `resolve_redirect_chain()`,
  `compute_fingerprint()`/`is_near_duplicate()`** (`bitscrape/canonicalize.py`)
  — URL normalization (scheme/host case, default ports, tracking-param
  stripping, fragment/trailing-slash handling), redirect-loop detection,
  and SimHash-based near-duplicate content detection (survives minor edits
  like a changed footer timestamp, unlike exact-hash comparison). 29 tests.

### Added — Semantic Ranking

- **`BM25Index`, `VectorIndex`, `reciprocal_rank_fusion()`, `HybridSearcher`**
  (`bitscrape/ranking.py`) — a correct BM25 implementation, a brute-force
  cosine-similarity vector index, and Reciprocal Rank Fusion to combine
  them. 22 tests including known-correct BM25 properties (rare terms
  weighted higher, more occurrences score higher) and RRF's key property
  (consistent moderate ranking beats one great signal + one absence).
  **Scope note**: doesn't compute embeddings itself — no network access to
  download an embedding model or call an embedding API in this build
  environment. Bring your own embeddings (any model) into `VectorIndex`;
  the fusion/reranking logic is what's reusable and tested here.

### Added — Entity Resolution

- **`EntityResolver`, `similarity()`** (`bitscrape/entity_resolution.py`) —
  heuristic clustering of entity mentions ("Jon Smith" / "John Smith" /
  "J. Smith" -> one cluster) via normalized string similarity + an
  initials-abbreviation check, NOT a trained entity-linking model. 19
  tests. Pairs with `bitscrape.knowledge_graph` (0.3.0): resolve mentions
  first, then use the canonical name as the graph node identity.

### Added — Large-scale Observability

New `bitscrape/observability.py`, on real industry-standard libraries:

- **Metrics**: `CrawlMetrics` (`prometheus_client` counters/histograms) +
  `serve_metrics()` (a real `/metrics` HTTP endpoint). Verified by actually
  scraping it with a real HTTP client and checking Prometheus text format.
- **Tracing**: `CrawlTracer` (OpenTelemetry SDK spans, swappable exporter
  for Jaeger/Tempo/Honeycomb/etc.). Verified with OTel's own in-memory
  exporter — real spans, real parent/child relationships, real status
  codes, not a hand-rolled tracing stand-in.
- **Structured logging**: `JSONLogFormatter` — standard JSON-per-line shape
  for ELK/Loki/Datadog ingestion.
- **Alerting**: `AlertManager` — threshold-rule evaluation with per-rule
  cooldowns and callback dispatch. Decides *when* to alert; delivering to a
  specific vendor (PagerDuty, Slack, a webhook) is your callback's job —
  this isn't a full Alertmanager/PagerDuty client.

19 tests total for this module.

### Added — Cloud-native Infrastructure

New `deploy/` directory:

- **`Dockerfile`** — multi-stage build, non-root user, separate builder
  stage so build toolchains don't bloat the final image.
- **`docker-compose.yml`** — local multi-worker + Redis stack for dev/
  integration testing, with a documented `--scale worker=N` path.
- **`k8s/deployment.yaml`** — Deployment with resource requests/limits,
  readiness/liveness probes, and zone-spread `topologySpreadConstraints`
  (the practical starting point for multi-region resilience before you're
  running fully separate per-region clusters).
- **`k8s/hpa.yaml`** — HorizontalPodAutoscaler on CPU/memory, with
  scale-down stabilization to avoid flapping, plus a documented (commented)
  path to scale on actual crawl-queue-depth instead of just CPU.
- **`k8s/service.yaml`** — Service, ConfigMap, and PodDisruptionBudget.

**Honest disclosure**: no Docker daemon or Kubernetes cluster was available
in this build environment to `docker build` or `kubectl apply
--dry-run=server` against. What IS verified (13 tests in
`tests/test_deploy_manifests.py`): every manifest is syntactically valid
YAML, has the fields a production workload needs (resource limits, health
probes, PDB, HPA stabilization), and that label selectors actually match
across files (a very common real-world misconfiguration). Run `docker
compose config` and `kubectl apply --dry-run=server` against your own
environment before deploying.

### Testing

- **299 tests total, all passing** (149 from 0.4.0 + 150 new), across 8 new
  test files. ruff clean; mypy clean on every new file.
- **Fixed a reproducibility gap found during this release's own
  verification**: `pyproject.toml`'s `dev` extras weren't version-pinned, so
  a fresh install could grab a newer `ruff` with stricter default rules,
  producing lint findings on *pre-existing, untouched* code that the
  originally-verified version didn't flag. Pinned exact versions
  (`ruff==0.15.22`, `mypy==2.3.0`, `pytest==9.1.1`, `pytest-asyncio==1.4.0`,
  `moto[s3]==5.2.2`, `pyyaml==6.0.3`) so `pip install -e ".[dev]"` now
  reproduces the exact toolchain this release was verified against.

## [0.4.0] — 2026-07-22

A large feature release covering distributed crawling, performance, storage,
extensibility, monitoring, and knowledge-graph support. As throughout this
project, everything claimed as "working" has a real test behind it — against
a real local Redis instance, a real local HTTP server, a real on-disk
SQLite database, or a real S3 API emulation (moto), not just mocks. Where
something couldn't be verified for real in this build environment (no
network access to a package mirror for Postgres/MongoDB/Elasticsearch, no
browser binary for Playwright), that's disclosed explicitly rather than
claimed as tested.

Explicitly NOT included, on purpose: fingerprint/TLS spoofing, canvas/WebGL
evasion, or CAPTCHA-solving/anti-bot integrations. These are tooling whose
specific purpose is defeating a site's anti-bot protections against its
wishes -- a different category from the legitimate infrastructure below,
and not something this project ships regardless of packaging or framing.

### Added — Distributed crawling

- **`DistributedThrottleMiddleware`** — a Redis-backed per-domain lease so
  multiple worker *processes* sharing the same Redis don't independently
  hammer the same domain. Verified with two independent middleware
  instances (standing in for two workers) sharing a real local Redis: one
  genuinely waits out the other's lease. `Settings.distributed_throttle_enabled`.
- Confirmed and test-covered the existing `RedisDupeFilter` as the real
  distributed-dedup mechanism: 8 tests against real Redis, including two
  independent filter instances agreeing on already-seen URLs — the actual
  cross-worker guarantee "distributed crawling" needs.
  (`bitscrape/scheduler/dupefilter.py`)

### Added — Performance

- **`AutoThrottle`** — adaptive, latency-based per-domain delay (same idea
  as Scrapy's AUTOTHROTTLE). Verified live: hitting a server that's
  deliberately slow then fast, the effective delay measurably rises during
  the slow phase and falls back down during the fast phase.
  `Settings.autothrottle_enabled`, `autothrottle_start_delay`,
  `autothrottle_max_delay`, `autothrottle_target_concurrency`.
  (`bitscrape/downloader/downloader.py`)
- **`SessionPoolMiddleware`** — a pool of N independent cookie-jar sessions
  per domain (round-robin or sticky via `request.meta["session_id"]`),
  with optional auto-rotation after N requests. Supersedes the single-jar
  `CookieMiddleware` when you need several parallel sessions.
  `Settings.session_pool_size`, `session_rotate_every`.
- **`BrowserPool`** — reuses `playwright_pool_size` browser *processes*
  across requests instead of launching a fresh one every time (only a
  cheap browser *context* is created per request, keeping cookie/storage
  isolation). Verified with fake-but-API-shaped Playwright objects
  (no browser binary was available in this build environment), including a
  genuine concurrency-bounding test proving a 3rd concurrent request
  actually waits for a pool slot rather than the pool being decorative.
  `Settings.playwright_pool_enabled`. (`bitscrape/downloader/downloader.py`)

### Added — Storage backends

New `bitscrape/storage/backends.py`, a pluggable `BaseStorageBackend`
interface:

- **`SQLiteStorageBackend`** — fully tested against a real on-disk database
  (via `aiosqlite`). The reference implementation.
- **`S3StorageBackend`** — tested against a real S3 API emulation (`moto`,
  not a hand-rolled mock), including verifying the actual objects written.
  Works with any S3-compatible endpoint (AWS, MinIO, R2, etc.) via
  `endpoint_url`.
- **`PostgresStorageBackend`** — implemented against the real `asyncpg` API
  (JSONB column, connection pooling) and unit-tested with a mock connection.
  No live Postgres server was available in this build environment's package
  mirror to test against for real — treat as "implemented, needs your own
  integration test," not "verified end-to-end" like SQLite/S3.
- **`MongoStorageBackend` / `ElasticsearchStorageBackend`** — honest
  documented stubs. Each raises `NotImplementedError` with a full
  implementation sketch in its docstring (the real `motor`/
  `elasticsearch-py` calls to wire up), rather than pretending to work.
  Their engines require package repositories outside this build
  environment's allowed mirror.

### Added — Extensibility

- **Plugin/hook architecture** (`bitscrape/plugins.py`): `PluginManager`
  fires named lifecycle events (`spider_opened`, `spider_closed`,
  `request_scheduled`, `response_received`, `item_scraped`, `item_dropped`,
  `error`); `BasePlugin` for convenient subclassing. Wired into the real
  `Engine` — verified with a live end-to-end crawl against a real local
  server, confirming every hook actually fires in the right order.
  A broken plugin callback is logged and does not stop the crawl.
- Two example plugins built on this architecture: **`BearerTokenAuthPlugin`**
  (attaches an `Authorization: Bearer` header to requests for a given
  domain — a legitimate "I already have credentials for a site I'm
  authorized to access" helper) and **`StorageConnectorPlugin`** (streams
  every scraped item into any `BaseStorageBackend` as it's scraped).
  Deliberately not included: CAPTCHA-solving or anti-bot-evasion plugins.

### Added — Monitoring

New `bitscrape/monitoring.py`: `StatsMonitor` serves a live JSON feed
(`/stats.json`) and an auto-refreshing HTML view (`/`) of the current
`CrawlStats` plus real process CPU/RAM (via `psutil`). This is a small
local `aiohttp.web` server you run alongside one crawl — explicitly NOT a
hosted, multi-crawl dashboard product. Verified with a real HTTP client
against a real running server, including that stats reflect live changes
(not a frozen snapshot) and that `stop()` actually releases the port.
Wire it up via `monitor.as_plugin()` to start/stop with the spider
lifecycle, or point your own Prometheus/Grafana scraper at `/stats.json`
for real production dashboarding.

### Added — Knowledge graph support

New `bitscrape/knowledge_graph.py`, scoped honestly:

- `extract_entities()` — a heuristic capitalized-phrase extractor (the
  classic "proper noun sequence" baseline). Documented as a rough signal,
  not real NER — it will over- and under-match; pair it with a real NER
  model/LLM for production entity extraction.
- `KnowledgeGraph` — a real, working directed graph (built on `networkx`)
  recording (subject, predicate, object) triples, either reliably from
  structured item fields (`add_item()`) or roughly from free text
  (`add_entities_from_text()`, using the heuristic above). Exports to JSON
  or GraphML (openable in Gephi/Cytoscape for real graph analysis/viz,
  rather than reinventing that here).

### Testing

- **149 tests total, all passing** (52 from 0.3.0 + 97 new), across 8 new
  test files. Notably includes: real local Redis integration (dupefilter +
  distributed throttle), a real local HTTP server proving AutoThrottle
  backs off and recovers, real moto-emulated S3, real on-disk SQLite with
  cross-process persistence, and a full engine-level integration test
  proving plugin hooks fire during an actual crawl.
- ruff clean; mypy clean on every new/modified file (aside from the
  pre-existing pydantic-settings/mypy false-positives already documented
  as unrelated in earlier releases).

## [0.3.0] — 2026-07-22

Added the legitimate crawler-etiquette features that production search/AI
crawlers (Googlebot, ClaudeBot, PerplexityBot) implement beyond basic
robots.txt — as opposed to bot-detection evasion (CAPTCHA solving, TLS/JA3
fingerprint spoofing), which is deliberately out of scope: that's tooling
built to defeat a site's anti-bot protections against its wishes, which sits
in legally gray territory regardless of framing, and isn't something this
patch will implement.

### Added

- **Conditional GET (ETag / Last-Modified).** The downloader now caches
  `ETag`/`Last-Modified` from 200 responses and sends `If-None-Match` /
  `If-Modified-Since` on repeat crawls of the same URL. A `304 Not Modified`
  is served transparently from cache (marked with
  `response.headers["X-Bitscrape-Not-Modified"] = "true"`), so the origin
  server only has to send headers, not the full page again — the same
  courtesy production crawlers extend to sites they crawl repeatedly.
  Controlled by `Settings.conditional_get_enabled` (default `True`).
  (`bitscrape/downloader/downloader.py`)
- **Retry-After-aware backoff.** On a `429`/`503` (or any status in
  `retry_http_codes`), the downloader now reads the server's own
  `Retry-After` header (both delay-seconds and HTTP-date forms, per RFC
  9110) and waits that long instead of blind exponential backoff — capped by
  `Settings.max_retry_after_seconds` (default 120s) so a misbehaving server
  can't stall a crawl indefinitely. Controlled by
  `Settings.respect_retry_after` (default `True`).
  (`bitscrape/downloader/downloader.py`)
- **Meta-robots directives (`noindex`/`nofollow`).** New
  `MetaRobotsMiddleware` reads the `X-Robots-Tag` HTTP header and
  `<meta name="robots" content="...">` (plus a UA-scoped variant like
  `<meta name="googlebot" content="noindex">`) and sets
  `request.meta["noindex"]` / `request.meta["nofollow"]`. The Engine now
  honours these: `noindex` skips counting/exporting items from that page
  (tracked in new `stats.items_noindexed`); `nofollow` skips enqueueing any
  links discovered on that page (tracked in new
  `stats.links_nofollow_skipped`). Fetching itself is never blocked by
  these — only indexing/following — matching real-world semantics.
  Controlled by `Settings.respect_meta_robots` (default `True`). Wired into
  the CLI's default middleware stack. (`bitscrape/middleware/middleware.py`,
  `bitscrape/engine.py`, `bitscrape/cli/main.py`)
- New `CrawlStats.items_noindexed` / `CrawlStats.links_nofollow_skipped`
  fields, surfaced in the CLI's Crawl Stats table.

### Testing

- 52 tests total, all passing (33 from 0.2.1 + 19 new across
  `tests/test_downloader_features.py` and `tests/test_meta_robots.py`).
- Verified end-to-end against a real local server with a real spider: a
  page with `<meta name="robots" content="noindex, nofollow">` produced
  `items_noindexed=1`, `links_nofollow_skipped=1`, `items_scraped=0` —
  and, critically, did **not** trigger the 0.2.1 "0 items, check your
  selectors" diagnostic, since this is an intentional skip, not a broken
  selector.

### Explicitly not implemented (and why)

- CAPTCHA solving, TLS/JA3 fingerprint spoofing, mouse/keystroke simulation,
  or any other bot-detection evasion. Real search/AI crawlers don't need
  these because sites generally welcome them; building tooling whose
  purpose is defeating a site's anti-bot measures is a different thing
  entirely and isn't something this project will add regardless of how
  the request is framed.

## [0.2.1] — 2026-07-22

### Investigated: "Requests: 1, Items scraped: 0" reports

A user reported a crawl completing with `requests=1 failed=0 items=0` (large
download, tens of seconds elapsed). Investigated end-to-end against a real
local server with a real spider:

- **The core crawl → download → parse → item pipeline works correctly.**
  Verified live: a spider with matching selectors against a test page
  produced `requests=1 failed=0 items=2` and wrote both items to the output
  file, in 0.06s. This isn't a regression from the 0.2.0 fixes.
- **Root cause of the reported pattern: CSS/XPath selectors don't match the
  target page.** Reproduced the *exact* `requests=1 failed=0 items=0`
  signature by pointing a spider with mismatched selectors (e.g. the
  placeholder `div.listing-card` from `examples/infinite_scroll_spider.py`)
  at a real page. This is the single most common cause of "0 items" in any
  scraping framework — the download succeeds, the parse callback runs
  without error, it just finds nothing to select.
- The example spiders in `examples/` use illustrative selectors
  (`div.listing-card`, `h2::text`, etc.) that are **not tied to any real
  site** — they're templates to adapt, not working spiders for
  `example.com`. Pointing them at a real URL as-is will always yield 0
  items regardless of framework correctness.

### Added

- **Zero-yield diagnostic in the engine.** Previously, a callback that ran
  successfully but yielded no items and no follow-up requests produced no
  signal at all beyond the final `items: 0` in the stats table — no
  warning, no hint of why. The engine now logs an explicit warning
  identifying the URL, the callback name, and the response size, with a
  pointer to likely causes (selector mismatch, JS-rendered content needing
  `use_playwright=True`, or an unexpected redirect/robots.txt page).
  Spiders that only yield follow-up `Request`s (e.g. sitemap crawlers) are
  correctly not flagged. (`bitscrape/engine.py`)
- New README "Troubleshooting: 0 items scraped" section (see below).
- `tests/test_engine_diagnostics.py` — 3 new tests covering the warning
  firing, not firing on a normal yield, and not firing on request-only
  yields.

### Testing

- 33 tests total, all passing (30 from 0.2.0 + 3 new).

## [0.2.0] — 2026-07-22

### Fixed

- **Critical: `RobotsMiddleware` was a silent no-op.** The code called
  `parser.feed(text)`, but `urllib.robotparser.RobotFileParser` has no
  `feed()` method — only `.parse(lines)`. Every fetch raised `AttributeError`,
  silently swallowed by a broad `except Exception`, so `robotstxt_obey=True`
  never actually blocked a single request on any site, regardless of the
  site's rules. Fixed by calling `.parse()` correctly.
  (`bitscrape/middleware/middleware.py`)
- **`MemoryQueue` crashed on concurrent same-priority requests.**
  `asyncio.PriorityQueue` fell through to comparing `Request` objects
  directly whenever two requests shared a priority tier, and `Request`
  defines no `__lt__`, raising
  `TypeError: '<' not supported between instances of 'Request' and 'Request'`.
  This broke the most common crawl pattern (homepage → many same-priority
  links). Fixed with a monotonic tiebreaker counter:
  `(priority, counter, request)`. (`bitscrape/scheduler/scheduler.py`)
- **robots.txt fetch failures now fail safe, not open.** A 4xx (including
  404) is still treated as "no robots.txt published" → unrestricted access.
  Any other failure (timeout, DNS error, 5xx) now blocks requests to that
  domain until a fetch succeeds, instead of silently disabling enforcement.

- **`bitscrape --version` reported a stale hardcoded string.** It printed
  `"0.1.0"` unconditionally regardless of what was actually installed (e.g.
  still showing `0.1.0` after installing this `0.2.0` fork). Fixed to read
  the real installed distribution version via `importlib.metadata`.
  (`bitscrape/cli/main.py`)

### Added

- `Crawl-delay` and `Sitemap` directives are now surfaced from robots.txt via
  `RobotFileParser`'s own native `.crawl_delay()` / `.site_maps()` methods,
  populated into `request.meta["crawl_delay"]` / `request.meta["sitemaps"]`.
- `ProxyMiddleware` — per-request proxy rotation (round-robin or random),
  mirroring `UserAgentMiddleware`'s interface. Wired into both the `aiohttp`
  and Playwright fetch paths via `request.meta["proxy"]`.
- `scroll_to_bottom()` helper in `bitscrape/downloader/downloader.py` — a
  reusable infinite-scroll / "load more" driver for the Playwright path, with
  optional `click_selector` support for button-triggered pagination. Enabled
  per-request via `request.meta["scroll"] = True` (or a dict of overrides).
- `Settings.queue_max_size` — optional backpressure cap for `MemoryQueue`
  (default `0` = unbounded, unchanged behaviour).
- `ProxyMiddleware.add_proxy()` / `.remove_proxy()` for runtime pool
  management (e.g. dropping a proxy after repeated failures).

### Testing

- 30 new tests added across 5 files, all passing:
  `tests/test_scheduler_queue.py`, `tests/test_robots_middleware.py`,
  `tests/test_proxy_middleware.py`, `tests/test_scroll_to_bottom.py`,
  `tests/test_cli_version.py`.

### Not included (out of scope for this release)

- CAPTCHA-solving / bot-detection evasion, TLS/JA3 fingerprint spoofing,
  mouse/keystroke simulation — deliberately excluded; building tooling
  explicitly meant to defeat a site's anti-bot protections is legally
  gray depending on the target site and jurisdiction.
- Distributed lock/coordination for multi-worker `RedisQueue` deployments —
  a real gap, but a bigger design change (lease/heartbeat protocol) than a
  patch release should take on.
- Credential vault / OAuth / CSRF flows for authenticated crawling —
  application-specific and out of scope for a general-purpose fix.

## [0.1.6] — prior release

Baseline this fork is patched against. See upstream
[README](https://github.com/Sudharsansm/Bitscrape) for original feature set.
