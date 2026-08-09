"""
Tests for bitscrape.monitoring.StatsMonitor -- a real aiohttp server is
started and hit with a real HTTP client (not mocked), verifying the live
stats feed actually works end-to-end.
"""

from __future__ import annotations

from types import SimpleNamespace

import aiohttp
import pytest

from bitscrape.core.models import CrawlStats
from bitscrape.monitoring import StatsMonitor, StatsSnapshot
from bitscrape.plugins import PluginManager


def _fake_stats(**overrides):
    base = dict(
        requests_made=10,
        requests_failed=1,
        responses_received=9,
        items_scraped=5,
        items_dropped=1,
        items_noindexed=0,
        links_nofollow_skipped=0,
        bytes_downloaded=2048,
    )
    base.update(overrides)
    return SimpleNamespace(**base, elapsed=1.5, rps=6.0)


# ---------------------------------------------------------------------------
# StatsSnapshot (pure logic, no server)
# ---------------------------------------------------------------------------


def test_snapshot_includes_all_core_stats_fields():
    snap = StatsSnapshot(lambda: _fake_stats())
    data = snap.to_dict()
    assert data["requests_made"] == 10
    assert data["items_scraped"] == 5
    assert data["bytes_downloaded"] == 2048
    assert "cpu_percent" in data
    assert "memory_mb" in data


def test_snapshot_works_with_real_crawlstats_model():
    stats = CrawlStats(requests_made=3, items_scraped=2)
    snap = StatsSnapshot(lambda: stats)
    data = snap.to_dict()
    assert data["requests_made"] == 3
    assert data["items_scraped"] == 2


def test_snapshot_extra_fields_are_merged_in():
    snap = StatsSnapshot(lambda: _fake_stats(), extra={"crawl_name": "my_spider"})
    data = snap.to_dict()
    assert data["crawl_name"] == "my_spider"


# ---------------------------------------------------------------------------
# StatsMonitor -- real HTTP server, real HTTP client
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_json_endpoint_returns_live_stats():
    monitor = StatsMonitor(stats_getter=lambda: _fake_stats(), host="127.0.0.1", port=18765)
    await monitor.start()
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get("http://127.0.0.1:18765/stats.json") as resp:
                assert resp.status == 200
                data = await resp.json()
                assert data["items_scraped"] == 5
                assert data["requests_made"] == 10
    finally:
        await monitor.stop()


@pytest.mark.asyncio
async def test_html_endpoint_renders_and_autorefreshes():
    monitor = StatsMonitor(stats_getter=lambda: _fake_stats(), host="127.0.0.1", port=18766)
    await monitor.start()
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get("http://127.0.0.1:18766/") as resp:
                assert resp.status == 200
                text = await resp.text()
                assert "items_scraped" in text
                assert "5" in text
                assert 'http-equiv="refresh"' in text
    finally:
        await monitor.stop()


@pytest.mark.asyncio
async def test_stats_reflect_live_changes_across_requests():
    """The feed must reflect CURRENT state, not a frozen snapshot at
    start() time -- crucial for it to be useful during a real crawl."""
    live_stats = {"items_scraped": 0}
    monitor = StatsMonitor(
        stats_getter=lambda: SimpleNamespace(
            requests_made=0,
            requests_failed=0,
            responses_received=0,
            items_scraped=live_stats["items_scraped"],
            items_dropped=0,
            items_noindexed=0,
            links_nofollow_skipped=0,
            bytes_downloaded=0,
            elapsed=0.0,
            rps=0.0,
        ),
        host="127.0.0.1",
        port=18767,
    )
    await monitor.start()
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get("http://127.0.0.1:18767/stats.json") as resp:
                assert (await resp.json())["items_scraped"] == 0

            live_stats["items_scraped"] = 42

            async with session.get("http://127.0.0.1:18767/stats.json") as resp:
                assert (await resp.json())["items_scraped"] == 42
    finally:
        await monitor.stop()


@pytest.mark.asyncio
async def test_stop_actually_releases_the_port():
    monitor = StatsMonitor(stats_getter=lambda: _fake_stats(), host="127.0.0.1", port=18768)
    await monitor.start()
    await monitor.stop()

    # A second monitor should be able to bind the same port now.
    monitor2 = StatsMonitor(stats_getter=lambda: _fake_stats(), host="127.0.0.1", port=18768)
    await monitor2.start()
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get("http://127.0.0.1:18768/stats.json") as resp:
                assert resp.status == 200
    finally:
        await monitor2.stop()


def test_snapshot_convenience_accessor_without_server():
    monitor = StatsMonitor(stats_getter=lambda: _fake_stats())
    data = monitor.snapshot()
    assert data["items_scraped"] == 5


# ---------------------------------------------------------------------------
# Plugin lifecycle integration
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_as_plugin_starts_and_stops_with_spider_lifecycle():
    monitor = StatsMonitor(stats_getter=lambda: _fake_stats(), host="127.0.0.1", port=18769)
    pm = PluginManager()
    pm.register_plugin(monitor.as_plugin())

    await pm.fire("spider_opened", spider=None)
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get("http://127.0.0.1:18769/stats.json") as resp:
                assert resp.status == 200
    finally:
        await pm.fire("spider_closed", spider=None, reason="done")

    # Server should be down now.
    with pytest.raises(aiohttp.ClientConnectorError):
        async with aiohttp.ClientSession() as session:
            async with session.get("http://127.0.0.1:18769/stats.json"):
                pass
