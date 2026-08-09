# Quickstart

This gets you from zero to a working, real crawl in a few minutes.

## 1. Install

```bash
pip install -e ".[cli]"
```

(See [installation/](../installation/index.md) if this doesn't work right away.)

## 2. Write a spider

The fastest form -- a decorated function, no class needed:

```python
# quotes.py
import bitscrape

@bitscrape.spider(name="quotes", start_urls=["https://example.com"])
async def parse(response):
    for quote in response.css("div.quote"):
        yield {
            "text": quote.css("span.text::text").get(),
            "author": quote.css("small.author::text").get(),
        }
```

## 3. Run it

```python
# run.py
import bitscrape
from quotes import parse

stats = bitscrape.run(parse, output="quotes.jsonl")
print(f"Scraped {stats.items_scraped} items in {stats.elapsed:.2f}s")
```

```bash
python run.py
cat quotes.jsonl
```

## Or run it from the CLI instead

The CLI expects a full `class`-based spider file (it looks for a `Spider`
subclass to instantiate):

```python
# spiders/quotes.py
from bitscrape.core.spider import Spider

class QuotesSpider(Spider):
    name = "quotes"
    start_urls = ["https://example.com"]

    async def parse(self, response):
        for quote in response.css("div.quote"):
            yield {
                "text": quote.css("span.text::text").get(),
                "author": quote.css("small.author::text").get(),
            }
```

```bash
bitscrape crawl spiders/quotes.py -o quotes.jsonl
```

You'll see a live summary table when it finishes:

```
       Crawl Stats
+---------------+--------+
| Requests      | 1      |
| Failed        | 0      |
| Items scraped | 3      |
| Items dropped | 0      |
...
+---------------+--------+
```

## Common next steps

**Extract with XPath instead of CSS:**
```python
response.xpath("//div[@class='quote']/span[@class='text']/text()").get()
```

**Follow links to other pages:**
```python
async def parse(self, response):
    for href in response.css("a.next::attr(href)").getall():
        yield self.follow(href)
```

**Export CSV instead of JSONL:**
```bash
bitscrape crawl spiders/quotes.py -o quotes.csv --fmt csv
```

**Render JavaScript-heavy pages:**
```python
yield self.follow(url, use_playwright=True)
```
(Needs `playwright install chromium` first -- see
[browser/](../browser/index.md).)

**Scale to Redis-backed distributed crawling -- same spider, no code changes:**
```python
from bitscrape.core.settings import Settings

stats = bitscrape.run(parse, settings=Settings(
    scheduler_use_redis=True,
    distributed_throttle_enabled=True,
))
```

## What just happened, briefly

`bitscrape.run()` (and the CLI) call `build_engine()`, which:
1. Builds a `Scheduler` (in-memory by default, or Redis-backed if `scheduler_use_redis=True`).
2. Assembles a middleware stack from your `Settings` (user-agent rotation, robots.txt + meta-robots handling, cookies/sessions, proxy rotation, distributed throttling -- whichever are enabled).
3. Runs your spider's `start_requests()` through the `Engine`'s fetch -> middleware -> parse -> pipeline -> export loop until the frontier is empty.

Full detail: [architecture/](../architecture/index.md).

## Something not working?

-> [troubleshooting/](../troubleshooting/index.md) -- especially the "0 items
scraped" section, the single most common thing that looks broken but isn't.
