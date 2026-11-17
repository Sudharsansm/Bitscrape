"""Streaming results: async + sync, backpressure, early stop, custom_settings."""

from __future__ import annotations

import asyncio
import time
from typing import Any

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from bitscrape.core.settings import Settings
from bitscrape.core.spider import Spider
from bitscrape.streaming import CrawlStream, stream, stream_sync

N = 40


def _app(hits: list[int], delay: float = 0.03) -> web.Application:
    async def page(request: web.Request) -> web.Response:
        i = int(request.match_info["i"])
        hits.append(i)
        await asyncio.sleep(delay)
        nxt = f'<a href="/p/{i + 1}">n</a>' if i + 1 < N else ""
        return web.Response(text=f"<h1>{i}</h1>{nxt}", content_type="text/html")

    app = web.Application()
    app.router.add_get("/p/{i}", page)
    return app


def _spider_cls(base: str):
    class ChainSpider(Spider):
        name = "chain"
        start_urls = [f"{base}/p/0"]

        async def parse(self, response):
            yield {"n": int(response.css("h1::text").get())}
            nxt = response.css("a::attr(href)").get()
            if nxt:
                yield self.follow(nxt)

    return ChainSpider


def _settings(**kw: Any) -> Settings:
    return Settings(robotstxt_obey=False, concurrent_requests=1, **kw)


@pytest.mark.asyncio
async def test_items_arrive_while_the_crawl_is_still_running():
    hits: list[int] = []
    async with TestServer(_app(hits)) as srv:
        cls = _spider_cls(f"http://127.0.0.1:{srv.port}")
        async with CrawlStream(cls, settings=_settings()) as crawl:
            first = await crawl.__anext__()
            assert first == {"n": 0}
            assert crawl.engine is not None and crawl.engine.is_running  # crawl not finished yet
            assert len(hits) < N
            rest = [i async for i in crawl]
        assert [first, *rest] == [{"n": i} for i in range(N)]
        assert crawl.stats.items_scraped == N
        assert crawl.stats.finish_reason == "finished"


@pytest.mark.asyncio
async def test_backpressure_bounds_how_far_the_crawl_runs_ahead():
    hits: list[int] = []
    async with TestServer(_app(hits, delay=0.0)) as srv:
        cls = _spider_cls(f"http://127.0.0.1:{srv.port}")
        buffer = 3
        consumed = 0
        max_lead = 0
        async with CrawlStream(cls, settings=_settings(), buffer=buffer) as crawl:
            async for _ in crawl:
                consumed += 1
                await asyncio.sleep(0.03)  # slow consumer; the crawl itself is instant
                lead = crawl.stats.items_scraped - consumed
                max_lead = max(max_lead, lead)
        assert consumed == N
        # buffer + the item being handed over + one in the parse pipeline
        assert max_lead <= buffer + 2, f"crawl ran {max_lead} items ahead of the consumer"


@pytest.mark.asyncio
async def test_breaking_out_stops_the_crawl_early():
    hits: list[int] = []
    async with TestServer(_app(hits)) as srv:
        cls = _spider_cls(f"http://127.0.0.1:{srv.port}")
        got = []
        async with CrawlStream(cls, settings=_settings()) as crawl:
            async for item in crawl:
                got.append(item)
                if len(got) == 4:
                    break
        assert got == [{"n": i} for i in range(4)]
        assert len(hits) < N // 2, f"crawl kept going after the consumer left ({len(hits)} pages)"
        assert crawl.stats.finish_reason == "cancelled"


@pytest.mark.asyncio
async def test_stream_helper_function():
    hits: list[int] = []
    async with TestServer(_app(hits, delay=0.0)) as srv:
        cls = _spider_cls(f"http://127.0.0.1:{srv.port}")
        items = [i async for i in stream(cls, settings=_settings())]
        assert len(items) == N


@pytest.mark.asyncio
async def test_spider_errors_surface_to_the_consumer():
    class Broken(Spider):
        name = "broken"

        def start_requests(self):
            raise RuntimeError("seed failure")

    with pytest.raises(RuntimeError, match="seed failure"):
        async with CrawlStream(Broken, settings=_settings()) as crawl:
            async for _ in crawl:
                pass


@pytest.mark.asyncio
async def test_invalid_buffer_rejected():
    with pytest.raises(ValueError):
        CrawlStream(Spider, buffer=0)


def test_stream_sync_yields_items_and_supports_early_exit():
    import threading

    hits: list[int] = []
    loop = asyncio.new_event_loop()
    ready = threading.Event()
    holder: dict[str, Any] = {}

    def serve() -> None:
        asyncio.set_event_loop(loop)
        srv = TestServer(_app(hits, delay=0.01))
        loop.run_until_complete(srv.start_server())
        holder["port"] = srv.port
        holder["srv"] = srv
        ready.set()
        loop.run_forever()

    t = threading.Thread(target=serve, daemon=True)
    t.start()
    assert ready.wait(10)
    try:
        cls = _spider_cls(f"http://127.0.0.1:{holder['port']}")
        # full run
        items = list(stream_sync(cls, settings=_settings()))
        assert items == [{"n": i} for i in range(N)]
        # early exit
        hits.clear()
        t0 = time.perf_counter()
        seen = []
        for item in stream_sync(cls, settings=_settings()):
            seen.append(item)
            if len(seen) == 3:
                break
        assert seen == [{"n": i} for i in range(3)]
        assert time.perf_counter() - t0 < 5
        assert len(hits) < N
    finally:
        loop.call_soon_threadsafe(loop.stop)
        t.join(5)
