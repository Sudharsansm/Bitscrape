"""Pause/resume, graceful stop, signals and the engine-loop upgrades."""

from __future__ import annotations

import asyncio
import json
import os
import signal
import sys
from typing import Any

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from bitscrape.checkpoint import Checkpoint
from bitscrape.core.models import Request
from bitscrape.core.settings import Settings
from bitscrape.core.spider import Spider
from bitscrape.engine import Engine
from bitscrape.plugins import PluginManager

N_PAGES = 30


def _make_app(
    hits: dict[str, int], delay: float = 0.02, state: dict[str, Any] | None = None
) -> web.Application:
    state = state if state is not None else {}

    async def page(request: web.Request) -> web.Response:
        i = int(request.match_info["i"])
        hits[str(i)] = hits.get(str(i), 0) + 1
        state["current"] = state.get("current", 0) + 1
        state["peak"] = max(state.get("peak", 0), state["current"])
        try:
            await asyncio.sleep(delay)
        finally:
            state["current"] -= 1
        nxt = [f"/p/{j}" for j in (2 * i + 1, 2 * i + 2) if j < N_PAGES]  # binary tree of pages
        links = "".join(f'<a href="{u}">n</a>' for u in nxt)
        return web.Response(
            text=f"<html><body><h1>page {i}</h1>{links}</body></html>", content_type="text/html"
        )

    async def boom(request: web.Request) -> web.Response:
        return web.Response(status=404, text="nope")

    app = web.Application()
    app.router.add_get("/p/{i}", page)
    app.router.add_get("/boom", boom)
    return app


class TreeSpider(Spider):
    name = "tree"

    def __init__(
        self, base: str, settings: Settings | None = None, pause_after: int | None = None
    ) -> None:
        super().__init__(settings=settings)
        self.start_urls = [f"{base}/p/0"]
        self.pause_after = pause_after
        self.engine: Engine | None = None
        self.seen_pages: list[int] = []

    async def parse(self, response):
        n = int(response.css("h1::text").get().split()[1])
        self.seen_pages.append(n)
        yield {"page": n}
        for href in response.css("a::attr(href)").getall():
            yield self.follow(href)
        if self.pause_after and len(self.seen_pages) >= self.pause_after and self.engine:
            self.engine.pause()


def _settings(tmp_path, **kw: Any) -> Settings:
    base = dict(
        robotstxt_obey=False,
        concurrent_requests=4,
        download_delay=0.0,
        jobdir=str(tmp_path / "job"),
        checkpoint_interval=0.0,
    )
    base.update(kw)
    return Settings(**base)


def _collect(engine: Engine) -> list[dict]:
    items: list[dict] = []
    pm = engine._plugins

    async def on_item(item, spider):
        items.append(dict(item))

    pm.on("item_scraped", on_item)
    return items


@pytest.mark.asyncio
async def test_pause_saves_checkpoint_and_resume_completes_without_recrawling(tmp_path):
    hits: dict[str, int] = {}
    async with TestServer(_make_app(hits)) as srv:
        base = f"http://127.0.0.1:{srv.port}"
        settings = _settings(tmp_path)

        # ---- run 1: pause part-way -------------------------------------------
        spider1 = TreeSpider(base, settings, pause_after=8)
        engine1 = Engine(spider=spider1, settings=settings)
        spider1.engine = engine1
        items1 = _collect(engine1)
        stats1 = await engine1.run()

        assert stats1.finish_reason == "paused"
        assert 8 <= len(items1) < N_PAGES
        cp = Checkpoint(settings.jobdir)
        assert cp.exists()
        data = cp.load()
        assert data is not None and data.status == "paused"
        assert data.pending, "a paused crawl must have work left"
        assert len(data.seen) >= len(items1)

        # ---- run 2: resume ----------------------------------------------------
        spider2 = TreeSpider(base, settings)
        engine2 = Engine(spider=spider2, settings=settings)
        items2 = _collect(engine2)
        stats2 = await engine2.run()

        assert stats2.finish_reason == "finished"
        pages = sorted(i["page"] for i in items1 + items2)
        assert pages == list(range(N_PAGES)), "every page exactly once across both runs"
        assert max(hits.values()) == 1, f"pages re-fetched after resume: {hits}"
        # counters accumulate across the pause
        assert stats2.items_scraped == N_PAGES
        # a finished crawl cleans up after itself
        assert not Checkpoint(settings.jobdir).exists()


