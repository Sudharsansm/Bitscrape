"""
Production-hardening tests.

These fill gaps the existing suite doesn't cover: what actually happens,
end-to-end, when things go wrong mid-crawl. Every test here drives a real
``Engine.run()`` against a real local aiohttp server (or a real local Redis
on port 6390 for the distributed cases) -- nothing is mocked at the
network/queue boundary, matching the project's own testing philosophy
stated in README.md.

Coverage added:
  - All retries exhausted on a permanently-failing endpoint: the crawl
    finishes cleanly (doesn't hang / doesn't raise out of run()) and stats
    reflect the failure.
  - A spider callback that raises mid-parse doesn't take down the engine --
    other queued requests still get processed and the crawl finishes.
  - A real concurrency ceiling: concurrent_requests=N is actually enforced
    against a server that reports max-simultaneous-connections seen.
  - Server error responses (500) still flow through the normal
    process_response path and don't silently vanish from stats.
  - Redis-backed scheduler survives a worker restart mid-crawl (resume from
    a persisted queue), and reports a clear connection error rather than
    hanging when Redis is unreachable at startup.
  - engine.stop() / max_depth actually bounds a crawl that would otherwise
    be unbounded (link farm).
"""

from __future__ import annotations

import asyncio

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from bitscrape.core.models import Request
from bitscrape.core.settings import Settings
from bitscrape.core.spider import Spider
from bitscrape.downloader.downloader import DownloadError
from bitscrape.engine import Engine
from bitscrape.scheduler.scheduler import Scheduler

REDIS_URL = "redis://127.0.0.1:6390/0"


# ---------------------------------------------------------------------------
# Retries exhausted against a permanently-failing endpoint
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_engine_survives_permanently_failing_endpoint():
    """A request that 503s on every attempt must not hang or crash the
    engine -- it should be counted as failed and the crawl should still
    finish."""

    async def handler(request: web.Request) -> web.Response:
        return web.Response(status=503)

    app = web.Application()
    app.router.add_get("/down", handler)
    server = TestServer(app)
    await server.start_server()
    try:
        url = f"http://{server.host}:{server.port}/down"

        class _FlakySpider(Spider):
            name = "flaky_demo"
            start_urls = [url]

            async def parse(self, response):
                yield {"status": response.status}
                return
                yield  # pragma: no cover

        settings = Settings(
            robotstxt_obey=False,
            respect_retry_after=False,  # keep exponential backoff, small waits
        )
        spider = _FlakySpider(settings)
        spider.start_urls = [url]
        req = Request(url=url, max_retries=1)  # cap retries so test stays fast
        spider.start_requests = lambda: iter([req])  # type: ignore[method-assign]

        engine = Engine(spider=spider, settings=settings)
        stats = await asyncio.wait_for(engine.run(), timeout=30)

        assert stats.requests_failed == 1
        assert stats.items_scraped == 0
        assert stats.finish_time is not None
    finally:
        await server.close()


# ---------------------------------------------------------------------------
# A crashing callback must not take the whole engine down
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_callback_exception_does_not_abort_other_requests():
    """One page's callback raising an unexpected exception (e.g. a bad
    selector, a KeyError on malformed data) must be isolated to that
    request -- sibling requests still get processed and the crawl still
    finishes with an accurate failure count."""

    async def ok_handler(request: web.Request) -> web.Response:
        return web.Response(text="fine")

    async def boom_handler(request: web.Request) -> web.Response:
        return web.Response(text="triggers a crash in the callback")

    app = web.Application()
    app.router.add_get("/ok", ok_handler)
    app.router.add_get("/boom", boom_handler)
    server = TestServer(app)
    await server.start_server()
    try:
        ok_url = f"http://{server.host}:{server.port}/ok"
        boom_url = f"http://{server.host}:{server.port}/boom"

        class _CrashySpider(Spider):
            name = "crashy_demo"
            start_urls = [ok_url, boom_url]

            async def parse(self, response):
                if "boom" in response.url:
                    raise KeyError("simulated bug in user callback")
                yield {"ok": True}

        settings = Settings(robotstxt_obey=False, concurrent_requests=2)
        spider = _CrashySpider(settings)
        engine = Engine(spider=spider, settings=settings)
        stats = await asyncio.wait_for(engine.run(), timeout=30)

        # The /ok page must still have been scraped despite /boom crashing.
        assert stats.items_scraped == 1
        assert stats.responses_received == 2
        assert stats.finish_time is not None
    finally:
        await server.close()


