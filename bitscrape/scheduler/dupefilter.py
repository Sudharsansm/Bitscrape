"""
URL fingerprinting and duplicate request filter.
Uses a fast hashset (in-memory) or a Redis set for distributed mode.
"""
from __future__ import annotations

import hashlib
from abc import ABC, abstractmethod

from bitscrape.core.models import Request


def fingerprint(request: Request) -> str:
    """Canonical fingerprint for a request (method + url + sorted body)."""
    raw = f"{request.method.upper()}:{request.url}"
    if request.body:
        raw += ":" + hashlib.blake2b(request.body, digest_size=8).hexdigest()
    return hashlib.blake2b(raw.encode(), digest_size=16).hexdigest()


class BaseDupeFilter(ABC):
    @abstractmethod
    async def seen(self, fp: str) -> bool:
        """Return True if already seen (and record it); False otherwise."""

    @abstractmethod
    async def close(self) -> None: ...


class MemoryDupeFilter(BaseDupeFilter):
    """Single-process in-memory filter (default)."""

    def __init__(self) -> None:
        self._seen: set[str] = set()

    async def seen(self, fp: str) -> bool:
        if fp in self._seen:
            return True
        self._seen.add(fp)
        return False

    async def close(self) -> None:
        self._seen.clear()

    @property
    def count(self) -> int:
        return len(self._seen)


class RedisDupeFilter(BaseDupeFilter):
    """Distributed dupe filter backed by a Redis set."""

    def __init__(self, redis_client: "redis.asyncio.Redis", key: str = "bitscrape:dupes") -> None:  # type: ignore[name-defined]
        self._redis = redis_client
        self._key = key

    async def seen(self, fp: str) -> bool:
        added = await self._redis.sadd(self._key, fp)
        return added == 0  # 0 means already existed

    async def close(self) -> None:
        pass  # keep Redis state for resume
