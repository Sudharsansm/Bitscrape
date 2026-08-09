"""
Tests for AutoThrottle: unit tests for the adaptive delay algorithm, plus a
live end-to-end test proving the Downloader actually backs off against a
real slow server and recovers when it speeds back up.
"""

from __future__ import annotations

import asyncio
import time

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from bitscrape.core.models import Request
from bitscrape.core.settings import Settings
from bitscrape.downloader.downloader import AutoThrottle, Downloader

# ---------------------------------------------------------------------------
# Unit tests: the adaptive algorithm itself
# ---------------------------------------------------------------------------


def test_starts_at_start_delay_for_unseen_domain():
    at = AutoThrottle(start_delay=1.0, max_delay=60.0, target_concurrency=2.0)
    assert at.get_delay("example.com") == 1.0


def test_slow_response_increases_delay():
    at = AutoThrottle(start_delay=1.0, max_delay=60.0, target_concurrency=2.0)
    at.update("example.com", latency_seconds=10.0)  # target_delay = 10/2 = 5.0
    # new_delay = (1.0 + 5.0) / 2 = 3.0
    assert at.get_delay("example.com") == pytest.approx(3.0)


def test_fast_response_decreases_delay():
    at = AutoThrottle(start_delay=5.0, max_delay=60.0, target_concurrency=2.0)
    at.update("example.com", latency_seconds=0.2)  # target_delay = 0.1
    # new_delay = (5.0 + 0.1) / 2 = 2.55
    assert at.get_delay("example.com") == pytest.approx(2.55)


def test_delay_is_capped_at_max_delay():
    at = AutoThrottle(start_delay=1.0, max_delay=5.0, target_concurrency=1.0)
    for _ in range(20):
        at.update("example.com", latency_seconds=100.0)  # would push way past max
    assert at.get_delay("example.com") <= 5.0


def test_delay_never_negative():
    at = AutoThrottle(start_delay=1.0, max_delay=60.0, target_concurrency=2.0)
    at.update("example.com", latency_seconds=-5.0)  # malformed input, shouldn't go negative
    assert at.get_delay("example.com") >= 0.0


def test_domains_are_tracked_independently():
    at = AutoThrottle(start_delay=1.0, max_delay=60.0, target_concurrency=2.0)
    at.update("slow.com", latency_seconds=20.0)
    assert at.get_delay("slow.com") != at.get_delay("fast.com")
    assert at.get_delay("fast.com") == 1.0  # untouched, still default


def test_reset_specific_domain():
    at = AutoThrottle(start_delay=1.0)
    at.update("example.com", latency_seconds=20.0)
    assert at.get_delay("example.com") != 1.0
    at.reset("example.com")
    assert at.get_delay("example.com") == 1.0


def test_reset_all_domains():
    at = AutoThrottle(start_delay=1.0)
    at.update("a.com", latency_seconds=20.0)
    at.update("b.com", latency_seconds=20.0)
    at.reset()
    assert at.get_delay("a.com") == 1.0
    assert at.get_delay("b.com") == 1.0


def test_converges_toward_target_delay_over_repeated_updates():
    at = AutoThrottle(start_delay=0.0, max_delay=60.0, target_concurrency=1.0)
    for _ in range(30):
        at.update("example.com", latency_seconds=4.0)  # target_delay = 4.0
    assert at.get_delay("example.com") == pytest.approx(4.0, abs=0.01)


# ---------------------------------------------------------------------------
# End-to-end: Downloader actually slows down against a genuinely slow server
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_downloader_backs_off_against_slow_server_then_recovers():
    """
    Live proof, not just unit math: hit a server that's slow for the first
    few requests then fast, and confirm the effective per-domain delay
    (as tracked by the downloader's AutoThrottle) rose during the slow
    phase and fell back down during the fast phase.
    """
    state = {"phase": "slow"}

    async def handler(request: web.Request) -> web.Response:
        if state["phase"] == "slow":
            await asyncio.sleep(0.3)
        return web.Response(text="ok")

    app = web.Application()
    app.router.add_get("/page", handler)
    server = TestServer(app)
    await server.start_server()
    try:
        url = f"http://{server.host}:{server.port}/page"
        domain = f"{server.host}:{server.port}"
        settings = Settings(
            autothrottle_enabled=True,
            autothrottle_start_delay=0.1,
            autothrottle_max_delay=10.0,
            autothrottle_target_concurrency=1.0,
        )
        downloader = Downloader(settings)
        await downloader.open()
        try:
            assert downloader._autothrottle is not None

            # Slow phase: a few requests while the server is deliberately slow.
            for _ in range(3):
                await downloader.fetch(Request(url=url))
            delay_after_slow = downloader._autothrottle.get_delay(domain)
            assert delay_after_slow > 0.1  # rose above the starting delay

            # Server speeds back up.
            state["phase"] = "fast"
            for _ in range(10):
                await downloader.fetch(Request(url=url))
            delay_after_fast = downloader._autothrottle.get_delay(domain)
            assert delay_after_fast < delay_after_slow  # recovered downward
        finally:
            await downloader.close()
    finally:
        await server.close()


@pytest.mark.asyncio
async def test_autothrottle_disabled_uses_static_download_delay():
    async def handler(request: web.Request) -> web.Response:
        return web.Response(text="ok")

    app = web.Application()
    app.router.add_get("/page", handler)
    server = TestServer(app)
    await server.start_server()
    try:
        url = f"http://{server.host}:{server.port}/page"
        settings = Settings(autothrottle_enabled=False, download_delay=0.3)
        downloader = Downloader(settings)
        await downloader.open()
        try:
            assert downloader._autothrottle is None
            t0 = time.monotonic()
            await downloader.fetch(Request(url=url))
            await downloader.fetch(Request(url=url))
            elapsed = time.monotonic() - t0
            assert elapsed >= 0.25  # static delay enforced between the two
        finally:
            await downloader.close()
    finally:
        await server.close()
