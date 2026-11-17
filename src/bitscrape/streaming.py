"""
Streaming results
=================

Consume items *as they are scraped* instead of waiting for the crawl to end::

    async with CrawlStream(QuotesSpider) as crawl:
        async for item in crawl:
            print(item)              # arrives while the crawl is still running
    print(crawl.stats)

or, in synchronous code::

    for item in stream_sync(QuotesSpider):
        print(item)

The stream is *backpressured*: it holds at most ``buffer`` undelivered items,
and once that many are waiting the crawl pauses at the next item until the
consumer catches up -- a slow consumer slows the crawl rather than exhausting
memory. Stopping early (``break`` / ``aclose()``) stops the crawl gracefully.

Under the hood it runs the same engine as ``bitscrape.run()`` /
``bitscrape crawl`` (via :func:`bitscrape.factory.build_engine`), so every
feature driven by ``Settings`` works identically.
"""

from __future__ import annotations

import asyncio
import queue
import threading
from collections.abc import AsyncIterator, Iterator
from typing import Any

from bitscrape.core.models import CrawlStats
from bitscrape.core.settings import Settings
from bitscrape.core.spider import Spider
from bitscrape.engine import Engine
from bitscrape.plugins import PluginManager

_END = object()


class CrawlStream:
    """Async iterator over the items of a running crawl.

    Args:
        spider_cls: the :class:`~bitscrape.core.spider.Spider` subclass.
        settings: run settings (defaults to ``Settings()``).
        buffer: maximum undelivered items before the crawl is backpressured.
        spider_kwargs: extra keyword arguments for the spider constructor.
        pipelines / middlewares: as for :func:`bitscrape.run`.
    """

    def __init__(
        self,
        spider_cls: type[Spider],
        *,
        settings: Settings | None = None,
        buffer: int = 256,
        spider_kwargs: dict[str, Any] | None = None,
        pipelines: list[Any] | None = None,
        middlewares: list[Any] | None = None,
    ) -> None:
        if buffer < 1:
            raise ValueError("buffer must be >= 1")
        self._spider_cls = spider_cls
        self._settings = settings or Settings()
        self._spider_kwargs = spider_kwargs or {}
        self._pipelines = pipelines
        self._middlewares = middlewares
        self._queue: asyncio.Queue[Any] = asyncio.Queue(maxsize=buffer)
        self._task: asyncio.Task[CrawlStats] | None = None
        self._engine: Engine | None = None
        self._closed = False
        self._error: BaseException | None = None
        self._stats: CrawlStats | None = None

    # -- lifecycle ------------------------------------------------------------

    async def start(self) -> CrawlStream:
        if self._task is not None:
            return self
        from bitscrape.factory import build_engine
        from bitscrape.middleware.middleware import MiddlewareManager
        from bitscrape.pipeline.pipelines import PipelineManager

        spider = self._spider_cls(settings=self._settings, **self._spider_kwargs)
        plugins = PluginManager()

        async def on_item(item: Any, spider: Any) -> None:
            if self._closed:
                return  # consumer left: drop instead of blocking the engine
            await self._queue.put(item)  # blocks when full => backpressure

        plugins.on("item_scraped", on_item)
        engine = await build_engine(spider, spider.settings, plugin_manager=plugins)
        if self._middlewares is not None:
            engine._middleware_manager = MiddlewareManager(self._middlewares)
        if self._pipelines:
            engine._pipeline_manager = PipelineManager(self._pipelines)
        self._engine = engine
        self._task = asyncio.create_task(self._run(engine))
        return self

    async def _run(self, engine: Engine) -> CrawlStats:
        try:
            self._stats = await engine.run()
            return self._stats
        except BaseException as exc:
            self._error = exc
            raise
        finally:
            # Terminate the iterator. Wait for room rather than dropping an
            # undelivered item to make space (a slow consumer may still have a
            # full buffer); if the consumer has already left there is no one to
            # signal.
            if not self._closed:
                await self._queue.put(_END)

    async def aclose(self) -> None:
        """Stop the crawl (gracefully) and release resources."""
        self._closed = True
        if self._engine is not None and self._engine.is_running:
            self._engine.stop()
        # Unblock an engine stuck on a full queue.
        while not self._queue.empty():
            self._queue.get_nowait()
        if self._task is not None:
            try:
                await self._task
            except BaseException:  # noqa: BLE001 - surfaced through .error / iteration
                pass

    async def __aenter__(self) -> CrawlStream:
        return await self.start()

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    # -- iteration ------------------------------------------------------------

    def __aiter__(self) -> CrawlStream:
        return self

    async def __anext__(self) -> Any:
        if self._task is None:
            await self.start()
        item = await self._queue.get()
        if item is _END:
            # re-arm the sentinel so repeated iteration after the end stays finished
            try:
                self._queue.put_nowait(_END)
            except asyncio.QueueFull:  # pragma: no cover
                pass
            if self._error is not None and not isinstance(self._error, asyncio.CancelledError):
                err, self._error = self._error, None
                raise err
            raise StopAsyncIteration
        return item

    # -- results --------------------------------------------------------------

    @property
    def stats(self) -> CrawlStats:
        """Final stats (available once the stream is exhausted or closed);
        live stats while running."""
        if self._stats is not None:
            return self._stats
        if self._engine is not None:
            return self._engine.stats
        return CrawlStats()

    @property
    def engine(self) -> Engine | None:
        return self._engine

    def pause(self) -> None:
        """Pause the crawl (requires ``Settings.jobdir``); the stream then ends."""
        if self._engine is not None:
            self._engine.pause()


