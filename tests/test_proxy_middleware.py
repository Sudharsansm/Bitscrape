from __future__ import annotations

from types import SimpleNamespace

import pytest

from bitscrape.core.models import Request
from bitscrape.middleware.middleware import ProxyMiddleware


def _fake_spider():
    return SimpleNamespace(settings=SimpleNamespace())


@pytest.mark.asyncio
async def test_no_proxies_configured_is_a_noop():
    mw = ProxyMiddleware(proxies=[])
    request = Request(url="https://example.com")
    result = await mw.process_request(request, _fake_spider())
    assert result is None
    assert "proxy" not in request.meta


@pytest.mark.asyncio
async def test_single_proxy_applied_to_every_request():
    mw = ProxyMiddleware(proxies=["http://proxy1:8080"], rotate=True)
    for _ in range(5):
        request = Request(url="https://example.com")
        updated = await mw.process_request(request, _fake_spider())
        assert updated.meta["proxy"] == "http://proxy1:8080"


@pytest.mark.asyncio
async def test_round_robin_rotation_cycles_through_all_proxies():
    proxies = ["http://p1:8080", "http://p2:8080", "http://p3:8080"]
    mw = ProxyMiddleware(proxies=proxies, rotate=False)

    seen = []
    for _ in range(6):
        request = Request(url="https://example.com")
        updated = await mw.process_request(request, _fake_spider())
        seen.append(updated.meta["proxy"])

    assert seen == proxies + proxies  # exactly two full cycles


@pytest.mark.asyncio
async def test_random_rotation_only_uses_configured_proxies():
    proxies = ["http://p1:8080", "http://p2:8080"]
    mw = ProxyMiddleware(proxies=proxies, rotate=True)

    for _ in range(20):
        request = Request(url="https://example.com")
        updated = await mw.process_request(request, _fake_spider())
        assert updated.meta["proxy"] in proxies


def test_add_and_remove_proxy():
    mw = ProxyMiddleware(proxies=["http://p1:8080"])
    mw.add_proxy("http://p2:8080")
    assert set(mw.proxies) == {"http://p1:8080", "http://p2:8080"}

    mw.remove_proxy("http://p1:8080")
    assert mw.proxies == ["http://p2:8080"]


@pytest.mark.asyncio
async def test_original_request_headers_and_url_untouched():
    mw = ProxyMiddleware(proxies=["http://p1:8080"])
    request = Request(url="https://example.com/page", headers={"X-Test": "1"})
    updated = await mw.process_request(request, _fake_spider())
    assert updated.url == request.url
    assert updated.headers == request.headers
