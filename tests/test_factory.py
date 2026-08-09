"""
Tests for bitscrape.factory.build_engine() -- the core claim being tested is
"the same spider runs unchanged from a laptop to a distributed setup,"
verified by running the SAME spider class through build_engine() with
different Settings (in-memory vs Redis-backed) against a real local server.
"""

from __future__ import annotations

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from bitscrape.core.settings import Settings
from bitscrape.core.spider import Spider
from bitscrape.factory import build_engine, build_middlewares
from bitscrape.middleware.middleware import (
    CookieMiddleware,
    DistributedThrottleMiddleware,
    MetaRobotsMiddleware,
    ProxyMiddleware,
    RobotsMiddleware,
    SessionPoolMiddleware,
    UserAgentMiddleware,
)

REDIS_URL = "redis://127.0.0.1:6390/0"


class _EchoSpider(Spider):
    name = "factory_demo"
    start_urls = []

    async def parse(self, response):
        yield {"title": "hello", "status": response.status}


# ---------------------------------------------------------------------------
# build_middlewares -- selection logic
# ---------------------------------------------------------------------------


def test_default_settings_produce_minimal_stack():
    settings = Settings()
    middlewares = build_middlewares(settings)
    types = [type(m) for m in middlewares]
    assert UserAgentMiddleware in types
    assert CookieMiddleware in types
    assert RobotsMiddleware in types
    assert MetaRobotsMiddleware in types
    assert ProxyMiddleware not in types
    assert SessionPoolMiddleware not in types
    assert DistributedThrottleMiddleware not in types


def test_proxies_configured_adds_proxy_middleware():
    settings = Settings(proxies=["http://proxy1:8080", "http://proxy2:8080"])
    middlewares = build_middlewares(settings)
    assert any(isinstance(m, ProxyMiddleware) for m in middlewares)


def test_no_proxies_configured_skips_proxy_middleware():
    settings = Settings(proxies=[])
    middlewares = build_middlewares(settings)
    assert not any(isinstance(m, ProxyMiddleware) for m in middlewares)


def test_session_pool_size_over_one_uses_session_pool_middleware():
    settings = Settings(session_pool_size=3)
    middlewares = build_middlewares(settings)
    types = [type(m) for m in middlewares]
    assert SessionPoolMiddleware in types
    assert CookieMiddleware not in types


def test_session_pool_size_one_uses_plain_cookie_middleware():
    settings = Settings(session_pool_size=1)
    middlewares = build_middlewares(settings)
    types = [type(m) for m in middlewares]
    assert CookieMiddleware in types
    assert SessionPoolMiddleware not in types


def test_robotstxt_obey_false_skips_robots_middleware():
    settings = Settings(robotstxt_obey=False)
    middlewares = build_middlewares(settings)
    assert not any(isinstance(m, RobotsMiddleware) for m in middlewares)


def test_distributed_throttle_without_redis_client_skips_gracefully(caplog):
    import logging

    settings = Settings(distributed_throttle_enabled=True)
    with caplog.at_level(logging.WARNING):
        middlewares = build_middlewares(settings, redis_client=None)
    assert not any(isinstance(m, DistributedThrottleMiddleware) for m in middlewares)
    assert any("skipping DistributedThrottleMiddleware" in r.getMessage() for r in caplog.records)


def test_distributed_throttle_with_redis_client_is_included():
    settings = Settings(distributed_throttle_enabled=True)
    fake_redis = object()
    middlewares = build_middlewares(settings, redis_client=fake_redis)
    assert any(isinstance(m, DistributedThrottleMiddleware) for m in middlewares)


# ---------------------------------------------------------------------------
# build_engine -- end-to-end, real local server
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_build_engine_local_in_memory_mode_crawls_successfully():
    """The 'laptop' end of the claim: default Settings, in-memory queue,
    single process."""

    async def handler(request: web.Request) -> web.Response:
        return web.Response(text="<html>ok</html>")

    app = web.Application()
    app.router.add_get("/page", handler)
    server = TestServer(app)
    await server.start_server()
    try:
        url = f"http://{server.host}:{server.port}/page"
        spider = _EchoSpider()
        spider.start_urls = [url]

        engine = await build_engine(spider, Settings(robotstxt_obey=False))
        stats = await engine.run()

        assert stats.items_scraped == 1
        assert stats.requests_failed == 0
    finally:
        await server.close()


@pytest.mark.asyncio
async def test_build_engine_distributed_mode_same_spider_class():
    """The 'distributed cluster' end of the claim: SAME spider class,
    Redis-backed scheduler + distributed throttle, still crawls
    successfully against the same real server."""

    async def handler(request: web.Request) -> web.Response:
        return web.Response(text="<html>ok</html>")

    app = web.Application()
    app.router.add_get("/page2", handler)
    server = TestServer(app)
    await server.start_server()
    try:
        url = f"http://{server.host}:{server.port}/page2"
        spider = _EchoSpider()
        spider.start_urls = [url]

        settings = Settings(
            robotstxt_obey=False,
            scheduler_use_redis=True,
            redis_url=REDIS_URL,
            distributed_throttle_enabled=True,
        )
        engine = await build_engine(spider, settings)

        assert any(
            isinstance(m, DistributedThrottleMiddleware)
            for m in engine._middleware_manager._middlewares
        )

        stats = await engine.run()
        assert stats.items_scraped == 1
        assert stats.requests_failed == 0
    finally:
        await server.close()


@pytest.mark.asyncio
async def test_build_engine_with_monitoring_enabled_serves_live_stats():
    async def handler(request: web.Request) -> web.Response:
        return web.Response(text="<html>ok</html>")

    app = web.Application()
    app.router.add_get("/page3", handler)
    server = TestServer(app)
    await server.start_server()
    try:
        url = f"http://{server.host}:{server.port}/page3"
        spider = _EchoSpider()
        spider.start_urls = [url]

        settings = Settings(robotstxt_obey=False, monitoring_enabled=True, monitoring_port=18770)
        engine = await build_engine(spider, settings)
        stats = await engine.run()
        assert stats.items_scraped == 1
    finally:
        await server.close()


@pytest.mark.asyncio
async def test_build_engine_with_metrics_enabled_completes_crawl_cleanly():
    async def handler(request: web.Request) -> web.Response:
        return web.Response(text="<html>ok</html>")

    app = web.Application()
    app.router.add_get("/page4", handler)
    server = TestServer(app)
    await server.start_server()
    try:
        url = f"http://{server.host}:{server.port}/page4"
        spider = _EchoSpider()
        spider.start_urls = [url]

        settings = Settings(robotstxt_obey=False, metrics_enabled=True, metrics_port=19101)
        engine = await build_engine(spider, settings)
        stats = await engine.run()
        assert stats.items_scraped == 1
    finally:
        await server.close()


@pytest.mark.asyncio
async def test_build_engine_uses_default_settings_when_none_given():
    spider = _EchoSpider()
    spider.start_urls = []
    engine = await build_engine(spider, settings=None)
    assert engine._settings is not None
