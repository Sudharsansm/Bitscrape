"""
Tests for the Engine's zero-yield diagnostic: a spider callback that yields
no items and no follow-up requests from a non-empty response should log a
clear, actionable warning instead of silently reporting "items: 0" with no
explanation.
"""

from __future__ import annotations

import logging

import pytest

from bitscrape.core.models import Request, Response
from bitscrape.core.settings import Settings
from bitscrape.core.spider import Spider
from bitscrape.engine import Engine


class _EmptyYieldSpider(Spider):
    name = "empty_yield_demo"
    start_urls = ["http://example.test/"]

    async def parse(self, response):
        # Deliberately mismatched selector -- yields nothing.
        for _ in response.css("div.does-not-exist"):
            yield {"never": "reached"}
        return
        yield  # pragma: no cover -- keeps this an async generator


class _NormalSpider(Spider):
    name = "normal_demo"
    start_urls = ["http://example.test/"]

    async def parse(self, response):
        yield {"ok": True}


def _make_engine(spider: Spider) -> Engine:
    return Engine(spider=spider, settings=Settings())


@pytest.mark.asyncio
async def test_zero_yield_logs_actionable_warning(caplog):
    engine = _make_engine(_EmptyYieldSpider())
    request = Request(url="http://example.test/", callback="parse")
    response = Response(
        url="http://example.test/",
        status=200,
        headers={},
        body=b"<html><body><div class='quote'>hi</div></body></html>",
        request=request,
        elapsed_ms=1.0,
        encoding="utf-8",
    )

    with caplog.at_level(logging.WARNING, logger="bitscrape.engine"):
        await engine._parse_response(request, response)

    assert engine._stats.items_scraped == 0
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert any("yielded 0 items and 0 requests" in r.getMessage() for r in warnings)
    assert any("53-byte response" in r.getMessage() for r in warnings)


@pytest.mark.asyncio
async def test_normal_yield_does_not_warn(caplog):
    engine = _make_engine(_NormalSpider())
    request = Request(url="http://example.test/", callback="parse")
    response = Response(
        url="http://example.test/",
        status=200,
        headers={},
        body=b"<html></html>",
        request=request,
        elapsed_ms=1.0,
        encoding="utf-8",
    )

    with caplog.at_level(logging.WARNING, logger="bitscrape.engine"):
        await engine._parse_response(request, response)

    assert engine._stats.items_scraped == 1
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert not any("yielded 0 items" in r.getMessage() for r in warnings)


@pytest.mark.asyncio
async def test_yielding_only_followup_requests_does_not_warn(caplog):
    """Sitemap-style spiders that only yield Requests (no items yet) shouldn't
    be flagged as broken."""

    class _SitemapSpider(Spider):
        name = "sitemap_demo"
        start_urls = ["http://example.test/"]

        async def parse(self, response):
            yield self.follow("http://example.test/page2")

    engine = _make_engine(_SitemapSpider())
    engine._scheduler = await _fake_scheduler()
    request = Request(url="http://example.test/", callback="parse")
    response = Response(
        url="http://example.test/",
        status=200,
        headers={},
        body=b"<html></html>",
        request=request,
        elapsed_ms=1.0,
        encoding="utf-8",
    )

    with caplog.at_level(logging.WARNING, logger="bitscrape.engine"):
        await engine._parse_response(request, response)

    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert not any("yielded 0 items" in r.getMessage() for r in warnings)


async def _fake_scheduler():
    from bitscrape.scheduler.scheduler import Scheduler

    return await Scheduler.from_settings(Settings())
