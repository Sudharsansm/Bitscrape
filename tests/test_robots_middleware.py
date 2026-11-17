"""
Tests for robots.txt compliance: ``bitscrape.middleware.robots`` (the
Protego-backed cache/fetch engine) and ``RobotsMiddleware`` (the
``process_request`` adapter).

A fake :class:`RobotsFetcher` stands in for the network throughout -- it's a plain
async function, so tests can assert on *how many times* and *with what* it was
called (concurrency, caching, retries) without mocking ``aiohttp`` internals.
"""

from __future__ import annotations

import asyncio
import datetime
from types import SimpleNamespace
from typing import Any

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from bitscrape.core.models import Request
from bitscrape.core.settings import Settings
from bitscrape.core.spider import Spider
from bitscrape.factory import build_engine
from bitscrape.middleware.middleware import RobotsMiddleware
from bitscrape.middleware.robots import (
    RobotsCache,
    domain_of,
)
from bitscrape.pipeline.pipelines import DropItem

ROBOTS_TXT = """
User-agent: *
Crawl-delay: 10
Disallow: /private/
Allow: /private/public-notice
Disallow: /*.pdf$
Sitemap: https://example.com/sitemap.xml
Sitemap: https://example.com/sitemap-news.xml

User-agent: BitscrapeBot
Crawl-delay: 2
Disallow: /no-bitscrape/
Request-rate: 1/5
"""

WINDOWED_ROBOTS_TXT = """
User-agent: *
Request-rate: 1/2 0600-0845
"""


# ---------------------------------------------------------------------------
# Fake fetcher
# ---------------------------------------------------------------------------


class FakeFetcher:
    """Records every call; ``responses`` maps a URL to either a
    ``(status, body)`` tuple, an ``Exception`` instance to raise, or a list of
    either (consumed in order, then the last entry repeats)."""

    def __init__(self, responses: dict[str, Any]) -> None:
        self.responses = responses
        self.calls: list[tuple[str, str, float]] = []
        self._indices: dict[str, int] = {}

    async def fetch(self, url: str, *, user_agent: str, timeout: float) -> tuple[int, bytes]:
        self.calls.append((url, user_agent, timeout))
        entry = self.responses[url]
        if isinstance(entry, list):
            i = self._indices.get(url, 0)
            self._indices[url] = min(i + 1, len(entry) - 1)
            entry = entry[i]
        if isinstance(entry, Exception):
            raise entry
        status, body = entry
        return status, body if isinstance(body, bytes) else body.encode()


def _cache(responses: dict[str, Any], **kw: Any) -> tuple[RobotsCache, FakeFetcher]:
    fetcher = FakeFetcher(responses)
    cache = RobotsCache(fetcher=fetcher, **kw)
    return cache, fetcher


class FakeClock:
    def __init__(self, t: float = 0.0) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t


# ---------------------------------------------------------------------------
# Disallow / Allow matching (via Protego)
# ---------------------------------------------------------------------------


async def test_disallowed_path_is_blocked():
    cache, _ = _cache({"https://example.com/robots.txt": (200, ROBOTS_TXT)})
    info = await cache.get("example.com", "https", "BitscrapeBot")
    assert not info.can_fetch("https://example.com/no-bitscrape/secret", "BitscrapeBot")


async def test_allowed_path_passes():
    cache, _ = _cache({"https://example.com/robots.txt": (200, ROBOTS_TXT)})
    info = await cache.get("example.com", "https", "BitscrapeBot")
    assert info.can_fetch("https://example.com/allowed-page", "BitscrapeBot")


async def test_allow_overrides_a_broader_disallow():
    cache, _ = _cache({"https://example.com/robots.txt": (200, ROBOTS_TXT)})
    info = await cache.get("example.com", "https", "OtherBot")
    assert not info.can_fetch("https://example.com/private/secret", "OtherBot")
    assert info.can_fetch("https://example.com/private/public-notice", "OtherBot")


