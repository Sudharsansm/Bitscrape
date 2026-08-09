# Architecture

Condensed system design reference. See `docs/architecture/index.md` for a
version with more narrative detail and cross-links.

## Shape of a crawl

```
 Spider.start_requests()
          |
          v
     [ Scheduler ]  MemoryQueue/RedisQueue + DupeFilter (or Frontier, standalone)
          |  pop request
          v
     [ MiddlewareManager ]  process_request
          |   UserAgent -> Proxy? -> Session/Cookie -> DistributedThrottle? -> MetaRobots -> Robots?
          v
     [ Downloader ]  aiohttp, or Playwright via BrowserPool if use_playwright=True
          |
          v
     [ MiddlewareManager ]  process_response  (reverse order)
          |
          v
     [ Spider callback ]  parse(response) -> items and/or more Requests
          |                                        |
          v                                        v
     [ PipelineManager ]                  back to Scheduler
          |
          v
     [ Exporter ] + [ PluginManager hooks ]
          |
          v
     stats.items_scraped++
```

One `asyncio` event loop per process. No actor system, no message bus --
just `asyncio` tasks bounded by semaphores. See "Why not an actor system or
event bus?" below.

## Core components

- **`Engine`** (`bitscrape.engine`) -- the central coordinator. Opens all
  components, seeds the scheduler, runs the fetch/parse/pipeline loop,
  tracks `CrawlStats` (exposed live via `.stats`, safe to read mid-crawl),
  and closes everything gracefully. Logs an explicit diagnostic if a
  callback yields zero items and zero requests from a non-empty response
  (distinct from an intentional `noindex`/`nofollow` skip, which doesn't
  trigger it).
- **`build_engine()`** (`bitscrape.factory`) -- the single factory that
  turns a `Settings` object into a fully-wired `Engine`. Both the CLI's
  `crawl` command and the top-level `bitscrape.run()` call this same
  function, so there's exactly one code path deciding how `Settings`
  becomes a running crawl.
- **`Downloader`** (`bitscrape.downloader.downloader`) -- `aiohttp` fetching
  with per-domain semaphores, conditional GET, Retry-After-aware backoff,
  and optional `AutoThrottle`; or the Playwright path (optionally pooled
  via `BrowserPool`) for JS rendering.
- **`Scheduler`** (`bitscrape.scheduler.scheduler`) -- `MemoryQueue`/
  `RedisQueue` + `MemoryDupeFilter`/`RedisDupeFilter`, selected by
  `Settings.scheduler_use_redis`.
- **`Frontier`** (`bitscrape.frontier`) -- a separate, Mercator-style
  priority-tiered + per-domain-politeness frontier, available as a
  standalone building block (not currently wired into `build_engine()`
  automatically).
- **Middleware** (`bitscrape.middleware.middleware`) -- `UserAgentMiddleware`,
  `RobotsMiddleware`, `MetaRobotsMiddleware`, `CookieMiddleware`/
  `SessionPoolMiddleware`, `ProxyMiddleware`, `DistributedThrottleMiddleware`.
  Assembled automatically by `build_middlewares(settings)`.
- **`PluginManager`** (`bitscrape.plugins`) -- fires lifecycle events
  (`spider_opened`, `spider_closed`, `request_scheduled`,
  `response_received`, `item_scraped`, `item_dropped`, `error`) to
  registered callbacks. A broken callback is logged, not fatal.

## Middleware selection logic (`build_middlewares`)

Fixed order:
1. `UserAgentMiddleware` -- always.
2. `ProxyMiddleware` -- if `settings.proxies` non-empty.
3. `SessionPoolMiddleware` (if `session_pool_size > 1`) or `CookieMiddleware`
   (otherwise) -- never both.
4. `DistributedThrottleMiddleware` -- if enabled and a Redis client is
   available (auto-created if `scheduler_use_redis` or
   `distributed_throttle_enabled` is set); otherwise skipped with a logged
   warning, not a hard failure.
5. `MetaRobotsMiddleware` -- always.
6. `RobotsMiddleware` -- if `robotstxt_obey=True`.

## Distributed crawling

Three independent, opt-in mechanisms:
1. **Shared queue** (`scheduler_use_redis=True`) -- `RedisQueue`.
2. **Shared dedup** (`dupefilter_enabled`, default on) -- `RedisDupeFilter`,
   an atomic Redis `SADD`; verified with two independent filter instances
   agreeing on already-seen state.
