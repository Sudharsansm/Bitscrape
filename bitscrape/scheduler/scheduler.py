"""
Bitscrape Scheduler
===================
Manages the crawl queue. Two backends:
  - MemoryQueue  — single-process asyncio.PriorityQueue (default, fast)
  - RedisQueue   — distributed, persistent (for multi-worker deployments)

Requests are deduplicated via the DupeFilter before enqueuing.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import redis.asyncio

import asyncio
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
    """Priority queue in asyncio memory. Lower priority int = higher urgency."""

    def __init__(self) -> None:
        self._q: asyncio.PriorityQueue[tuple[int, Request]] = asyncio.PriorityQueue()

    async def push(self, request: Request) -> None:
        await self._q.put((request.priority.value, request))

    async def pop(self) -> Request | None:
        try:
            _, req = self._q.get_nowait()
            return req
        except asyncio.QueueEmpty:
            return None

    async def close(self) -> None:
        pass

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

    def __init__(self, redis_client: redis.asyncio.Redis, key: str = "bitscrape:queue") -> None:  # type: ignore[name-defined]
        self._redis = redis_client
        self._key = key
        self._size_cache = 0

    async def push(self, request: Request) -> None:
        import orjson

        payload = orjson.dumps(request.model_dump())
        await self._redis.zadd(self._key, {payload: request.priority.value})

    async def pop(self) -> Request | None:
        import orjson

        items = await self._redis.zpopmin(self._key, 1)
        if not items:
            return None
        payload, _ = items[0]
        data = orjson.loads(payload)
        return Request(**data)

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
        if settings.scheduler_use_redis:
            import redis.asyncio as aioredis

            client = aioredis.from_url(settings.redis_url, decode_responses=False)
            queue = RedisQueue(client)
            dupefilter: BaseDupeFilter = RedisDupeFilter(client)
        else:
            queue = MemoryQueue()
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
        if self.settings.dupefilter_enabled and await self._dupefilter.seen(fp):
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

    async def close(self) -> None:
        await self._queue.close()
        await self._dupefilter.close()

    @property
    def queue_size(self) -> int:
        return self._queue.size

    @property
    def stats(self) -> dict[str, int]:
        return {"enqueued": self._enqueued, "duped": self._duped}
