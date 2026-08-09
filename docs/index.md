# Bitscrape Documentation

Bitscrape is an async, Python web scraping framework built around one
central `Engine`, a `Settings`-driven `build_engine()` factory, and a
growing set of optional subsystems (distributed crawling, storage
backends, link analysis, semantic ranking, entity resolution,
observability) that you opt into by toggling `Settings` fields — nothing
extra loads or runs unless you ask for it.

This documentation set describes the fork/patch tracked in this
repository's `CHANGELOG.md`, currently at **v0.7.0**. Every claim of
"tested" or "verified" in these docs corresponds to a real, passing test in
`tests/` — where something is a documented interface stub or an
infrastructure artifact that couldn't be verified against a live service in
the environment this was built in, that is stated explicitly rather than
implied.

## Where to start

| If you want to... | Go to |
|---|---|
| Install the package | [installation/](installation/index.md) |
| Write your first spider in 2 minutes | [quickstart/](quickstart/index.md) |
| Learn the concepts step by step | [getting-started/](getting-started/index.md) |
| Follow a longer worked example | [tutorials/](tutorials/index.md) |
| Look up `Settings` fields, classes, functions | [api/](api/index.md) |
| Understand how the pieces fit together | [architecture/](architecture/index.md) |
| Deploy to Docker/Kubernetes | [deployment/](deployment/index.md) |
| Fix a "0 items scraped" crawl | [troubleshooting/](troubleshooting/index.md) |

## Documentation map

- **[getting-started/](getting-started/index.md)** — orientation: what Bitscrape is, core concepts, the shape of a project.
- **[installation/](installation/index.md)** — `pip install`, extras, system requirements, Redis/Docker setup.
- **[quickstart/](quickstart/index.md)** — the fastest path from zero to a working spider.
- **[tutorials/](tutorials/index.md)** — longer, worked, step-by-step walkthroughs.
- **[user-guide/](user-guide/index.md)** — `Spider`, `Settings`, pipelines, exporters, middleware, in depth.
- **[developer-guide/](developer-guide/index.md)** — contributing, running the test suite, project layout, coding standards.
- **[architecture/](architecture/index.md)** — the Engine, Downloader, Scheduler, middleware chain, and the `build_engine()` factory.
- **[api/](api/index.md)** — condensed reference for every public class/function.
- **[plugins/](plugins/index.md)** — the `PluginManager`/`BasePlugin` hook system and bundled example plugins.
- **[browser/](browser/index.md)** — JS rendering via Playwright, `BrowserPool`, infinite scroll.
- **[crawling/](crawling/index.md)** — `Request`/`Response`, retries, redirects, robots.txt, meta-robots, canonicalization.
- **[scheduler/](scheduler/index.md)** — the request queue, dedup filter, and the large-scale `Frontier`.
- **[parser/](parser/index.md)** — CSS/XPath selection over responses.
- **[extractors/](extractors/index.md)** — structured extraction: canonicalization, near-duplicate detection, entity resolution.
- **[ai/](ai/index.md)** — hybrid lexical+vector ranking (BM25 + embeddings + RRF) and the knowledge graph.
- **[storage/](storage/index.md)** — pluggable storage backends (SQLite, S3, Postgres, and documented stubs).
- **[deployment/](deployment/index.md)** — Docker, Docker Compose, and Kubernetes manifests.
- **[security/](security/index.md)** — this project's security posture and what it deliberately won't build.
- **[monitoring/](monitoring/index.md)** — live stats, Prometheus metrics, OpenTelemetry tracing, alerting.
- **[cli/](cli/index.md)** — the `bitscrape` command-line tool.
- **[examples/](examples/index.md)** — complete, runnable example spiders.
- **[troubleshooting/](troubleshooting/index.md)** — diagnosing common failures.
- **[faq/](faq/index.md)** — short answers to recurring questions.
- **[roadmap/](roadmap/index.md)** — what's planned and explicitly what isn't.
- **[changelog/](changelog/index.md)** — release history (mirrors the root `CHANGELOG.md`).

## Conventions used throughout these docs

- **Verified** means a real automated test exercises the described
  behavior (a real local HTTP server, a real local Redis instance, a real
  on-disk SQLite database, a real S3 API emulation via `moto`, or real
  OpenTelemetry/Prometheus libraries) — not a mock standing in for the
  claim itself.
- **Documented stub** means the interface and intended implementation are
  written and explained, but the implementation raises `NotImplementedError`
  because the required backend (e.g. a live MongoDB or Elasticsearch
  server) wasn't available to build and test against.
- **Not live-verified** (used for the Kubernetes/Docker artifacts) means
  the file is syntactically and structurally valid — checked by a real
  test — but has not been applied against a live cluster or Docker daemon.

## Root project files

- [`README.md`](../README.md) — top-level overview and quickstart (same content style as this page, oriented at first-time visitors to the repository).
- [`CONTRIBUTING.md`](../CONTRIBUTING.md) — how to set up a dev environment and submit changes.
- [`SECURITY.md`](../SECURITY.md) — vulnerability reporting policy.
- [`CODE_OF_CONDUCT.md`](../CODE_OF_CONDUCT.md) — community standards.
- [`LICENSE`](../LICENSE) — MIT License.
- [`ROADMAP.md`](../ROADMAP.md) — planned work and explicit non-goals.
- [`CHANGELOG.md`](../CHANGELOG.md) — full release history.
- [`ARCHITECTURE.md`](../ARCHITECTURE.md) — system design reference.
- [`API_REFERENCE.md`](../API_REFERENCE.md) — flat, single-page API reference.
