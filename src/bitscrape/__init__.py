"""
Bitscrape — modern async web scraping framework.

The fastest possible start (function-based spider, one import, one call):

    import bitscrape

    @bitscrape.spider(name="myspider", start_urls=["https://example.com"])
    async def parse(response):
        yield {"title": response.css("h1::text").get()}

    bitscrape.run(parse, output="data.jsonl")

Or a full class-based spider when you need more than one callback:

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

Both paths go through ``build_engine()`` internally, so every feature
driven by ``Settings`` — distributed crawling (Redis), proxy rotation,
session pooling, robots.txt/meta-robots handling, live monitoring,
Prometheus metrics — is available automatically without extra wiring:

    stats = bitscrape.run(
        MySpider,
        settings=bitscrape.Settings(
            scheduler_use_redis=True,
            distributed_throttle_enabled=True,
            monitoring_enabled=True,
        ),
    )

Everything else in the ecosystem (link analysis/PageRank, incremental
recrawl scheduling, URL canonicalization, hybrid BM25+vector search, entity
resolution, knowledge graphs, storage backends, the plugin system) is also
available from this single import — see each module's own docstring
(``bitscrape.link_analysis``, ``bitscrape.ranking``, etc.) for details, or
the top-level ``README.md`` / ``CHANGELOG.md`` for a full feature tour.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

# ── Pydantic helpers ───────────────────────────────────────────────────────
from pydantic import Field

# ── Core models ────────────────────────────────────────────────────────────
from bitscrape.core.models import BaseItem, CrawlStats, Request, Response
from bitscrape.core.settings import Settings
from bitscrape.core.spider import Spider

# ── Engine ─────────────────────────────────────────────────────────────────
from bitscrape.engine import Engine

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
    DistributedThrottleMiddleware,
    MetaRobotsMiddleware,
    MiddlewareManager,
    ProxyMiddleware,
    RobotsMiddleware,
    SessionPoolMiddleware,
    UserAgentMiddleware,
)

# ── Factory: one call to build a fully-wired Engine from Settings ─────────
from bitscrape.factory import build_engine, build_middlewares

# ── URL Frontier ───────────────────────────────────────────────────────────
from bitscrape.frontier import Frontier

# ── Link analysis (PageRank / HITS) ────────────────────────────────────────
from bitscrape.link_analysis import LinkGraph

# ── Incremental recrawling ─────────────────────────────────────────────────
from bitscrape.recrawl import PageHistory, RecrawlScheduler

# ── Canonicalization / dedup ───────────────────────────────────────────────
from bitscrape.canonicalize import (
    ContentFingerprint,
    RedirectLoopError,
    canonicalize_url,
    compute_fingerprint,
    is_near_duplicate,
    resolve_redirect_chain,
)

# ── Semantic ranking (BM25 + vectors + fusion) ─────────────────────────────
from bitscrape.ranking import (
    BM25Index,
    HybridSearcher,
    HybridSearchResult,
    VectorIndex,
    reciprocal_rank_fusion,
)

# ── Entity resolution ──────────────────────────────────────────────────────
from bitscrape.entity_resolution import EntityResolver, similarity

# ── Knowledge graph ─────────────────────────────────────────────────────────
from bitscrape.knowledge_graph import KnowledgeGraph, extract_entities

# ── Observability: metrics, tracing, structured logging, alerting ─────────
from bitscrape.observability import (
    AlertManager,
    CrawlMetrics,
    CrawlTracer,
    JSONLogFormatter,
)

# ── Live monitoring dashboard (local JSON/HTML stats feed) ─────────────────
from bitscrape.monitoring import StatsMonitor

# ── Plugin system ──────────────────────────────────────────────────────────
from bitscrape.plugins import (
    BasePlugin,
    BearerTokenAuthPlugin,
    PluginManager,
    StorageConnectorPlugin,
)

# ── Storage backends (SQLite/S3 fully supported; Postgres implemented but
#    unverified against a live server; Mongo/Elasticsearch are documented
#    stubs — see bitscrape.storage.backends for the exact status of each) ──
from bitscrape.storage.backends import (
    BaseStorageBackend,
    PostgresStorageBackend,
    S3StorageBackend,
    SQLiteStorageBackend,
)

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

# ── Scheduler ──────────────────────────────────────────────────────────────
from bitscrape.scheduler.scheduler import MemoryQueue, RedisQueue, Scheduler

# ── Convenient aliases ─────────────────────────────────────────────────────

# ``Item`` — short alias for BaseItem (like Scrapy's scrapy.Item)
Item = BaseItem


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
            headers = {
                **self.headers,
                "Content-Type": "application/x-www-form-urlencoded",
            }
            object.__setattr__(self, "headers", headers)


# ── Function-based spiders: the simplest possible entry point ──────────────


def spider(
    name: str, start_urls: list[str] | None = None, **class_attrs: Any
) -> Any:
    """
    Turns a plain async generator function into a full ``Spider`` class --
    the simplest possible way to define a spider when you don't need
    multiple callbacks or spider-level state.

    Usage::

        import bitscrape

        @bitscrape.spider(name="quotes", start_urls=["https://example.com"])
        async def parse(response):
            for q in response.css("div.quote"):
                yield {"text": q.css("span.text::text").get()}

        stats = bitscrape.run(parse)  # `parse` is now a Spider subclass

    The decorated function's signature is ``async def parse(response)`` --
    no ``self`` needed, since a function-based spider carries no instance
    state beyond what ``Settings``/``class_attrs`` provide. Extra keyword
    arguments (e.g. ``custom_settings={...}``) are set as class attributes,
    the same as they would be on a full ``class MySpider(Spider):`` body.

    For anything needing multiple callbacks, ``self.follow()``, or spider
    state across requests, define a full ``Spider`` subclass instead --
    this decorator is deliberately for the common simple case.
    """

    def decorator(func: Any) -> type[Spider]:
        async def _parse_method(self: Spider, response: Any) -> Any:
            async for item in func(response):
                yield item

        attrs: dict[str, Any] = {
            "name": name,
            "start_urls": list(start_urls or []),
            "parse": _parse_method,
            **class_attrs,
        }
        cls_name = (
            "".join(part.capitalize() for part in name.replace("-", "_").split("_"))
            + "Spider"
        )
        return type(cls_name, (Spider,), attrs)

    return decorator


# ── Top-level ``run()`` helper ─────────────────────────────────────────────


def run(
    spider_cls: type[Spider],
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

    Internally delegates to ``bitscrape.factory.build_engine()`` — the same
    function the CLI uses — so this one-liner gets every feature driven by
    ``Settings`` (meta-robots handling, distributed throttling, live
    monitoring, proxy rotation, etc.) automatically, rather than a separate,
    simplified wiring path that could silently drift out of sync with it.
    Pass ``middlewares`` explicitly only if you need to override the
    automatic selection.

    Args:
        spider_cls:  Your Spider subclass (not an instance).
        output:      File path to write results to (e.g. "data.jsonl", "data.csv").
        fmt:         Export format — "jsonl" | "json" | "csv" | "xml".
        settings:    Optional Settings instance (uses defaults if omitted).
        pipelines:   Optional list of pipeline instances.
        middlewares: Optional list of middleware instances (overrides the
                     automatic Settings-driven selection if provided).
        log_level:   Logging level string (default "INFO").
    """
    logging.basicConfig(
        level=getattr(logging, log_level.upper(), logging.INFO),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )

    from bitscrape.factory import build_engine

    cfg = settings or Settings()
    spider = spider_cls(settings=cfg)
    exporter = get_exporter(fmt, output) if output else None

    async def _build_and_run() -> CrawlStats:
        engine = await build_engine(
            spider,
            cfg,
            exporter=exporter,
        )
        if middlewares is not None:
            engine._middleware_manager = MiddlewareManager(middlewares)
        if pipelines:
            engine._pipeline_manager = PipelineManager(pipelines)
        return await engine.run()

    return asyncio.run(_build_and_run())