async def test_end_of_url_wildcard_pattern():
    """`$` and `*` wildcards: unsupported by stdlib robotparser, standard in
    the real-world convention Protego implements."""
    cache, _ = _cache({"https://example.com/robots.txt": (200, ROBOTS_TXT)})
    info = await cache.get("example.com", "https", "OtherBot")
    assert not info.can_fetch("https://example.com/files/report.pdf", "OtherBot")
    assert info.can_fetch("https://example.com/files/report.pdf.html", "OtherBot")


async def test_most_specific_user_agent_group_wins():
    cache, _ = _cache({"https://example.com/robots.txt": (200, ROBOTS_TXT)})
    generic = await cache.get("example.com", "https", "OtherBot")
    specific = await cache.get("example.com", "https", "BitscrapeBot")
    # /no-bitscrape/ is only disallowed for the specific group.
    assert generic.can_fetch("https://example.com/no-bitscrape/x", "OtherBot")
    assert not specific.can_fetch("https://example.com/no-bitscrape/x", "BitscrapeBot")


# ---------------------------------------------------------------------------
# Crawl-delay
# ---------------------------------------------------------------------------


async def test_crawl_delay_uses_the_specific_group():
    cache, _ = _cache({"https://example.com/robots.txt": (200, ROBOTS_TXT)})
    info = await cache.get("example.com", "https", "BitscrapeBot")
    assert info.crawl_delay == 2.0


async def test_crawl_delay_falls_back_to_wildcard():
    cache, _ = _cache({"https://example.com/robots.txt": (200, ROBOTS_TXT)})
    info = await cache.get("example.com", "https", "SomeOtherBot")
    assert info.crawl_delay == 10.0


# ---------------------------------------------------------------------------
# Request-rate (the new directive)
# ---------------------------------------------------------------------------


async def test_request_rate_is_parsed_and_turned_into_a_delay():
    cache, _ = _cache({"https://example.com/robots.txt": (200, ROBOTS_TXT)})
    info = await cache.get("example.com", "https", "BitscrapeBot")
    assert info.request_rate is not None
    assert (info.request_rate.requests, info.request_rate.seconds) == (1, 5)
    assert info.request_rate.delay == pytest.approx(5.0)


async def test_effective_delay_is_the_max_of_crawl_delay_and_request_rate():
    # BitscrapeBot group: Crawl-delay 2, Request-rate 1/5 (delay 5) -> 5 wins.
    cache, _ = _cache({"https://example.com/robots.txt": (200, ROBOTS_TXT)})
    info = await cache.get("example.com", "https", "BitscrapeBot")
    assert info.effective_delay() == pytest.approx(5.0)


async def test_request_rate_time_window_gates_the_delay():
    cache, _ = _cache({"https://example.com/robots.txt": (200, WINDOWED_ROBOTS_TXT)})
    info = await cache.get("example.com", "https", "AnyBot")
    inside = datetime.time(7, 0)
    outside = datetime.time(23, 0)
    assert info.effective_delay(inside) == pytest.approx(2.0)
    assert info.effective_delay(outside) is None  # window doesn't cover "now"


async def test_request_rate_window_is_re_evaluated_per_call_not_cached():
    """Regression: baking `in_window` in at fetch time would keep a cached
    entry stuck in/out of its window for the whole cache_ttl."""
    cache, _ = _cache({"https://example.com/robots.txt": (200, WINDOWED_ROBOTS_TXT)})
    info = await cache.get("example.com", "https", "AnyBot")  # fetched once
    assert info.effective_delay(datetime.time(7, 0)) == pytest.approx(2.0)
    assert info.effective_delay(datetime.time(23, 0)) is None
    assert info.effective_delay(datetime.time(7, 0)) == pytest.approx(2.0)  # back "in window"


async def test_overnight_request_rate_window_wraps_midnight():
    cache, _ = _cache(
        {"https://example.com/robots.txt": (200, "User-agent: *\nRequest-rate: 1/3 2200-0600\n")}
    )
    info = await cache.get("example.com", "https", "AnyBot")
    assert info.effective_delay(datetime.time(23, 0)) == pytest.approx(3.0)
    assert info.effective_delay(datetime.time(3, 0)) == pytest.approx(3.0)
    assert info.effective_delay(datetime.time(12, 0)) is None


