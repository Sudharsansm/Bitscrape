"""
Bitscrape Monitoring
====================

A lightweight, LOCAL live-stats feed for a running crawl: current
CrawlStats plus process CPU/RAM, served as JSON (for scripts/automation) and
a small auto-refreshing HTML page (for a human watching a terminal-less
crawl). This is NOT a hosted, multi-crawl, multi-user dashboard product --
it's a single small aiohttp.web server you run alongside one crawl, on your
own machine or network. Wire it up via the plugin system:

    from bitscrape.monitoring import StatsMonitor
    from bitscrape.plugins import PluginManager

    monitor = StatsMonitor(engine_stats_getter=lambda: engine.stats, port=8765)
    await monitor.start()
    pm = PluginManager()
    pm.register_plugin(monitor.as_plugin())
    ...
    await monitor.stop()

Then open http://localhost:8765/ for the HTML view, or GET
http://localhost:8765/stats.json for raw JSON (e.g. to feed your own
Grafana/Prometheus scrape, which is the realistic path to an actual
production dashboard -- this module deliberately doesn't reinvent that).
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from typing import Any

from bitscrape.plugins import BasePlugin

logger = logging.getLogger(__name__)


def _process_resource_usage() -> dict[str, float]:
    """Real CPU/RAM usage for the current process, via psutil. Returns
    zeros (rather than raising) if psutil isn't installed, since monitoring
    is optional and shouldn't be a hard dependency for running a crawl."""
    try:
        import psutil

        proc = psutil.Process()
        return {
            "cpu_percent": proc.cpu_percent(interval=None),
            "memory_mb": proc.memory_info().rss / (1024 * 1024),
            "memory_percent": proc.memory_percent(),
        }
    except ImportError:
        return {"cpu_percent": 0.0, "memory_mb": 0.0, "memory_percent": 0.0}


class StatsSnapshot:
    """Pure data holder + serializer -- kept separate from the web server
    so the snapshot logic can be unit-tested without spinning up aiohttp."""

    def __init__(self, stats_getter: Callable[[], Any], extra: dict[str, Any] | None = None):
        self._stats_getter = stats_getter
        self._extra = extra or {}
        self._started_at = time.time()

    def to_dict(self) -> dict[str, Any]:
        stats = self._stats_getter()
        base = {
            "requests_made": getattr(stats, "requests_made", 0),
            "requests_failed": getattr(stats, "requests_failed", 0),
            "responses_received": getattr(stats, "responses_received", 0),
            "items_scraped": getattr(stats, "items_scraped", 0),
            "items_dropped": getattr(stats, "items_dropped", 0),
            "items_noindexed": getattr(stats, "items_noindexed", 0),
            "links_nofollow_skipped": getattr(stats, "links_nofollow_skipped", 0),
            "bytes_downloaded": getattr(stats, "bytes_downloaded", 0),
            "elapsed_seconds": getattr(stats, "elapsed", 0.0),
            "requests_per_second": getattr(stats, "rps", 0.0),
            "monitor_uptime_seconds": time.time() - self._started_at,
        }
        base.update(_process_resource_usage())
        base.update(self._extra)
        return base


_HTML_PAGE = """<!DOCTYPE html>
<html><head><title>Bitscrape — live stats</title>
<meta http-equiv="refresh" content="2">
<style>
body {{ font-family: monospace; background: #111; color: #ddd; padding: 2rem; }}
table {{ border-collapse: collapse; }}
td {{ padding: 0.25rem 1rem; border-bottom: 1px solid #333; }}
td:first-child {{ color: #8fd; }}
h1 {{ color: #8fd; }}
</style></head>
<body>
<h1>Bitscrape crawl stats</h1>
<table>
{rows}
</table>
<p style="color:#666">Auto-refreshes every 2s. Raw JSON: <a style="color:#8fd" href="/stats.json">/stats.json</a></p>
</body></html>
"""


class StatsMonitor:
    """
    Owns an aiohttp.web server exposing:
      GET /            - human-readable auto-refreshing HTML view
      GET /stats.json  - raw JSON snapshot

    ``stats_getter`` is any zero-arg callable returning an object with the
    same attributes as ``bitscrape.core.models.CrawlStats`` (duck-typed
    deliberately, so it works with the real Engine or a test double).
    """

    def __init__(
        self,
        stats_getter: Callable[[], Any],
        host: str = "127.0.0.1",
        port: int = 8765,
        extra: dict[str, Any] | None = None,
    ) -> None:
        self._snapshot = StatsSnapshot(stats_getter, extra=extra)
        self._host = host
        self._port = port
        self._runner: Any = None

    async def start(self) -> None:
        from aiohttp import web

        app = web.Application()
        app.router.add_get("/", self._handle_html)
        app.router.add_get("/stats.json", self._handle_json)

        self._runner = web.AppRunner(app)
        await self._runner.setup()
        site = web.TCPSite(self._runner, self._host, self._port)
        await site.start()
        logger.info("StatsMonitor listening on http://%s:%d", self._host, self._port)

    async def stop(self) -> None:
        if self._runner is not None:
            await self._runner.cleanup()
            self._runner = None
            logger.info("StatsMonitor stopped")

    async def _handle_json(self, request: Any) -> Any:
        from aiohttp import web

        return web.json_response(self._snapshot.to_dict())

    async def _handle_html(self, request: Any) -> Any:
        from aiohttp import web

        data = self._snapshot.to_dict()
        rows = "\n".join(f"<tr><td>{k}</td><td>{v}</td></tr>" for k, v in data.items())
        return web.Response(text=_HTML_PAGE.format(rows=rows), content_type="text/html")

    def snapshot(self) -> dict[str, Any]:
        """Synchronous convenience accessor, e.g. for periodic logging
        without needing the HTTP server running."""
        return self._snapshot.to_dict()

    def as_plugin(self) -> BasePlugin:
        """
        Wraps this monitor as a plugin so it starts/stops with the spider's
        own lifecycle via PluginManager, instead of needing manual
        start()/stop() calls around engine.run().
        """
        monitor = self

        class _MonitorPlugin(BasePlugin):
            async def spider_opened(self, spider: Any) -> None:
                await monitor.start()

            async def spider_closed(self, spider: Any, reason: str) -> None:
                await monitor.stop()

        return _MonitorPlugin()
