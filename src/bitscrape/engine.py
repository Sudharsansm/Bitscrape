"""
Bitscrape Engine
================
The central coordinator.  For each crawl run it:

1. Opens all components (downloader, scheduler, pipelines, exporter).
2. Seeds the scheduler with spider.start_requests().
3. Runs an asyncio loop: pop request → middleware → download → parse → pipeline.
4. Stats are tracked and logged.
5. Gracefully closes everything when the queue empties or a signal is received.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

from bitscrape.core.models import CrawlStats, Request, Response
from bitscrape.core.settings import Settings
from bitscrape.core.spider import Spider, _current_response_var
from bitscrape.downloader.downloader import Downloader, DownloadError
from bitscrape.exporters.feed import BaseExporter
from bitscrape.middleware.middleware import MiddlewareManager
from bitscrape.parser.selector import ParsedResponse
from bitscrape.pipeline.pipelines import PipelineManager
from bitscrape.plugins import PluginManager
from bitscrape.scheduler.scheduler import Scheduler

logger = logging.getLogger(__name__)


class Engine:
    """
    Bitscrape crawl engine.

    Usage::

        engine = Engine(spider=MySpider(), settings=Settings())
        await engine.run()
    """

    def __init__(
        self,
        spider: Spider,
        settings: Settings | None = None,
        pipelines: list[Any] | None = None,
        middlewares: list[Any] | None = None,
        exporter: BaseExporter | None = None,
        plugin_manager: PluginManager | None = None,
    ) -> None:
        self._spider = spider
        self._settings = settings or Settings()
        self._downloader = Downloader(self._settings)
        self._scheduler: Scheduler | None = None
        self._pipeline_manager = PipelineManager(pipelines or [])
        self._middleware_manager = MiddlewareManager(middlewares or [])
        self._exporter = exporter
        self._plugins = plugin_manager or PluginManager()
        self._stats = CrawlStats()
        self._running = False
        self._finish_reason = "finished"

    @property
    def stats(self) -> CrawlStats:
        """Live crawl stats -- safe to read while a crawl is in progress
        (e.g. from a StatsMonitor polling this on a timer), not just after
        run() returns."""
        return self._stats

    @property
    def _sched(self) -> Scheduler:
        """Type-narrowed accessor for the scheduler. Only valid once
        ``run()`` has initialised it; raises a clear error otherwise rather
        than a bare AttributeError on ``None``."""
        if self._scheduler is None:
            raise RuntimeError("Engine.run() must be started before the scheduler is used")
        return self._scheduler

    # ------------------------------------------------------------------
    # Public entry-point
    # ------------------------------------------------------------------

    async def run(self) -> CrawlStats:
        """Run the full crawl and return stats."""
        self._stats = CrawlStats(start_time=time.time())
        self._running = True

        # Initialise components
        self._scheduler = await Scheduler.from_settings(self._settings)
        await self._downloader.open()
        await self._pipeline_manager.open_spider(self._spider)
        await self._spider.open_spider()
        await self._plugins.fire("spider_opened", spider=self._spider)

        if self._exporter:
            self._exporter.open()

        # Seed start requests
        for req in self._spider.start_requests():
            await self._sched.enqueue(req)
            await self._plugins.fire("request_scheduled", request=req, spider=self._spider)

        # Concurrency gate
        semaphore = asyncio.Semaphore(self._settings.concurrent_requests)
        tasks: set[asyncio.Task] = set()

        try:
            while self._running:
                # Drain finished tasks
                done = {t for t in tasks if t.done()}
                for t in done:
                    tasks.discard(t)
                    if t.exception():
                        logger.error("Worker error: %s", t.exception())

                # Pop next request
                request = await self._sched.next_request()
                if request is None:
                    if not tasks:
                        break  # queue empty and no in-flight requests → done
                    await asyncio.sleep(0.05)
                    continue

                # Spawn a worker task
                task = asyncio.create_task(self._process_request(request, semaphore))
                tasks.add(task)

            # Wait for remaining tasks
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)

        except asyncio.CancelledError:
            logger.info("Engine cancelled")
            self._finish_reason = "cancelled"
        finally:
            await self._teardown()

        self._stats.finish_time = time.time()
        self._log_stats()
        return self._stats

    # ------------------------------------------------------------------
    # Request processing
    # ------------------------------------------------------------------

    async def _process_request(self, request: Request, semaphore: asyncio.Semaphore) -> None:
        async with semaphore:
            try:
                # Middleware: process_request
                result = await self._middleware_manager.process_request(request, self._spider)
                if isinstance(result, Response):
                    response = result  # short-circuited by middleware
                elif isinstance(result, Request):
                    request = result
                    response = await self._downloader.fetch(request)
                else:
                    response = await self._downloader.fetch(request)

                self._stats.requests_made += 1
                self._stats.responses_received += 1
                self._stats.bytes_downloaded += len(response.body)
                await self._plugins.fire(
                    "response_received", request=request, response=response, spider=self._spider
                )

                # Middleware: process_response
                resp_result = await self._middleware_manager.process_response(
                    request, response, self._spider
                )
                if resp_result is None:
                    return
                if isinstance(resp_result, Request):
                    await self._sched.enqueue(resp_result)
                    await self._plugins.fire(
                        "request_scheduled", request=resp_result, spider=self._spider
                    )
                    return
                response = resp_result

                # Parse
                await self._parse_response(request, response)

            except DownloadError as exc:
                self._stats.requests_failed += 1
                logger.warning("Download error: %s — %s", request.url, exc)
                await self._plugins.fire(
                    "error", request=request, exception=exc, spider=self._spider
                )
                exc_result = await self._middleware_manager.process_exception(
                    request, exc, self._spider
                )
                if isinstance(exc_result, Request):
                    await self._sched.enqueue(exc_result)

            except Exception as exc:
                self._stats.requests_failed += 1
                logger.exception("Unexpected error on %s", request.url)
                await self._plugins.fire(
                    "error", request=request, exception=exc, spider=self._spider
                )

    async def _parse_response(self, request: Request, response: Response) -> None:
        # Resolve callback name → spider method
        callback_name = request.callback or "parse"
        callback = getattr(self._spider, callback_name, None)
        if callback is None:
            logger.warning("Spider has no callback %r", callback_name)
            return

        # Page-level indexing directives (set by MetaRobotsMiddleware from
        # X-Robots-Tag / <meta name="robots">). "noindex" means fetchable
        # but its content shouldn't be indexed/exported; "nofollow" means
        # don't follow links discovered on this page. Mirrors what
        # Googlebot/ClaudeBot/PerplexityBot-class crawlers do beyond
        # robots.txt.
        respect_meta_robots = getattr(self._settings, "respect_meta_robots", True)
        noindex = respect_meta_robots and request.meta.get("noindex", False)
        nofollow = respect_meta_robots and request.meta.get("nofollow", False)

        parsed = ParsedResponse(response)
        items_this_response = 0
        requests_this_response = 0
        # Set for the duration of this callback so Spider.follow() can
        # resolve relative URLs against this response. Safe under the
        # Engine's concurrent per-request tasks: contextvars.ContextVar.set()
        # only affects the current asyncio Task's context, not sibling
        # tasks processing other requests at the same time.
        context_token = _current_response_var.set(response)
        try:
            try:
                async for output in callback(parsed):
                    if isinstance(output, Request):
                        requests_this_response += 1
                        if nofollow:
                            self._stats.links_nofollow_skipped += 1
                            logger.debug(
                                "Skipping %s: nofollow directive on %s", output.url, response.url
                            )
                            continue
                        output = output.model_copy(update={"depth": request.depth + 1})
                        await self._sched.enqueue(output)
                        await self._plugins.fire(
                            "request_scheduled", request=output, spider=self._spider
                        )
                    else:
                        # It's an item (dict or BaseModel)
                        items_this_response += 1
                        if noindex:
                            self._stats.items_noindexed += 1
                            logger.debug(
                                "Not indexing item from %s: noindex directive", response.url
                            )
                            continue
                        self._stats.items_scraped += 1
                        processed = await self._pipeline_manager.process_item(output, self._spider)
                        if processed is None:
                            self._stats.items_dropped += 1
                            await self._plugins.fire(
                                "item_dropped",
                                item=output,
                                exception=Exception("dropped by pipeline"),
                                spider=self._spider,
                            )
                        else:
                            if self._exporter:
                                self._exporter.export_item(processed)
                            await self._plugins.fire(
                                "item_scraped", item=processed, spider=self._spider
                            )
            except Exception:
                logger.exception("Parse error on %s", request.url)
                return
        finally:
            _current_response_var.reset(context_token)

        # Diagnostic: a callback that yields nothing at all is almost always a
        # sign that CSS/XPath selectors don't match the page, robots.txt/JS
        # rendering left the body empty, or the wrong callback ran -- not a
        # crash, so nothing else would ever surface it. Surface it here
        # instead of leaving users staring at a silent "Items scraped: 0".
        # (Doesn't fire for noindex/nofollow pages -- that's an intentional
        # skip, not a broken selector.)
        if items_this_response == 0 and requests_this_response == 0:
            body_len = len(response.body)
            logger.warning(
                "%s: callback %r yielded 0 items and 0 requests from a %d-byte response. "
                "This usually means your CSS/XPath selectors don't match this page's HTML "
                "(inspect response.text / response.body), the content is JS-rendered and "
                "needs use_playwright=True, or robots.txt / a redirect served an "
                "unexpected page. See README 'Troubleshooting: 0 items scraped'.",
                response.url,
                callback_name,
                body_len,
            )

    # ------------------------------------------------------------------
    # Teardown
    # ------------------------------------------------------------------

    async def _teardown(self) -> None:
        if self._exporter:
            self._exporter.close()
        await self._pipeline_manager.close_spider(self._spider)
        await self._spider.close_spider()
        await self._downloader.close()
        if self._scheduler:
            await self._scheduler.close()
        await self._plugins.fire("spider_closed", spider=self._spider, reason=self._finish_reason)

    # ------------------------------------------------------------------
    # Stats
    # ------------------------------------------------------------------

    def _log_stats(self) -> None:
        s = self._stats
        logger.info(
            "Crawl finished | requests=%d failed=%d items=%d dropped=%d "
            "bytes=%.1fkB time=%.1fs rps=%.1f",
            s.requests_made,
            s.requests_failed,
            s.items_scraped,
            s.items_dropped,
            s.bytes_downloaded / 1024,
            s.elapsed,
            s.rps,
        )
