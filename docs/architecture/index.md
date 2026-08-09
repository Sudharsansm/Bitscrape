# Architecture

This mirrors the root [`ARCHITECTURE.md`](../../ARCHITECTURE.md) with more
narrative detail. See that file for the condensed version.

## The high-level shape

```
                          BITSCRAPE 0.8.0
                    Web Crawling Framework
                              │
          ┌───────────────────┴───────────────────┐
          │                                       │
     Developer API                              CLI
          │                                       │
          └───────────────────┬───────────────────┘
                              ▼
                       ┌─────────────┐
                       │   Factory   │
                       │build_engine │
                       └──────┬──────┘
                              ▼
┌────────────────────────────────────────────────────────────────────┐
│                         CORE ENGINE                                │
│                                                                    │
│                         ┌─────────┐                                │
│                         │ Engine  │                                │
│                         └────┬────┘                                │
│                              │                                     │
│       ┌──────────────────────┼───────────────────────┐             │
│       │                      │                       │             │
│       ▼                      ▼                       ▼             │
│  ┌──────────┐         ┌─────────────┐        ┌────────────┐        │
│  │ Scheduler│         │ Middleware  │        │ Downloader │        │
│  └────┬─────┘         └──────┬──────┘        └─────┬──────┘        │
│       │                      │                     │               │
│       │                UA / Proxy /               │                │
│       │                Session / Robots /          │               │
│       │                Throttle / MetaRobots       │               │
│       │                      │                     │               │
│       │                      └──────────┬──────────┘               │
│       │                                 ▼                          │
│       │                          HTTP / Playwright                 │
│       │                                 │                          │
│       └─────────────────────────────────┤                          │
│                                         ▼                          │
│                                ┌─────────────────┐                 │
│                                │ Spider Callback │                 │
│                                │     parse()     │                 │
│                                └───────┬─────────┘                 │
│                                        │                           │
│                              ┌─────────┴─────────┐                 │
│                              ▼                   ▼                 │
│                           Items              Requests              │
│                              │                   │                 │
│                              ▼                   │                 │
│                       ┌────────────┐             │                 │
│                       │ Pipelines  │             │                 │
│                       └─────┬──────┘             │                 │
│                             ▼                    │                 │
│                        ┌─────────┐               │                 │
│                        │Exporter │               │                 │
│                        └─────────┘               │                 │
│                                                  │                 │
│                                                  └──► Scheduler    │
└────────────────────────────────────────────────────────────────────┘
                              │
                              ▼
                 ┌──────────────────────────┐
                 │   Swappable Adapters     │
                 │                          │
                 │ Queue / Dupe / Storage   │
                 │ Exporter / Plugins       │
                 └────────────┬─────────────┘
                              │
             ┌────────────────┼─────────────────┐
             ▼                ▼                 ▼
          Memory            Redis           Storage
                              │           SQLite/Postgres
                              │           MongoDB/S3
                              │           Elasticsearch*
                              │
                              ▼
                    Distributed Crawling
                              │
             ┌────────────────┼────────────────┐
             ▼                ▼                ▼
         Shared Queue    Shared Dedup    Distributed
                                          Throttle


              ┌────────────────────────────────────┐
              │      WEB INTELLIGENCE LAYER        │
              │        Separate from Core          │
              └─────────────────┬──────────────────┘
                                │
         ┌──────────────────────┼─────────────────────────┐
         ▼                      ▼                         ▼
    Link Analysis            Recrawl                   Ranking
         │                      │                         │
    PageRank/HITS        Change Frequency       BM25 / Vector / RRF
         │                      │                         │
         └──────────────────────┼─────────────────────────┘
                                ▼
                        Knowledge Graph
                                │
                         Entity Resolution


              ┌────────────────────────────────────┐
              │       OBSERVABILITY LAYER           │
              └─────────────────┬──────────────────┘
                                │
             ┌──────────────────┼──────────────────┐
             ▼                  ▼                  ▼
        Prometheus         OpenTelemetry       StatsMonitor
          Metrics             Tracing          JSON / HTML
             │                  │
             └──────────────────┼──────────────────┘
                                ▼
                           AlertManager


              ┌────────────────────────────────────┐
              │        EXTENSIBILITY LAYER          │
              └─────────────────┬──────────────────┘
                                ▼
                         PluginManager
                                │
        ┌───────────────────────┼─────────────────────┐
        ▼                       ▼                     ▼
   Lifecycle Hooks       Custom Components      Integrations


              ┌────────────────────────────────────┐
              │         DEPLOYMENT LAYER             │
              └─────────────────┬──────────────────┘
                                │
                    ┌───────────┴───────────┐
                    ▼                       ▼
                 Docker                  Kubernetes
                    │                       │
                    └───────────┬───────────┘
                                ▼
                              Redis
                           + Workers
```

