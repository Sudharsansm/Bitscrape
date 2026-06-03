# Bitscrape Documentation

> ⚡ A modern, production-grade async web scraping framework for Python 3.11+

---

## Table of Contents

| Doc | Description |
|-----|-------------|
| [installation.md](installation.md) | Install via pip, uv, or from source |
| [quickstart.md](quickstart.md) | Build your first spider in 5 minutes |
| [spiders.md](spiders.md) | Spider API — writing, callbacks, following links |
| [selectors.md](selectors.md) | CSS and XPath selectors reference |
| [items.md](items.md) | Defining scraped data models with Pydantic |
| [settings.md](settings.md) | All settings and environment variables |
| [downloader.md](downloader.md) | HTTP downloader, retries, Playwright JS rendering |
| [scheduler.md](scheduler.md) | Request queue, deduplication, depth limits |
| [middleware.md](middleware.md) | Request/response middleware — built-in and custom |
| [pipelines.md](pipelines.md) | Item pipelines — validation, dedup, storage |
| [exporters.md](exporters.md) | Feed exporters — JSONL, JSON, CSV, XML |
| [engine.md](engine.md) | Engine internals and programmatic usage |
| [workflow.md](workflow.md) | LangGraph state machine orchestration |
| [cli.md](cli.md) | CLI reference — crawl, startproject, genspider |
| [distributed.md](distributed.md) | Redis-backed distributed crawling |
| [storage.md](storage.md) | PostgreSQL and Supabase storage |
| [publishing.md](publishing.md) | Publishing to PyPI — pip install bitscrape |
| [contributing.md](contributing.md) | How to contribute to Bitscrape |
| [faq.md](faq.md) | Frequently asked questions |
| [changelog.md](changelog.md) | Version history |

---

## What is Bitscrape?

Bitscrape is a Python web scraping framework designed for data engineers and
developers who need to collect large amounts of structured data reliably and
quickly.

It is inspired by Scrapy but built on modern Python:

- **Native asyncio** instead of Twisted
- **Pydantic v2** for type-safe data models
- **Built-in Playwright** for JavaScript-rendered pages
- **LangGraph** for structured crawl workflow orchestration
- **Single import API** — `import bitscrape`

---

## Quick Example

```python
import bitscrape

class QuotesSpider(bitscrape.Spider):
    name = "quotes"
    start_urls = ["https://quotes.toscrape.com/"]

    async def parse(self, response):
        for quote in response.css("div.quote"):
            yield {
                "text":   quote.css("span.text::text").get(),
                "author": quote.css("small.author::text").get(),
            }
        nxt = response.css("li.next a::attr(href)").get()
        if nxt:
            yield self.follow(f"https://quotes.toscrape.com{nxt}")

stats = bitscrape.run(QuotesSpider, output="quotes.jsonl")
print(f"Scraped {stats.items_scraped} items")
```

---

## Architecture Overview

```
start_urls
    │
    ▼
[Scheduler]  ──────────────────────────────────┐
    │  next_request()                           │
    ▼                                           │
[Middleware.process_request]                    │
    │                                           │
    ▼                                           │
[Downloader]  ←── Playwright (JS pages)        │
    │                                           │
    ▼                                           │
[Middleware.process_response]                   │
    │                                           │
    ▼                                           │
[Spider.parse(ParsedResponse)]                  │
    ├── yield Request ──────────────────────────┘
    └── yield Item
              │
              ▼
        [PipelineManager]
              │
              ▼
         [FeedExporter]  →  file / stdout
              │
              ▼
        [PostgreSQL / Supabase]
```
