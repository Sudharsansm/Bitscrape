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
from bitscrape.core.spider import Spider
from bitscrape.downloader.downloader import DownloadError, Downloader
from bitscrape.exporters.feed import BaseExporter, get_exporter
from bitscrape.middleware.middleware import MiddlewareManager
from bitscrape.parser.selector import ParsedResponse
from bitscrape.pipeline.pipelines import PipelineManager
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
    ) -> None:
        self._spider = spider
        self._settings = settings or Settings()
        self._downloader = Downloader(self._settings)
        self._scheduler: Scheduler | None = None
        self._pipeline_manager = PipelineManager(pipelines or [])
        self._middleware_manager = MiddlewareManager(middlewares or [])
        self._exporter = exporter
        self._stats = CrawlStats()
        self._running = False

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

        if self._exporter:
            self._exporter.open()

        # Seed start requests
        for req in self._spider.start_requests():
            await self._scheduler.enqueue(req)

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
                request = await self._scheduler.next_request()
                if request is None:
                    if not tasks:
                        break  # queue empty and no in-flight requests → done
                    await asyncio.sleep(0.05)
                    continue

                # Spawn a worker task
                task = asyncio.create_task(
                    self._process_request(request, semaphore)
                )
                tasks.add(task)

            # Wait for remaining tasks
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)

        except asyncio.CancelledError:
            logger.info("Engine cancelled")
        finally:
            await self._teardown()

        self._stats.finish_time = time.time()
        self._log_stats()
        return self._stats

    # ------------------------------------------------------------------
    # Request processing
    # ------------------------------------------------------------------

    async def _process_request(
        self, request: Request, semaphore: asyncio.Semaphore
    ) -> None:
        async with semaphore:
            try:
                # Middleware: process_request
                result = await self._middleware_manager.process_request(
                    request, self._spider
                )
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

                # Middleware: process_response
                resp_result = await self._middleware_manager.process_response(
                    request, response, self._spider
                )
                if resp_result is None:
                    return
                if isinstance(resp_result, Request):
                    await self._scheduler.enqueue(resp_result)
                    return
                response = resp_result

                # Parse
                await self._parse_response(request, response)

            except DownloadError as exc:
                self._stats.requests_failed += 1
                logger.warning("Download error: %s — %s", request.url, exc)
                exc_result = await self._middleware_manager.process_exception(
                    request, exc, self._spider
                )
                if isinstance(exc_result, Request):
                    await self._scheduler.enqueue(exc_result)

            except Exception as exc:
                self._stats.requests_failed += 1
                logger.error("Unexpected error on %s: %s", request.url, exc, exc_info=True)

    async def _parse_response(self, request: Request, response: Response) -> None:
        # Resolve callback name → spider method
        callback_name = request.callback or "parse"
        callback = getattr(self._spider, callback_name, None)
        if callback is None:
            logger.warning("Spider has no callback %r", callback_name)
            return

        parsed = ParsedResponse(response)
        try:
            async for output in callback(parsed):  # type: ignore[arg-type]
                if isinstance(output, Request):
                    output = output.model_copy(update={"depth": request.depth + 1})
                    await self._scheduler.enqueue(output)
                else:
                    # It's an item (dict or BaseModel)
                    self._stats.items_scraped += 1
                    processed = await self._pipeline_manager.process_item(
                        output, self._spider
                    )
                    if processed is None:
                        self._stats.items_dropped += 1
                    elif self._exporter:
                        self._exporter.export_item(processed)
        except Exception as exc:
            logger.error("Parse error on %s: %s", request.url, exc, exc_info=True)

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
