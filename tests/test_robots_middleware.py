"""
Tests for RobotsMiddleware fixes.

The critical fix: the original code called ``parser.feed(text)``, but
``RobotFileParser`` has no ``feed()`` method -- only ``.parse(lines)`` does.
Every fetch therefore raised ``AttributeError``, silently caught by a broad
``except Exception``, which set the cached parser to ``None`` forever.
Practically: ``robotstxt_obey=True`` never actually blocked anything,
regardless of the site's rules -- it was always a silent no-op.

Also covered:
  - Crawl-delay / Sitemap now surfaced via RobotFileParser's own
    ``.crawl_delay()`` / ``.site_maps()`` (native, once ``.parse()`` is used)
  - Fail-safe behaviour on fetch errors (previously fail-open)
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

import pytest

from bitscrape.core.models import Request
from bitscrape.middleware.middleware import RobotsMiddleware
from bitscrape.pipeline.pipelines import DropItem

ROBOTS_TXT_WITH_EXTRAS = """
User-agent: *
Crawl-delay: 10
Disallow: /private/
Sitemap: https://example.com/sitemap.xml
Sitemap: https://example.com/sitemap-news.xml

User-agent: BitscrapeBot
Crawl-delay: 2
Disallow: /no-bitscrape/
"""


def _fake_spider(user_agent: str = "BitscrapeBot/0.1", robotstxt_obey: bool = True):
    return SimpleNamespace(
        settings=SimpleNamespace(user_agent=user_agent, robotstxt_obey=robotstxt_obey)
    )


class _FakeResponse:
    def __init__(self, status: int, text: str):
        self.status = status
        self._text = text

    async def text(self):
        return self._text

    def raise_for_status(self):
        if self.status >= 400:
            raise RuntimeError(f"HTTP {self.status}")

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False


class _FakeSession:
    def __init__(self, response):
        self._response = response

    def get(self, url, timeout=None):
        if isinstance(self._response, Exception):
            raise self._response
        return self._response

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False


# ---------------------------------------------------------------------------
# Root-cause regression: parser.feed() -> parser.parse() means enforcement
# actually runs at all.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_disallowed_path_is_actually_blocked():
    """
    This is the critical regression test. With the original `.feed()` bug,
    this would NOT raise -- the AttributeError was swallowed and the parser
    was always None, so nothing was ever blocked.
    """
    mw = RobotsMiddleware()
    spider = _fake_spider()
    fake_session = _FakeSession(_FakeResponse(200, ROBOTS_TXT_WITH_EXTRAS))

    with patch("aiohttp.ClientSession", return_value=fake_session):
        request = Request(url="https://example.com/no-bitscrape/secret")
        with pytest.raises(DropItem):
            await mw.process_request(request, spider)


@pytest.mark.asyncio
async def test_allowed_path_passes_through():
    mw = RobotsMiddleware()
    spider = _fake_spider()
    fake_session = _FakeSession(_FakeResponse(200, ROBOTS_TXT_WITH_EXTRAS))

    with patch("aiohttp.ClientSession", return_value=fake_session):
        request = Request(url="https://example.com/allowed-page")
        result = await mw.process_request(request, spider)
    assert result is None  # not blocked


# ---------------------------------------------------------------------------
# Crawl-delay / Sitemap now populated into request.meta
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_process_request_populates_meta_with_delay_and_sitemaps():
    mw = RobotsMiddleware()
    spider = _fake_spider(user_agent="BitscrapeBot/0.1")
    fake_session = _FakeSession(_FakeResponse(200, ROBOTS_TXT_WITH_EXTRAS))

    with patch("aiohttp.ClientSession", return_value=fake_session):
        request = Request(url="https://example.com/allowed-page")
        await mw.process_request(request, spider)

    assert request.meta["crawl_delay"] == 2.0  # specific UA rule wins over wildcard
    assert request.meta["sitemaps"] == [
        "https://example.com/sitemap.xml",
        "https://example.com/sitemap-news.xml",
    ]


@pytest.mark.asyncio
async def test_falls_back_to_wildcard_crawl_delay_for_unmatched_ua():
    mw = RobotsMiddleware()
    spider = _fake_spider(user_agent="SomeOtherBot/1.0")
    fake_session = _FakeSession(_FakeResponse(200, ROBOTS_TXT_WITH_EXTRAS))

    with patch("aiohttp.ClientSession", return_value=fake_session):
        request = Request(url="https://example.com/allowed-page")
        await mw.process_request(request, spider)

    assert request.meta["crawl_delay"] == 10.0  # falls back to "*" group


# ---------------------------------------------------------------------------
# Fetch-error handling
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_404_robots_txt_means_unrestricted_access():
    mw = RobotsMiddleware()
    spider = _fake_spider()
    fake_session = _FakeSession(_FakeResponse(404, ""))

    with patch("aiohttp.ClientSession", return_value=fake_session):
        request = Request(url="https://example.com/anything")
        await mw.process_request(request, spider)  # should not raise


@pytest.mark.asyncio
async def test_fetch_failure_fails_safe_not_open():
    """
    Regression: previously any fetch failure (timeout, DNS error, 5xx, or
    even the feed()/AttributeError bug above) silently disabled enforcement.
    Now it should fail SAFE -- block the request -- since the real rules are
    unknown, not confirmed absent.
    """
    mw = RobotsMiddleware()
    spider = _fake_spider()
    fake_session = _FakeSession(TimeoutError("robots.txt fetch timed out"))

    with patch("aiohttp.ClientSession", return_value=fake_session):
        request = Request(url="https://slow-site.com/page")
        with pytest.raises(DropItem):
            await mw.process_request(request, spider)


@pytest.mark.asyncio
async def test_5xx_robots_txt_fails_safe():
    mw = RobotsMiddleware()
    spider = _fake_spider()
    fake_session = _FakeSession(_FakeResponse(503, ""))

    with patch("aiohttp.ClientSession", return_value=fake_session):
        request = Request(url="https://flaky-site.com/page")
        with pytest.raises(DropItem):
            await mw.process_request(request, spider)


@pytest.mark.asyncio
async def test_robotstxt_obey_false_skips_all_checks():
    mw = RobotsMiddleware()
    spider = _fake_spider(robotstxt_obey=False)
    # No network call should even be attempted.
    request = Request(url="https://example.com/no-bitscrape/secret")
    result = await mw.process_request(request, spider)
    assert result is None


@pytest.mark.asyncio
async def test_domain_result_is_cached_after_success():
    mw = RobotsMiddleware()
    spider = _fake_spider()
    fake_session = _FakeSession(_FakeResponse(200, ROBOTS_TXT_WITH_EXTRAS))

    with patch("aiohttp.ClientSession", return_value=fake_session) as ctor:
        await mw.process_request(Request(url="https://example.com/a"), spider)
        await mw.process_request(Request(url="https://example.com/b"), spider)
        # Only one robots.txt fetch for two requests to the same domain.
        assert ctor.call_count == 1
