"""
Bitscrape Factory
=================

The single assembly point that turns a ``Settings`` object into a fully
wired ``Engine`` -- middleware stack, plugins, and observability all
selected automatically from config toggles, instead of every user
hand-assembling several subsystems (which is what the CLI was doing ad hoc,
and what a library user would otherwise have to copy).

This is deliberately NOT a new architectural pattern (no event bus, no
actor runtime, no hexagonal ports-and-adapters ceremony) -- it's a plain
factory function over the dependency injection the codebase already uses
(``Scheduler.from_settings()``, `Settings` toggles selecting backends). The
practical payoff is real: the same spider file runs against
``Settings()`` (in-memory queue, single process) or
``Settings(scheduler_use_redis=True, distributed_throttle_enabled=True)``
(Redis-backed, safe across a fleet of worker processes) with zero code
changes in the spider itself -- "simple for beginners, powerful for
experts" achieved through configuration, not new abstraction layers.

    from bitscrape.factory import build_engine
    from bitscrape.core.settings import Settings

    engine = await build_engine(MySpider(), Settings(scheduler_use_redis=True))
    stats = await engine.run()
"""

from __future__ import annotations

import logging
from typing import Any

from bitscrape.core.settings import Settings
from bitscrape.core.spider import Spider
from bitscrape.engine import Engine
from bitscrape.exporters.feed import BaseExporter
from bitscrape.middleware.middleware import (
    BaseMiddleware,
    CookieMiddleware,
    DistributedThrottleMiddleware,
    MetaRobotsMiddleware,
    ProxyMiddleware,
    RobotsMiddleware,
    SessionPoolMiddleware,
    UserAgentMiddleware,
)
from bitscrape.plugins import BasePlugin, PluginManager

logger = logging.getLogger(__name__)


def build_middlewares(settings: Settings, redis_client: Any = None) -> list[BaseMiddleware]:
    """
    Assembles the middleware stack from Settings toggles, in a fixed,
    sensible order: UserAgent -> Proxy (if configured) -> Session/Cookie ->
    DistributedThrottle (if enabled) -> MetaRobots -> Robots (if obeyed).

    Exposed as a standalone function (not just an internal detail of
    ``build_engine``) so you can inspect or extend the stack without
    re-implementing the selection logic.
    """
    middlewares: list[BaseMiddleware] = [UserAgentMiddleware()]

    if settings.proxies:
        middlewares.append(
            ProxyMiddleware(proxies=list(settings.proxies), rotate=settings.proxy_rotate)
        )

    if settings.session_pool_size > 1:
        middlewares.append(
            SessionPoolMiddleware(
                pool_size=settings.session_pool_size,
                rotate_every=settings.session_rotate_every,
            )
        )
    else:
        middlewares.append(CookieMiddleware())

    if settings.distributed_throttle_enabled:
        if redis_client is None:
            logger.warning(
                "distributed_throttle_enabled=True but no Redis client is available "
                "(set scheduler_use_redis=True, or pass redis_client explicitly to "
                "build_engine()) -- skipping DistributedThrottleMiddleware."
            )
        else:
            middlewares.append(DistributedThrottleMiddleware(redis_client))

    middlewares.append(MetaRobotsMiddleware())

    if settings.robotstxt_obey:
        middlewares.append(RobotsMiddleware())

    return middlewares


class _MetricsPlugin(BasePlugin):
    """Bridges Engine lifecycle events to CrawlMetrics + a live /metrics
    endpoint. Internal to build_engine(); constructed with the metrics
    port already bound."""

    def __init__(self, metrics: Any, port: int) -> None:
        self._metrics = metrics
        self._port = port
        self._runner: Any = None

    async def spider_opened(self, spider: Any) -> None:
        from bitscrape.observability import serve_metrics

        self._runner = await serve_metrics(self._metrics, port=self._port)

    async def response_received(self, request: Any, response: Any, spider: Any) -> None:
        self._metrics.record_request(status=str(response.status))

    async def item_scraped(self, item: Any, spider: Any) -> None:
        self._metrics.record_item_scraped()

    async def item_dropped(self, item: Any, exception: Exception, spider: Any) -> None:
        self._metrics.record_item_dropped()

    async def error(self, request: Any, exception: Exception, spider: Any) -> None:
        self._metrics.record_error(error_type=type(exception).__name__)

    async def spider_closed(self, spider: Any, reason: str) -> None:
        if self._runner is not None:
            await self._runner.cleanup()


async def build_engine(
    spider: Spider,
    settings: Settings | None = None,
    exporter: BaseExporter | None = None,
    redis_client: Any = None,
    plugin_manager: PluginManager | None = None,
) -> Engine:
    """
    Builds a fully-wired Engine from a Settings object. This is the one
    function most users need -- everything else in this module is the
    internals it calls.

    ``redis_client``: if you already have a ``redis.asyncio.Redis``
    instance (e.g. shared with other parts of your application), pass it
    here to reuse it for ``DistributedThrottleMiddleware``; otherwise one
    is created from ``settings.redis_url`` automatically when needed.

    Automatically wires, purely from ``settings`` toggles:
      - Proxy rotation, session pooling, robots.txt + meta-robots handling
      - Distributed per-domain throttling (needs Redis)
      - A live StatsMonitor (``monitoring_enabled``)
      - Real Prometheus metrics (``metrics_enabled``)
    """
    settings = settings or Settings()

    needs_redis = settings.scheduler_use_redis or settings.distributed_throttle_enabled
    if needs_redis and redis_client is None:
        import redis.asyncio as aioredis

        redis_client = aioredis.from_url(settings.redis_url, decode_responses=False)

    middlewares = build_middlewares(settings, redis_client=redis_client)
    pm = plugin_manager or PluginManager()

    engine = Engine(
        spider=spider,
        settings=settings,
        middlewares=middlewares,
        exporter=exporter,
        plugin_manager=pm,
    )

    if settings.monitoring_enabled:
        from bitscrape.monitoring import StatsMonitor

        monitor = StatsMonitor(stats_getter=lambda: engine.stats, port=settings.monitoring_port)
        pm.register_plugin(monitor.as_plugin())

    if settings.metrics_enabled:
        from bitscrape.observability import CrawlMetrics

        metrics = CrawlMetrics()
        pm.register_plugin(_MetricsPlugin(metrics, port=settings.metrics_port))

    return engine