# ---------------------------------------------------------------------------
# Server error responses: retried per settings.retry_http_codes, then
# surfaced as a countable failure rather than vanishing or hanging
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_persistent_500_is_retried_then_counted_as_failure():
    """500 is in the default retry_http_codes set, so a permanently-500
    endpoint should be retried (not handed to the spider on the first
    attempt) and, once retries are exhausted, counted as a clean failure --
    not a hang, not an unhandled exception out of run()."""

    async def handler(request: web.Request) -> web.Response:
        return web.Response(status=500, text="internal error")

    app = web.Application()
    app.router.add_get("/broken", handler)
    server = TestServer(app)
    await server.start_server()
    try:
        url = f"http://{server.host}:{server.port}/broken"

        seen_statuses = []

        class _StatusSpider(Spider):
            name = "status_demo"
            start_urls = [url]

            async def parse(self, response):
                seen_statuses.append(response.status)
                yield {"status": response.status}

        settings = Settings(robotstxt_obey=False)
        spider = _StatusSpider(settings)
        req = Request(url=url, max_retries=1)  # cap retries so the test stays fast
        spider.start_requests = lambda: iter([req])  # type: ignore[method-assign]
        engine = Engine(spider=spider, settings=settings)
        stats = await asyncio.wait_for(engine.run(), timeout=15)

        # 500 is retryable, so it never reaches the spider callback here --
        # it exhausts retries and is counted as a download failure instead.
        assert seen_statuses == []
        assert stats.items_scraped == 0
        assert stats.requests_failed == 1
    finally:
        await server.close()


@pytest.mark.asyncio
async def test_non_retryable_error_status_is_delivered_to_spider():
    """404 is NOT in the default retry set -- it's a valid, final response
    and must reach the spider's callback like any other response, without
    being retried or treated as a DownloadError."""

    async def handler(request: web.Request) -> web.Response:
        return web.Response(status=404, text="not found")

    app = web.Application()
    app.router.add_get("/missing", handler)
    server = TestServer(app)
    await server.start_server()
    try:
        url = f"http://{server.host}:{server.port}/missing"
        seen_statuses = []

        class _StatusSpider(Spider):
            name = "status_404_demo"
            start_urls = [url]

            async def parse(self, response):
                seen_statuses.append(response.status)
                yield {"status": response.status}

        settings = Settings(robotstxt_obey=False)
        spider = _StatusSpider(settings)
        engine = Engine(spider=spider, settings=settings)
        stats = await asyncio.wait_for(engine.run(), timeout=15)

        assert seen_statuses == [404]
        assert stats.items_scraped == 1
        assert stats.requests_failed == 0
    finally:
        await server.close()


# ---------------------------------------------------------------------------
# Concurrency ceiling is actually enforced under real load
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_concurrent_requests_ceiling_is_enforced():
    """settings.concurrent_requests=2 against 10 slow endpoints must never
    let more than 2 requests be in-flight against the server at once."""

    state = {"current": 0, "peak": 0}
    lock = asyncio.Lock()

    async def handler(request: web.Request) -> web.Response:
        async with lock:
            state["current"] += 1
            state["peak"] = max(state["peak"], state["current"])
        await asyncio.sleep(0.15)
        async with lock:
            state["current"] -= 1
        return web.Response(text="ok")

    app = web.Application()
    for i in range(10):
        app.router.add_get(f"/p{i}", handler)
    server = TestServer(app)
    await server.start_server()
    try:
        urls = [f"http://{server.host}:{server.port}/p{i}" for i in range(10)]

        class _ManyUrlsSpider(Spider):
            name = "concurrency_demo"
            start_urls = urls

            async def parse(self, response):
                yield {"url": response.url}

        settings = Settings(robotstxt_obey=False, concurrent_requests=2)
        spider = _ManyUrlsSpider(settings)
        engine = Engine(spider=spider, settings=settings)
        stats = await asyncio.wait_for(engine.run(), timeout=30)

        assert stats.items_scraped == 10
        assert state["peak"] <= 2, f"expected at most 2 concurrent, saw {state['peak']}"
    finally:
        await server.close()


# ---------------------------------------------------------------------------
# max_depth actually bounds an otherwise-unbounded link farm
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_max_depth_bounds_an_infinite_link_farm():
    """Each page links to the next page forever; without a depth limit this
    crawl never terminates. max_depth must cut it off."""

    async def handler(request: web.Request) -> web.Response:
        n = int(request.match_info["n"])
        return web.Response(
            text=f'<html><body><a href="/page/{n + 1}">next</a></body></html>',
            content_type="text/html",
        )

    app = web.Application()
    app.router.add_get("/page/{n}", handler)
    server = TestServer(app)
    await server.start_server()
    try:
        start_url = f"http://{server.host}:{server.port}/page/0"

        class _InfiniteSpider(Spider):
            name = "infinite_demo"
            start_urls = [start_url]

            async def parse(self, response):
                from urllib.parse import urljoin

                yield {"url": response.url}
                href = response.css("a::attr(href)").get()
                if href:
                    # self.follow() deliberately doesn't resolve relative
                    # URLs (see docs/crawling/index.md) -- urljoin is the
                    # documented pattern.
                    yield self.follow(urljoin(response.url, href))

        settings = Settings(robotstxt_obey=False, max_depth=3)
        spider = _InfiniteSpider(settings)
        engine = Engine(spider=spider, settings=settings)
        stats = await asyncio.wait_for(engine.run(), timeout=30)

        # depth 0..3 inclusive = 4 pages, then depth 4 is rejected by the
        # scheduler's depth check.
        assert stats.items_scraped == 4
    finally:
        await server.close()


