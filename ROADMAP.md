# Roadmap

## Planned / candidate work

- **Live-verify the Kubernetes/Docker artifacts** against a real cluster
  and Docker daemon. As of 0.7.0 they're validated for syntax/structure
  only (13 automated tests) -- no Docker daemon or Kubernetes cluster was
  available in this project's build environment. See
  `docs/deployment/index.md`.
- **DONE (0.8.0)**: `PostgresStorageBackend` is now live-verified against
  a real PostgreSQL server; `MongoStorageBackend` is now a real
  implementation tested against a genuine emulator (`mongomock_motor`).
- **Implement `ElasticsearchStorageBackend`** for real, against a live
  instance. No comparable, easily-installable ES emulator existed to test
  a real implementation against in this build environment. Currently an
  honest `NotImplementedError` stub with the intended shape documented in
  the class's docstring.
- **Wire `Frontier` into `build_engine()`/`Engine`** as an alternative
  scheduler backend, instead of remaining a standalone building block
  users have to wire in manually.
- **DONE (0.8.0)**: `self.follow()` now resolves relative URLs
  automatically via a concurrency-safe `contextvars.ContextVar` tracking
  the response currently being parsed.
- **A Prometheus-adapter-based HPA example**, live-tested against a real
  cluster scaling on an actual custom metric
  (`bitscrape_frontier_queue_depth` or similar), rather than the current
  commented-out sketch in `deploy/k8s/hpa.yaml`.
- **A generic pagination/"next page" link-matcher**, so common pagination
  patterns don't need hand-written per-spider logic.
- **Distributed lock/coordination beyond the current lease-based
  throttle** (`DistributedThrottleMiddleware`), for workloads needing
  strict one-worker-at-a-time access to a resource rather than just
  spaced-out access.
- **Multi-region deployment guidance** beyond single-cluster zone
  spreading -- currently `deployment.yaml`'s
  `topologySpreadConstraints` spreads replicas across zones within one
  cluster; true multi-cluster, multi-region orchestration (cross-region
  Redis, geo-routed traffic) is left to the deployer's own infrastructure
  decisions.

## Explicit non-goals

These have been proposed and evaluated more than once, and declined for
concrete, stated reasons rather than a blanket policy:

- **CAPTCHA-solving integrations.** Tooling whose specific purpose is
  defeating a site's anti-bot protections against its wishes.
- **Bot-detection evasion**: browser/TLS fingerprint spoofing, canvas/WebGL
  spoofing, mouse/keystroke simulation designed to mimic human behavior.
  Same reasoning as above.
- **An "anti-bot" plugin category** of any kind, regardless of framing
  (defensive testing, simulation, incremental requests that add up to the
  same capability).
- **An actor-based concurrency runtime** and **an internal event bus.**
  Proposed as part of a broader 5-pattern architecture rewrite (alongside
  Clean/Hexagonal Architecture, plugin-first design, and a distributed
  scheduler layer) and declined:
  - An event bus adds indirection, not speed -- real performance here comes
    from `asyncio` + semaphores + connection pooling, already in place.
  - An actor runtime solves fault-isolation problems at a scale this
    project isn't operating at, and would mean maintaining two competing
    concurrency models for no demonstrated benefit.
  - Plugin-first and distributed scheduling were already delivered
    (`PluginManager`, `RedisQueue`, `RedisDupeFilter`,
    `DistributedThrottleMiddleware`) before the proposal came in.
  - What was real in the proposal -- "the same spider runs unchanged from a
    laptop to a distributed cluster" -- was delivered via `build_engine()`,
    a plain factory function, without adding these abstraction layers. See
    `CHANGELOG.md` 0.6.0 and `docs/architecture/index.md`.

## How to propose something

Open an issue or discussion describing the concrete gap or use case you're
hitting, not just a pattern/technology name -- see [`CONTRIBUTING.md`](CONTRIBUTING.md).
Proposals that map to a specific, demonstrated problem get evaluated on
their merits; proposals framed only in terms of adopting a named
architecture pattern or technology get asked "what problem does this solve
that isn't already solved" first.