async def test_no_request_rate_directive_leaves_only_crawl_delay():
    cache, _ = _cache({"https://example.com/robots.txt": (200, "User-agent: *\nCrawl-delay: 4\n")})
    info = await cache.get("example.com", "https", "AnyBot")
    assert info.request_rate is None
    assert info.effective_delay() == pytest.approx(4.0)


async def test_neither_directive_present_gives_no_delay():
    cache, _ = _cache({"https://example.com/robots.txt": (200, "User-agent: *\nDisallow:\n")})
    info = await cache.get("example.com", "https", "AnyBot")
    assert info.effective_delay() is None


# ---------------------------------------------------------------------------
# Sitemaps
# ---------------------------------------------------------------------------


async def test_sitemaps_are_collected():
    cache, _ = _cache({"https://example.com/robots.txt": (200, ROBOTS_TXT)})
    info = await cache.get("example.com", "https", "AnyBot")
    assert info.sitemaps == [
        "https://example.com/sitemap.xml",
        "https://example.com/sitemap-news.xml",
    ]


# ---------------------------------------------------------------------------
# No robots.txt / fetch failures / fail-safe policy
# ---------------------------------------------------------------------------


async def test_404_means_unrestricted_access():
    cache, _ = _cache({"https://example.com/robots.txt": (404, "")})
    info = await cache.get("example.com", "https", "AnyBot")
    assert info.ok and info.parser is None
    assert info.can_fetch("https://example.com/anything", "AnyBot")


async def test_other_4xx_also_means_unrestricted():
    cache, _ = _cache({"https://example.com/robots.txt": (401, "")})
    info = await cache.get("example.com", "https", "AnyBot")
    assert info.can_fetch("https://example.com/anything", "AnyBot")


async def test_first_ever_fetch_failure_fails_safe():
    cache, _ = _cache({"https://example.com/robots.txt": TimeoutError("slow")})
    info = await cache.get("example.com", "https", "AnyBot")
    assert not info.ok
    assert not info.can_fetch("https://example.com/anything", "AnyBot")


async def test_5xx_also_fails_safe():
    cache, _ = _cache({"https://example.com/robots.txt": (503, "")})
    info = await cache.get("example.com", "https", "AnyBot")
    assert not info.can_fetch("https://example.com/x", "AnyBot")


async def test_malformed_body_fails_safe_not_open():
    # Protego is lenient, but feed a type that would break parsing to be sure
    # a parser-level exception still fails safe rather than propagating.
    cache, _fetcher = _cache({"https://example.com/robots.txt": (200, ROBOTS_TXT)})

    class ExplodingParse:
        @staticmethod
        def parse(_text):
            raise ValueError("boom")

    import bitscrape.middleware.robots as robots_mod

    original = robots_mod.Protego
    robots_mod.Protego = ExplodingParse  # type: ignore[assignment]
    try:
        info = await cache.get("example.com", "https", "AnyBot")
    finally:
        robots_mod.Protego = original
    assert not info.ok


# ---------------------------------------------------------------------------
# Stale-while-revalidate: a refresh failure keeps the last known-good rules
# ---------------------------------------------------------------------------


async def test_refresh_failure_keeps_last_known_good_rules():
    url = "https://example.com/robots.txt"
    cache, fetcher = _cache(
        {url: [(200, ROBOTS_TXT), TimeoutError("down")]},
        cache_ttl=1.0,
    )
    clock = FakeClock(0.0)
    cache._clock = clock
    good = await cache.get("example.com", "https", "BitscrapeBot")
    assert good.ok and good.crawl_delay == 2.0

    clock.t = 2.0  # past cache_ttl: a refetch is attempted, and fails
    refreshed = await cache.get("example.com", "https", "BitscrapeBot")
    assert refreshed.ok  # NOT flipped to fail-safe-block
    assert refreshed.crawl_delay == 2.0  # still enforcing what we knew
    assert refreshed.stale is True
    assert refreshed.can_fetch("https://example.com/no-bitscrape/x", "BitscrapeBot") is False
    assert len(fetcher.calls) == 2


