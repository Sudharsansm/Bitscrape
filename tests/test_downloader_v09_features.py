"""
Tests for the 0.9 downloader features: proxy rotation, dev-mode disk
cache/replay, and blocked-request detection + retry. Uses a real local
aiohttp test server where behaviour needs to be verified end-to-end, same
convention as ``tests/test_downloader_features.py``.
"""

from __future__ import annotations

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from bitscrape.core.models import Request
from bitscrape.core.settings import Settings
from bitscrape.downloader.blocked import BlockedRequestDetector
from bitscrape.downloader.blocking import DomainBlocker
from bitscrape.downloader.devcache import DevModeCache
from bitscrape.downloader.downloader import Downloader
from bitscrape.downloader.proxy import ProxyRotator

# ---------------------------------------------------------------------------
# ProxyRotator (pure unit tests)
# ---------------------------------------------------------------------------


def test_round_robin_cycles_through_all_proxies_in_order():
    rotator = ProxyRotator(["p1", "p2", "p3"], strategy="round_robin")
    seen = [rotator.get_proxy() for _ in range(6)]
    assert seen == ["p1", "p2", "p3", "p1", "p2", "p3"]


def test_least_used_prefers_proxy_with_fewest_uses():
    rotator = ProxyRotator(["p1", "p2"], strategy="least_used")
    assert rotator.get_proxy() == "p1"  # tie -> first with min uses (0)
    assert rotator.get_proxy() == "p2"  # p1 now has 1 use, p2 has 0
    assert rotator.get_proxy() in ("p1", "p2")  # both now have 1 use


def test_sticky_keeps_same_proxy_for_same_domain():
    rotator = ProxyRotator(["p1", "p2", "p3"], strategy="sticky")
    first = rotator.get_proxy(domain="example.com")
    for _ in range(5):
        assert rotator.get_proxy(domain="example.com") == first


def test_ban_after_threshold_failures_excludes_proxy():
    rotator = ProxyRotator(["only-proxy"], strategy="random", ban_threshold=2, ban_seconds=999)
    rotator.report_failure("only-proxy")
    assert rotator.get_proxy() == "only-proxy"  # 1 failure: not banned yet
    rotator.report_failure("only-proxy")
    assert rotator.get_proxy() is None  # 2 failures hit threshold: now banned


def test_success_resets_consecutive_failure_count():
    rotator = ProxyRotator(["only-proxy"], ban_threshold=2, ban_seconds=999)
    rotator.report_failure("only-proxy")
    rotator.report_success("only-proxy")
    rotator.report_failure("only-proxy")
    assert rotator.get_proxy() == "only-proxy"  # reset means this is failure #1, not #2


def test_empty_pool_returns_none():
    rotator = ProxyRotator([])
    assert rotator.get_proxy() is None


def test_from_file_reads_one_proxy_per_line_and_skips_comments(tmp_path):
    f = tmp_path / "proxies.txt"
    f.write_text("http://p1:8080\n# a comment\n\nhttp://p2:8080\n")
    rotator = ProxyRotator.from_file(f)
    assert len(rotator) == 2


# ---------------------------------------------------------------------------
# DevModeCache (pure unit tests)
# ---------------------------------------------------------------------------


def test_devcache_miss_then_put_then_hit(tmp_path):
    cache = DevModeCache(tmp_path)
    assert cache.get("GET", "https://example.com") is None
    cache.put("GET", "https://example.com", 200, {"X": "y"}, b"hello")
    hit = cache.get("GET", "https://example.com")
    assert hit is not None
    assert hit["body"] == b"hello"
    assert hit["status"] == 200


def test_devcache_does_not_store_non_cacheable_status(tmp_path):
    cache = DevModeCache(tmp_path, cacheable_statuses=[200])
    stored = cache.put("GET", "https://example.com", 500, {}, b"error page")
    assert stored is False
    assert cache.get("GET", "https://example.com") is None


