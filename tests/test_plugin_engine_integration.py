"""
Integration test: PluginManager hooks actually fire during a real crawl
through the Engine (not just when called directly), against a real local
HTTP server.
"""

from __future__ import annotations

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from bitscrape.core.settings import Settings
from bitscrape.core.spider import Spider
from bitscrape.engine import Engine
from bitscrape.plugins import BasePlugin, PluginManager


class _RecordingPlugin(BasePlugin):
    def __init__(self):
        self.events: list[tuple] = []

    async def spider_opened(self, spider):
        self.events.append(("spider_opened",))

    async def spider_closed(self, spider, reason):
        self.events.append(("spider_closed", reason))

    async def request_scheduled(self, request, spider):
        self.events.append(("request_scheduled", request.url))

    async def response_received(self, request, response, spider):
        self.events.append(("response_received", response.status))

    async def item_scraped(self, item, spider):
        self.events.append(("item_scraped", item))


class _QuoteSpider(Spider):
    name = "plugin_integration_demo"
    start_urls = []  # set dynamically in the test

    async def parse(self, response):
        yield {"title": "hello"}


@pytest.mark.asyncio
async def test_plugin_hooks_fire_during_real_crawl():
    async def handler(request: web.Request) -> web.Response:
        return web.Response(text="<html>ok</html>")

    app = web.Application()
    app.router.add_get("/page", handler)
    server = TestServer(app)
    await server.start_server()
    try:
        url = f"http://{server.host}:{server.port}/page"
        spider = _QuoteSpider()
        spider.start_urls = [url]

        recorder = _RecordingPlugin()
        pm = PluginManager()
        pm.register_plugin(recorder)

        engine = Engine(spider=spider, settings=Settings(), plugin_manager=pm)
        stats = await engine.run()

        assert stats.items_scraped == 1
        event_names = [e[0] for e in recorder.events]
        assert event_names[0] == "spider_opened"
        assert "request_scheduled" in event_names
        assert "response_received" in event_names
        assert "item_scraped" in event_names
        assert event_names[-1] == "spider_closed"

        item_events = [e for e in recorder.events if e[0] == "item_scraped"]
        assert item_events[0][1] == {"title": "hello"}
    finally:
        await server.close()
