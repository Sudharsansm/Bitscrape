# Roadmap

This mirrors the root [`ROADMAP.md`](../../ROADMAP.md). See that file for the
canonical version.

## Planned / candidate work

- **Live-verify the Kubernetes/Docker artifacts** against a real cluster
  and Docker daemon (currently syntax/structure-validated only -- see
  [deployment/](../deployment/index.md)).
- **Live-verify `PostgresStorageBackend`** against a real PostgreSQL server
  (currently implemented against the real `asyncpg` API, mock-tested only).
- **Implement `MongoStorageBackend` and `ElasticsearchStorageBackend`**
  for real, against live instances, following the same
  implement-and-test-for-real standard as `SQLiteStorageBackend`/
  `S3StorageBackend`.
- **Wire `Frontier` into `build_engine()`/`Engine`** as an alternative
  scheduler backend, instead of remaining a standalone building block.
- **DONE (0.8.0)**: same as above -- `self.follow()` resolves relative
  URLs automatically now.
- **A Prometheus-adapter-based HPA example**, live-tested against a real
  cluster with a real custom metric (`bitscrape_frontier_queue_depth` or
  similar), rather than the current commented-out sketch in
  `deploy/k8s/hpa.yaml`.
- **A generic pagination/"next page" link-matcher**, so common pagination
  patterns don't need to be hand-written per spider.
- **Distributed lock/coordination beyond the current lease-based
  throttle**, for workloads that need strict one-worker-at-a-time access
  to a resource, not just spaced-out access.

## Explicit non-goals

These have been proposed and declined, deliberately, more than once:

- **CAPTCHA-solving integrations.**
- **Bot-detection evasion** (browser/TLS fingerprint spoofing, canvas/WebGL
  spoofing, mouse/keystroke simulation).
- **"Anti-bot" plugin categories** of any kind.
- **An actor-based concurrency runtime** or **internal event bus**,
  proposed as part of a broader architecture rewrite and declined -- see
  `CHANGELOG.md` 0.6.0 and [architecture/index.md#why-not-an-actor-system-or-event-bus](../architecture/index.md#why-not-an-actor-system-or-event-bus)
  for the specific reasoning.

See [security/](../security/index.md) and
[architecture/](../architecture/index.md) for the full reasoning behind
each of these.

## How to propose something

Open an issue or discussion describing the concrete gap or use case you're
hitting, not just a pattern/technology name -- see
[`CONTRIBUTING.md`](../../CONTRIBUTING.md).
