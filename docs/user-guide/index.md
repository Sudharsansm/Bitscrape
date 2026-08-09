# User Guide

In-depth reference for the pieces you'll touch in almost every project:
spiders, settings, pipelines, exporters, and middleware.

## Spiders

### Class-based

```python
from bitscrape.core.spider import Spider

class MySpider(Spider):
    name = "my_spider"                       # required, unique
    start_urls = ["https://example.com"]      # seeds start_requests()

    async def parse(self, response):
        yield {"title": response.css("h1::text").get()}
```

`Spider.__init__(self, settings=None)` raises `ValueError` if `name` isn't
set. `self.settings` and `self.logger` (a `logging.Logger` named after
`self.name`) are available in every method.

**Lifecycle hooks** (override as needed, both default to no-ops):
```python
async def open_spider(self) -> None:
    """Called once before crawling starts."""

async def close_spider(self) -> None:
    """Called once after crawling finishes."""
```

**`start_requests()`** — override this instead of relying on `start_urls`
if you need non-GET requests, custom headers, or `use_playwright=True` on
the seed requests:
```python
def start_requests(self):
    for url in self.start_urls:
        yield self.follow(url, use_playwright=True)
```

**`self.follow(url, callback="parse", meta=None, use_playwright=False)`**
— builds a `Request`. See [#relative-urls-fixed-in-080](#relative-urls-fixed-in-080)
below — it does **not** resolve relative URLs for you.

**Multiple callbacks** — pass `callback="method_name"` to route a
request's response to a specific method instead of the default `parse`:
```python
async def parse(self, response):
    for href in response.css("a.detail::attr(href)").getall():
        yield self.follow(href, callback="parse_detail")

async def parse_detail(self, response):
    yield {"title": response.css("h1::text").get()}
```

### Function-based (the simple path)

```python
import bitscrape

@bitscrape.spider(name="my_spider", start_urls=["https://example.com"])
async def parse(response):
    yield {"title": response.css("h1::text").get()}
```

The decorated function takes just `response` (no `self`) and must be an
async generator (use `yield`, not `return`). Extra keyword arguments to
`@bitscrape.spider(...)` become class attributes on the generated `Spider`
subclass — e.g. `@bitscrape.spider(name=..., start_urls=..., custom_settings={...})`.

Use this for the common single-callback case. For multiple callbacks or
spider-level state, use a full class.

### Relative URLs (fixed in 0.8.0)

**`self.follow()` now resolves relative URLs automatically** when called
from within a callback. If a page's `<a href="/next">` link is relative,
`response.css("a::attr(href)").get()` returns the literal string
`"/next"`, and `self.follow("/next")` correctly resolves it against the
current response's URL (e.g. `https://example.com/next`) — you don't need
to call `urljoin()` yourself:

```python
async def parse(self, response):
    href = response.css("a.next::attr(href)").get()
    if href:
        yield self.follow(href)  # resolved automatically
```

This works because the Engine tracks the response currently being parsed
via a `contextvars.ContextVar` (not a plain instance attribute — the
Engine processes requests *concurrently*, so a shared mutable attribute
would let two in-flight requests clobber each other's resolution context;
`ContextVar` is correctly isolated per `asyncio` task). Calling
`self.follow()` **outside** a callback (e.g. while building
`start_requests()`, before any response exists) still passes a relative
URL through unresolved — there's no "current page" to resolve against yet
at that point, so absolute URLs are required for seed requests.

Prior to 0.8.0, `self.follow()` did not resolve relative URLs at all — if
you're on an older version, resolve manually with
`urljoin(response.url, href)`.

## Settings

`bitscrape.core.settings.Settings` is a `pydantic-settings` `BaseSettings`
— every field can also be set via environment variable
(`BITSCRAPE_<FIELD_NAME>`, e.g. `BITSCRAPE_CONCURRENT_REQUESTS=32`).

### Concurrency & rate limiting
| Field | Default | Meaning |
|---|---|---|
| `concurrent_requests` | `16` | Global concurrent request cap |
| `concurrent_requests_per_domain` | `4` | Per-domain concurrent request cap |
| `download_delay` | `0.0` | Fixed seconds between requests to the same domain |
| `download_timeout` | `30.0` | Per-request timeout (seconds) |
| `autothrottle_enabled` | `False` | Adaptive per-domain delay based on observed latency (see [architecture/](../architecture/index.md)) |
| `autothrottle_start_delay` / `autothrottle_max_delay` / `autothrottle_target_concurrency` | `1.0` / `60.0` / `2.0` | Autothrottle tuning |
| `distributed_throttle_enabled` | `False` | Redis-backed per-domain lease across worker **processes** (needs Redis) |

### HTTP behavior
| Field | Default | Meaning |
|---|---|---|
| `user_agent` | `"BitscrapeBot/0.1 ..."` | Sent on every request; also used to match scoped robots.txt/meta-robots rules |
| `follow_redirects` | `True` | Follow HTTP redirects |
| `max_redirect_count` | `10` | Cap on redirect chain length |
| `retry_http_codes` | `[500,502,503,504,429]` | Statuses that trigger a retry |
| `respect_retry_after` | `True` | Honor a server's `Retry-After` header on retry instead of blind exponential backoff |
| `max_retry_after_seconds` | `120.0` | Cap on how long `Retry-After` is allowed to make a request wait |
| `conditional_get_enabled` | `True` | Send `If-None-Match`/`If-Modified-Since` on repeat requests; serve `304`s from cache |

### robots.txt / indexing directives
| Field | Default | Meaning |
|---|---|---|
| `robotstxt_obey` | `True` | Enforce `Disallow`, `Crawl-delay`, `Sitemap` from robots.txt |
| `respect_meta_robots` | `True` | Honor `<meta name="robots">` / `X-Robots-Tag` `noindex`/`nofollow` |

### Scheduler / distributed crawling
| Field | Default | Meaning |
|---|---|---|
| `scheduler_use_redis` | `False` | Use `RedisQueue`/`RedisDupeFilter` instead of in-memory |
| `redis_url` | `redis://localhost:6379/0` | Redis connection string |
| `dupefilter_enabled` | `True` | Skip already-seen URLs |
| `max_depth` | `None` | Cap on link-following depth (`None` = unlimited) |
| `queue_max_size` | `0` | Backpressure cap on the in-memory queue (`0` = unbounded) |

### JS rendering
| Field | Default | Meaning |
|---|---|---|
| `playwright_headless` | `True` | Run browser headless |
| `playwright_browser` | `"chromium"` | `chromium` / `firefox` / `webkit` |
| `playwright_pool_size` | `2` | Number of pooled browser instances |
| `playwright_pool_enabled` | `False` | Reuse pooled browsers instead of launching one per request |

### Sessions / proxies
| Field | Default | Meaning |
|---|---|---|
| `session_pool_size` | `1` | >1 switches from a single cookie jar to a rotating pool |
| `session_rotate_every` | `0` | Auto-clear a session's cookies after N requests (`0` = never) |
| `proxies` | `[]` | Non-empty enables `ProxyMiddleware` in `build_engine()` |
| `proxy_rotate` | `True` | Random rotation vs. round-robin |

### Monitoring / metrics
| Field | Default | Meaning |
|---|---|---|
| `monitoring_enabled` | `False` | Start a `StatsMonitor` (local JSON/HTML feed) alongside the crawl |
| `monitoring_port` | `8765` | Port for the above |
| `metrics_enabled` | `False` | Start a real Prometheus `/metrics` endpoint |
| `metrics_port` | `9100` | Port for the above |

### Storage / output
| Field | Default | Meaning |
|---|---|---|
| `database_url` | `None` | `asyncpg` DSN for `PostgresPipeline`/`PostgresStorageBackend` |
| `feed_uri` / `feed_format` | `None` / `"jsonl"` | Default export target/format if you don't pass one explicitly |

Full flat list: [api/index.md#settings](../api/index.md#settings).

## Pipelines

Pipelines process each scraped item after it leaves your spider's callback
and before export. Located in `bitscrape.pipeline.pipelines`:

| Class | What it does |
|---|---|
| `BasePipeline` | Abstract base — override `async def process_item(self, item, spider)` |
| `LoggingPipeline` | Logs every item at INFO |
| `ValidationPipeline` | Validates against a Pydantic model / schema; raises `DropItem` on failure |
| `DedupPipeline` | Drops items already seen (by a configurable key) |
| `PostgresPipeline` | Writes each item to PostgreSQL via `asyncpg` |

Raise `DropItem` from any pipeline to discard an item (it's counted in
`stats.items_dropped`, not `stats.items_scraped`). `PipelineManager` runs
pipelines in the order given and stops processing an item early if one of
them drops it.

```python
from bitscrape.pipeline.pipelines import ValidationPipeline, DedupPipeline

pipelines = [ValidationPipeline(schema=MyItemModel), DedupPipeline(key="url")]
```

Pass `pipelines=[...]` to `bitscrape.run()` or construct
`PipelineManager(pipelines)` directly when using `Engine`/`build_engine()`
manually.

## Exporters

Located in `bitscrape.exporters.feed`:

| Class | Format |
|---|---|
| `JSONLExporter` | One JSON object per line (the default) |
| `JSONExporter` | A single JSON array |
| `CSVExporter` | CSV, columns inferred from the first item |
| `XMLExporter` | Simple `<items><item>...</item></items>` XML |

`get_exporter(fmt, uri)` picks the right class from a format string
(`"jsonl"`, `"json"`, `"csv"`, `"xml"`) and a destination path. This is
what both `bitscrape.run(..., output=..., fmt=...)` and the CLI's
`-o`/`--fmt` flags use internally.

## Middleware

Located in `bitscrape.middleware.middleware`. `build_engine()` assembles
these automatically based on `Settings` — see
[architecture/index.md#middleware-selection](../architecture/index.md#middleware-selection)
for the exact selection logic — but you can also construct the list
yourself and pass it to `bitscrape.run(..., middlewares=[...])` to override
the automatic selection entirely.

| Class | Purpose |
|---|---|
| `UserAgentMiddleware` | Rotates the `User-Agent` header |
| `RobotsMiddleware` | Enforces robots.txt (`Disallow`, `Crawl-delay`, `Sitemap`) |
| `MetaRobotsMiddleware` | Enforces page-level `noindex`/`nofollow` |
| `CookieMiddleware` | Single cookie jar per domain |
| `SessionPoolMiddleware` | Pool of N rotating cookie-jar sessions per domain |
| `ProxyMiddleware` | Rotates outbound proxy per request |
| `DistributedThrottleMiddleware` | Redis-backed per-domain lease across worker processes |

Full detail on each: [architecture/](../architecture/index.md) and
[crawling/](../crawling/index.md).

## Next

- [architecture/](../architecture/index.md) — how these pieces fit together end to end.
- [api/](../api/index.md) — condensed signatures for everything above.
- [crawling/](../crawling/index.md) — Request/Response model, retries, canonicalization.
