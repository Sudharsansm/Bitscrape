# Examples

Complete, runnable examples. The two files in `examples/` at the project
root are referenced below in full; additional inline examples cover
patterns not represented there.

## `examples/polite_sitemap_spider.py`

Demonstrates the fixed `RobotsMiddleware`: real `Disallow` enforcement,
`Crawl-delay` respected, `Sitemap` URLs auto-discovered from
`request.meta`.

```python
from bitscrape.core.spider import Spider

class PoliteSitemapSpider(Spider):
    name = "polite_sitemap_demo"
    start_urls = ["https://example.com/"]
    custom_settings = {"robotstxt_obey": True, "download_delay": 1.0}

    async def parse(self, response):
        sitemaps = response.request.meta.get("sitemaps", [])
        crawl_delay = response.request.meta.get("crawl_delay")

        if crawl_delay:
            self.logger.info("robots.txt requests %.1fs delay for %s", crawl_delay, response.url)

        for sitemap_url in sitemaps:
            yield self.follow(sitemap_url, callback="parse_sitemap")

        for link in response.css("a::attr(href)").getall():
            yield self.follow(link)

    async def parse_sitemap(self, response):
        for loc in response.xpath("//*[local-name()='loc']/text()").getall():
            yield self.follow(loc)
```

Run: `bitscrape crawl examples/polite_sitemap_spider.py -o pages.jsonl`

## `examples/infinite_scroll_spider.py`

Demonstrates Playwright-based JS rendering with `scroll_to_bottom()` and
`ProxyMiddleware` for rotating outbound IPs.

```python
from bitscrape.core.spider import Spider

class InfiniteScrollSpider(Spider):
    name = "infinite_scroll_demo"
    start_urls = ["https://example.com/listings"]
    custom_settings = {"robotstxt_obey": True}

    def start_requests(self):
        for url in self.start_urls:
            yield self.follow(url, use_playwright=True, meta={"scroll": True})

    async def parse(self, response):
        for card in response.css("div.listing-card"):
            yield {
                "title": card.css("h2::text").get(),
                "price": card.css(".price::text").get(),
                "url": card.css("a::attr(href)").get(),
            }
```

Run (after `playwright install chromium`):
`bitscrape crawl examples/infinite_scroll_spider.py -o listings.jsonl`

## Additional inline examples

### A spider with pipelines and a custom exporter format

```python
import bitscrape
from bitscrape.pipeline.pipelines import ValidationPipeline, DedupPipeline

stats = bitscrape.run(
    MySpider,
    output="products.csv",
    fmt="csv",
    pipelines=[ValidationPipeline(schema=ProductModel), DedupPipeline(key="sku")],
)
```

### A spider with proxy rotation and a session pool

```python
from bitscrape.core.settings import Settings

settings = Settings(
    proxies=["http://user:pass@proxy1:8080", "http://user:pass@proxy2:8080"],
    proxy_rotate=True,
    session_pool_size=3,
    session_rotate_every=50,
)
bitscrape.run(MySpider, settings=settings)
```

### A minimal knowledge-graph-building crawl

```python
import bitscrape
from bitscrape.knowledge_graph import KnowledgeGraph
from bitscrape.entity_resolution import EntityResolver

kg = KnowledgeGraph()
resolver = EntityResolver()

@bitscrape.spider(name="kg_demo", start_urls=["https://example.com/articles"])
async def parse(response):
    for article in response.css("article"):
        title = article.css("h2::text").get()
        author_raw = article.css(".author::text").get()
        author = resolver.resolve(author_raw) if author_raw else None
        if author:
            kg.add_relation(title, "written_by", author, source_url=response.url)
        yield {"title": title, "author": author}

bitscrape.run(parse)
kg.export_json("articles_graph.json")
```

### A full observability stack in one crawl

```python
import bitscrape

stats = bitscrape.run(MySpider, settings=bitscrape.Settings(
    monitoring_enabled=True,
    metrics_enabled=True,
    autothrottle_enabled=True,
))
# http://localhost:8765/        -- live HTML stats
# http://localhost:9100/metrics -- Prometheus
```

## See also

- [tutorials/](../tutorials/index.md) -- the same patterns, explained step by step.
- [api/](../api/index.md) -- signatures for everything used above.