async def test_negative_ttl_avoids_hammering_a_down_domain():
    """The original implementation refetched on *every single request* while a
    domain was failing. It must now be attempted at most once per negative_ttl."""
    url = "https://example.com/robots.txt"
    cache, fetcher = _cache({url: TimeoutError("down")}, negative_ttl=5.0, cache_ttl=3600.0)
    clock = FakeClock(0.0)
    cache._clock = clock
    for _ in range(20):
        await cache.get("example.com", "https", "AnyBot")
    assert len(fetcher.calls) == 1  # cached (as a failure) after the first attempt

    clock.t = 5.0  # negative_ttl elapsed: eligible for one more attempt
    await cache.get("example.com", "https", "AnyBot")
    assert len(fetcher.calls) == 2


async def test_successful_refresh_updates_the_rules():
    url = "https://example.com/robots.txt"
    cache, _fetcher = _cache(
        {url: [(200, "User-agent: *\nDisallow: /a/\n"), (200, "User-agent: *\nDisallow: /b/\n")]},
        cache_ttl=1.0,
    )
    clock = FakeClock(0.0)
    cache._clock = clock
    first = await cache.get("example.com", "https", "AnyBot")
    assert not first.can_fetch("https://example.com/a/x", "AnyBot")
    clock.t = 2.0
    second = await cache.get("example.com", "https", "AnyBot")
    assert second.can_fetch("https://example.com/a/x", "AnyBot")
    assert not second.can_fetch("https://example.com/b/x", "AnyBot")


# ---------------------------------------------------------------------------
# Per-domain concurrency-safe fetching
# ---------------------------------------------------------------------------


async def test_concurrent_first_requests_share_one_fetch():
    calls = 0
    gate = asyncio.Event()

    class SlowFetcher:
        async def fetch(self, url, *, user_agent, timeout):
            nonlocal calls
            calls += 1
            await gate.wait()
            return 200, ROBOTS_TXT.encode()

    cache = RobotsCache(fetcher=SlowFetcher())
    tasks = [asyncio.create_task(cache.get("example.com", "https", "AnyBot")) for _ in range(10)]
    await asyncio.sleep(0.01)
    gate.set()
    results = await asyncio.gather(*tasks)
    assert calls == 1
    assert all(r.crawl_delay == 10.0 for r in results)


async def test_different_domains_fetch_independently_and_concurrently():
    order: list[str] = []

    class RecordingFetcher:
        async def fetch(self, url, *, user_agent, timeout):
            order.append(f"start:{url}")
            await asyncio.sleep(0.01)
            order.append(f"end:{url}")
            return 200, b"User-agent: *\nDisallow:\n"

    cache = RobotsCache(fetcher=RecordingFetcher())
    await asyncio.gather(
        cache.get("a.com", "https", "X"),
        cache.get("b.com", "https", "X"),
    )
    # Both starts happen before either end -> they ran concurrently, not serialized.
    assert order.index("start:https://a.com/robots.txt") < order.index("end:https://b.com/robots.txt")
    assert order.index("start:https://b.com/robots.txt") < order.index("end:https://a.com/robots.txt")


async def test_cache_hit_after_first_fetch():
    cache, fetcher = _cache({"https://example.com/robots.txt": (200, ROBOTS_TXT)})
    await cache.get("example.com", "https", "AnyBot")
    await cache.get("example.com", "https", "AnyBot")
    await cache.get("example.com", "https", "AnyBot")
    assert len(fetcher.calls) == 1
    assert cache.stats()["cache_hits"] == 2


# ---------------------------------------------------------------------------
# Bounded cache: LRU eviction + content-size cap
# ---------------------------------------------------------------------------


async def test_lru_eviction_bounds_memory():
    responses = {f"https://d{i}.com/robots.txt": (200, "User-agent: *\nDisallow:\n") for i in range(5)}
    cache, fetcher = _cache(responses, max_domains=3)
    for i in range(5):
        await cache.get(f"d{i}.com", "https", "X")
    assert len(cache._cache) == 3
    assert set(cache._cache) == {"d2.com", "d3.com", "d4.com"}  # the 3 most recently used
    # d0 was evicted: asking again re-fetches rather than reusing a stale slot.
    await cache.get("d0.com", "https", "X")
    assert fetcher.calls.count(("https://d0.com/robots.txt", "X", cache.timeout)) == 2


