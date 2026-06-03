# Frequently Asked Questions

---

## General

**Q: What is the difference between Bitscrape and Scrapy?**

Bitscrape is inspired by Scrapy but built on modern Python. Key differences:
- Bitscrape uses native `asyncio`; Scrapy uses Twisted (an older async framework)
- Bitscrape uses Pydantic v2 for type-safe data models; Scrapy uses plain dicts
- Bitscrape has built-in Playwright; Scrapy needs the `scrapy-playwright` plugin
- Bitscrape has a single-import API (`import bitscrape`); Scrapy has multiple imports
- Scrapy is mature with 10+ years of production use; Bitscrape is new

See [comparing Scrapy and Bitscrape](https://github.com/yourorg/bitscrape) for a
full comparison.

---

**Q: Does Bitscrape support Python 3.10?**

No. Bitscrape requires Python 3.11 or higher. It uses `str | None` union
syntax, `tomllib`, and other 3.11+ features.

---

**Q: Can I use Bitscrape without Pydantic items?**

Yes. You can yield plain dicts from your spider:

```python
async def parse(self, response):
    yield {"title": response.css("h1::text").get()}
```

Pydantic items are recommended but not required.

---

## Scraping

**Q: A page shows content only after JavaScript runs. How do I scrape it?**

Use Playwright:

```bash
pip install "bitscrape[playwright]"
playwright install chromium
```

```python
yield self.follow("/spa-page", use_playwright=True)
```

---

**Q: How do I scrape a page that requires login?**

Use `FormRequest` to submit the login form, then use `CookieMiddleware` to
maintain the session:

```python
class LoginSpider(bitscrape.Spider):
    name = "loginspider"
    start_urls = ["https://example.com/login"]

    async def parse(self, response):
        yield bitscrape.FormRequest(
            url="https://example.com/login",
            formdata={"username": "myuser", "password": "mypass"},
            callback="parse_after_login",
        )

    async def parse_after_login(self, response):
        # Now authenticated — scrape protected pages
        yield self.follow("/dashboard")
```

Make sure `CookieMiddleware` is active (it is by default).

---

**Q: How do I limit the crawl to a specific domain?**

Filter links in your `parse` method:

```python
from urllib.parse import urlparse

ALLOWED_DOMAIN = "example.com"

async def parse(self, response):
    for href in response.css("a::attr(href)").getall():
        if href.startswith("http"):
            domain = urlparse(href).netloc
            if ALLOWED_DOMAIN in domain:
                yield self.follow(href)
        elif href.startswith("/"):
            yield self.follow(f"https://{ALLOWED_DOMAIN}{href}")
```

---

**Q: How do I avoid scraping the same URL twice?**

The `DupeFilter` handles this automatically. Every URL is fingerprinted
before being added to the queue. Duplicate URLs are silently dropped.

Make sure it's enabled (it is by default):

```python
settings = bitscrape.Settings(dupefilter_enabled=True)
```

---

**Q: How do I set a maximum crawl depth?**

```python
settings = bitscrape.Settings(max_depth=3)
```

Depth 0 = start URLs, depth 1 = pages linked from start URLs, etc.

---

**Q: The site is blocking me. What can I do?**

- Set a realistic `user_agent` in settings
- Add a `download_delay` (e.g. `1.0` second)
- Use `UserAgentMiddleware(rotate=True)` with a list of real browser agents
- Use Playwright — it runs a real browser which is harder to detect
- Reduce `concurrent_requests_per_domain` to look less like a bot
- Add `download_delay` jitter in a custom middleware

---

## Data

**Q: How do I save data to a database?**

Use `PostgresPipeline`:

```python
from bitscrape import PostgresPipeline

bitscrape.run(
    MySpider,
    pipelines=[PostgresPipeline(table="products", conflict_cols=["url"])],
    settings=bitscrape.Settings(
        database_url="postgresql://user:pass@localhost/mydb"
    ),
)
```

---

**Q: Can I output to multiple formats at once?**

The built-in exporter writes to one format. For multiple outputs, write a
custom pipeline alongside the exporter:

```python
from bitscrape import BasePipeline
from bitscrape.exporters.feed import CSVExporter

class CSVPipeline(BasePipeline):
    async def open_spider(self, spider):
        self._exp = CSVExporter("backup.csv")
        self._exp.open()

    async def process_item(self, item, spider):
        self._exp.export_item(item)
        return item

    async def close_spider(self, spider):
        self._exp.close()

bitscrape.run(MySpider, output="data.jsonl", pipelines=[CSVPipeline()])
```

---

## Performance

**Q: How fast is Bitscrape?**

Speed depends almost entirely on the target website (network latency,
server response time). Bitscrape's own overhead per request is minimal.

With `concurrent_requests=64` on a fast local network, it can handle
thousands of requests per minute.

---

**Q: How do I make it faster?**

```python
settings = bitscrape.Settings(
    concurrent_requests=64,
    concurrent_requests_per_domain=16,
    download_delay=0.0,
)
```

For Linux/macOS, install `uvloop` for a faster event loop:

```bash
pip install "bitscrape[speed]"
```

---

**Q: How do I slow it down to be polite?**

```python
settings = bitscrape.Settings(
    concurrent_requests=4,
    concurrent_requests_per_domain=1,
    download_delay=2.0,
    robotstxt_obey=True,
)
```

---

## Errors

**Q: `'SelectorList' object is not iterable`**

Update to the latest Bitscrape version. This was a bug in the initial release
where iterating `response.css("div.item")` failed. It is fixed in 0.1.0+.

---

**Q: `ImportError: playwright is required`**

```bash
pip install "bitscrape[playwright]"
playwright install chromium
```

---

**Q: `Connection refused` to Redis**

Make sure Redis is running:

```bash
docker run -d -p 6379:6379 redis:7
```

And the URL is correct:

```python
settings = bitscrape.Settings(redis_url="redis://localhost:6379/0")
```

---

**Q: Items are being dropped unexpectedly**

Check the crawl stats — `items_dropped` shows how many were dropped.
Enable `LoggingPipeline` to see which items are being dropped and why:

```python
bitscrape.run(MySpider, pipelines=[bitscrape.LoggingPipeline()])
```

Also set `log_level="DEBUG"` to see `DropItem` messages.