3. **Shared politeness** (`distributed_throttle_enabled=True`) --
   `DistributedThrottleMiddleware`, a Redis-backed lease
   (`SET key val NX PX <delay_ms>`); verified with two independent
   middleware instances, one genuinely waiting out the other's lease.

## Why a factory, not a bigger rewrite

An architecture proposal suggested restructuring around five patterns at
once: Clean/Hexagonal Architecture, an internal event-driven runtime, an
actor-style concurrency model, plugin-first design, and a distributed
scheduler layer. Evaluated and declined:

- **Event bus**: adds indirection, not speed. Real performance comes from
  `asyncio` + semaphores + connection pooling, already present.
- **Actor runtime**: solves fault-isolation problems at a scale this
  project isn't at; would mean maintaining two competing concurrency
  models for no demonstrated benefit over `Semaphore`-bounded `asyncio`
  tasks.
- **Plugin-first / distributed scheduling**: already delivered
  (`PluginManager`, `RedisQueue`, `RedisDupeFilter`,
  `DistributedThrottleMiddleware`) before the proposal -- restating them as
  a new layer would be relabeling, not new capability.
- **Stacking five patterns simultaneously**: real risk to a large, working,
  tested codebase, for terminology rather than a demonstrated gap.

What was real in the proposal -- "the same spider runs unchanged from a
laptop to a distributed cluster" -- is delivered by `build_engine()`
without adding these layers. Verified end-to-end: the identical spider
class run once with default (in-memory) `Settings` and once with
`scheduler_use_redis=True` + `distributed_throttle_enabled=True` against a
real local Redis, both completing a real crawl against a real server.

## Existing hexagonal-style seams

Rather than formalizing a new "ports and adapters" module, the project
already has ABC-per-seam swappability where a real seam exists:
`BaseQueue`/`MemoryQueue`/`RedisQueue`,
`BaseDupeFilter`/`MemoryDupeFilter`/`RedisDupeFilter`,
`BaseStorageBackend`/`SQLiteStorageBackend`/`S3StorageBackend`/etc.,
`BaseExporter`/`JSONLExporter`/etc. Each is selected by `Settings` or
passed explicitly, giving the same practical swappability without added
ceremony.

## Search infrastructure (separate concern from crawling)

- **`LinkGraph`** (`bitscrape.link_analysis`) -- PageRank/HITS over the
  discovered hyperlink graph (via `networkx`).
- **`RecrawlScheduler`** (`bitscrape.recrawl`) -- importance + estimated
  change-frequency based revisit scheduling.
- **`BM25Index` / `VectorIndex` / `HybridSearcher`** (`bitscrape.ranking`)
  -- lexical + vector search with Reciprocal Rank Fusion. Embeddings are
  supplied by the caller, not computed by this module.
- **`KnowledgeGraph`** (`bitscrape.knowledge_graph`) -- subject-predicate-
  object triples, from structured item fields (reliable) or heuristic
  text extraction (rough).
- **`EntityResolver`** (`bitscrape.entity_resolution`) -- heuristic mention
  clustering, feeding canonical names into the knowledge graph.

## Observability (separate concern from crawling)

- **`CrawlMetrics`** (`bitscrape.observability`) -- real
  `prometheus_client` counters/histogram, served on a real `/metrics`
  endpoint.
- **`CrawlTracer`** (`bitscrape.observability`) -- real OpenTelemetry SDK
  spans, swappable exporter.
- **`StatsMonitor`** (`bitscrape.monitoring`) -- a small local live
  JSON/HTML stats feed, not a hosted dashboard product.
- **`AlertManager`** (`bitscrape.observability`) -- threshold-rule
  evaluation with cooldowns; delivery to a specific vendor is the
  registered callback's job.

## Deployment (separate concern from crawling)

`deploy/Dockerfile` (multi-stage, non-root runtime user),
`deploy/docker-compose.yml` (local multi-worker + Redis stack), and
`deploy/k8s/*.yaml` (Deployment with zone-spread + probes, HPA with
scale-down stabilization, Service/ConfigMap/PodDisruptionBudget) --
syntax/structure-validated by 13 automated tests, not live-cluster-tested
(no Docker daemon or Kubernetes cluster was available in this project's
build environment).

## See also

`docs/architecture/index.md`, `docs/api/index.md`, and the per-topic pages
under `docs/` for detail on any component above.