async def test_getting_a_cached_domain_refreshes_its_lru_position():
    responses = {f"https://d{i}.com/robots.txt": (200, "User-agent: *\nDisallow:\n") for i in range(3)}
    cache, _ = _cache(responses, max_domains=2)
    await cache.get("d0.com", "https", "X")
    await cache.get("d1.com", "https", "X")
    await cache.get("d0.com", "https", "X")  # touch d0 again -> d1 becomes LRU
    await cache.get("d2.com", "https", "X")  # evicts d1, not d0
    assert set(cache._cache) == {"d0.com", "d2.com"}


async def test_oversized_robots_txt_is_truncated_not_rejected():
    huge = "User-agent: *\n" + ("Disallow: /x\n" * 100_000)  # comfortably over 500 KiB
    assert len(huge.encode()) > 500 * 1024
    cache, _ = _cache({"https://example.com/robots.txt": (200, huge)}, max_content_bytes=1024)
    info = await cache.get("example.com", "https", "X")
    assert info.ok and info.parser is not None  # parsed the truncated prefix, didn't fail safe


# ---------------------------------------------------------------------------
# domain_of / UA token
# ---------------------------------------------------------------------------


def test_domain_of():
    assert domain_of("https://example.com:8080/a/b") == "example.com:8080"


async def test_ua_token_strips_version_and_comment_so_it_cant_stray_match():
    cache, _ = _cache(
        {"https://example.com/robots.txt": (200, "User-agent: BitscrapeBot\nDisallow: /x/\n")}
    )
    info = await cache.get("example.com", "https", "BitscrapeBot/2.1 (+https://example.org/bot)")
    assert not info.can_fetch("https://example.com/x/y", "BitscrapeBot")


# ---------------------------------------------------------------------------
# RobotsMiddleware: process_request adapter
# ---------------------------------------------------------------------------


def _spider(user_agent: str = "BitscrapeBot/0.1", **overrides: Any) -> Any:
    overrides.setdefault("robotstxt_obey", True)
    return SimpleNamespace(settings=SimpleNamespace(user_agent=user_agent, **overrides))


async def test_middleware_raises_dropitem_for_disallowed_request():
    mw = RobotsMiddleware(_cache({"https://example.com/robots.txt": (200, ROBOTS_TXT)})[0])
    with pytest.raises(DropItem):
        await mw.process_request(Request(url="https://example.com/no-bitscrape/secret"), _spider())


async def test_middleware_allows_and_annotates_meta():
    mw = RobotsMiddleware(_cache({"https://example.com/robots.txt": (200, ROBOTS_TXT)})[0])
    req = Request(url="https://example.com/allowed-page")
    result = await mw.process_request(req, _spider())
    assert result is None
    assert req.meta["crawl_delay"] == 5.0  # BitscrapeBot: max(Crawl-delay 2, Request-rate 1/5 -> 5)
    assert req.meta["request_rate"]["requests"] == 1
    assert req.meta["sitemaps"][0] == "https://example.com/sitemap.xml"


async def test_middleware_skips_everything_when_disabled():
    fetcher = FakeFetcher({})
    mw = RobotsMiddleware(RobotsCache(fetcher=fetcher))
    req = Request(url="https://example.com/no-bitscrape/secret")
    result = await mw.process_request(req, _spider(robotstxt_obey=False))
    assert result is None and fetcher.calls == []


async def test_middleware_fetch_failure_raises_dropitem():
    mw = RobotsMiddleware(_cache({"https://slow.com/robots.txt": TimeoutError("x")})[0])
    with pytest.raises(DropItem, match="unavailable"):
        await mw.process_request(Request(url="https://slow.com/page"), _spider())


async def test_middleware_reuses_one_fetch_per_domain():
    cache, fetcher = _cache({"https://example.com/robots.txt": (200, ROBOTS_TXT)})
    mw = RobotsMiddleware(cache)
    await mw.process_request(Request(url="https://example.com/a"), _spider())
    await mw.process_request(Request(url="https://example.com/b"), _spider())
    assert len(fetcher.calls) == 1