@pytest.mark.asyncio
async def test_finished_crawl_starts_fresh_next_time(tmp_path):
    hits: dict[str, int] = {}
    async with TestServer(_make_app(hits, delay=0.0)) as srv:
        base = f"http://127.0.0.1:{srv.port}"
        settings = _settings(tmp_path)
        for _ in range(2):
            spider = TreeSpider(base, settings)
            stats = await Engine(spider=spider, settings=settings).run()
            assert stats.items_scraped == N_PAGES
        assert set(hits.values()) == {2}  # second full run really re-crawled


@pytest.mark.asyncio
async def test_forced_cancel_checkpoints_inflight_requests(tmp_path):
    hits: dict[str, int] = {}
    async with TestServer(_make_app(hits, delay=0.3)) as srv:
        base = f"http://127.0.0.1:{srv.port}"
        settings = _settings(tmp_path)
        spider = TreeSpider(base, settings)
        engine = Engine(spider=spider, settings=settings)
        task = asyncio.create_task(engine.run())
        await asyncio.sleep(0.5)  # a few requests are in flight
        task.cancel()
        stats = await task
        assert stats.finish_reason == "cancelled"
        data = Checkpoint(settings.jobdir).load()
        assert data is not None and data.pending
        # resume finishes the crawl
        spider2 = TreeSpider(base, settings)
        engine2 = Engine(spider=spider2, settings=settings)
        items = _collect(engine2)
        stats2 = await engine2.run()
        assert stats2.finish_reason == "finished"
        assert {i["page"] for i in items} | set(spider.seen_pages) == set(range(N_PAGES))


@pytest.mark.asyncio
async def test_checkpoint_for_a_different_spider_is_ignored(tmp_path):
    hits: dict[str, int] = {}
    async with TestServer(_make_app(hits, delay=0.0)) as srv:
        base = f"http://127.0.0.1:{srv.port}"
        settings = _settings(tmp_path)
        Checkpoint(settings.jobdir).save(
            [Request(url=f"{base}/p/29")], ["x"], type("S", (), {})(), spider="someone-else"
        )
        spider = TreeSpider(base, settings)
        stats = await Engine(spider=spider, settings=settings).run()
        assert stats.items_scraped == N_PAGES  # full crawl, not the 1-request checkpoint


@pytest.mark.asyncio
async def test_stop_is_graceful_and_writes_no_checkpoint_without_jobdir(tmp_path):
    hits: dict[str, int] = {}
    async with TestServer(_make_app(hits)) as srv:
        base = f"http://127.0.0.1:{srv.port}"
        settings = Settings(robotstxt_obey=False, concurrent_requests=4)
        spider = TreeSpider(base, settings)
        engine = Engine(spider=spider, settings=settings)
        task = asyncio.create_task(engine.run())
        await asyncio.sleep(0.1)
        engine.stop()
        stats = await task
        assert stats.finish_reason == "cancelled"
        assert stats.items_scraped < N_PAGES


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX signals")
@pytest.mark.asyncio
async def test_sigint_pauses_gracefully_and_saves_state(tmp_path):
    hits: dict[str, int] = {}
    async with TestServer(_make_app(hits)) as srv:
        base = f"http://127.0.0.1:{srv.port}"
        settings = _settings(tmp_path)

        class SignalSpider(TreeSpider):
            async def parse(self, response):
                async for out in super().parse(response):
                    yield out
                if len(self.seen_pages) == 6:
                    os.kill(os.getpid(), signal.SIGINT)

        spider = SignalSpider(base, settings)
        engine = Engine(spider=spider, settings=settings)
        stats = await engine.run()  # would raise KeyboardInterrupt without the handler
        assert stats.finish_reason == "paused"
        assert Checkpoint(settings.jobdir).load().pending
        # handlers were restored: the default SIGINT handler is back
        assert signal.getsignal(signal.SIGINT) is signal.default_int_handler