async def stream(spider_cls: type[Spider], **kwargs: Any) -> AsyncIterator[Any]:
    """``async for item in stream(MySpider): ...`` -- see :class:`CrawlStream`.

    Breaking out of the loop stops the crawl once the generator is closed (use
    ``contextlib.aclosing`` for deterministic cleanup, or :class:`CrawlStream`
    directly).
    """
    async with CrawlStream(spider_cls, **kwargs) as crawl:
        async for item in crawl:
            yield item


def stream_sync(spider_cls: type[Spider], **kwargs: Any) -> Iterator[Any]:
    """Synchronous streaming: ``for item in stream_sync(MySpider): ...``

    The crawl runs on a private event loop in a background thread, so this is
    safe to call from ordinary blocking code (not from inside a running loop).
    Closing the generator (``break``) stops the crawl.
    """
    out: queue.Queue[Any] = queue.Queue(maxsize=int(kwargs.get("buffer", 256)))
    stop = threading.Event()
    holder: dict[str, Any] = {}

    def worker() -> None:
        async def main() -> None:
            crawl = CrawlStream(spider_cls, **kwargs)
            holder["crawl"] = crawl
            holder["loop"] = asyncio.get_running_loop()
            try:
                async with crawl:
                    async for item in crawl:
                        while not stop.is_set():
                            try:
                                out.put(item, timeout=0.05)
                                break
                            except queue.Full:
                                await asyncio.sleep(0)  # let the loop breathe
                        if stop.is_set():
                            break
            except BaseException as exc:  # noqa: BLE001
                holder["error"] = exc
            finally:
                holder["stats"] = crawl.stats
                out.put(_END)

        asyncio.run(main())

    thread = threading.Thread(target=worker, name="bitscrape-stream", daemon=True)
    thread.start()
    try:
        while True:
            item = out.get()
            if item is _END:
                break
            yield item
        if "error" in holder:
            raise holder["error"]
    finally:
        stop.set()
        # unblock the worker if it is waiting to put into a full queue
        while thread.is_alive():
            try:
                out.get(timeout=0.05)
            except queue.Empty:
                pass
        thread.join(timeout=10)