# ── Version ────────────────────────────────────────────────────────────────
def _resolve_version() -> str:
    """Reads the real installed distribution version instead of a hardcoded
    string that goes stale the moment the package is bumped (the exact bug
    fixed in the CLI's --version flag in an earlier release)."""
    from importlib.metadata import PackageNotFoundError
    from importlib.metadata import version as _pkg_version

    try:
        return _pkg_version("bitscrape")
    except PackageNotFoundError:
        return "0.0.0+unknown"


__version__ = _resolve_version()

__all__ = [
    # Core
    "Spider",
    "spider",
    "Item",
    "BaseItem",
    "Field",
    "Request",
    "FormRequest",
    "Response",
    "CrawlStats",
    "Settings",
    # Engine, runner & factory
    "Engine",
    "run",
    "build_engine",
    "build_middlewares",
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
    "MetaRobotsMiddleware",
    "CookieMiddleware",
    "SessionPoolMiddleware",
    "ProxyMiddleware",
    "DistributedThrottleMiddleware",
    "MiddlewareManager",
    # Scheduler
    "Scheduler",
    "MemoryQueue",
    "RedisQueue",
    # URL Frontier
    "Frontier",
    # Link analysis
    "LinkGraph",
    # Incremental recrawling
    "RecrawlScheduler",
    "PageHistory",
    # Canonicalization / dedup
    "canonicalize_url",
    "resolve_redirect_chain",
    "compute_fingerprint",
    "is_near_duplicate",
    "ContentFingerprint",
    "RedirectLoopError",
    # Semantic ranking
    "BM25Index",
    "VectorIndex",
    "HybridSearcher",
    "HybridSearchResult",
    "reciprocal_rank_fusion",
    # Entity resolution
    "EntityResolver",
    "similarity",
    # Knowledge graph
    "KnowledgeGraph",
    "extract_entities",
    # Observability
    "CrawlMetrics",
    "CrawlTracer",
    "JSONLogFormatter",
    "AlertManager",
    # Monitoring
    "StatsMonitor",
    # Plugins
    "PluginManager",
    "BasePlugin",
    "BearerTokenAuthPlugin",
    "StorageConnectorPlugin",
    # Storage backends
    "BaseStorageBackend",
    "SQLiteStorageBackend",
    "S3StorageBackend",
    "PostgresStorageBackend",
]
