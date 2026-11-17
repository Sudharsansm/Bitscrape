"""
Bitscrape Scheduler
===================
Manages the crawl queue. Two backends:
  - MemoryQueue  — single-process asyncio.PriorityQueue (default, fast)
  - RedisQueue   — distributed, persistent (for multi-worker deployments)

Requests are deduplicated via the DupeFilter before enqueuing.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, cast

if TYPE_CHECKING:
    import redis.asyncio

import asyncio
import itertools
import logging
from abc import ABC, abstractmethod

from bitscrape.core.models import Request
from bitscrape.core.settings import Settings
from bitscrape.scheduler.dupefilter import (
    BaseDupeFilter,
    MemoryDupeFilter,
    RedisDupeFilter,
    fingerprint,
)

logger = logging.getLogger(__name__)


def request_to_bytes(request: Request) -> bytes:
    """Serialise a Request to JSON bytes. ``bytes`` bodies (POST/PUT) are
    base64-encoded under ``body_b64`` -- ``orjson`` cannot encode raw bytes,
    which previously made every non-empty-body request crash the Redis queue."""
    import base64

    import orjson

    data = request.model_dump()
    body = data.pop("body", None)
    if body is not None:
        data["body_b64"] = base64.b64encode(body).decode("ascii")
    return orjson.dumps(data)


def request_from_bytes(payload: bytes | str) -> Request:
    """Inverse of :func:`request_to_bytes` (also reads payloads written by
    0.8.x, which have no ``body_b64`` key)."""
    import base64

    import orjson

    data = orjson.loads(payload)
    body_b64 = data.pop("body_b64", None)
    if body_b64 is not None:
        data["body"] = base64.b64decode(body_b64)
    return Request(**data)


# ---------------------------------------------------------------------------
# Queue abstraction
# ---------------------------------------------------------------------------


class BaseQueue(ABC):
    @abstractmethod
    async def push(self, request: Request) -> None: ...

    @abstractmethod
    async def pop(self) -> Request | None: ...

    @abstractmethod
    async def close(self) -> None: ...

    @property
    @abstractmethod
    def size(self) -> int: ...


class MemoryQueue(BaseQueue):
    """
    Priority queue in asyncio memory. Lower priority int = higher urgency.

    Fix: ``asyncio.PriorityQueue`` compares tuple elements left-to-right and
    falls through to the next element whenever priorities tie. Since
    ``Request`` defines no ``__lt__``, two same-priority requests pushed
    together used to raise ``TypeError: '<' not supported between instances
    of 'Request' and 'Request'`` — the single most common crawl pattern
    (homepage discovering many same-priority links) crashed the whole run.

    We fix this with a monotonically increasing tiebreaker counter as the
    second tuple element: ``(priority, counter, request)``. Counters are
    always unique and strictly increasing, so the heap never needs to compare
    ``Request`` objects, and FIFO order is preserved within the same priority
    (first pushed, first popped) — matching the ordering users expect.

    ``maxsize`` (0 = unbounded, the previous behaviour) adds an optional
    backpressure cap: once full, ``push()`` awaits until a slot frees up
    instead of growing memory without bound on very large crawls.
    """

    def __init__(self, maxsize: int = 0) -> None:
        self._q: asyncio.PriorityQueue[tuple[int, int, Request]] = asyncio.PriorityQueue(
            maxsize=maxsize
        )
        self._counter = itertools.count()

    async def push(self, request: Request) -> None:
        await self._q.put((request.priority.value, next(self._counter), request))

    async def pop(self) -> Request | None:
        try:
            _, _, req = self._q.get_nowait()
            return req
        except asyncio.QueueEmpty:
            return None

    async def close(self) -> None:
        pass

    def peek(self) -> list[Request]:
        """Return every queued request in priority order *without* removing
        them (periodic, crash-safe checkpoints while the crawl runs)."""
        heap = getattr(self._q, "_queue", [])
        return [item[2] for item in sorted(heap, key=lambda t: (t[0], t[1]))]

    def drain(self) -> list[Request]:
        """Remove and return every queued request in priority order (used to
        checkpoint a paused crawl)."""
        out: list[Request] = []
        while True:
            try:
                _, _, req = self._q.get_nowait()
            except asyncio.QueueEmpty:
                return out
            out.append(req)

    @property
    def size(self) -> int:
        return self._q.qsize()


class RedisQueue(BaseQueue):
    """
    Persistent priority queue backed by Redis sorted sets.
    Key: bitscrape:queue
    Score: priority value (lower = higher priority)
    Value: JSON-serialised Request
    """

    def __init__(self, redis_client: redis.asyncio.Redis, key: str = "bitscrape:queue") -> None:
        self._redis = redis_client
        self._key = key
        self._size_cache = 0

    async def push(self, request: Request) -> None:
        payload = request_to_bytes(request)
        await self._redis.zadd(self._key, {payload: request.priority.value})

    async def pop(self) -> Request | None:
        raw = await self._redis.zpopmin(self._key, 1)
        items = cast("list[tuple[bytes, float]]", raw)
        if not items:
            return None
        payload, _ = items[0]
        return request_from_bytes(payload)

    async def close(self) -> None:
        pass  # keep queue for resume

    @property
    def size(self) -> int:
        return 0  # expensive to call synchronously; use async size() separately

    async def async_size(self) -> int:
        return await self._redis.zcard(self._key)


# ---------------------------------------------------------------------------
# Scheduler
# ---------------------------------------------------------------------------


class Scheduler:
    """
    Wraps a queue + dupe filter.
    The Engine calls ``enqueue()`` and ``next_request()``.
    """

    def __init__(
        self,
        settings: Settings,
        queue: BaseQueue | None = None,
        dupefilter: BaseDupeFilter | None = None,
    ) -> None:
        self.settings = settings
        self._queue = queue or MemoryQueue()
        self._dupefilter = dupefilter or MemoryDupeFilter()
        self._enqueued = 0
        self._duped = 0

    @classmethod
    async def from_settings(cls, settings: Settings) -> Scheduler:
        queue: BaseQueue
        dupefilter: BaseDupeFilter
        if settings.scheduler_use_redis:
            import redis.asyncio as aioredis

            client = aioredis.from_url(settings.redis_url, decode_responses=False)
            queue = RedisQueue(client)
            dupefilter = RedisDupeFilter(client)
        else:
            queue = MemoryQueue(maxsize=settings.queue_max_size)
            dupefilter = MemoryDupeFilter()
        return cls(settings, queue, dupefilter)

    async def enqueue(self, request: Request) -> bool:
        """
        Attempt to enqueue a request.
        Returns True if enqueued, False if filtered (duplicate / depth exceeded).
        """
        # Depth check
        if self.settings.max_depth is not None and request.depth > self.settings.max_depth:
            logger.debug("Depth limit reached: %s", request.url)
            return False

        # Fingerprint
        fp = fingerprint(request)
        if (
            self.settings.dupefilter_enabled
            and not request.dont_filter
            and await self._dupefilter.seen(fp)
        ):
            self._duped += 1
            logger.debug("Duplicate filtered: %s", request.url)
            return False

        request = request.model_copy(update={"fingerprint": fp})
        await self._queue.push(request)
        self._enqueued += 1
        logger.debug("Enqueued [%d]: %s", self._queue.size, request.url)
        return True

    async def next_request(self) -> Request | None:
        return await self._queue.pop()

    # ------------------------------------------------------------------
    # Checkpoint support (pause / resume)
    # ------------------------------------------------------------------

    @property
    def is_persistent(self) -> bool:
        """True when the queue lives outside this process (Redis), in which
        case pause/resume needs no local checkpoint of the queue."""
        return not isinstance(self._queue, MemoryQueue)

    def snapshot(self, drain: bool = True) -> tuple[list[Request], list[str]]:
        """Return ``(pending_requests, seen_fingerprints)`` for an in-memory
        scheduler. ``drain=True`` empties the queue (final pause); ``False``
        leaves it intact (periodic checkpoint). Only meaningful for the
        in-memory backends -- Redis already persists its own queue/dupefilter."""
        if isinstance(self._queue, MemoryQueue):
            pending = self._queue.drain() if drain else self._queue.peek()
        else:
            pending = []
        seen = (
            list(self._dupefilter._seen) if isinstance(self._dupefilter, MemoryDupeFilter) else []
        )
        return pending, seen

    async def restore(self, pending: list[Request], seen: list[str]) -> None:
        """Reload a checkpoint: mark ``seen`` fingerprints as visited and
        re-queue ``pending`` requests (bypassing the dupefilter, since their
        fingerprints are in ``seen`` already)."""
        if isinstance(self._dupefilter, MemoryDupeFilter):
            self._dupefilter._seen.update(seen)
        for req in pending:
            await self._queue.push(req)
            self._enqueued += 1

    async def close(self) -> None:
        await self._queue.close()
        await self._dupefilter.close()

    @property
    def queue_size(self) -> int:
        return self._queue.size

    @property
    def stats(self) -> dict[str, int]:
        return {"enqueued": self._enqueued, "duped": self._duped}
