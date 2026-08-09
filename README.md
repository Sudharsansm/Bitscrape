<p align="center">
  <img src="https://raw.githubusercontent.com/Sudharsansm/Bitscrape/main/docs/bitscrape.png" width="500" alt="Bitscrape">
</p>

<h1 align="center">Bitscrape</h1>

<p align="center">
  <a href="https://pypi.org/project/bitscrape/">
    <img src="https://img.shields.io/pypi/v/bitscrape.svg">
  </a>
  <a href="https://pypi.org/project/bitscrape/">
    <img src="https://img.shields.io/pypi/pyversions/bitscrape.svg">
  </a>
  <a href="https://github.com/Sudharsansm/Bitscrape/blob/main/LICENSE">
    <img src="https://img.shields.io/pypi/l/bitscrape.svg">
  </a>
</p>

# Bitscrape

**A modular, async Python web scraping framework — simple for a first
spider, capable of distributed crawling, storage, search-ranking, and
observability for everything after that.**

It provides fast async networking, powerful HTML parsing, configurable
pipelines, distributed crawling support, and optional browser rendering with
Playwright.


## Install

```bash
pip install bitscrape
```

or

```bash
uv add bitscrape
```

## Documentation

* Repository: https://github.com/Sudharsansm/Bitscrape
* Issues: https://github.com/Sudharsansm/Bitscrape/issues


```python
import bitscrape

@bitscrape.spider(name="quotes", start_urls=["https://example.com"])
async def parse(response):
    for quote in response.css("div.quote"):
        yield {
            "text": quote.css("span.text::text").get(),
            "author": quote.css("small.author::text").get(),
        }

bitscrape.run(parse, output="quotes.jsonl")
```

That same call scales to a Redis-backed, multi-worker distributed crawl by
changing one argument — the spider code doesn't change:

```python
bitscrape.run(parse, settings=bitscrape.Settings(
    scheduler_use_redis=True,
    distributed_throttle_enabled=True,
))
```

## Why Bitscrape

- **One function call to run a spider**, whether it's a plain decorated
  function or a full class with multiple callbacks.
- **One `Settings` object controls everything else** — proxy rotation,
  session pooling, JS rendering, distributed crawling, live monitoring,
  Prometheus metrics — all opt-in toggles, nothing extra loads unless you
  ask for it.
- **Respects robots.txt and meta-robots by default**, and fails *safe*
  (blocks) rather than *open* (allows) if robots.txt can't be fetched.
- **Every documented feature has a real test behind it** — a real local
  Redis, a real local HTTP server, a real on-disk SQLite database, a real
  S3 API emulation, real Prometheus/OpenTelemetry libraries. Where
  something couldn't be verified for real (a live PostgreSQL/MongoDB
  server, a Kubernetes cluster), that's stated explicitly rather than
  implied — see [`CHANGELOG.md`](CHANGELOG.md) and
  [`docs/`](docs/index.md) for exactly what's verified vs. documented.

## What's included

| Area | What you get |
|---|---|
| **Crawling** | Async fetching (`aiohttp`), retries with `Retry-After` support, conditional GET, robots.txt + meta-robots compliance, redirect-loop detection |
| **JS rendering** | Playwright integration, pooled browser reuse, infinite-scroll helper |
| **Distributed crawling** | Redis-backed queue + dedup filter, cross-worker-process politeness throttle, a Mercator-style priority `Frontier` |
| **Extraction** | CSS/XPath selectors, URL canonicalization, SimHash near-duplicate detection, heuristic entity resolution |
| **Search infrastructure** | PageRank/HITS link analysis, incremental recrawl scheduling, BM25 + vector hybrid search with Reciprocal Rank Fusion, a knowledge-graph builder |
| **Storage** | Pluggable backends — SQLite and S3 fully tested, PostgreSQL implemented, MongoDB/Elasticsearch as documented stubs |
| **Extensibility** | A plugin/hook system (`PluginManager`), example auth-helper and storage-connector plugins |
| **Observability** | Real Prometheus metrics, real OpenTelemetry tracing, structured JSON logging, threshold-based alerting, a live local stats dashboard |
| **Deployment** | Dockerfile, Docker Compose, Kubernetes manifests (Deployment/HPA/Service/PDB) |

**Explicitly not included, by design**: CAPTCHA-solving, browser/TLS
fingerprint spoofing, or any other bot-detection evasion tooling. See
[`docs/security/`](docs/security/index.md) for why.

## Install

```bash
python3 -m venv venv
source venv/bin/activate          # Windows: venv\Scripts\Activate.ps1
pip install -e ".[all,dev,cli]"
bitscrape --version
```

See [`docs/installation/`](docs/installation/index.md) for extras, system
requirements, and troubleshooting.

## Quickstart

```bash
bitscrape genspider quotes example.com
bitscrape crawl spiders/quotes.py -o quotes.jsonl
```

Full walkthrough: [`docs/quickstart/`](docs/quickstart/index.md).

## Documentation

Full documentation lives in [`docs/`](docs/index.md):

- [Getting Started](docs/getting-started/index.md) · [Quickstart](docs/quickstart/index.md) · [Tutorials](docs/tutorials/index.md)
- [User Guide](docs/user-guide/index.md) · [Developer Guide](docs/developer-guide/index.md) · [Architecture](docs/architecture/index.md)
- [API Reference](docs/api/index.md) · [CLI Reference](docs/cli/index.md)
- [Plugins](docs/plugins/index.md) · [Browser Rendering](docs/browser/index.md) · [Crawling](docs/crawling/index.md)
- [Scheduler](docs/scheduler/index.md) · [Parser](docs/parser/index.md) · [Extractors](docs/extractors/index.md)
- [AI / Search Infra](docs/ai/index.md) · [Storage](docs/storage/index.md) · [Deployment](docs/deployment/index.md)
- [Security](docs/security/index.md) · [Monitoring](docs/monitoring/index.md) · [Examples](docs/examples/index.md)
- [Troubleshooting](docs/troubleshooting/index.md) · [FAQ](docs/faq/index.md) · [Roadmap](docs/roadmap/index.md) · [Changelog](docs/changelog/index.md)

Also at the repository root: [`ARCHITECTURE.md`](ARCHITECTURE.md) (condensed
system design), [`API_REFERENCE.md`](API_REFERENCE.md) (flat single-page API
reference), [`ROADMAP.md`](ROADMAP.md), [`CHANGELOG.md`](CHANGELOG.md).

## Running the test suite

```bash
pytest -q
```

Expect `440 passed`. A handful of tests need a local Redis on port 6390 and
a local PostgreSQL server (database `bitscrape_test`, user/password
`postgres`/`postgres`) —
if it's not running, only those specific tests fail, everything else still
passes. See [`docs/developer-guide/`](docs/developer-guide/index.md).

## Contributing

See [`CONTRIBUTING.md`](CONTRIBUTING.md). Security issues: see
[`SECURITY.md`](SECURITY.md) rather than a public issue. Community
standards: [`CODE_OF_CONDUCT.md`](CODE_OF_CONDUCT.md).

## License

MIT — see [`LICENSE`](LICENSE).
