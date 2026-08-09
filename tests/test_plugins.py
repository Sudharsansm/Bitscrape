"""
Tests for bitscrape.plugins: PluginManager hook registration/firing,
BasePlugin auto-registration, and the two example plugins (auth helper,
storage connector).
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from bitscrape.core.models import Request
from bitscrape.plugins import (
    BasePlugin,
    BearerTokenAuthPlugin,
    PluginManager,
    StorageConnectorPlugin,
)


# ---------------------------------------------------------------------------
# PluginManager core
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_sync_callback_is_called():
    pm = PluginManager()
    calls = []
    pm.on("item_scraped", lambda item, spider: calls.append(item))
    await pm.fire("item_scraped", item={"x": 1}, spider=None)
    assert calls == [{"x": 1}]


@pytest.mark.asyncio
async def test_async_callback_is_awaited():
    pm = PluginManager()
    calls = []

    async def handler(item, spider):
        calls.append(item)

    pm.on("item_scraped", handler)
    await pm.fire("item_scraped", item={"x": 1}, spider=None)
    assert calls == [{"x": 1}]


@pytest.mark.asyncio
async def test_multiple_callbacks_for_same_event_all_fire():
    pm = PluginManager()
    calls = []
    pm.on("spider_opened", lambda spider: calls.append("a"))
    pm.on("spider_opened", lambda spider: calls.append("b"))
    await pm.fire("spider_opened", spider=None)
    assert calls == ["a", "b"]


@pytest.mark.asyncio
async def test_broken_callback_does_not_stop_others():
    pm = PluginManager()
    calls = []

    def broken(spider):
        raise ValueError("boom")

    pm.on("spider_opened", broken)
    pm.on("spider_opened", lambda spider: calls.append("still ran"))
    await pm.fire("spider_opened", spider=None)  # should not raise
    assert calls == ["still ran"]


@pytest.mark.asyncio
async def test_off_removes_callback():
    pm = PluginManager()
    calls = []

    def handler(spider):
        calls.append(1)

    pm.on("spider_opened", handler)
    pm.off("spider_opened", handler)
    await pm.fire("spider_opened", spider=None)
    assert calls == []


@pytest.mark.asyncio
async def test_firing_event_with_no_callbacks_is_a_noop():
    pm = PluginManager()
    await pm.fire("item_scraped", item={}, spider=None)  # should not raise


def test_hook_count():
    pm = PluginManager()
    assert pm.hook_count("spider_opened") == 0
    pm.on("spider_opened", lambda spider: None)
    assert pm.hook_count("spider_opened") == 1


def test_unknown_event_warns_but_still_registers(caplog):
    import logging

    pm = PluginManager()
    with caplog.at_level(logging.WARNING):
        pm.on("totally_made_up_event", lambda: None)
    assert pm.hook_count("totally_made_up_event") == 1
    assert any("unknown event" in r.getMessage() for r in caplog.records)


# ---------------------------------------------------------------------------
# BasePlugin auto-registration
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_register_plugin_wires_up_all_overridden_hooks():
    calls = []

    class MyPlugin(BasePlugin):
        async def spider_opened(self, spider):
            calls.append(("opened", spider))

        async def item_scraped(self, item, spider):
            calls.append(("item", item))

    pm = PluginManager()
    pm.register_plugin(MyPlugin())

    await pm.fire("spider_opened", spider="s1")
    await pm.fire("item_scraped", item={"a": 1}, spider="s1")
    await pm.fire("spider_closed", spider="s1", reason="finished")  # not overridden, no-op fine

    assert ("opened", "s1") in calls
    assert ("item", {"a": 1}) in calls


@pytest.mark.asyncio
async def test_base_plugin_unoverridden_hooks_are_harmless_noops():
    pm = PluginManager()
    pm.register_plugin(BasePlugin())
    # Firing every known event against a plain BasePlugin should never raise.
    await pm.fire("spider_opened", spider=None)
    await pm.fire("spider_closed", spider=None, reason="x")
    await pm.fire("request_scheduled", request=None, spider=None)
    await pm.fire("response_received", request=None, response=None, spider=None)
    await pm.fire("item_scraped", item=None, spider=None)
    await pm.fire("item_dropped", item=None, exception=ValueError(), spider=None)
    await pm.fire("error", request=None, exception=ValueError(), spider=None)


# ---------------------------------------------------------------------------
# BearerTokenAuthPlugin
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_bearer_token_injected_for_matching_domain():
    plugin = BearerTokenAuthPlugin(domain="api.example.com", token="secret123")
    req = Request(url="https://api.example.com/data")
    await plugin.request_scheduled(req, spider=None)
    assert req.headers["Authorization"] == "Bearer secret123"


@pytest.mark.asyncio
async def test_bearer_token_not_injected_for_other_domain():
    plugin = BearerTokenAuthPlugin(domain="api.example.com", token="secret123")
    req = Request(url="https://other.com/data")
    await plugin.request_scheduled(req, spider=None)
    assert "Authorization" not in req.headers


@pytest.mark.asyncio
async def test_bearer_token_plugin_wired_through_plugin_manager():
    pm = PluginManager()
    pm.register_plugin(BearerTokenAuthPlugin(domain="api.example.com", token="tok"))
    req = Request(url="https://api.example.com/x")
    await pm.fire("request_scheduled", request=req, spider=None)
    assert req.headers["Authorization"] == "Bearer tok"


# ---------------------------------------------------------------------------
# StorageConnectorPlugin
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_storage_connector_opens_saves_and_closes():
    backend = AsyncMock()
    plugin = StorageConnectorPlugin(backend)

    await plugin.spider_opened(spider=None)
    backend.open.assert_awaited_once()

    await plugin.item_scraped({"title": "x"}, spider=None)
    backend.save_item.assert_awaited_once_with({"title": "x"})

    await plugin.spider_closed(spider=None, reason="finished")
    backend.close.assert_awaited_once()


@pytest.mark.asyncio
async def test_storage_connector_via_plugin_manager_lifecycle():
    backend = AsyncMock()
    pm = PluginManager()
    pm.register_plugin(StorageConnectorPlugin(backend))

    await pm.fire("spider_opened", spider=None)
    await pm.fire("item_scraped", item={"a": 1}, spider=None)
    await pm.fire("item_scraped", item={"a": 2}, spider=None)
    await pm.fire("spider_closed", spider=None, reason="done")

    assert backend.save_item.await_count == 2
    backend.open.assert_awaited_once()
    backend.close.assert_awaited_once()


@pytest.mark.asyncio
async def test_storage_connector_with_real_sqlite_backend(tmp_path):
    """End-to-end with the real SQLite backend, not a mock."""
    from bitscrape.storage.backends import SQLiteStorageBackend

    backend = SQLiteStorageBackend(str(tmp_path / "plugin_test.db"))
    pm = PluginManager()
    pm.register_plugin(StorageConnectorPlugin(backend))

    await pm.fire("spider_opened", spider=None)
    await pm.fire("item_scraped", item={"title": "Real item"}, spider=None)
    await pm.fire("spider_closed", spider=None, reason="done")

    # Reopen independently to verify it was actually persisted.
    verify = SQLiteStorageBackend(str(tmp_path / "plugin_test.db"))
    await verify.open()
    try:
        assert await verify.count() == 1
    finally:
        await verify.close()
