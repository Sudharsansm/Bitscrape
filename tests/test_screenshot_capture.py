"""
Tests for screenshot capture on Playwright fetches (request.meta["screenshot"]).
Uses fake Playwright-shaped objects, same convention as test_browser_pool.py,
since no real browser binary is available in this build environment.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from bitscrape.core.models import Request
from bitscrape.core.settings import Settings
from bitscrape.downloader.downloader import Downloader


class FakePage:
    def __init__(self, screenshot_bytes: bytes = b"FAKEPNGBYTES") -> None:
        self._screenshot_bytes = screenshot_bytes
        self.screenshot_calls: list[dict] = []

    async def goto(self, url, wait_until=None):
        return SimpleNamespace(status=200, headers={})

    async def content(self) -> str:
        return "<html>fake page</html>"

    async def screenshot(self, **kwargs) -> bytes:
        self.screenshot_calls.append(kwargs)
        return self._screenshot_bytes


class FakeContext:
    def __init__(self, page: FakePage) -> None:
        self._page = page

    async def new_page(self) -> FakePage:
        return self._page

    async def close(self) -> None:
        pass


class FakeContextCM:
    """Async context manager mimicking BrowserPool.acquire_context()."""

    def __init__(self, page: FakePage) -> None:
        self._page = page

    async def __aenter__(self) -> FakeContext:
        return FakeContext(self._page)

    async def __aexit__(self, *exc: object) -> bool:
        return False


class FakePool:
    def __init__(self, page: FakePage) -> None:
        self._page = page

    def acquire_context(self, **kwargs) -> FakeContextCM:
        return FakeContextCM(self._page)


@pytest.mark.asyncio
async def test_screenshot_not_taken_by_default():
    page = FakePage()
    downloader = Downloader(Settings())
    downloader._browser_pool = FakePool(page)  # type: ignore[attr-defined]

    response = await downloader.fetch(Request(url="https://example.com", use_playwright=True))

    assert page.screenshot_calls == []
    assert "screenshot" not in response.extra


@pytest.mark.asyncio
async def test_screenshot_true_captures_full_page_png():
    page = FakePage(screenshot_bytes=b"PNGDATA123")
    downloader = Downloader(Settings())
    downloader._browser_pool = FakePool(page)  # type: ignore[attr-defined]

    response = await downloader.fetch(
        Request(url="https://example.com", use_playwright=True, meta={"screenshot": True})
    )

    assert response.extra["screenshot"] == b"PNGDATA123"
    assert page.screenshot_calls == [{"full_page": True}]


@pytest.mark.asyncio
async def test_screenshot_dict_options_pass_through_to_playwright():
    page = FakePage()
    downloader = Downloader(Settings())
    downloader._browser_pool = FakePool(page)  # type: ignore[attr-defined]

    await downloader.fetch(
        Request(
            url="https://example.com",
            use_playwright=True,
            meta={"screenshot": {"full_page": False, "type": "jpeg"}},
        )
    )

    assert page.screenshot_calls == [{"full_page": False, "type": "jpeg"}]


@pytest.mark.asyncio
async def test_screenshot_via_unpooled_path(monkeypatch):
    """Same feature, but through the non-pooled Playwright path (used when
    settings.playwright_pool_enabled=False)."""
    page = FakePage(screenshot_bytes=b"UNPOOLEDPNG")

    class FakeBrowser:
        async def new_context(self, **kwargs):
            return FakeContext(page)

        async def close(self):
            pass

    class FakeBrowserType:
        async def launch(self, **kwargs):
            return FakeBrowser()

    class FakePlaywright:
        def __init__(self):
            self.chromium = FakeBrowserType()

        async def stop(self):
            pass

    class _Starter:
        async def __aenter__(self):
            return FakePlaywright()

        async def __aexit__(self, *exc):
            return False

    def _fake_async_playwright():
        return _Starter()

    import bitscrape.downloader.downloader as downloader_module

    import playwright.async_api as pw_async_api

    monkeypatch.setattr(pw_async_api, "async_playwright", _fake_async_playwright)

    downloader = downloader_module.Downloader(Settings(playwright_pool_enabled=False))
    response = await downloader.fetch(
        Request(url="https://example.com", use_playwright=True, meta={"screenshot": True})
    )
    assert response.extra["screenshot"] == b"UNPOOLEDPNG"
