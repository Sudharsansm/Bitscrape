# Getting Started

## What is Bitscrape?

Bitscrape is an async Python web scraping framework. You write a `Spider`
(or a plain decorated function) describing what to fetch and how to parse
it; Bitscrape's `Engine` handles concurrency, retries, robots.txt
compliance, deduplication, and (optionally) distributed coordination,
storage, and observability — each of those "optionally" pieces is a
`Settings` toggle, not a separate thing you have to bolt on.

## The three things you need to know

### 1. A Spider describes what to crawl

```python
import bitscrape

class MySpider(bitscrape.Spider):
    name = "my_spider"
    start_urls = ["https://example.com"]

    async def parse(self, response):
        yield {"title": response.css("h1::text").get()}
```

Or, for the common case of one callback and no spider-level state, skip
the class entirely:

```python
@bitscrape.spider(name="my_spider", start_urls=["https://example.com"])
async def parse(response):
    yield {"title": response.css("h1::text").get()}
```

Both forms produce the same thing: a `Spider` subclass. See
[user-guide/index.md#spiders](../user-guide/index.md#spiders) for the full picture
(multiple callbacks, `self.follow()`, lifecycle hooks).

### 2. Settings configures everything, without touching your spider

```python
from bitscrape.core.settings import Settings

# Local: in-memory queue, single process (the default)
settings = Settings()

# Distributed: Redis-backed queue + cross-worker politeness, same spider
settings = Settings(
    scheduler_use_redis=True,
    distributed_throttle_enabled=True,
    redis_url="redis://localhost:6379/0",
)
```

Every optional subsystem — proxy rotation, session pooling, JS rendering,
live monitoring, Prometheus metrics, adaptive throttling — is a field on
`Settings`. See [user-guide/index.md#settings](../user-guide/index.md#settings) for the
full field reference (or [api/index.md#settings](../api/index.md#settings) for the flat
list).

### 3. `bitscrape.run()` (or the CLI) ties it together

```python
stats = bitscrape.run(MySpider, output="data.jsonl")
print(stats.items_scraped)
```

`bitscrape.run()` and the `bitscrape crawl` CLI command both go through the
same `build_engine()` factory internally — there's exactly one code path
that decides how a `Settings` object becomes a running crawl, not two that
could quietly drift apart (an actual bug found and fixed in this project's
own history — see `CHANGELOG.md` 0.7.0).

## Where to go next

- Never run Bitscrape before? → [quickstart/](../quickstart/index.md) for the fastest working example.
- Want a longer, guided build? → [tutorials/](../tutorials/index.md).
- Want the full picture before writing code? → [user-guide/](../user-guide/index.md) and [architecture/](../architecture/index.md).
- Something's not working? → [troubleshooting/](../troubleshooting/index.md).

## Project layout at a glance

```
src/bitscrape/
  core/            Spider, Settings, Request/Response/CrawlStats models
  engine.py        The crawl loop
  factory.py       build_engine() -- the one function that wires everything
  downloader/      HTTP fetching (aiohttp) + JS rendering (Playwright) + BrowserPool
  scheduler/       Queue (memory/Redis) + dedup filter
  frontier.py      Large-scale priority + per-domain-politeness frontier
  middleware/      UserAgent, Robots, MetaRobots, Cookies/Sessions, Proxy, DistributedThrottle
  parser/          CSS/XPath selection over responses
  pipeline/        Item validation, dedup, Postgres pipeline
  exporters/       JSONL/JSON/CSV/XML output
  storage/         Pluggable storage backends (SQLite/S3/Postgres/stubs)
  plugins.py       Hook/event system (PluginManager, BasePlugin)
  monitoring.py    Live local stats server
  observability.py Prometheus metrics, OpenTelemetry tracing, alerting
  link_analysis.py PageRank/HITS over the crawl's link graph
  recrawl.py       Incremental recrawl scheduling
  canonicalize.py  URL normalization, redirect resolution, SimHash dedup
  ranking.py       BM25 + vector search + Reciprocal Rank Fusion
  entity_resolution.py  Heuristic entity-mention clustering
  knowledge_graph.py    Subject-predicate-object graph (networkx-backed)
  cli/             The `bitscrape` command-line tool
deploy/            Dockerfile, docker-compose.yml, Kubernetes manifests
tests/             323 tests covering all of the above
```
