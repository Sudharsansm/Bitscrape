"""
Tests for BrowserPool: verifies the actual pooling behaviour (reuse browser
instances across requests, cap concurrent browsers at pool_size, isolate
contexts per request) using fake Playwright-shaped objects, since no real
browser binary is available in this build environment (no network access to
download one). The fakes implement the exact same async method shapes
(``launch``, ``new_context``, ``new_page``, ``close``) that real Playwright
objects expose, so the pooling logic itself -- acquire/release, reuse
counting, context isolation -- is genuinely exercised.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, patch

import pytest

from bitscrape.downloader.downloader import BrowserPool


class FakeContext:
    def __init__(self, browser: "FakeBrowser", **kwargs):
        self.browser = browser
        self.kwargs = kwargs
        self.closed = False

    async def new_page(self):
        return FakePage()

    async def close(self):
        self.closed = True


class FakePage:
    async def goto(self, url, wait_until=None):
        return AsyncMock(status=200)

    async def content(self):
        return "<html>fake</html>"


class FakeBrowser:
    _next_id = 0

    def __init__(self):
        FakeBrowser._next_id += 1
        self.id = FakeBrowser._next_id
        self.closed = False
        self.contexts_created = 0

    async def new_context(self, **kwargs):
        self.contexts_created += 1
        return FakeContext(self, **kwargs)

    async def close(self):
        self.closed = True


class FakeBrowserType:
    def __init__(self):
        self.launch_count = 0

    async def launch(self, headless=True, **kwargs):
        self.launch_count += 1
        return FakeBrowser()


class FakePlaywright:
    def __init__(self):
        self.chromium = FakeBrowserType()
        self.firefox = FakeBrowserType()
        self.stopped = False

    async def stop(self):
        self.stopped = True


def _patch_async_playwright(fake_pw: FakePlaywright):
    class _Starter:
        async def start(self):
            return fake_pw

    return patch(
        "playwright.async_api.async_playwright",
        return_value=_Starter(),
    )


@pytest.mark.asyncio
async def test_start_launches_exactly_pool_size_browsers():
    fake_pw = FakePlaywright()
    with _patch_async_playwright(fake_pw):
        pool = BrowserPool(playwright_browser="chromium", headless=True, pool_size=3)
        await pool.start()
        try:
            assert fake_pw.chromium.launch_count == 3
            assert pool.size == 3
            assert pool.available == 3
        finally:
            await pool.stop()


@pytest.mark.asyncio
async def test_stop_closes_all_browsers_and_playwright():
    fake_pw = FakePlaywright()
    with _patch_async_playwright(fake_pw):
        pool = BrowserPool(playwright_browser="chromium", headless=True, pool_size=2)
        await pool.start()
        browsers = list(pool._all_browsers)
        await pool.stop()
        assert all(b.closed for b in browsers)
        assert fake_pw.stopped is True


@pytest.mark.asyncio
async def test_acquire_context_reuses_browser_not_relaunching():
    """The actual point of pooling: N requests through a pool of size 1
    should launch exactly ONE browser, not one per request."""
    fake_pw = FakePlaywright()
    with _patch_async_playwright(fake_pw):
        pool = BrowserPool(playwright_browser="chromium", headless=True, pool_size=1)
        await pool.start()
        try:
            for _ in range(5):
                async with pool.acquire_context() as context:
                    page = await context.new_page()
                    await page.goto("https://example.com")
            assert fake_pw.chromium.launch_count == 1  # only launched once, reused 5x
        finally:
            await pool.stop()


@pytest.mark.asyncio
async def test_context_is_closed_but_browser_is_not_after_each_use():
    fake_pw = FakePlaywright()
    with _patch_async_playwright(fake_pw):
        pool = BrowserPool(playwright_browser="chromium", headless=True, pool_size=1)
        await pool.start()
        try:
            async with pool.acquire_context() as context:
                pass
            assert context.closed is True
            assert context.browser.closed is False  # browser stays alive for reuse
        finally:
            await pool.stop()


@pytest.mark.asyncio
async def test_pool_bounds_concurrent_browser_usage():
    """With pool_size=2, a 3rd concurrent acquire must wait for one of the
    first two to be released -- proving the pool actually bounds concurrency
    rather than just being decorative."""
    fake_pw = FakePlaywright()
    with _patch_async_playwright(fake_pw):
        pool = BrowserPool(playwright_browser="chromium", headless=True, pool_size=2)
        await pool.start()
        try:
            acquired_order = []
            release_events = [asyncio.Event() for _ in range(3)]

            async def worker(i):
                async with pool.acquire_context() as _context:
                    acquired_order.append(i)
                    await release_events[i].wait()

            tasks = [asyncio.create_task(worker(i)) for i in range(3)]
            await asyncio.sleep(0.1)
            # Only 2 of the 3 should have acquired so far (pool_size=2).
            assert len(acquired_order) == 2
            assert pool.available == 0

            release_events[acquired_order[0]].set()
            await asyncio.sleep(0.1)
            assert len(acquired_order) == 3  # third one got in after a release

            for ev in release_events:
                ev.set()
            await asyncio.gather(*tasks)
            assert fake_pw.chromium.launch_count == 2  # never launched a 3rd
        finally:
            await pool.stop()


@pytest.mark.asyncio
async def test_proxy_is_passed_through_to_new_context():
    fake_pw = FakePlaywright()
    with _patch_async_playwright(fake_pw):
        pool = BrowserPool(playwright_browser="chromium", headless=True, pool_size=1)
        await pool.start()
        try:
            async with pool.acquire_context(proxy={"server": "http://proxy:8080"}) as context:
                assert context.kwargs.get("proxy") == {"server": "http://proxy:8080"}
        finally:
            await pool.stop()


@pytest.mark.asyncio
async def test_start_is_idempotent():
    fake_pw = FakePlaywright()
    with _patch_async_playwright(fake_pw):
        pool = BrowserPool(playwright_browser="chromium", headless=True, pool_size=2)
        await pool.start()
        await pool.start()  # calling again should be a no-op
        try:
            assert fake_pw.chromium.launch_count == 2  # not launched twice
        finally:
            await pool.stop()