def test_devcache_expires_after_ttl(tmp_path):
    cache = DevModeCache(tmp_path, ttl=1.0)
    cache.put("GET", "https://example.com", 200, {}, b"hello")
    assert cache.get("GET", "https://example.com") is not None

    import json

    path = next(cache._dir.glob("*.json"))
    raw = json.loads(path.read_text())
    raw["cached_at"] = 0.0  # force it to look ancient relative to ttl=1.0
    path.write_text(json.dumps(raw))
    assert cache.get("GET", "https://example.com") is None


def test_devcache_ttl_zero_never_expires(tmp_path):
    cache = DevModeCache(tmp_path, ttl=0.0)
    cache.put("GET", "https://example.com", 200, {}, b"hello")
    import json

    path = next(cache._dir.glob("*.json"))
    raw = json.loads(path.read_text())
    raw["cached_at"] = 0.0
    path.write_text(json.dumps(raw))
    assert cache.get("GET", "https://example.com") is not None


def test_devcache_clear_removes_all_entries(tmp_path):
    cache = DevModeCache(tmp_path)
    cache.put("GET", "https://a.com", 200, {}, b"a")
    cache.put("GET", "https://b.com", 200, {}, b"b")
    assert len(cache) == 2
    removed = cache.clear()
    assert removed == 2
    assert len(cache) == 0


# ---------------------------------------------------------------------------
# BlockedRequestDetector (pure unit tests)
# ---------------------------------------------------------------------------


def test_detects_block_by_status_code():
    d = BlockedRequestDetector(blocked_http_codes=frozenset({403}))
    assert d.is_blocked(403) is True
    assert d.is_blocked(200) is False


def test_detects_block_by_body_marker():
    d = BlockedRequestDetector(
        blocked_http_codes=frozenset(), extra_markers=("access denied", r"rate.limited")
    )
    assert d.is_blocked(200, b"<html>Access Denied</html>") is True
    assert d.is_blocked(200, b"<html>You have been Rate-Limited</html>") is True
    assert d.is_blocked(200, b"<html>all good</html>") is False


def test_backoff_seconds_doubles_and_caps():
    d = BlockedRequestDetector(
        blocked_http_codes=frozenset(), backoff_base=2.0, backoff_max=10.0
    )
    assert d.backoff_seconds(1) == 2.0
    assert d.backoff_seconds(2) == 4.0
    assert d.backoff_seconds(3) == 8.0
    assert d.backoff_seconds(4) == 10.0  # capped


def test_from_settings_reads_all_blocked_fields():
    settings = Settings(
        blocked_detection_enabled=True,
        blocked_http_codes=[451],
        blocked_extra_markers=["captcha"],
        blocked_max_retries=5,
        blocked_retry_backoff=1.0,
        blocked_max_backoff=8.0,
    )
    d = BlockedRequestDetector.from_settings(settings)
    assert d.is_blocked(451) is True
    assert d.is_blocked(200, b"please solve this CAPTCHA") is True
    assert d.max_retries == 5


# ---------------------------------------------------------------------------
# DomainBlocker (pure unit tests)
# ---------------------------------------------------------------------------


def test_domain_blocker_matches_subdomains():
    blocker = DomainBlocker(extra_domains=["ads.example.com"])
    assert blocker.is_blocked_url("https://ads.example.com/x") is True
    assert blocker.is_blocked_url("https://sub.ads.example.com/x") is True
    assert blocker.is_blocked_url("https://example.com/x") is False


def test_domain_blocker_builtin_ads_list():
    blocker = DomainBlocker(block_ads=True)
    assert blocker.is_blocked_url("https://www.googletagmanager.com/gtm.js") is True
    assert blocker.is_blocked_url("https://example.com/") is False


def test_domain_blocker_resource_type():
    blocker = DomainBlocker(block_resource_types=["image", "font"])
    assert blocker.is_blocked_resource_type("image") is True
    assert blocker.is_blocked_resource_type("document") is False


def test_domain_blocker_hosts_file(tmp_path):
    f = tmp_path / "hosts.txt"
    f.write_text("0.0.0.0 tracker.example.com\nplain-list-domain.com\n# comment\n")
    blocker = DomainBlocker(hosts_file=str(f))
    assert blocker.is_blocked_url("https://tracker.example.com/x") is True
    assert blocker.is_blocked_url("https://plain-list-domain.com/x") is True


