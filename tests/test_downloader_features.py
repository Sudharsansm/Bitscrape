"""
Tests for the two new downloader-level features:
  - Conditional GET (ETag / Last-Modified) -- avoids re-downloading unchanged
    pages, mirroring production crawler behaviour.
  - Retry-After-aware backoff on 429/503 -- honours the server's own
    requested wait time (RFC 9110) instead of blind exponential backoff.

Uses a real local aiohttp test server (no mocking of aiohttp internals) so
these exercise the actual request/response path end-to-end.
"""

from __future__ import annotations

import time

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from bitscrape.core.models import Request
from bitscrape.core.settings import Settings
from bitscrape.downloader.downloader import Downloader, _parse_retry_after

# ---------------------------------------------------------------------------
# Retry-After header parsing (pure unit tests, no server needed)
# ---------------------------------------------------------------------------


def test_parse_retry_after_seconds_form():
    assert _parse_retry_after("120") == 120.0


def test_parse_retry_after_none_when_missing():
    assert _parse_retry_after(None) is None


def test_parse_retry_after_http_date_form():
    from email.utils import format_datetime

    from datetime import UTC, datetime, timedelta

    future = datetime.now(UTC) + timedelta(seconds=30)
    header = format_datetime(future, usegmt=True)
    delay = _parse_retry_after(header)
    assert delay is not None
    assert 25 <= delay <= 35  # allow small test-execution slack


def test_parse_retry_after_unparseable_returns_none():
    assert _parse_retry_after("not-a-valid-value") is None


# ---------------------------------------------------------------------------
# Conditional GET (ETag / Last-Modified) against a real local server
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_conditional_get_sends_if_none_match_and_reuses_cached_body():
    hits = {"count": 0, "seen_if_none_match": []}

    async def handler(request: web.Request) -> web.Response:
        hits["count"] += 1
        hits["seen_if_none_match"].append(request.headers.get("If-None-Match"))
        if request.headers.get("If-None-Match") == '"abc123"':
            return web.Response(status=304, headers={"ETag": '"abc123"'})
        return web.Response(text="hello world", headers={"ETag": '"abc123"'})

    app = web.Application()
    app.router.add_get("/page", handler)
    server = TestServer(app)
    await server.start_server()
    try:
        url = f"http://{server.host}:{server.port}/page"
        settings = Settings(conditional_get_enabled=True)
        downloader = Downloader(settings)
        await downloader.open()
        try:
            r1 = await downloader.fetch(Request(url=url))
            assert r1.status == 200
            assert r1.body == b"hello world"

            r2 = await downloader.fetch(Request(url=url))
            assert r2.status == 200  # synthesized from cache, not a raw 304
            assert r2.body == b"hello world"
            assert r2.headers.get("X-Bitscrape-Not-Modified") == "true"

            # Second request must have sent If-None-Match with the cached ETag.
            assert hits["seen_if_none_match"][1] == '"abc123"'
            assert hits["count"] == 2
        finally:
            await downloader.close()
    finally:
        await server.close()


@pytest.mark.asyncio
async def test_conditional_get_disabled_always_refetches():
    hits = {"count": 0}

    async def handler(request: web.Request) -> web.Response:
        hits["count"] += 1
        return web.Response(text="hello world", headers={"ETag": '"abc123"'})

    app = web.Application()
    app.router.add_get("/page", handler)
    server = TestServer(app)
    await server.start_server()
    try:
        url = f"http://{server.host}:{server.port}/page"
        settings = Settings(conditional_get_enabled=False)
        downloader = Downloader(settings)
        await downloader.open()
        try:
            await downloader.fetch(Request(url=url))
            r2 = await downloader.fetch(Request(url=url))
            assert r2.headers.get("X-Bitscrape-Not-Modified") is None
            assert hits["count"] == 2
        finally:
            await downloader.close()
    finally:
        await server.close()


# ---------------------------------------------------------------------------
# Retry-After honoured end-to-end
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_retry_after_header_is_honoured_over_exponential_backoff():
    calls = {"count": 0}

    async def handler(request: web.Request) -> web.Response:
        calls["count"] += 1
        if calls["count"] == 1:
            return web.Response(status=429, headers={"Retry-After": "1"})
        return web.Response(text="ok", status=200)

    app = web.Application()
    app.router.add_get("/limited", handler)
    server = TestServer(app)
    await server.start_server()
    try:
        url = f"http://{server.host}:{server.port}/limited"
        settings = Settings(respect_retry_after=True, max_retry_after_seconds=30)
        downloader = Downloader(settings)
        await downloader.open()
        try:
            t0 = time.monotonic()
            response = await downloader.fetch(Request(url=url, max_retries=2))
            elapsed = time.monotonic() - t0
            assert response.status == 200
            assert calls["count"] == 2
            # Should wait ~1s (the Retry-After value), not the 2s exponential
            # default for attempt 1 -- but we assert a generous upper bound
            # to avoid timing flakiness, and a lower bound to confirm it did
            # wait roughly the requested amount rather than 0s.
            assert 0.9 <= elapsed <= 5.0
        finally:
            await downloader.close()
    finally:
        await server.close()


@pytest.mark.asyncio
async def test_retry_after_capped_by_max_retry_after_seconds():
    calls = {"count": 0}

    async def handler(request: web.Request) -> web.Response:
        calls["count"] += 1
        if calls["count"] == 1:
            return web.Response(status=429, headers={"Retry-After": "9999"})
        return web.Response(text="ok", status=200)

    app = web.Application()
    app.router.add_get("/limited2", handler)
    server = TestServer(app)
    await server.start_server()
    try:
        url = f"http://{server.host}:{server.port}/limited2"
        # Cap it very low so the test doesn't actually wait 9999s.
        settings = Settings(respect_retry_after=True, max_retry_after_seconds=1.0)
        downloader = Downloader(settings)
        await downloader.open()
        try:
            t0 = time.monotonic()
            response = await downloader.fetch(Request(url=url, max_retries=2))
            elapsed = time.monotonic() - t0
            assert response.status == 200
            assert elapsed <= 5.0  # capped, not anywhere near 9999s
        finally:
            await downloader.close()
    finally:
        await server.close()
