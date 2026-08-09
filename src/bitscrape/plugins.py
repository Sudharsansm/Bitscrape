"""
Bitscrape Plugin System
=======================

A generic hook/signal architecture: plugins register callbacks against
named lifecycle events, and ``PluginManager`` fires them at the right point
in a crawl. This is the extension point for things like authentication
helpers and cloud-storage connectors.

Deliberately NOT included as built-in plugins: CAPTCHA-solving integrations
or "anti-bot" extensions. Those are tooling whose specific purpose is
defeating a site's anti-bot protections against its wishes -- that's a
different category of thing from an extension point, and isn't something
this project ships regardless of how it's packaged.

Built-in lifecycle events:
    spider_opened(spider)
    spider_closed(spider, reason)
    request_scheduled(request, spider)
    response_received(request, response, spider)
    item_scraped(item, spider)
    item_dropped(item, exception, spider)
    error(request, exception, spider)
"""

from __future__ import annotations

import inspect
import logging
from collections import defaultdict
from collections.abc import Callable
from typing import Any

logger = logging.getLogger(__name__)

HookCallback = Callable[..., Any]

KNOWN_EVENTS = frozenset(
    {
        "spider_opened",
        "spider_closed",
        "request_scheduled",
        "response_received",
        "item_scraped",
        "item_dropped",
        "error",
    }
)


class PluginManager:
    """
    Register callbacks (plain functions, coroutine functions, or bound
    methods) against named events; ``fire()`` calls every callback
    registered for that event, awaiting coroutine results. A callback
    raising an exception is logged and does NOT stop other callbacks or the
    crawl -- a broken plugin shouldn't take down the whole run.
    """

    def __init__(self) -> None:
        self._hooks: dict[str, list[HookCallback]] = defaultdict(list)

    def on(self, event: str, callback: HookCallback) -> None:
        if event not in KNOWN_EVENTS:
            logger.warning(
                "Registering callback for unknown event %r (known: %s) -- "
                "this is allowed but likely a typo",
                event,
                sorted(KNOWN_EVENTS),
            )
        self._hooks[event].append(callback)

    def off(self, event: str, callback: HookCallback) -> None:
        if callback in self._hooks.get(event, []):
            self._hooks[event].remove(callback)

    def register_plugin(self, plugin: BasePlugin) -> None:
        """Registers every hook method a BasePlugin subclass defines."""
        for event in KNOWN_EVENTS:
            method = getattr(plugin, event, None)
            if method is not None and callable(method):
                self.on(event, method)

    async def fire(self, event: str, **kwargs: Any) -> None:
        for callback in list(self._hooks.get(event, [])):
            try:
                result = callback(**kwargs)
                if inspect.isawaitable(result):
                    await result
            except Exception:
                logger.exception(
                    "Plugin callback %r for event %r raised",
                    getattr(callback, "__qualname__", callback),
                    event,
                )

    def hook_count(self, event: str) -> int:
        return len(self._hooks.get(event, []))


class BasePlugin:
    """
    Convenience base class: subclass and override only the hooks you need
    as ``async def spider_opened(self, spider): ...``-style methods; unused
    hooks stay as no-ops. Pass an instance to
    ``PluginManager.register_plugin()`` rather than wiring each method by
    hand.
    """

    async def spider_opened(self, spider: Any) -> None: ...
    async def spider_closed(self, spider: Any, reason: str) -> None: ...
    async def request_scheduled(self, request: Any, spider: Any) -> None: ...
    async def response_received(self, request: Any, response: Any, spider: Any) -> None: ...
    async def item_scraped(self, item: Any, spider: Any) -> None: ...
    async def item_dropped(self, item: Any, exception: Exception, spider: Any) -> None: ...
    async def error(self, request: Any, exception: Exception, spider: Any) -> None: ...


# ---------------------------------------------------------------------------
# Example plugin: authentication helper
# ---------------------------------------------------------------------------


class BearerTokenAuthPlugin(BasePlugin):
    """
    Example authentication-helper plugin: injects an ``Authorization:
    Bearer <token>`` header into every scheduled request matching a given
    domain. A concrete, legitimate use of the plugin architecture -- unlike
    CAPTCHA-solving or anti-bot evasion, this is just "attach credentials I
    already have to requests to a site I'm authorized to access."

    This mutates ``request.headers`` in place via the ``request_scheduled``
    hook (Request is a mutable pydantic model field, not the queued copy),
    so register it before the request is enqueued.
    """

    def __init__(self, domain: str, token: str) -> None:
        self._domain = domain
        self._token = token

    async def request_scheduled(self, request: Any, spider: Any) -> None:
        from urllib.parse import urlparse

        if urlparse(request.url).netloc == self._domain:
            request.headers["Authorization"] = f"Bearer {self._token}"


# ---------------------------------------------------------------------------
# Example plugin: cloud storage connector
# ---------------------------------------------------------------------------


class StorageConnectorPlugin(BasePlugin):
    """
    Example cloud-storage-connector plugin: writes every scraped item to a
    ``BaseStorageBackend`` (see ``bitscrape.storage.backends``) as it's
    scraped, in addition to whatever the spider's normal export path does.
    Opens the backend on ``spider_opened`` and closes it on
    ``spider_closed``, so a spider just needs:

        plugin_manager.register_plugin(StorageConnectorPlugin(SQLiteStorageBackend("out.db")))
    """

    def __init__(self, backend: Any) -> None:
        self._backend = backend

    async def spider_opened(self, spider: Any) -> None:
        await self._backend.open()

    async def item_scraped(self, item: Any, spider: Any) -> None:
        await self._backend.save_item(item)

    async def spider_closed(self, spider: Any, reason: str) -> None:
        await self._backend.close()