# ---------------------------------------------------------------------------
# End-to-end: Downloader wiring, against a real local server
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dev_mode_serves_from_cache_without_hitting_server_again(tmp_path):
    hits = {"count": 0}

    async def handler(request: web.Request) -> web.Response:
        hits["count"] += 1
        return web.Response(text="hello world")

    app = web.Application()
    app.router.add_get("/page", handler)
    server = TestServer(app)
    await server.start_server()
    try:
        url = f"http://{server.host}:{server.port}/page"
        settings = Settings(dev_mode=True, dev_cache_dir=str(tmp_path))
        downloader = Downloader(settings)
        await downloader.open()
        try:
            r1 = await downloader.fetch(Request(url=url))
            r2 = await downloader.fetch(Request(url=url))
            assert r1.body == r2.body == b"hello world"
            assert hits["count"] == 1  # second fetch served entirely from disk
        finally:
            await downloader.close()
    finally:
        await server.close()


@pytest.mark.asyncio
async def test_blocked_detection_retries_then_succeeds():
    attempts = {"count": 0}

    async def handler(request: web.Request) -> web.Response:
        attempts["count"] += 1
        if attempts["count"] < 3:
            return web.Response(status=200, text="Access Denied - blocked")
        return web.Response(status=200, text="real content")

    app = web.Application()
    app.router.add_get("/page", handler)
    server = TestServer(app)
    await server.start_server()
    try:
        url = f"http://{server.host}:{server.port}/page"
        settings = Settings(
            blocked_detection_enabled=True,
            blocked_extra_markers=["access denied"],
            blocked_max_retries=5,
            blocked_retry_backoff=0.01,
            blocked_max_backoff=0.02,
        )
        downloader = Downloader(settings)
        await downloader.open()
        try:
            response = await downloader.fetch(Request(url=url))
            assert response.text == "real content"
            assert attempts["count"] == 3
        finally:
            await downloader.close()
    finally:
        await server.close()


@pytest.mark.asyncio
async def test_blocked_detection_gives_up_after_max_retries():
    async def handler(request: web.Request) -> web.Response:
        return web.Response(status=200, text="Access Denied - blocked forever")

    app = web.Application()
    app.router.add_get("/page", handler)
    server = TestServer(app)
    await server.start_server()
    try:
        url = f"http://{server.host}:{server.port}/page"
        settings = Settings(
            blocked_detection_enabled=True,
            blocked_extra_markers=["access denied"],
            blocked_max_retries=2,
            blocked_retry_backoff=0.01,
            blocked_max_backoff=0.02,
        )
        downloader = Downloader(settings)
        await downloader.open()
        try:
            response = await downloader.fetch(Request(url=url))
            # Gives up and returns the (still blocked-looking) response
            # rather than raising -- the caller/spider can inspect it.
            assert "Access Denied" in response.text
        finally:
            await downloader.close()
    finally:
        await server.close()


@pytest.mark.asyncio
async def test_unimplemented_session_kind_raises_clear_error():
    settings = Settings(sessions={"stealthy": {"kind": "stealth"}})
    downloader = Downloader(settings)
    await downloader.open()
    try:
        with pytest.raises(NotImplementedError, match="stealth"):
            await downloader.fetch(Request(url="https://example.com", session="stealthy"))
    finally:
        await downloader.close()


@pytest.mark.asyncio
async def test_undeclared_session_name_raises_value_error():
    downloader = Downloader(Settings())
    await downloader.open()
    try:
        with pytest.raises(ValueError, match="isn't declared"):
            await downloader.fetch(Request(url="https://example.com", session="nope"))
    finally:
        await downloader.close()


def test_spider_sessions_merge_into_settings():
    from bitscrape.core.spider import Spider

    class MySpider(Spider):
        name = "s"
        sessions = {"fast": {"kind": "http"}}

    spider = MySpider()
    assert spider.settings.sessions["fast"] == {"kind": "http"}
