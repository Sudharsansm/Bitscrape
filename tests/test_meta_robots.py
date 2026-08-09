"""
Tests for MetaRobotsMiddleware (X-Robots-Tag / <meta name="robots"> parsing)
and its effect when wired into the Engine: noindex pages should not have
their items counted/exported, nofollow pages should not have their links
enqueued -- mirroring Googlebot/ClaudeBot/PerplexityBot-class semantics.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from bitscrape.core.models import Request, Response
from bitscrape.core.settings import Settings
from bitscrape.core.spider import Spider
from bitscrape.engine import Engine
from bitscrape.middleware.middleware import MetaRobotsMiddleware


def _fake_spider(user_agent: str = "BitscrapeBot/0.1", respect_meta_robots: bool = True):
    return SimpleNamespace(
        settings=SimpleNamespace(user_agent=user_agent, respect_meta_robots=respect_meta_robots)
    )


def _response(body: bytes, headers: dict | None = None) -> Response:
    request = Request(url="http://example.test/page")
    return Response(
        url="http://example.test/page",
        status=200,
        headers=headers or {},
        body=body,
        request=request,
        elapsed_ms=1.0,
        encoding="utf-8",
    )


# ---------------------------------------------------------------------------
# MetaRobotsMiddleware directive detection
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_x_robots_tag_header_noindex():
    mw = MetaRobotsMiddleware()
    response = _response(b"<html></html>", headers={"X-Robots-Tag": "noindex"})
    request = response.request
    await mw.process_response(request, response, _fake_spider())
    assert request.meta["noindex"] is True
    assert request.meta["nofollow"] is False


@pytest.mark.asyncio
async def test_x_robots_tag_header_none_sets_both():
    mw = MetaRobotsMiddleware()
    response = _response(b"<html></html>", headers={"X-Robots-Tag": "none"})
    request = response.request
    await mw.process_response(request, response, _fake_spider())
    assert request.meta["noindex"] is True
    assert request.meta["nofollow"] is True


@pytest.mark.asyncio
async def test_meta_tag_nofollow_in_html():
    mw = MetaRobotsMiddleware()
    html = b'<html><head><meta name="robots" content="nofollow"></head></html>'
    response = _response(html)
    request = response.request
    await mw.process_response(request, response, _fake_spider())
    assert request.meta["noindex"] is False
    assert request.meta["nofollow"] is True


@pytest.mark.asyncio
async def test_meta_tag_combined_directives():
    mw = MetaRobotsMiddleware()
    html = b'<html><head><meta name="robots" content="noindex, nofollow"></head></html>'
    response = _response(html)
    request = response.request
    await mw.process_response(request, response, _fake_spider())
    assert request.meta["noindex"] is True
    assert request.meta["nofollow"] is True


@pytest.mark.asyncio
async def test_scoped_meta_tag_matches_own_user_agent():
    mw = MetaRobotsMiddleware()
    html = b'<html><head><meta name="bitscrapebot" content="noindex"></head></html>'
    response = _response(html)
    request = response.request
    await mw.process_response(request, response, _fake_spider(user_agent="BitscrapeBot/0.1"))
    assert request.meta["noindex"] is True


@pytest.mark.asyncio
async def test_no_directives_present_defaults_false():
    mw = MetaRobotsMiddleware()
    response = _response(b"<html><body>hi</body></html>")
    request = response.request
    await mw.process_response(request, response, _fake_spider())
    assert request.meta["noindex"] is False
    assert request.meta["nofollow"] is False


@pytest.mark.asyncio
async def test_respect_meta_robots_false_skips_processing_entirely():
    mw = MetaRobotsMiddleware()
    response = _response(b"<html></html>", headers={"X-Robots-Tag": "noindex"})
    request = response.request
    await mw.process_response(request, response, _fake_spider(respect_meta_robots=False))
    assert "noindex" not in request.meta


@pytest.mark.asyncio
async def test_returns_response_unchanged_never_drops():
    """Middleware must always return the response, never None (which the
    MiddlewareManager would interpret as 'drop')."""
    mw = MetaRobotsMiddleware()
    response = _response(b"<html></html>", headers={"X-Robots-Tag": "noindex, nofollow"})
    result = await mw.process_response(response.request, response, _fake_spider())
    assert result is response


# ---------------------------------------------------------------------------
# Engine wiring: noindex skips item export, nofollow skips link enqueueing
# ---------------------------------------------------------------------------


class _ItemAndLinkSpider(Spider):
    name = "meta_robots_demo"
    start_urls = ["http://example.test/"]

    async def parse(self, response):
        yield {"title": "should be skipped if noindex"}
        yield self.follow("http://example.test/next")


@pytest.mark.asyncio
async def test_engine_skips_item_export_on_noindex():
    engine = Engine(spider=_ItemAndLinkSpider(), settings=Settings())
    from bitscrape.scheduler.scheduler import Scheduler

    engine._scheduler = await Scheduler.from_settings(Settings())

    request = Request(url="http://example.test/", callback="parse", meta={"noindex": True})
    response = Response(
        url="http://example.test/",
        status=200,
        headers={},
        body=b"<html></html>",
        request=request,
        elapsed_ms=1.0,
        encoding="utf-8",
    )
    await engine._parse_response(request, response)

    assert engine._stats.items_scraped == 0
    assert engine._stats.items_noindexed == 1
    # nofollow wasn't set, so the link should still be enqueued.
    assert await engine._scheduler.next_request() is not None


@pytest.mark.asyncio
async def test_engine_skips_link_enqueue_on_nofollow():
    engine = Engine(spider=_ItemAndLinkSpider(), settings=Settings())
    from bitscrape.scheduler.scheduler import Scheduler

    engine._scheduler = await Scheduler.from_settings(Settings())

    request = Request(url="http://example.test/", callback="parse", meta={"nofollow": True})
    response = Response(
        url="http://example.test/",
        status=200,
        headers={},
        body=b"<html></html>",
        request=request,
        elapsed_ms=1.0,
        encoding="utf-8",
    )
    await engine._parse_response(request, response)

    assert engine._stats.items_scraped == 1  # item not affected by nofollow
    assert engine._stats.links_nofollow_skipped == 1
    assert await engine._scheduler.next_request() is None  # link was NOT enqueued


@pytest.mark.asyncio
async def test_engine_ignores_meta_flags_when_setting_disabled():
    engine = Engine(
        spider=_ItemAndLinkSpider(), settings=Settings(respect_meta_robots=False)
    )
    from bitscrape.scheduler.scheduler import Scheduler

    engine._scheduler = await Scheduler.from_settings(Settings())

    request = Request(
        url="http://example.test/",
        callback="parse",
        meta={"noindex": True, "nofollow": True},
    )
    response = Response(
        url="http://example.test/",
        status=200,
        headers={},
        body=b"<html></html>",
        request=request,
        elapsed_ms=1.0,
        encoding="utf-8",
    )
    await engine._parse_response(request, response)

    # respect_meta_robots=False -> flags are ignored entirely.
    assert engine._stats.items_scraped == 1
    assert engine._stats.items_noindexed == 0
    assert engine._stats.links_nofollow_skipped == 0
    assert await engine._scheduler.next_request() is not None
