# Tutorials

Longer, worked examples. Each is self-contained and runnable.

## Tutorial 1: A multi-page crawl with pagination

Goal: scrape every page of a paginated listing, not just the first.

```python
from bitscrape.core.spider import Spider

class ListingSpider(Spider):
    name = "listings"
    start_urls = ["https://example.com/listings?page=1"]

    async def parse(self, response):
        for card in response.css("div.listing-card"):
            yield {
                "title": card.css("h2::text").get(),
                "price": card.css(".price::text").get(),
            }

        next_page = response.css("a.next-page::attr(href)").get()
        if next_page:
            yield self.follow(next_page)  # keep crawling until no "next" link
```

Run it:
```bash
bitscrape crawl spiders/listings.py -o listings.jsonl
```

`self.follow(url)` enqueues a new request with the same callback (`parse`)
by default, and (as of 0.8.0) automatically resolves relative URLs against
the current page — if `a.next-page::attr(href)` returns a relative path
like `/listings?page=2`, `self.follow(next_page)` correctly resolves it to
the full absolute URL for you:

```python
next_page = response.css("a.next-page::attr(href)").get()
if next_page:
    yield self.follow(next_page)
```

See [user-guide/index.md#relative-urls-fixed-in-080](../user-guide/index.md#relative-urls-fixed-in-080)
for more on this. The crawl naturally terminates once a page has no
`a.next-page` link and the frontier empties.

## Tutorial 2: Multiple callbacks (crawl an index, then each detail page)

```python
from bitscrape.core.spider import Spider

class ArticleSpider(Spider):
    name = "articles"
    start_urls = ["https://example.com/articles"]

    async def parse(self, response):
        for href in response.css("a.article-link::attr(href)").getall():
            yield self.follow(href, callback="parse_article")

    async def parse_article(self, response):
        yield {
            "title": response.css("h1::text").get(),
            "body": " ".join(response.css("article p::text").getall()),
            "url": response.url,
        }
```

`self.follow(url, callback="parse_article")` routes that specific request's
response to a different method than the default `parse`. Any number of
callbacks is fine -- this is how you model "index page -> N detail pages"
crawls.

## Tutorial 3: JavaScript-rendered content

Some sites only populate content client-side. If `curl`/view-source
doesn't show the data but your browser does, you need JS rendering:

```python
from bitscrape.core.spider import Spider

class SpaSpider(Spider):
    name = "spa_demo"
    start_urls = ["https://example.com/dashboard"]

    async def parse(self, response):
        yield {"value": response.css(".metric::text").get()}

    def start_requests(self):
        for url in self.start_urls:
            yield self.follow(url, use_playwright=True)
```

Requires the browser binary: `playwright install chromium`. For
lazy-loaded / infinite-scroll content, add scrolling:

```python
yield self.follow(url, use_playwright=True, meta={"scroll": True})
# or with overrides:
yield self.follow(url, use_playwright=True,
                   meta={"scroll": {"max_scrolls": 30, "pause_ms": 500}})
```

See [browser/](../browser/index.md) for the full `scroll_to_bottom()` and
`BrowserPool` reference.

## Tutorial 4: Distributed crawling across multiple worker processes

Goal: run the same spider from several processes/machines, sharing one
frontier and without duplicating work or hammering a domain from multiple
workers simultaneously.

```python
# worker.py -- run this file on N machines/processes unchanged
import bitscrape
from bitscrape.core.settings import Settings
from spiders.listings import ListingSpider

settings = Settings(
    scheduler_use_redis=True,
    redis_url="redis://redis-host:6379/0",
    distributed_throttle_enabled=True,   # per-domain politeness across ALL workers
    dupefilter_enabled=True,             # RedisDupeFilter: no duplicate crawling across workers
)

bitscrape.run(ListingSpider, settings=settings)
```

Start Redis first (`docker run -d -p 6379:6379 redis:7-alpine`, or see
[installation/](../installation/index.md)), then run `worker.py` on as many
processes as you want. Each worker pulls from the same Redis-backed queue;
`RedisDupeFilter` (a Redis `SADD`, which is atomic) guarantees two workers
never both claim the same URL; `DistributedThrottleMiddleware` uses a
Redis-backed lease so requests to any one domain are spaced out
cluster-wide, not just within a single process.

See [scheduler/](../scheduler/index.md) and
[architecture/index.md#distributed-crawling](../architecture/index.md#distributed-crawling)
for how this works internally.

## Tutorial 5: Storing results in SQLite instead of a flat file

```python
import asyncio
import bitscrape
from bitscrape.storage.backends import SQLiteStorageBackend
from bitscrape.plugins import PluginManager, StorageConnectorPlugin
from spiders.listings import ListingSpider

async def main():
    backend = SQLiteStorageBackend("listings.db")
    pm = PluginManager()
    pm.register_plugin(StorageConnectorPlugin(backend))

    engine = await bitscrape.build_engine(
        ListingSpider(), bitscrape.Settings(), plugin_manager=pm
    )
    stats = await engine.run()
    print(f"Stored {stats.items_scraped} items in listings.db")

asyncio.run(main())
```

`StorageConnectorPlugin` opens the backend on `spider_opened`, writes each
item as it's scraped, and closes the backend on `spider_closed` -- no
manual lifecycle management needed. See [storage/](../storage/index.md) and
[plugins/](../plugins/index.md).

## Tutorial 6: Watching a live crawl

```python
import bitscrape

stats = bitscrape.run(
    ListingSpider,
    settings=bitscrape.Settings(monitoring_enabled=True, monitoring_port=8765),
)
```

While the crawl runs, open `http://localhost:8765/` for an
auto-refreshing HTML view, or `http://localhost:8765/stats.json` for raw
JSON. For production-grade metrics you'd actually alert on, enable
`metrics_enabled=True` instead (or as well) and point Prometheus at
`http://localhost:9100/metrics`. See [monitoring/](../monitoring/index.md).

## Tutorial 7: Ranking scraped pages by importance (PageRank)

```python
from bitscrape.link_analysis import LinkGraph

graph = LinkGraph()

class RankedSpider(bitscrape.Spider):
    name = "ranked"
    start_urls = ["https://example.com/"]

    async def parse(self, response):
        links = response.css("a::attr(href)").getall()
        graph.add_links(response.url, links)
        for href in links:
            yield self.follow(href)

bitscrape.run(RankedSpider)

for url, score in graph.top_by_pagerank(n=10):
    print(f"{score:.4f}  {url}")
```

See [ai/index.md#link-analysis](../ai/index.md#link-analysis) for HITS and
other options.

## Next steps

- Full API details for anything above: [api/](../api/index.md)
- Design rationale for how these pieces fit together: [architecture/](../architecture/index.md)
- More runnable examples: [examples/](../examples/index.md)
