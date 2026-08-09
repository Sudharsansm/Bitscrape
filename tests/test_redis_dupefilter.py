"""
Tests for RedisDupeFilter against a REAL local Redis instance (not mocked) --
verifying persistent, cross-process-safe deduplication, which is what
"distributed request filtering" actually requires: two independent
DupeFilter instances (simulating two worker processes) must agree on what's
already been seen.
"""

from __future__ import annotations

import pytest
import redis.asyncio as aioredis

from bitscrape.core.models import Request
from bitscrape.scheduler.dupefilter import MemoryDupeFilter, RedisDupeFilter, fingerprint

REDIS_URL = "redis://127.0.0.1:6390/0"


@pytest.fixture
async def redis_client():
    client = aioredis.from_url(REDIS_URL, decode_responses=False)
    await client.flushdb()
    yield client
    await client.flushdb()
    await client.aclose()


def test_fingerprint_is_deterministic():
    r1 = Request(url="https://example.com/a")
    r2 = Request(url="https://example.com/a")
    assert fingerprint(r1) == fingerprint(r2)


def test_fingerprint_differs_by_url():
    r1 = Request(url="https://example.com/a")
    r2 = Request(url="https://example.com/b")
    assert fingerprint(r1) != fingerprint(r2)


def test_fingerprint_differs_by_body():
    r1 = Request(url="https://example.com/a", method="POST", body=b"x=1")
    r2 = Request(url="https://example.com/a", method="POST", body=b"x=2")
    assert fingerprint(r1) != fingerprint(r2)


@pytest.mark.asyncio
async def test_memory_dupefilter_basic():
    df = MemoryDupeFilter()
    fp = fingerprint(Request(url="https://example.com/a"))
    assert await df.seen(fp) is False
    assert await df.seen(fp) is True
    assert df.count == 1


@pytest.mark.asyncio
async def test_redis_dupefilter_basic(redis_client):
    df = RedisDupeFilter(redis_client, key="test:dupes")
    fp = fingerprint(Request(url="https://example.com/a"))
    assert await df.seen(fp) is False
    assert await df.seen(fp) is True


@pytest.mark.asyncio
async def test_redis_dupefilter_is_shared_across_independent_instances(redis_client):
    """
    This is the actual distributed-crawling guarantee: two DupeFilter
    objects (standing in for two separate worker processes) pointed at the
    same Redis key must agree on what's already been crawled.
    """
    worker_a = RedisDupeFilter(redis_client, key="test:shared-dupes")

    client_b = aioredis.from_url(REDIS_URL, decode_responses=False)
    worker_b = RedisDupeFilter(client_b, key="test:shared-dupes")

    fp = fingerprint(Request(url="https://example.com/shared-page"))
    assert await worker_a.seen(fp) is False  # worker A claims it first
    assert await worker_b.seen(fp) is True  # worker B sees it's already taken

    await client_b.aclose()


@pytest.mark.asyncio
async def test_redis_dupefilter_survives_close_for_resume(redis_client):
    """Redis-backed state must persist across close() -- unlike the memory
    filter -- so a crawl can be resumed without re-crawling everything."""
    df1 = RedisDupeFilter(redis_client, key="test:resume-dupes")
    fp = fingerprint(Request(url="https://example.com/resumed-page"))
    await df1.seen(fp)
    await df1.close()

    df2 = RedisDupeFilter(redis_client, key="test:resume-dupes")
    assert await df2.seen(fp) is True  # still known after "restart"


@pytest.mark.asyncio
async def test_redis_dupefilter_different_keys_are_isolated(redis_client):
    df_a = RedisDupeFilter(redis_client, key="test:crawl-a")
    df_b = RedisDupeFilter(redis_client, key="test:crawl-b")
    fp = fingerprint(Request(url="https://example.com/x"))

    assert await df_a.seen(fp) is False
    assert await df_b.seen(fp) is False  # different key/crawl, not shared