@pytest.mark.asyncio
async def test_concurrency_ceiling_and_no_queue_hoarding():
    hits: dict[str, int] = {}
    state: dict[str, Any] = {}
    async with TestServer(_make_app(hits, delay=0.05, state=state)) as srv:
        base = f"http://127.0.0.1:{srv.port}"
        settings = Settings(
            robotstxt_obey=False, concurrent_requests=3, concurrent_requests_per_domain=3
        )
        spider = TreeSpider(base, settings)
        engine = Engine(spider=spider, settings=settings)
        max_tasks = 0
        orig = asyncio.create_task

        stats_task = asyncio.create_task(engine.run())
        for _ in range(60):
            await asyncio.sleep(0.02)
            max_tasks = max(max_tasks, len(engine._inflight))
            if stats_task.done():
                break
        stats = await stats_task
        del orig
        assert stats.items_scraped == N_PAGES
        assert state["peak"] <= 3
        assert max_tasks <= 3, f"engine hoarded {max_tasks} requests with concurrency 3"


class KwargsSpider(Spider):
    name = "kw"

    def __init__(self, base: str, settings: Settings | None = None) -> None:
        super().__init__(settings=settings)
        self.base = base
        self.got: list[Any] = []

    async def start_requests(self):  # async generator seeds
        yield Request(url=f"{self.base}/p/0", callback="parse", cb_kwargs={"tag": "seed"})
        # nothing listens on port 1: a genuine connection failure (a 404 would
        # NOT be -- HTTP error statuses are delivered to the callback, not errbacked)
        yield Request(url="http://127.0.0.1:1/dead", callback="parse", errback="on_fail")

    async def parse(self, response, tag="none"):
        self.got.append(("parse", tag))
        yield {"tag": tag}
        yield self.follow(f"{self.base}/p/1", callback="detail", cb_kwargs={"n": 7})
        # same URL again, but explicitly exempt from the duplicate filter
        yield self.follow(
            f"{self.base}/p/1", callback="detail", cb_kwargs={"n": 8}, dont_filter=True
        )
        yield self.follow(f"{self.base}/p/1", callback="detail", cb_kwargs={"n": 9})  # deduped

    async def detail(self, response, n):
        self.got.append(("detail", n))
        return {"n": n}  # a coroutine returning an item, not an async generator

    async def on_fail(self, request, exc):
        self.got.append(("errback", request.url.rsplit("/", 1)[-1]))  # -> "dead"
        yield Request(url=f"{self.base}/p/2", callback="detail", cb_kwargs={"n": 99})


@pytest.mark.asyncio
async def test_cb_kwargs_dont_filter_coroutine_callbacks_async_seeds_and_errback():
    hits: dict[str, int] = {}
    async with TestServer(_make_app(hits, delay=0.0)) as srv:
        base = f"http://127.0.0.1:{srv.port}"
        settings = Settings(robotstxt_obey=False, max_retries=0)
        spider = KwargsSpider(base, settings)
        engine = Engine(spider=spider, settings=settings)
        items = _collect(engine)
        await engine.run()

        assert ("parse", "seed") in spider.got
        detail_ns = sorted(n for kind, n in spider.got if kind == "detail")
        assert detail_ns == [7, 8, 99], (
            detail_ns
        )  # 9 was deduped; 8 (dont_filter) and 99 (errback) ran
        assert ("errback", "dead") in spider.got
        assert sorted(json.dumps(i, sort_keys=True) for i in items) == sorted(
            json.dumps(x, sort_keys=True) for x in [{"tag": "seed"}, {"n": 7}, {"n": 8}, {"n": 99}]
        )


@pytest.mark.asyncio
async def test_custom_settings_override_run_settings():
    class S(Spider):
        name = "s"
        custom_settings = {"download_delay": 3.5}

    assert S(settings=Settings(download_delay=0.0)).settings.download_delay == 3.5
    assert Spider.custom_settings == {}


def test_plugin_manager_unchanged_api():
    pm = PluginManager()
    assert pm.hook_count("item_scraped") == 0