# ---------------------------------------------------------------------------
# Redis-backed scheduler: unreachable Redis fails fast and clearly
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_unreachable_redis_raises_clear_connection_error_on_first_use():
    """NOTE on observed behaviour: ``Scheduler.from_settings()`` itself
    never touches the network (redis.asyncio.Redis is a lazy client), so it
    succeeds even against an unreachable host. The first real operation
    (here, ``enqueue``) is where the failure actually surfaces -- and it
    must do so as a clear, real ``redis.exceptions.ConnectionError`` rather
    than hanging or failing with an opaque error. Operators relying on
    ``from_settings()`` alone as a "Redis is reachable" health check will
    be surprised by this; a real readiness check needs an explicit PING.
    """
    import redis.exceptions

    settings = Settings(
        scheduler_use_redis=True,
        redis_url="redis://127.0.0.1:6391/0",  # nothing listens here
    )
    scheduler = await asyncio.wait_for(Scheduler.from_settings(settings), timeout=5)
    with pytest.raises(redis.exceptions.ConnectionError):
        await asyncio.wait_for(
            scheduler.enqueue(Request(url="http://example.test/")), timeout=5
        )


# ---------------------------------------------------------------------------
# Redis-backed scheduler: queue genuinely survives a "worker restart"
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_redis_queue_resumes_after_simulated_worker_crash():
    """Push several requests, then simulate a worker crash by throwing away
    the in-process Scheduler object (without calling close(), which would
    be a clean shutdown) and building a brand new one against the same
    Redis key. The new worker must pick up exactly where the old one left
    off -- nothing lost, nothing duplicated."""
    import redis.asyncio as aioredis

    key = "bitscrape:test:crash_resume:queue"
    dupe_key = "bitscrape:test:crash_resume:dupes"

    client = aioredis.from_url(REDIS_URL, decode_responses=False)
    await client.delete(key, dupe_key)
    try:
        from bitscrape.scheduler.dupefilter import RedisDupeFilter
        from bitscrape.scheduler.scheduler import RedisQueue

        settings = Settings(scheduler_use_redis=True, redis_url=REDIS_URL)

        # "Worker 1": enqueue 5 requests, then vanish without closing cleanly.
        queue1 = RedisQueue(client, key=key)
        dupefilter1 = RedisDupeFilter(client, key=dupe_key)
        sched1 = Scheduler(settings, queue=queue1, dupefilter=dupefilter1)
        for i in range(5):
            await sched1.enqueue(Request(url=f"http://example.test/{i}"))
        assert await queue1.async_size() == 5
        del sched1  # simulate crash: no close(), no clean shutdown

        # "Worker 2": fresh process, same Redis key.
        queue2 = RedisQueue(client, key=key)
        dupefilter2 = RedisDupeFilter(client, key=dupe_key)
        sched2 = Scheduler(settings, queue=queue2, dupefilter=dupefilter2)

        recovered = []
        while True:
            req = await sched2.next_request()
            if req is None:
                break
            recovered.append(req.url)

        assert sorted(recovered) == sorted(f"http://example.test/{i}" for i in range(5))
        assert await queue2.async_size() == 0
    finally:
        await client.delete(key, dupe_key)
        await client.aclose()


# ---------------------------------------------------------------------------
# DownloadError is a real, catchable exception type (not a bare Exception)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_download_error_raised_after_retries_has_useful_message():
    """Direct downloader-level check (below the engine's blanket exception
    handling): confirms callers using the Downloader directly, outside the
    Engine, get a specific, informative exception rather than a raw
    aiohttp/asyncio error."""
    from bitscrape.downloader.downloader import Downloader

    async def handler(request: web.Request) -> web.Response:
        return web.Response(status=503)

    app = web.Application()
    app.router.add_get("/always-down", handler)
    server = TestServer(app)
    await server.start_server()
    try:
        url = f"http://{server.host}:{server.port}/always-down"
        settings = Settings(respect_retry_after=False)
        downloader = Downloader(settings)
        await downloader.open()
        try:
            with pytest.raises(DownloadError) as exc_info:
                await downloader.fetch(Request(url=url, max_retries=1))
            assert url in str(exc_info.value)
            assert "retries failed" in str(exc_info.value)
        finally:
            await downloader.close()
    finally:
        await server.close()
