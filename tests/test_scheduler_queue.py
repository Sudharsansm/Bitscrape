"""
Tests for the MemoryQueue tiebreaker fix.

Regression target: pushing two (or more) Request objects that share the same
priority used to raise
    TypeError: '<' not supported between instances of 'Request' and 'Request'
because asyncio.PriorityQueue falls through to comparing the second tuple
element whenever the first (priority) ties, and Request defines no __lt__.
"""

from __future__ import annotations

import asyncio

import pytest

from bitscrape.core.models import Request
from bitscrape.core.models import RequestPriority as Priority
from bitscrape.scheduler.scheduler import MemoryQueue


@pytest.mark.asyncio
async def test_same_priority_requests_do_not_crash():
    """The exact crash scenario: homepage -> many same-priority links."""
    q = MemoryQueue()
    urls = [f"https://example.com/article-{i}" for i in range(50)]
    for url in urls:
        await q.push(Request(url=url))  # all default (NORMAL) priority

    popped = []
    for _ in urls:
        req = await q.pop()
        assert req is not None
        popped.append(req.url)

    assert popped == urls  # FIFO within same priority is preserved


@pytest.mark.asyncio
async def test_fifo_order_within_same_priority():
    q = MemoryQueue()
    await q.push(Request(url="https://a.com/1", priority=Priority.NORMAL))
    await q.push(Request(url="https://a.com/2", priority=Priority.NORMAL))
    await q.push(Request(url="https://a.com/3", priority=Priority.NORMAL))

    first = await q.pop()
    second = await q.pop()
    third = await q.pop()

    assert [first.url, second.url, third.url] == [
        "https://a.com/1",
        "https://a.com/2",
        "https://a.com/3",
    ]


@pytest.mark.asyncio
async def test_higher_priority_popped_first_regardless_of_push_order():
    q = MemoryQueue()
    await q.push(Request(url="https://a.com/low", priority=Priority.LOW))
    await q.push(Request(url="https://a.com/high", priority=Priority.HIGH))
    await q.push(Request(url="https://a.com/normal", priority=Priority.NORMAL))

    first = await q.pop()
    second = await q.pop()
    third = await q.pop()

    assert first.url == "https://a.com/high"
    assert second.url == "https://a.com/normal"
    assert third.url == "https://a.com/low"


@pytest.mark.asyncio
async def test_pop_on_empty_queue_returns_none():
    q = MemoryQueue()
    assert await q.pop() is None


@pytest.mark.asyncio
async def test_size_tracks_pushes_and_pops():
    q = MemoryQueue()
    assert q.size == 0
    await q.push(Request(url="https://a.com/1"))
    await q.push(Request(url="https://a.com/2"))
    assert q.size == 2
    await q.pop()
    assert q.size == 1


@pytest.mark.asyncio
async def test_maxsize_provides_backpressure():
    """A bounded queue should block push() once full, rather than growing forever."""
    q = MemoryQueue(maxsize=1)
    await q.push(Request(url="https://a.com/1"))

    blocked = asyncio.Event()

    async def try_push_second():
        blocked.set()
        await q.push(Request(url="https://a.com/2"))

    task = asyncio.create_task(try_push_second())
    await blocked.wait()
    await asyncio.sleep(0.05)
    assert not task.done()  # still blocked, queue is full

    await q.pop()  # free a slot
    await asyncio.wait_for(task, timeout=1.0)
    assert task.done()


@pytest.mark.asyncio
async def test_many_concurrent_pushes_same_priority_no_crash():
    """Stress test: concurrent pushes at the same priority from many coroutines."""
    q = MemoryQueue()

    async def pusher(i: int):
        await q.push(Request(url=f"https://stress.com/{i}"))

    await asyncio.gather(*(pusher(i) for i in range(200)))
    assert q.size == 200

    seen = set()
    for _ in range(200):
        req = await q.pop()
        assert req is not None
        seen.add(req.url)
    assert len(seen) == 200