The whole thing runs inside one `asyncio` event loop per process. There is
no actor system, no message bus, and no custom concurrency runtime — just
`asyncio` tasks bounded by semaphores (`concurrent_requests`,
`concurrent_requests_per_domain`). See
[#why-not-an-actor-system-or-event-bus](#why-not-an-actor-system-or-event-bus)
below for why that's a deliberate choice, not an oversight.

## The Engine

`bitscrape.engine.Engine` is the central coordinator. Per its own
docstring, for each crawl run it:

1. Opens all components (downloader, scheduler, pipelines, exporter).
2. Seeds the scheduler with `spider.start_requests()`.
3. Runs an asyncio loop: pop request → middleware → download → parse → pipeline.
4. Tracks and logs stats.
5. Gracefully closes everything when the queue empties (or on
   `asyncio.CancelledError`).

It exposes a public `.stats` property (a live `CrawlStats`, safe to read
while a crawl is in progress — e.g. from `StatsMonitor` polling it on a
timer) in addition to the `CrawlStats` returned by `.run()` once the crawl
finishes.

### The zero-yield diagnostic

If a spider callback runs without error but yields **zero items and zero
follow-up requests** from a non-empty response, the Engine logs an
explicit warning identifying the URL, callback name, and response size,
with likely causes (selector mismatch, JS-rendered content, an unexpected
redirect). This exists because "0 items scraped, 0 errors" used to be a
silent dead end for users — see
[troubleshooting/](../troubleshooting/index.md). This diagnostic correctly
does **not** fire when items/requests were intentionally skipped by
`noindex`/`nofollow` (see below) — that's tracked separately in
`stats.items_noindexed` / `stats.links_nofollow_skipped`.

## The factory: `build_engine()`

`bitscrape.factory.build_engine(spider, settings, exporter=None,
redis_client=None, plugin_manager=None) -> Engine` is the single function
that turns a `Settings` object into a fully-wired `Engine`. Both the CLI's
`crawl` command and the top-level `bitscrape.run()` call this same
function — there is exactly one code path that decides how `Settings`
becomes a running crawl, not two that could silently drift apart (a real
bug found and fixed in this project's history: `bitscrape.run()` used to
have its own separate, simplified wiring that was missing
`MetaRobotsMiddleware` entirely — see `CHANGELOG.md` 0.7.0).

### Middleware selection

`build_middlewares(settings, redis_client=None)` assembles the stack in
this fixed order:

1. `UserAgentMiddleware` — always included.
2. `ProxyMiddleware` — only if `settings.proxies` is non-empty.
3. `SessionPoolMiddleware` (if `session_pool_size > 1`) **or**
   `CookieMiddleware` (otherwise) — never both.
4. `DistributedThrottleMiddleware` — only if
   `distributed_throttle_enabled=True` **and** a Redis client is available
   (either passed explicitly, or created automatically because
   `scheduler_use_redis` or `distributed_throttle_enabled` is set). If
   enabled but no Redis client can be obtained, this is skipped with a
   logged warning rather than failing the whole build.
5. `MetaRobotsMiddleware` — always included.
6. `RobotsMiddleware` — only if `robotstxt_obey=True`.

### Optional plugin wiring

`build_engine()` also registers plugins automatically based on `Settings`:

- `monitoring_enabled=True` → registers a `StatsMonitor` (see
  [monitoring/](../monitoring/index.md)) whose HTTP server starts/stops
  with the spider lifecycle.
- `metrics_enabled=True` → registers an internal plugin bridging Engine
  lifecycle events to real `CrawlMetrics` (Prometheus), also served on its
  own HTTP endpoint tied to the spider lifecycle.

### Why a factory, not a bigger rewrite

An architecture proposal once suggested restructuring this project around
five patterns simultaneously: Clean/Hexagonal Architecture, an internal
event-driven runtime, an actor-style concurrency model, plugin-first
design, and a distributed scheduler layer. That was evaluated and
declined — see [#why-not-an-actor-system-or-event-bus](#why-not-an-actor-system-or-event-bus)
and [#on-hexagonal-architecture](#on-hexagonal-architecture) below for the
specific reasoning per pattern. What was real in that proposal — "the same
spider runs unchanged from a laptop to a distributed cluster" — is what
`build_engine()` actually delivers, without adding new abstraction layers.

## Distributed crawling

Three independent mechanisms combine to make multi-worker-process crawling
safe, and each is opt-in via `Settings`:

1. **Shared queue** (`scheduler_use_redis=True`): `Scheduler.from_settings()`
   builds a `RedisQueue` instead of `MemoryQueue`, so all worker processes
   pull `Request`s from the same priority queue in Redis.
2. **Shared dedup** (`dupefilter_enabled=True`, automatic when
   `scheduler_use_redis=True`): `RedisDupeFilter` uses a Redis `SADD`
   (atomic) to guarantee two workers never both claim the same URL —
   verified with two independent filter instances agreeing on
   already-seen state.
3. **Distributed politeness** (`distributed_throttle_enabled=True`):
   `DistributedThrottleMiddleware` uses a Redis-backed lease
   (`SET key val NX PX <delay_ms>`) so requests to any one domain are
   spaced out **across the whole cluster**, not just within one process —
   verified with two independent middleware instances, one genuinely
   waiting out the other's lease.

Within a single process, `bitscrape.frontier.Frontier` offers an
alternative, structurally-enforced politeness guarantee (Mercator-style
priority tiers + one per-domain back-queue with its own next-allowed-time)
— see [scheduler/](../scheduler/index.md).

## The Downloader

`bitscrape.downloader.downloader.Downloader` handles two fetch paths:

- **`aiohttp`** (default): connection-pooled, with per-domain semaphores
  (`concurrent_requests_per_domain`), conditional GET
  (`If-None-Match`/`If-Modified-Since`, serving cached bodies on `304`),
  Retry-After-aware backoff, and (if `autothrottle_enabled`) an
  `AutoThrottle` instance adjusting the effective per-domain delay from
  observed response latency.
- **Playwright** (`request.use_playwright=True` or
  `use_playwright=True` passed to `self.follow()`): launches a real
  browser. If `playwright_pool_enabled=True`, a `BrowserPool` reuses
  `playwright_pool_size` browser **processes** across requests (only a
  cheap browser *context* — cookies/storage isolation — is created per
  request), instead of launching/tearing down a full browser every time.

See [browser/](../browser/index.md) and [crawling/](../crawling/index.md).

## Plugins

`bitscrape.plugins.PluginManager` fires named lifecycle events
(`spider_opened`, `spider_closed`, `request_scheduled`, `response_received`,
`item_scraped`, `item_dropped`, `error`) to registered callbacks. A broken
callback is logged and does not stop the crawl. `BasePlugin` is a
convenience base class for subclassing only the hooks you need. See
[plugins/](../plugins/index.md).

## Why not an actor system or event bus?

This was evaluated directly (see `CHANGELOG.md` 0.6.0) and declined, for
concrete reasons rather than a style preference:

- **An event bus adds indirection, not speed.** The real performance in
  this project comes from `asyncio` + semaphores + connection pooling,
  already in place. Publish/dispatch/subscriber-lookup overhead doesn't
  make a crawl faster.
- **An actor runtime (mailboxes, supervision trees) solves fault-isolation
  problems at a scale this project isn't operating at.** Maintaining a
  bespoke actor runtime alongside `asyncio` tasks would mean two competing
  concurrency models for no demonstrated benefit over
  `Semaphore`-bounded tasks.
- **Plugin-first and distributed scheduling were already delivered**
  (`PluginManager`, `RedisQueue`, `RedisDupeFilter`,
  `DistributedThrottleMiddleware`) before this proposal came in — restating
  them as a new architectural layer would be relabeling, not new
  capability.
- **Stacking five patterns simultaneously is a real risk to a large,
  working, tested codebase** for terminology's sake, not a demonstrated
  gap.

## On Hexagonal Architecture

The project already has the useful parts of "ports and adapters" where a
real seam exists — `BaseQueue`/`MemoryQueue`/`RedisQueue`,
`BaseDupeFilter`/`MemoryDupeFilter`/`RedisDupeFilter`,
`BaseStorageBackend`/`SQLiteStorageBackend`/`S3StorageBackend`/etc.,
`BaseExporter`/`JSONLExporter`/etc. — each is an abstract base with
swappable implementations selected by `Settings`. Formalizing this further
into an explicit "ports" module/ceremony wasn't judged worth the added
indirection when the existing ABC-per-seam pattern already gives the same
practical swappability.

## Related pages

- [crawling/](../crawling/index.md) — the request/response lifecycle in detail.
- [scheduler/](../scheduler/index.md) — queues, dedup, and the Frontier.
- [browser/](../browser/index.md) — Playwright and BrowserPool.
- [plugins/](../plugins/index.md) — the hook system.
- [api/](../api/index.md) — flat reference for every class mentioned here.
