# ⚡ Bitscrape

> A modern, production-grade async web scraping framework — smarter, faster, and leaner than the alternatives.

## Features at a glance

| Feature | Bitscrape |
|---|---|
| Concurrency | `asyncio` — thousands of concurrent requests |
| Type safety | Pydantic v2 models for **everything** |
| JS rendering | Built-in Playwright support |
| Workflow | LangGraph state machine (no LLMs) |
| Storage | PostgreSQL/Supabase via asyncpg |
| Distributed | Redis queue + multi-worker |
| Exports | JSONL · JSON · CSV · XML |
| CLI | `bitscrape crawl`, `startproject`, `genspider` |

---

## Install

```bash
pip install bitscrape
# With Playwright support:
pip install "bitscrape[playwright]"
playwright install chromium
```

---

## Quickstart (5 minutes)

### 1. Define items & a spider

```python
# spiders/quotes.py
from bitscrape import Spider, BaseItem
from bitscrape.parser.selector import ParsedResponse

class QuoteItem(BaseItem):
    text: str
    author: str

class QuotesSpider(Spider):
    name = "quotes"
    start_urls = ["https://quotes.toscrape.com/"]

    async def parse(self, response: ParsedResponse):
        for q in response.css("div.quote"):
            yield QuoteItem(
                text=q.css("span.text::text").get(default=""),
                author=q.css("small.author::text").get(default=""),
            )
        nxt = response.css("li.next a::attr(href)").get()
        if nxt:
            yield self.follow(f"https://quotes.toscrape.com{nxt}")
```

### 2. Run it

```bash
bitscrape crawl spiders/quotes.py -o quotes.jsonl
```

### 3. Programmatic use

```python
import asyncio
from bitscrape import Engine, Settings
from spiders.quotes import QuotesSpider

async def main():
    settings = Settings(concurrent_requests=16)
    stats = await Engine(QuotesSpider(settings=settings)).run()
    print(f"Scraped {stats.items_scraped} items in {stats.elapsed:.1f}s")

asyncio.run(main())
```

---

## Architecture

```
bitscrape/
├── core/
│   ├── models.py       # Request, Response, BaseItem, CrawlStats (Pydantic)
│   ├── settings.py     # Settings (pydantic-settings, env-driven)
│   └── spider.py       # Base Spider class
├── scheduler/
│   ├── scheduler.py    # MemoryQueue | RedisQueue + Scheduler
│   └── dupefilter.py   # Fingerprint-based deduplication
├── downloader/
│   └── downloader.py   # aiohttp async downloader + retry + Playwright
├── parser/
│   └── selector.py     # CSS/XPath via selectolax + ParsedResponse
├── middleware/
│   └── middleware.py   # UserAgent, Robots, Cookie + MiddlewareManager
├── pipeline/
│   └── pipelines.py    # BasePipeline, Logging, Validation, Dedup, Postgres
├── exporters/
│   └── feed.py         # JSONL, JSON, CSV, XML exporters
├── workflow/
│   └── graph.py        # LangGraph state machine (fetch→parse→pipeline)
├── cli/
│   └── main.py         # Click CLI: crawl, startproject, genspider, list
└── engine.py           # Central Engine — orchestrates everything
```

### Crawl flow

```
start_urls
    │
    ▼
[Scheduler] ──────────────────────────────────────────┐
    │  next_request()                                  │
    ▼                                                  │
[Middleware.process_request]                           │
    │                                                  │
    ▼                                                  │
[Downloader.fetch]  ◄── Playwright (use_playwright=True) │
    │                                                  │
    ▼                                                  │
[Middleware.process_response]                          │
    │                                                  │
    ▼                                                  │
[Spider.parse(ParsedResponse)]                         │
    │                                                  │
    ├── yield Request ─────────────────────────────────┘
    │
    └── yield Item
            │
            ▼
      [PipelineManager]
            │
            ▼
       [FeedExporter]  → file / stdout
            │
            ▼
       [Postgres/Supabase]  (PostgresPipeline)
```

---

## Settings reference

All settings can be overridden via environment variables prefixed `BITSCRAPE_`:

| Setting | Default | Env var |
|---|---|---|
| `concurrent_requests` | 16 | `BITSCRAPE_CONCURRENT_REQUESTS` |
| `download_delay` | 0.0 | `BITSCRAPE_DOWNLOAD_DELAY` |
| `download_timeout` | 30 | `BITSCRAPE_DOWNLOAD_TIMEOUT` |
| `scheduler_use_redis` | false | `BITSCRAPE_SCHEDULER_USE_REDIS` |
| `redis_url` | redis://localhost:6379/0 | `BITSCRAPE_REDIS_URL` |
| `database_url` | None | `BITSCRAPE_DATABASE_URL` |
| `robotstxt_obey` | true | `BITSCRAPE_ROBOTSTXT_OBEY` |
| `max_depth` | None | `BITSCRAPE_MAX_DEPTH` |
| `feed_uri` | None | `BITSCRAPE_FEED_URI` |
| `feed_format` | jsonl | `BITSCRAPE_FEED_FORMAT` |

---

## Distributed mode (Redis)

```bash
export BITSCRAPE_SCHEDULER_USE_REDIS=true
export BITSCRAPE_REDIS_URL=redis://redis-host:6379/0

# Start multiple workers — they all share the queue
bitscrape crawl myspider.py &
bitscrape crawl myspider.py &
bitscrape crawl myspider.py &
```

---

## JavaScript rendering (Playwright)

```python
class JSSpider(Spider):
    name = "js"
    start_urls = ["https://spa-example.com/"]

    async def parse(self, response: ParsedResponse):
        # response.body already contains fully rendered HTML
        yield {"title": response.css("h1::text").get()}

# Mark requests for Playwright:
yield self.follow("/dynamic-page", use_playwright=True)
```

---

## Testing

```bash
pip install "bitscrape[dev]"
pytest tests/ -v --cov=bitscrape
```

---

## Roadmap

- [ ] Prometheus metrics exporter
- [ ] Supabase Storage for media files  
- [ ] Autothrottle middleware
- [ ] Built-in proxy rotation
- [ ] Dashboard UI (FastAPI + htmx)
- [ ] Kubernetes Helm chart

---

## License

MIT © 2024 Bitscrape Contributors
