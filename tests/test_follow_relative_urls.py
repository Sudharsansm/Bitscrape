"""
Tests for the follow()-resolves-relative-URLs fix.

This was a real, documented bug (self.follow(href) enqueued the literal
relative string instead of resolving it against the current page) fixed by
tracking the "current response" via a contextvars.ContextVar set by the
Engine for the duration of each callback -- NOT a plain instance attribute,
because the Engine processes multiple requests concurrently via
asyncio.create_task() against one shared Spider instance, and a plain
attribute would let concurrent in-flight requests clobber each other's
resolution context at an await point inside a callback.
"""

from __future__ import annotations

import asyncio

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from bitscrape.core.models import Response
from bitscrape.core.settings import Settings
from bitscrape.core.spider import Spider, _current_response_var
from bitscrape.engine import Engine


def _make_response(url: str, request=None) -> Response:
    from bitscrape.core.models import Request

    req = request or Request(url=url)
    return Response(url=url, status=200, headers={}, body=b"", request=req, elapsed_ms=1.0)


class _DemoSpider(Spider):
    name = "follow_fix_demo"
    start_urls = []

    async def parse(self, response):
        yield {}


# ---------------------------------------------------------------------------
# Unit-level: follow() resolution behavior
# ---------------------------------------------------------------------------


def test_follow_passes_through_absolute_url_unchanged():
    spider = _DemoSpider()
    req = spider.follow("https://example.com/other-page")
    assert req.url == "https://example.com/other-page"


def test_follow_leaves_relative_url_unresolved_outside_a_callback():
    """No current response set (e.g. building seed requests) -- can't
    resolve, so passed through as-is, same as the pre-fix behavior."""
    spider = _DemoSpider()
    assert _current_response_var.get() is None
    req = spider.follow("/relative/path")
    assert req.url == "/relative/path"


def test_follow_resolves_relative_url_against_current_response():
    spider = _DemoSpider()
    response = _make_response("https://example.com/section/index.html")
    token = _current_response_var.set(response)
    try:
        req = spider.follow("/page/2")
        assert req.url == "https://example.com/page/2"

        req2 = spider.follow("next.html")  # relative to current path, not root
        assert req2.url == "https://example.com/section/next.html"
    finally:
        _current_response_var.reset(token)


def test_follow_resolves_query_only_relative_url():
    spider = _DemoSpider()
    response = _make_response("https://example.com/search?q=old")
    token = _current_response_var.set(response)
    try:
        req = spider.follow("?q=new")
        assert req.url == "https://example.com/search?q=new"
    finally:
        _current_response_var.reset(token)


def test_context_var_defaults_to_none_when_unset():
    assert _current_response_var.get() is None


# ---------------------------------------------------------------------------
# End-to-end: real crawl with real relative hrefs
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_engine_follows_relative_href_end_to_end():
    """The actual regression this was reported against: a page with
    <a href="/next"> (relative) must result in a real second request to
    the resolved absolute URL, not a failed request to the literal string
    '/next'."""

    async def index_handler(request: web.Request) -> web.Response:
        return web.Response(
            text='<html><body><a href="/next">go</a></body></html>', content_type="text/html"
        )

    async def next_handler(request: web.Request) -> web.Response:
        return web.Response(text="<html><body>done</body></html>")

    app = web.Application()
    app.router.add_get("/index", index_handler)
    app.router.add_get("/next", next_handler)
    server = TestServer(app)
    await server.start_server()
    try:
        base = f"http://{server.host}:{server.port}"

        class _RelativeLinkSpider(Spider):
            name = "relative_link_demo"
            start_urls = [f"{base}/index"]

            async def parse(self, response):
                href = response.css("a::attr(href)").get()
                if href:
                    yield self.follow(href)
                else:
                    yield {"landed_on": response.url}

        engine = Engine(spider=_RelativeLinkSpider(), settings=Settings(robotstxt_obey=False))
        stats = await engine.run()

        assert stats.requests_made == 2  # index + next, not a failed relative request
        assert stats.requests_failed == 0
        assert stats.items_scraped == 1
    finally:
        await server.close()


@pytest.mark.asyncio
async def test_concurrent_requests_resolve_against_their_own_response_not_each_others():
    """
    The concurrency-safety proof: two DIFFERENT pages, each with a
    DIFFERENT relative link, processed concurrently. If the current-response
    tracking used a shared instance attribute instead of a ContextVar, a
    slow response's relative link could resolve against a different,
    faster-arriving response's URL instead of its own. Verified by making
    one page deliberately slower than the other so their processing
    genuinely overlaps.
    """

    async def slow_page_a(request: web.Request) -> web.Response:
        await asyncio.sleep(0.2)  # slow -- still "in flight" when B finishes
        return web.Response(text='<html><body><a href="/target-a">x</a></body></html>')

    async def fast_page_b(request: web.Request) -> web.Response:
        return web.Response(text='<html><body><a href="/target-b">x</a></body></html>')

    landed = {}

    async def target_a(request: web.Request) -> web.Response:
        landed["a"] = True
        return web.Response(text="ok")

    async def target_b(request: web.Request) -> web.Response:
        landed["b"] = True
        return web.Response(text="ok")

    app = web.Application()
    app.router.add_get("/page-a", slow_page_a)
    app.router.add_get("/page-b", fast_page_b)
    app.router.add_get("/target-a", target_a)
    app.router.add_get("/target-b", target_b)
    server = TestServer(app)
    await server.start_server()
    try:
        base = f"http://{server.host}:{server.port}"

        class _ConcurrentSpider(Spider):
            name = "concurrent_resolve_demo"
            start_urls = [f"{base}/page-a", f"{base}/page-b"]

            async def parse(self, response):
                href = response.css("a::attr(href)").get()
                if href:
                    yield self.follow(href)

        engine = Engine(
            spider=_ConcurrentSpider(),
            settings=Settings(robotstxt_obey=False, concurrent_requests=2),
        )
        stats = await engine.run()

        assert stats.requests_failed == 0
        # Both targets must have been hit -- if contexts had leaked between
        # the two concurrent tasks, one relative href could have resolved
        # against the WRONG page's URL and 404'd (or hit the wrong target).
        assert landed.get("a") is True
        assert landed.get("b") is True
    finally:
        await server.close()
