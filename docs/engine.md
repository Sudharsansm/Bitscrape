# Engine

The `Engine` is the central coordinator of a Bitscrape crawl. It ties together
the Scheduler, Downloader, Middleware, Pipeline, and Exporter into a single
async crawl loop.

Most users don't interact with the Engine directly — `bitscrape.run()` wraps
it for convenience. Use the Engine directly when you need fine-grained control.

---

## Using `bitscrape.run()` (Recommended)

```python
import bitscrape

stats = bitscrape.run(
    MySpider,                          # spider class (not instance)
    output="data.jsonl",               # optional output file
    fmt="jsonl",                       # format: jsonl|json|csv|xml
    settings=bitscrape.Settings(),     # optional settings
    pipelines=[...],                   # optional pipelines
    middlewares=[...],                 # optional middlewares
    log_level="INFO",                  # log level
)
print(f"Scraped {stats.items_scraped} items in {stats.elapsed:.1f}s")
```

---

## Using Engine Directly

```python
import asyncio
import bitscrape
from bitscrape import Engine, Settings
from bitscrape.exporters.feed import get_exporter
from bitscrape import (
    ValidationPipeline, DedupPipeline,
    UserAgentMiddleware, CookieMiddleware, RobotsMiddleware,
)

async def main():
    settings = Settings(concurrent_requests=16)
    spider   = MySpider(settings=settings)

    engine = Engine(
        spider=spider,
        settings=settings,
        pipelines=[
            ValidationPipeline(),
            DedupPipeline(),
        ],
        middlewares=[
            RobotsMiddleware(),
            UserAgentMiddleware(),
            CookieMiddleware(),
        ],
        exporter=get_exporter("jsonl", "data.jsonl"),
    )

    stats = await engine.run()
    print(stats)

asyncio.run(main())
```

---

## Engine Constructor

```python
Engine(
    spider:      Spider,                  # required
    settings:    Settings | None = None,  # uses defaults if None
    pipelines:   list[BasePipeline] = [], # item processing chain
    middlewares: list[BaseMiddleware] = [],# request/response hooks
    exporter:    BaseExporter | None = None, # file output
)
```

---

## CrawlStats

`engine.run()` returns a `CrawlStats` object:

```python
stats = await engine.run()

stats.requests_made       # int — total requests sent
stats.requests_failed     # int — requests that exhausted retries
stats.responses_received  # int — successful responses
stats.items_scraped       # int — items yielded by spider
stats.items_dropped       # int — items dropped by pipelines
stats.bytes_downloaded    # int — total bytes downloaded
stats.start_time          # float — unix timestamp
stats.finish_time         # float — unix timestamp
stats.elapsed             # float — seconds (property)
stats.rps                 # float — requests per second (property)
```

---

## Crawl Lifecycle

```
engine.run()
  │
  ├── open components
  │     ├── downloader.open()
  │     ├── pipeline_manager.open_spider(spider)
  │     └── spider.open_spider()
  │
  ├── seed scheduler with spider.start_requests()
  │
  ├── loop (async concurrent):
  │     ├── scheduler.next_request()
  │     ├── middleware.process_request()
  │     ├── downloader.fetch()
  │     ├── middleware.process_response()
  │     ├── spider.parse()  (async generator)
  │     │     ├── yield Request → scheduler.enqueue()
  │     │     └── yield Item → pipeline_manager.process_item()
  │     │                            └── exporter.export_item()
  │     └── (repeat until queue empty and no in-flight requests)
  │
  └── close components
        ├── exporter.close()
        ├── pipeline_manager.close_spider(spider)
        ├── spider.close_spider()
        ├── downloader.close()
        └── scheduler.close()
```

---

## Stopping a Crawl

```python
import asyncio
import bitscrape
from bitscrape import Engine

async def main():
    engine = Engine(spider=MySpider())
    task   = asyncio.create_task(engine.run())

    # Stop after 30 seconds
    await asyncio.sleep(30)
    task.cancel()

    try:
        stats = await task
    except asyncio.CancelledError:
        print("Crawl cancelled")

asyncio.run(main())
```

---

## Embedding in a Larger Application

```python
from fastapi import FastAPI
import bitscrape
from bitscrape import Engine

app = FastAPI()

@app.post("/crawl/{spider_name}")
async def start_crawl(spider_name: str):
    spider_map = {"quotes": QuotesSpider, "books": BooksSpider}
    cls = spider_map.get(spider_name)
    if not cls:
        return {"error": "Unknown spider"}

    engine = Engine(spider=cls())
    stats  = await engine.run()
    return {
        "items":   stats.items_scraped,
        "elapsed": stats.elapsed,
        "rps":     stats.rps,
    }
```
