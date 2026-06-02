"""
Bitscrape — modern async web scraping framework.

Everything you need is available from a single import:

    import bitscrape

    class MySpider(bitscrape.Spider):
        name = "myspider"
        start_urls = ["https://example.com"]

        async def parse(self, response):
            yield bitscrape.Item(
                title=response.css("h1::text").get(),
                url=response.url,
            )

    bitscrape.run(MySpider)

Or with destructured imports (Scrapy-style):

    from bitscrape import Spider, Item, Field, run
    from bitscrape import Request, Response, Settings
    from bitscrape import Engine, FormRequest
    from bitscrape import LoggingPipeline, ValidationPipeline, DedupPipeline, PostgresPipeline
    from bitscrape import JSONLExporter, JSONExporter, CSVExporter, XMLExporter
    from bitscrape import UserAgentMiddleware, RobotsMiddleware, CookieMiddleware
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any, Type

# ── Core models ────────────────────────────────────────────────────────────
from bitscrape.core.models import BaseItem, CrawlStats, Request, Response
from bitscrape.core.settings import Settings
from bitscrape.core.spider import Spider

# ── Engine ─────────────────────────────────────────────────────────────────
from bitscrape.engine import Engine

# ── Parser / selectors ─────────────────────────────────────────────────────
from bitscrape.parser.selector import NodeSelector, ParsedResponse, SelectorList

# ── Pipelines ──────────────────────────────────────────────────────────────
from bitscrape.pipeline.pipelines import (
    BasePipeline,
    DedupPipeline,
    DropItem,
    LoggingPipeline,
    PipelineManager,
    PostgresPipeline,
    ValidationPipeline,
)

# ── Exporters ──────────────────────────────────────────────────────────────
from bitscrape.exporters.feed import (
    BaseExporter,
    CSVExporter,
    JSONExporter,
    JSONLExporter,
    XMLExporter,
    get_exporter,
)

# ── Middleware ─────────────────────────────────────────────────────────────
from bitscrape.middleware.middleware import (
    BaseMiddleware,
    CookieMiddleware,
    MiddlewareManager,
    RobotsMiddleware,
    UserAgentMiddleware,
)

# ── Scheduler ──────────────────────────────────────────────────────────────
from bitscrape.scheduler.scheduler import MemoryQueue, RedisQueue, Scheduler

# ── Convenient aliases ─────────────────────────────────────────────────────

# ``Item`` — short alias for BaseItem (like Scrapy's scrapy.Item)
Item = BaseItem

# ``Field`` — returns a Pydantic Field (covers scrapy.Field use-case)
from pydantic import Field

# ``FormRequest`` — a Request pre-configured for form submission
class FormRequest(Request):
    """
    Convenience subclass for POST form submissions.

    Example::

        yield bitscrape.FormRequest(
            url="https://example.com/login",
            formdata={"user": "john", "pass": "secret"},
            callback="parse_after_login",
        )
    """
    method: str = "POST"
    formdata: dict[str, str] = {}

    def model_post_init(self, __context: Any) -> None:
        if self.formdata and not self.body:
            from urllib.parse import urlencode
            encoded = urlencode(self.formdata).encode()
            object.__setattr__(self, "body", encoded)
            headers = {**self.headers, "Content-Type": "application/x-www-form-urlencoded"}
            object.__setattr__(self, "headers", headers)


# ── Top-level ``run()`` helper ─────────────────────────────────────────────

def run(
    spider_cls: Type[Spider],
    *,
    output: str | None = None,
    fmt: str = "jsonl",
    settings: Settings | None = None,
    pipelines: list[BasePipeline] | None = None,
    middlewares: list[BaseMiddleware] | None = None,
    log_level: str = "INFO",
) -> CrawlStats:
    """
    Run a spider with one function call — no boilerplate needed.

    Usage::

        import bitscrape

        class MySpider(bitscrape.Spider):
            name = "my"
            start_urls = ["https://example.com"]

            async def parse(self, response):
                yield {"title": response.css("h1::text").get()}

        stats = bitscrape.run(MySpider, output="data.jsonl")
        print(f"Scraped {stats.items_scraped} items")

    Args:
        spider_cls:  Your Spider subclass (not an instance).
        output:      File path to write results to (e.g. "data.jsonl", "data.csv").
        fmt:         Export format — "jsonl" | "json" | "csv" | "xml".
        settings:    Optional Settings instance (uses defaults if omitted).
        pipelines:   Optional list of pipeline instances.
        middlewares: Optional list of middleware instances.
        log_level:   Logging level string (default "INFO").
    """
    logging.basicConfig(
        level=getattr(logging, log_level.upper(), logging.INFO),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )

    cfg = settings or Settings()
    spider = spider_cls(settings=cfg)

    exporter = get_exporter(fmt, output) if output else None

    mws: list[BaseMiddleware] = middlewares or [
        UserAgentMiddleware(),
        CookieMiddleware(),
    ]
    if cfg.robotstxt_obey:
        mws.insert(0, RobotsMiddleware())

    engine = Engine(
        spider=spider,
        settings=cfg,
        pipelines=pipelines or [],
        middlewares=mws,
        exporter=exporter,
    )

    return asyncio.run(engine.run())


# ── Version ────────────────────────────────────────────────────────────────
__version__ = "0.1.0"

__all__ = [
    # Core
    "Spider",
    "Item",
    "BaseItem",
    "Field",
    "Request",
    "FormRequest",
    "Response",
    "CrawlStats",
    "Settings",
    # Engine & runner
    "Engine",
    "run",
    # Parser
    "ParsedResponse",
    "SelectorList",
    "NodeSelector",
    # Pipelines
    "BasePipeline",
    "LoggingPipeline",
    "ValidationPipeline",
    "DedupPipeline",
    "PostgresPipeline",
    "PipelineManager",
    "DropItem",
    # Exporters
    "BaseExporter",
    "JSONLExporter",
    "JSONExporter",
    "CSVExporter",
    "XMLExporter",
    "get_exporter",
    # Middleware
    "BaseMiddleware",
    "UserAgentMiddleware",
    "RobotsMiddleware",
    "CookieMiddleware",
    "MiddlewareManager",
    # Scheduler
    "Scheduler",
    "MemoryQueue",
    "RedisQueue",
]