async def test_middleware_respects_robots_user_agent_override():
    cache, fetcher = _cache({"https://example.com/robots.txt": (200, ROBOTS_TXT)})
    mw = RobotsMiddleware(cache)
    req = Request(url="https://example.com/no-bitscrape/x")
    with pytest.raises(DropItem):
        await mw.process_request(req, _spider(user_agent="Whatever", robots_user_agent="BitscrapeBot"))
    assert fetcher.calls[0][1] == "BitscrapeBot"


async def test_middleware_can_disable_request_rate_meta():
    cache, _ = _cache({"https://example.com/robots.txt": (200, ROBOTS_TXT)})
    mw = RobotsMiddleware(cache)
    req = Request(url="https://example.com/allowed-page")
    await mw.process_request(req, _spider(robots_respect_request_rate=False))
    assert "request_rate" not in req.meta
    assert req.meta["crawl_delay"] == 5.0  # still folded into the effective delay


async def test_middleware_stats_bridge():
    cache, _ = _cache({"https://example.com/robots.txt": (200, ROBOTS_TXT)})
    mw = RobotsMiddleware(cache)
    await mw.process_request(Request(url="https://example.com/a"), _spider())
    assert mw.stats()["fetches"] == 1
    assert "example.com" in mw._cache


# ---------------------------------------------------------------------------
# Settings validation
# ---------------------------------------------------------------------------


def test_robots_settings_have_sane_defaults():
    s = Settings()
    assert s.robotstxt_obey is True
    assert s.robots_cache_ttl == 3600.0
    assert s.robots_negative_cache_ttl == 30.0
    assert s.robots_max_cache_domains == 10_000
    assert s.robots_max_content_bytes == 500 * 1024
    assert s.robots_respect_request_rate is True


def test_robots_settings_reject_bad_values():
    with pytest.raises(Exception):  # noqa: B017 -- pydantic ValidationError
        Settings(robots_timeout=0)
    with pytest.raises(Exception):  # noqa: B017
        Settings(robots_max_cache_domains=0)


# ---------------------------------------------------------------------------
# End-to-end: a real local server via build_engine()/factory
# ---------------------------------------------------------------------------


def _robots_app(body: str, *, status: int = 200) -> web.Application:
    async def robots(request: web.Request) -> web.Response:
        return web.Response(status=status, text=body)

    async def page(request: web.Request) -> web.Response:
        return web.Response(text=f"<html><body>{request.path}</body></html>")

    app = web.Application()
    app.router.add_get("/robots.txt", robots)
    app.router.add_get("/{tail:.*}", page)
    return app


@pytest.fixture
async def robots_server():
    server = TestServer(_robots_app("User-agent: *\nDisallow: /private/\nCrawl-delay: 0.05\n"))
    await server.start_server()
    try:
        yield server
    finally:
        await server.close()


class _RobotsSpider(Spider):
    name = "robots_e2e"

    def __init__(self, base: str, **kw: Any) -> None:
        super().__init__(**kw)
        self.base = base
        self.parsed: list[str] = []

    def start_requests(self):
        return [
            Request(url=f"{self.base}/allowed", dont_filter=True),
            Request(url=f"{self.base}/private/secret", dont_filter=True),
        ]

    async def parse(self, response):
        self.parsed.append(response.url)
        yield {"url": response.url}


async def test_end_to_end_disallowed_url_is_never_fetched(robots_server):
    base = str(robots_server.make_url("")).rstrip("/")
    spider = _RobotsSpider(base)
    engine = await build_engine(spider, spider.settings)
    stats = await engine.run()
    assert spider.parsed == [f"{base}/allowed"]
    assert stats.items_scraped == 1


async def test_end_to_end_can_be_disabled_via_settings(robots_server):
    base = str(robots_server.make_url("")).rstrip("/")
    spider = _RobotsSpider(base)
    settings = spider.settings.model_copy(update={"robotstxt_obey": False})
    engine = await build_engine(spider, settings)
    stats = await engine.run()
    assert sorted(spider.parsed) == sorted([f"{base}/allowed", f"{base}/private/secret"])
    assert stats.items_scraped == 2
