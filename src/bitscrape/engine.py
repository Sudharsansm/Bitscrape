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
import contextlib
import inspect
import logging
import signal
import threading
import time
from collections.abc import Callable
from typing import Any

from bitscrape.checkpoint import Checkpoint, restore_counters
from bitscrape.core.models import CrawlStats, Request, Response
from bitscrape.core.settings import Settings
from bitscrape.core.spider import Spider, _current_response_var, iterate_output
from bitscrape.downloader.downloader import Downloader, DownloadError
from bitscrape.exporters.feed import BaseExporter
from bitscrape.middleware.middleware import MiddlewareManager
from bitscrape.parser.adaptive import AdaptiveStore
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
        # -- pause / resume / stop ----------------------------------------
        self._pause_requested = False
        self._stop_requested = False
        self._force_cancel = False
        # Requests spawned but not finished (re-queued into a checkpoint if the
        # run is interrupted while they are in flight).
        self._inflight: dict[str, Request] = {}
        self._checkpoint: Checkpoint | None = None
        self._resumed = False
        self._adaptive_store = AdaptiveStore(self._settings.adaptive_db_path)  # lazy: no I/O yet
        self._task: asyncio.Task[Any] | None = None
        self.install_signal_handlers: bool | None = None  # None = only when jobdir is set

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

    def pause(self) -> None:
        """Stop taking new requests, let in-flight ones finish, then save a
        checkpoint (needs ``Settings.jobdir``) so the crawl can be resumed."""
        self._pause_requested = True
        self._finish_reason = "paused"

    def stop(self) -> None:
        """Stop gracefully without a checkpoint (in-flight requests finish)."""
        self._stop_requested = True
        self._finish_reason = "cancelled"

    @property
    def is_running(self) -> bool:
        return self._running

    async def run(self) -> CrawlStats:
        """Run the full crawl and return stats."""
        self._stats = CrawlStats(start_time=time.time())
        self._running = True
        self._pause_requested = self._stop_requested = self._force_cancel = False
        self._finish_reason = "finished"
        self._inflight = {}
        self._task = asyncio.current_task()

        # Initialise components
        self._scheduler = await Scheduler.from_settings(self._settings)
        await self._downloader.open(configure=self._spider.configure_sessions)
        await self._pipeline_manager.open_spider(self._spider)
        await self._spider.open_spider()
        await self._plugins.fire("spider_opened", spider=self._spider)

        # Resume from a previous checkpoint, if any.
        self._resumed = False
        self._checkpoint = Checkpoint(self._settings.jobdir) if self._settings.jobdir else None
        if self._checkpoint is not None and not self._sched.is_persistent:
            data = self._checkpoint.load()
            if data is not None and (not data.spider or data.spider == self._spider.name):
                await self._sched.restore(data.pending, data.seen)
                restore_counters(self._stats, data.counters)
                self._resumed = True
                logger.info(
                    "Resuming crawl from %s: %d pending requests, %d already seen",
                    self._checkpoint.dir,
                    len(data.pending),
                    len(data.seen),
                )

        if self._exporter:
            self._open_exporter(append=self._resumed)

        # Seed start requests (skipped on resume: they are already in the queue
        # or in the seen set).
        if not self._resumed:
            await self._seed_start_requests()

        restore_signals = self._install_signals()

        # Concurrency gate. In-flight work is capped at ``concurrent_requests`` so
        # a worker never hoards requests it isn't executing -- crucial with a
        # shared Redis queue, where hoarding would starve every other worker.
        cap = max(1, self._settings.concurrent_requests)
        semaphore = asyncio.Semaphore(cap)
        tasks: set[asyncio.Task[None]] = set()
        last_checkpoint = time.monotonic()

        try:
            while self._running and not self._pause_requested and not self._stop_requested:
                # Reap finished tasks
                for t in [t for t in tasks if t.done()]:
                    tasks.discard(t)
                    if not t.cancelled() and t.exception():
                        logger.error("Worker error: %s", t.exception())

                if len(tasks) >= cap:
                    await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
                    continue

                # Periodic crash-safe checkpoint
                interval = self._settings.checkpoint_interval
                if (
                    self._checkpoint is not None
                    and interval > 0
                    and time.monotonic() - last_checkpoint >= interval
                ):
                    self._save_checkpoint("running", drain=False)
                    last_checkpoint = time.monotonic()

                request = await self._sched.next_request()
                if request is None:
                    if not tasks:
                        break  # queue empty and nothing in flight -> done
                    # Wake as soon as any worker finishes (it may have queued more work).
                    await asyncio.wait(tasks, timeout=0.05, return_when=asyncio.FIRST_COMPLETED)
                    continue

                self._inflight[request.request_id] = request
                tasks.add(asyncio.create_task(self._process_request(request, semaphore)))

            # Let running requests finish (graceful pause / stop / normal end).
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)

        except asyncio.CancelledError:
            logger.info("Engine cancelled")
            self._finish_reason = "cancelled"
            for t in tasks:
                t.cancel()
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)
            self._force_cancel = True
        finally:
            restore_signals()
            self._finalize_checkpoint()
            await self._teardown()

        self._running = False
        self._stats.finish_time = time.time()
        self._stats.finish_reason = self._finish_reason
        self._log_stats()
        return self._stats

    # ------------------------------------------------------------------
    # Seeding, checkpoints, signals
    # ------------------------------------------------------------------

    async def _seed_start_requests(self) -> None:
        seeds = self._spider.start_requests()
        if inspect.isawaitable(seeds):
            seeds = await seeds
        if hasattr(seeds, "__aiter__"):
            async for req in seeds:
                await self._enqueue_seed(req)
        else:
            for req in seeds or []:
                await self._enqueue_seed(req)

    async def _enqueue_seed(self, req: Request) -> None:
        await self._sched.enqueue(req)
        await self._plugins.fire("request_scheduled", request=req, spider=self._spider)

    def _open_exporter(self, append: bool) -> None:
        assert self._exporter is not None
        if append:
            try:
                self._exporter.open(append=True)  # type: ignore[call-arg]
                return
            except TypeError:
                logger.warning(
                    "%s cannot append on resume; existing output will be overwritten",
                    type(self._exporter).__name__,
                )
        self._exporter.open()

    def _save_checkpoint(self, status: str, *, drain: bool) -> None:
        if self._checkpoint is None or self._sched.is_persistent:
            return
        pending, seen = self._sched.snapshot(drain=drain)
        # In-flight requests are re-run on resume (at-least-once delivery).
        pending = [*self._inflight.values(), *pending]
        try:
            self._checkpoint.save(
                pending, seen, self._stats, spider=self._spider.name, status=status
            )
        except OSError as exc:
            logger.error("Could not write checkpoint: %s", exc)

    def _finalize_checkpoint(self) -> None:
        """On exit: a finished crawl clears its checkpoint; an interrupted one
        (pause / cancel / force-stop) saves everything still to do."""
        if self._checkpoint is None or self._sched_or_none() is None:
            return
        if self._finish_reason == "finished":
            self._checkpoint.clear()
        elif self._finish_reason in ("paused", "cancelled"):
            self._save_checkpoint("paused", drain=True)

    def _sched_or_none(self) -> Scheduler | None:
        return self._scheduler

    def _install_signals(self) -> Callable[[], None]:
        """SIGINT/SIGTERM -> graceful pause (first signal) / hard stop (second).
        Only active when a jobdir is configured (or forced on), so the default
        Ctrl-C behaviour of scripts without checkpointing is untouched."""
        wanted = self.install_signal_handlers
        if wanted is None:
            wanted = self._settings.jobdir is not None
        if not wanted or threading.current_thread() is not threading.main_thread():
            return lambda: None
        loop = asyncio.get_running_loop()
        installed: list[int] = []

        def handler() -> None:
            if not self._pause_requested and not self._stop_requested:
                logger.warning("Signal received: pausing (finishing in-flight requests)...")
                if self._checkpoint is not None:
                    self.pause()
                else:
                    self.stop()
            elif self._task is not None:
                logger.warning("Second signal: cancelling immediately")
                self._task.cancel()

        try:
            for sig in (signal.SIGINT, signal.SIGTERM):
                loop.add_signal_handler(sig, handler)
                installed.append(sig)
        except (NotImplementedError, RuntimeError, ValueError):  # Windows / non-main loops
            return lambda: None

        def restore() -> None:
            for sig in installed:
                with contextlib.suppress(Exception):
                    loop.remove_signal_handler(sig)

        return restore

    # ------------------------------------------------------------------
    # Request processing
    # ------------------------------------------------------------------

    async def _process_request(self, request: Request, semaphore: asyncio.Semaphore) -> None:
        try:
            await self._process_request_inner(request, semaphore)
        except asyncio.CancelledError:
            raise  # deliberately left in ``_inflight`` so a checkpoint re-queues it
        else:
            self._inflight.pop(request.request_id, None)

    async def _call_errback(self, request: Request, exc: Exception) -> None:
        """Invoke the request's ``errback`` (by name) or the spider default.
        An errback may yield follow-up Requests, which are scheduled."""
        fn = (
            getattr(self._spider, request.errback, None)
            if request.errback
            else self._spider.errback
        )
        if fn is None:
            return
        try:
            async for out in iterate_output(fn(request, exc)):
                if isinstance(out, Request):
                    await self._sched.enqueue(out)
        except Exception:
            logger.exception("errback for %s raised", request.url)

    async def _process_request_inner(self, request: Request, semaphore: asyncio.Semaphore) -> None:
        from bitscrape.middleware.middleware import OffsiteDrop
        from bitscrape.pipeline.pipelines import DropItem

        drop_exceptions = (DropItem, OffsiteDrop)

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
                sid = response.session_id or request.session or "default"
                self._bump(self._stats.sessions_requests, sid)
                self._sync_throttle_stats()
                if response.headers.get("X-Bitscrape-Dev-Cache") == "true":
                    self._stats.cache_hits += 1
                await self._plugins.fire(
                    "response_received", request=request, response=response, spider=self._spider
                )

                # Blocked-response handling: count it, and give the spider a
                # chance to escalate (e.g. retry through another session).
                if await self._spider.is_blocked(response):
                    self._stats.requests_blocked += 1
                    self._bump(self._stats.sessions_blocked, sid)
                    retry = await self._escalate_blocked(request, response)
                    if retry is not None:
                        await self._sched.enqueue(retry)
                        await self._plugins.fire(
                            "request_scheduled", request=retry, spider=self._spider
                        )
                        return

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

            except drop_exceptions as exc:
                # A process_request middleware (RobotsMiddleware, OffsiteMiddleware, ...)
                # deliberately declined this request. That's policy, not failure: it must
                # not inflate requests_failed, trigger an ERROR-level traceback, or invoke
                # the spider's errback (which is for download/parse failures).
                self._stats.requests_dropped += 1
                if isinstance(exc, OffsiteDrop):
                    self._stats.requests_offsite_dropped += 1
                logger.info("Request dropped: %s — %s", request.url, exc)
                await self._plugins.fire(
                    "request_dropped", request=request, reason=str(exc), spider=self._spider
                )

            except DownloadError as exc:
                self._stats.requests_failed += 1
                self._bump(self._stats.sessions_failed, request.session or "default")
                logger.warning("Download error: %s — %s", request.url, exc)
                await self._plugins.fire(
                    "error", request=request, exception=exc, spider=self._spider
                )
                exc_result = await self._middleware_manager.process_exception(
                    request, exc, self._spider
                )
                if isinstance(exc_result, Request):
                    await self._sched.enqueue(exc_result)
                else:
                    await self._call_errback(request, exc)

            except Exception as exc:
                self._stats.requests_failed += 1
                logger.exception("Unexpected error on %s", request.url)
                await self._plugins.fire(
                    "error", request=request, exception=exc, spider=self._spider
                )
                await self._call_errback(request, exc)

    def _sync_throttle_stats(self) -> None:
        """Copy the downloader's politeness state into the crawl stats."""
        info = self._downloader.throttle_info()
        self._stats.throttle_backoffs = info["backoffs"]
        self._stats.throttle_cooldowns = info["cooldowns"]
        self._stats.throttle_wait_seconds = info["wait_seconds"]
        self._stats.throttle_delays = {d: v["delay"] for d, v in info["domains"].items()}

    @staticmethod
    def _bump(counter: dict[str, int], key: str) -> None:
        counter[key] = counter.get(key, 0) + 1

    async def _escalate_blocked(self, request: Request, response: Response) -> Request | None:
        """Ask the spider for a replacement for a blocked request.

        Returns the request to enqueue, or ``None`` to fall through and parse the
        response as-is (no hook override, escalation budget spent, or the hook
        declined). The replacement bypasses the dupefilter and carries an
        escalation counter so a misbehaving hook can never loop forever.
        """
        used = int(request.meta.get("_escalations", 0))
        if used >= self._settings.blocked_escalation_max:
            logger.warning(
                "Blocked response for %s and escalation budget (%d) is spent; parsing as-is",
                request.url,
                self._settings.blocked_escalation_max,
            )
            return None
        try:
            replacement = await self._spider.retry_blocked_request(
                request.model_copy(deep=True), response
            )
        except Exception:
            logger.exception("retry_blocked_request() raised for %s", request.url)
            return None
        if replacement is None:
            return None
        self._stats.requests_escalated += 1
        logger.info(
            "Escalating blocked request %s (%s -> %s), attempt %d/%d",
            request.url,
            request.session or "default",
            replacement.session or "default",
            used + 1,
            self._settings.blocked_escalation_max,
        )
        return replacement.model_copy(
            update={
                "dont_filter": True,
                "meta": {**replacement.meta, "_escalations": used + 1},
            }
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

        parsed = ParsedResponse(response, adaptive_store=self._adaptive_store)
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
                async for output in iterate_output(callback(parsed, **request.cb_kwargs)):
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
                            if self._settings.collect_items:
                                self._stats.items.append(processed)
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
        self._sync_throttle_stats()
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
