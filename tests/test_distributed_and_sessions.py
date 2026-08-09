"""
Tests for:
  - DistributedThrottleMiddleware -- real Redis-backed per-domain rate
    limiting, verified across two independent middleware instances
    (simulating two worker processes) sharing the same Redis.
  - SessionPoolMiddleware -- rotating cookie-jar sessions per domain.
"""

from __future__ import annotations

import time
from types import SimpleNamespace

import pytest
import redis.asyncio as aioredis

from bitscrape.core.models import Request, Response
from bitscrape.middleware.middleware import DistributedThrottleMiddleware, SessionPoolMiddleware

REDIS_URL = "redis://127.0.0.1:6390/0"


def _fake_spider(distributed_throttle_enabled=True, download_delay=0.0):
    return SimpleNamespace(
        settings=SimpleNamespace(
            distributed_throttle_enabled=distributed_throttle_enabled,
            download_delay=download_delay,
        )
    )


@pytest.fixture
async def redis_client():
    client = aioredis.from_url(REDIS_URL, decode_responses=False)
    await client.flushdb()
    yield client
    await client.flushdb()
    await client.aclose()


# ---------------------------------------------------------------------------
# DistributedThrottleMiddleware
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_disabled_setting_is_a_fast_noop(redis_client):
    mw = DistributedThrottleMiddleware(redis_client)
    spider = _fake_spider(distributed_throttle_enabled=False)
    t0 = time.monotonic()
    await mw.process_request(Request(url="https://example.com/a"), spider)
    assert time.monotonic() - t0 < 0.05


@pytest.mark.asyncio
async def test_zero_delay_is_a_fast_noop(redis_client):
    mw = DistributedThrottleMiddleware(redis_client)
    spider = _fake_spider(download_delay=0.0)
    t0 = time.monotonic()
    await mw.process_request(Request(url="https://example.com/a"), spider)
    assert time.monotonic() - t0 < 0.05


@pytest.mark.asyncio
async def test_first_request_to_domain_acquires_immediately(redis_client):
    mw = DistributedThrottleMiddleware(redis_client)
    spider = _fake_spider(download_delay=1.0)
    t0 = time.monotonic()
    await mw.process_request(Request(url="https://example.com/a"), spider)
    assert time.monotonic() - t0 < 0.3  # first caller doesn't wait


@pytest.mark.asyncio
async def test_second_worker_waits_out_the_lease_held_by_first(redis_client):
    """
    The core distributed guarantee: two INDEPENDENT middleware instances
    (standing in for two worker processes) sharing Redis must serialize
    their requests to the same domain with at least `delay` seconds between
    them -- not just within one process.
    """
    client_b = aioredis.from_url(REDIS_URL, decode_responses=False)
    worker_a = DistributedThrottleMiddleware(redis_client)
    worker_b = DistributedThrottleMiddleware(client_b)
    spider = _fake_spider(download_delay=0.8)

    t0 = time.monotonic()
    await worker_a.process_request(Request(url="https://shared-domain.test/x"), spider)
    t_a = time.monotonic() - t0

    t1 = time.monotonic()
    await worker_b.process_request(Request(url="https://shared-domain.test/y"), spider)
    t_b = time.monotonic() - t1

    assert t_a < 0.3  # worker A got it immediately
    assert t_b >= 0.5  # worker B had to wait out most of worker A's lease

    await client_b.aclose()


@pytest.mark.asyncio
async def test_different_domains_do_not_block_each_other(redis_client):
    mw = DistributedThrottleMiddleware(redis_client)
    spider = _fake_spider(download_delay=1.0)

    t0 = time.monotonic()
    await mw.process_request(Request(url="https://domain-a.test/"), spider)
    await mw.process_request(Request(url="https://domain-b.test/"), spider)
    elapsed = time.monotonic() - t0
    assert elapsed < 0.3  # unrelated domains, no cross-blocking


