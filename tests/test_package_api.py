"""
Tests for the top-level `bitscrape` package API:
  - Dynamic __version__ resolution (fixed from a hardcoded stale "0.1.0")
  - The `@bitscrape.spider(...)` decorator for function-based spiders
  - `bitscrape.run()` now delegating to `build_engine()` -- verified by
    proving it picks up MetaRobotsMiddleware (previously missing from its
    own separate, now-removed hardcoded wiring), not just by checking it
    still runs.
"""

from __future__ import annotations

import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from importlib.metadata import version as pkg_version

import pytest

import bitscrape
from bitscrape.core.settings import Settings


class _ThreadedTestServer:
    """
    A real, loop-independent local HTTP server for testing bitscrape.run()
    (a synchronous API that manages its own internal asyncio event loop).

    aiohttp's own `aiohttp.test_utils.TestServer` is bound to whichever
    asyncio event loop started it -- once that loop closes, the server
    stops actually processing requests even though its socket stays open,
    which caused exactly this: a hang when accessed from a DIFFERENT event
    loop (the one bitscrape.run() creates internally via asyncio.run()).
    A plain threading + http.server based server has no such coupling.
    """

    def __init__(self, body: bytes = b"<html><body>hello</body></html>", status: int = 200):
        self.body = body
        self.status = status
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802 -- required name by http.server
                self.send_response(outer.status)
                self.send_header("Content-Type", "text/html")
                self.end_headers()
                self.wfile.write(outer.body)

            def log_message(self, format: str, *args: object) -> None:  # noqa: A002
                pass  # silence default request logging

        self._httpd = HTTPServer(("127.0.0.1", 0), Handler)
        self._thread = threading.Thread(target=self._httpd.serve_forever, daemon=True)
        self._thread.start()

    @property
    def url(self) -> str:
        host, port = self._httpd.server_address
        return f"http://{host}:{port}/page"

    def close(self) -> None:
        self._httpd.shutdown()
        self._httpd.server_close()
        self._thread.join(timeout=2)


# ---------------------------------------------------------------------------
# __version__
# ---------------------------------------------------------------------------


def test_version_matches_installed_distribution():
    assert bitscrape.__version__ == pkg_version("bitscrape")


def test_version_is_not_the_old_hardcoded_string():
    """Regression: __version__ used to be hardcoded to '0.1.0' regardless
    of what was actually installed."""
    assert bitscrape.__version__ != "0.1.0"


# ---------------------------------------------------------------------------
# @bitscrape.spider() decorator
# ---------------------------------------------------------------------------


def test_spider_decorator_produces_a_spider_subclass():
    @bitscrape.spider(name="demo", start_urls=["https://example.com"])
    async def parse(response):
        yield {"ok": True}

    assert issubclass(parse, bitscrape.Spider)
    assert parse.name == "demo"
    assert parse.start_urls == ["https://example.com"]


def test_spider_decorator_class_name_is_readable():
    @bitscrape.spider(name="my_cool_spider", start_urls=[])
    async def parse(response):
        yield {}

    assert parse.__name__ == "MyCoolSpiderSpider"


def test_spider_decorator_passes_through_extra_class_attrs():
    @bitscrape.spider(name="demo", start_urls=[], custom_settings={"foo": "bar"})
    async def parse(response):
        yield {}

    assert parse.custom_settings == {"foo": "bar"}


@pytest.mark.asyncio
async def test_spider_decorator_parse_method_yields_items():
    @bitscrape.spider(name="demo", start_urls=[])
    async def parse(response):
        yield {"a": 1}
        yield {"a": 2}

    instance = parse()
    results = [item async for item in instance.parse(None)]
    assert results == [{"a": 1}, {"a": 2}]


def test_spider_decorator_requires_no_self_in_wrapped_function():
    """The whole point: the decorated function takes just `response`, not
    `self, response` -- confirmed by successfully instantiating and calling
    it without ever passing an explicit self argument to the original func."""

    calls = []

    @bitscrape.spider(name="demo", start_urls=[])
    async def parse(response):
        calls.append(response)
        yield {}

    import asyncio

    async def _drain():
        instance = parse()
        async for _ in instance.parse("a-response-object"):
            pass

    asyncio.run(_drain())
    assert calls == ["a-response-object"]


# ---------------------------------------------------------------------------
# bitscrape.run() -- end-to-end against a real local server
# ---------------------------------------------------------------------------


def test_run_with_function_based_spider_end_to_end(tmp_path):
    server = _ThreadedTestServer()
    try:
        @bitscrape.spider(name="run_test", start_urls=[server.url])
        async def parse(response):
            yield {"body_len": len(response.body)}

        output = str(tmp_path / "out.jsonl")
        stats = bitscrape.run(parse, settings=Settings(robotstxt_obey=False), output=output)

        assert stats.items_scraped == 1
        assert stats.requests_failed == 0
    finally:
        server.close()


def test_run_with_class_based_spider_end_to_end(tmp_path):
    server = _ThreadedTestServer()
    try:
        class _RunTestSpider(bitscrape.Spider):
            name = "run_test_class"
            start_urls = [server.url]

            async def parse(self, response):
                yield {"ok": True}

        output = str(tmp_path / "out.jsonl")
        stats = bitscrape.run(
            _RunTestSpider, settings=Settings(robotstxt_obey=False), output=output
        )
        assert stats.items_scraped == 1
    finally:
        server.close()


def test_run_now_honours_meta_robots_noindex():
    """
    Regression test for the actual bug found: `run()` used to wire its own
    separate, simplified middleware stack that DIDN'T include
    MetaRobotsMiddleware at all -- so a noindex page would have had its
    item counted as scraped via the old code path. Now that run() goes
    through build_engine(), a <meta name="robots" content="noindex"> page
    must be correctly excluded from items_scraped, same as the CLI.
    """
    server = _ThreadedTestServer(
        body=b'<html><head><meta name="robots" content="noindex"></head>'
        b"<body>hidden</body></html>"
    )
    try:
        @bitscrape.spider(name="noindex_test", start_urls=[server.url])
        async def parse(response):
            yield {"should_not_be_indexed": True}

        stats = bitscrape.run(parse, settings=Settings(robotstxt_obey=False))

        assert stats.items_scraped == 0
        assert stats.items_noindexed == 1
    finally:
        server.close()


def test_run_supports_explicit_middleware_override():
    """If middlewares= is passed explicitly, it should override the
    automatic Settings-driven selection entirely."""
    server = _ThreadedTestServer(
        body=b'<html><head><meta name="robots" content="noindex"></head>'
        b"<body>x</body></html>"
    )
    try:
        @bitscrape.spider(name="override_test", start_urls=[server.url])
        async def parse(response):
            yield {"counted": True}

        stats = bitscrape.run(
            parse,
            settings=Settings(robotstxt_obey=False),
            middlewares=[bitscrape.UserAgentMiddleware()],
        )
        assert stats.items_scraped == 1
    finally:
        server.close()
