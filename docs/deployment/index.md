# Deployment

Docker, Docker Compose, and Kubernetes artifacts live in `deploy/`.

> **Honest disclosure upfront**: these manifests were validated for
> syntactic and structural correctness in this project's build environment
> (13 automated tests confirm valid YAML, required fields like resource
> limits and health probes, and that label selectors actually match across
> files) — but no Docker daemon or Kubernetes cluster was available to
> `docker build` or `kubectl apply --dry-run=server` against. Validate
> against your own environment before deploying to production:
> ```bash
> docker compose -f deploy/docker-compose.yml config
> kubectl apply --dry-run=server -f deploy/k8s/
> ```

## Docker

`deploy/Dockerfile` is a multi-stage build:

- **Builder stage**: installs build toolchains (`build-essential`,
  `libxml2-dev`, `libxslt1-dev`) needed to compile some wheels, installs
  the package with `pip install --prefix=/install ".[all,cli]"`.
- **Runtime stage**: copies only the installed packages from the builder
  (`/install` -> `/usr/local`), installs just the runtime shared libraries
  (`libxml2`, `libxslt1.1`), runs as a non-root user (`bitscrape`, uid
  1000), and exposes ports `8765` (monitoring) and `9100` (Prometheus
  metrics).

```bash
docker build -t bitscrape:0.7.0 -f deploy/Dockerfile .
docker run --rm bitscrape:0.7.0 crawl /app/spiders/my_spider.py
```

Playwright's browser binaries are **not** included by default (a much
larger layer) -- uncomment the relevant line in the Dockerfile if your
spiders need JS rendering:
```dockerfile
# RUN pip install playwright && playwright install --with-deps chromium
```

## Docker Compose (local multi-worker dev/testing)

`deploy/docker-compose.yml` brings up Redis + one or more worker
containers sharing it:

```bash
docker compose -f deploy/docker-compose.yml up
docker compose -f deploy/docker-compose.yml up --scale worker=5   # 5 workers, same Redis
```

The `worker` service sets `BITSCRAPE_SCHEDULER_USE_REDIS=true`,
`BITSCRAPE_REDIS_URL=redis://redis:6379/0`, and
`BITSCRAPE_DISTRIBUTED_THROTTLE_ENABLED=true` via environment variables
(matching `Settings`' env-var support, `BITSCRAPE_<FIELD_NAME>`) -- every
scaled-up worker shares the same frontier and cross-worker politeness
automatically.

## Kubernetes

`deploy/k8s/` contains:

| File | Contents |
|---|---|
| `deployment.yaml` | `Deployment` with resource requests/limits, readiness/liveness probes against the `StatsMonitor` endpoint, and `topologySpreadConstraints` spreading replicas across zones |
| `hpa.yaml` | `HorizontalPodAutoscaler` on CPU/memory (70%/80% targets), with scale-down stabilization to avoid flapping, plus a commented-out path to scale on actual crawl-queue-depth via a custom Prometheus metric instead of just CPU |
| `service.yaml` | `Service` (exposes the stats/metrics ports), `ConfigMap` (the `BITSCRAPE_*` env vars), and `PodDisruptionBudget` (`minAvailable: 1`, so voluntary disruptions like node drains don't take out every replica at once) |

Apply (after validating against your own cluster):
```bash
kubectl apply -f deploy/k8s/
```

### Multi-region notes

`topologySpreadConstraints` in `deployment.yaml` spreads replicas across
availability zones within one cluster -- a practical starting point for
resilience, but **not** the same as running fully separate clusters per
region. True multi-region deployment (separate clusters, cross-region
Redis replication or per-region Redis instances, geo-routed traffic) is
a larger infrastructure decision this project doesn't prescribe a specific
answer for -- the same `bitscrape` image and `Settings`-driven configuration
work in either topology; the orchestration around it is yours to design
for your specific regions/providers.

### Autoscaling on real crawl backlog, not just CPU

The commented-out block in `hpa.yaml` shows how to scale on a custom
metric (`bitscrape_frontier_queue_depth` or similar) instead of just CPU --
this needs a Prometheus adapter exposing that metric from
`bitscrape_requests_total`/queue-depth (see
[monitoring/](../monitoring/index.md) for the real `/metrics` endpoint this
would scrape) plus the Prometheus Adapter installed on your cluster. Not
configured out of the box since it depends on your specific Prometheus
setup.

## Environment variables

Every `Settings` field is settable via `BITSCRAPE_<FIELD_NAME>` (uppercase,
matching `pydantic-settings`' default env-var naming). Useful in any
container/orchestration context without baking config into the image:

```bash
docker run -e BITSCRAPE_CONCURRENT_REQUESTS=32 \
           -e BITSCRAPE_ROBOTSTXT_OBEY=true \
           -e BITSCRAPE_REDIS_URL=redis://redis:6379/0 \
           bitscrape:0.7.0 crawl /app/spiders/my_spider.py
```

## See also

- [monitoring/](../monitoring/index.md) -- the `/stats.json` and `/metrics` endpoints these manifests expose via probes and Service ports.
- [security/](../security/index.md) -- running as non-root, secrets handling.
- [scheduler/](../scheduler/index.md) -- what `scheduler_use_redis`/`distributed_throttle_enabled` actually do once you're running multiple replicas.