@pytest.mark.asyncio
async def test_uses_crawl_delay_from_request_meta_over_settings(redis_client):
    client_b = aioredis.from_url(REDIS_URL, decode_responses=False)
    worker_a = DistributedThrottleMiddleware(redis_client)
    worker_b = DistributedThrottleMiddleware(client_b)
    spider = _fake_spider(download_delay=0.05)  # low default

    req = Request(url="https://meta-delay.test/x", meta={"crawl_delay": 0.6})
    await worker_a.process_request(req, spider)
    t1 = time.monotonic()
    await worker_b.process_request(
        Request(url="https://meta-delay.test/y", meta={"crawl_delay": 0.6}), spider
    )
    elapsed = time.monotonic() - t1
    assert elapsed >= 0.4  # honoured the higher per-request crawl_delay, not settings' 0.05

    await client_b.aclose()


# ---------------------------------------------------------------------------
# SessionPoolMiddleware
# ---------------------------------------------------------------------------


def _fake_response(request: Request, set_cookie: str = "") -> Response:
    headers = {"Set-Cookie": set_cookie} if set_cookie else {}
    return Response(
        url=request.url, status=200, headers=headers, body=b"", request=request, elapsed_ms=1.0
    )


@pytest.mark.asyncio
async def test_round_robin_assigns_different_sessions():
    mw = SessionPoolMiddleware(pool_size=3)
    seen_ids = set()
    for _ in range(6):
        req = await mw.process_request(Request(url="https://example.com/"), _fake_spider())
        seen_ids.add(req.meta["session_id"])
    assert seen_ids == {0, 1, 2}


@pytest.mark.asyncio
async def test_sticky_session_id_is_honoured():
    mw = SessionPoolMiddleware(pool_size=3)
    req = Request(url="https://example.com/", meta={"session_id": 1})
    out = await mw.process_request(req, _fake_spider())
    assert out.meta["session_id"] == 1


@pytest.mark.asyncio
async def test_cookies_are_isolated_per_session():
    mw = SessionPoolMiddleware(pool_size=2)

    req0 = await mw.process_request(
        Request(url="https://example.com/", meta={"session_id": 0}), _fake_spider()
    )
    await mw.process_response(req0, _fake_response(req0, "user=alice"), _fake_spider())

    req1 = await mw.process_request(
        Request(url="https://example.com/", meta={"session_id": 1}), _fake_spider()
    )
    await mw.process_response(req1, _fake_response(req1, "user=bob"), _fake_spider())

    # Next request through session 0 should carry alice's cookie, not bob's.
    next_req0 = await mw.process_request(
        Request(url="https://example.com/", meta={"session_id": 0}), _fake_spider()
    )
    assert "user=alice" in next_req0.headers["Cookie"]
    assert "bob" not in next_req0.headers.get("Cookie", "")


@pytest.mark.asyncio
async def test_session_rotates_after_configured_request_count():
    mw = SessionPoolMiddleware(pool_size=1, rotate_every=2)
    req = Request(url="https://example.com/", meta={"session_id": 0})
    req = await mw.process_request(req, _fake_spider())
    await mw.process_response(req, _fake_response(req, "user=alice"), _fake_spider())  # count=1

    req2 = Request(url="https://example.com/", meta={"session_id": 0})
    req2 = await mw.process_request(req2, _fake_spider())
    assert "user=alice" in req2.headers["Cookie"]
    await mw.process_response(
        req2, _fake_response(req2, "user=alice"), _fake_spider()
    )  # count=2 -> rotate

    req3 = Request(url="https://example.com/", meta={"session_id": 0})
    req3 = await mw.process_request(req3, _fake_spider())
    assert "Cookie" not in req3.headers  # session was rotated -- fresh jar


@pytest.mark.asyncio
async def test_different_domains_get_independent_pools():
    mw = SessionPoolMiddleware(pool_size=2)
    req_a = await mw.process_request(
        Request(url="https://a.test/", meta={"session_id": 0}), _fake_spider()
    )
    await mw.process_response(req_a, _fake_response(req_a, "user=alice"), _fake_spider())

    req_b = await mw.process_request(
        Request(url="https://b.test/", meta={"session_id": 0}), _fake_spider()
    )
    assert "Cookie" not in req_b.headers  # different domain, no leakage
