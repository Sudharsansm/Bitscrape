"""DNS-over-HTTPS end to end, text helpers, fast JSON, aliases."""
import socket
from decimal import Decimal

import pytest
from aiohttp import web

from bitscrape import ContentSpider, SiteToMarkdownSpider, jsonx, textutil
from bitscrape.core.settings import Settings
from bitscrape.dns import DoHResolver, make_resolver, resolve_doh_url
from bitscrape.fetchers import AsyncFetcherSession
from bitscrape.sessions import HttpSession


async def _serve(app):
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    return runner, site._server.sockets[0].getsockname()[1]


async def _doh_and_target():
    queries = []

    async def doh(req):
        queries.append((req.query["name"], req.query["type"]))
        name, typ = req.query["name"], req.query["type"]
        if name.endswith(".nx"):
            return web.json_response({"Status": 3})
        ans = [{"name": name, "type": 1, "TTL": 5, "data": "127.0.0.1"}] if typ == "A" else []
        return web.json_response({"Status": 0, "Answer": ans})

    async def target(req):
        return web.Response(text=f"host={req.host}")

    doh_app, tgt_app = web.Application(), web.Application()
    doh_app.router.add_get("/resolve", doh)
    tgt_app.router.add_get("/", target)
    r1, p1 = await _serve(doh_app)
    r2, p2 = await _serve(tgt_app)
    return (r1, r2), f"http://127.0.0.1:{p1}/resolve", p2, queries


def test_provider_names_and_validation():
    assert resolve_doh_url("google").startswith("https://8.8.8.8")
    assert resolve_doh_url(None).startswith("https://1.1.1.1")
    assert resolve_doh_url("https://dns.example/q") == "https://dns.example/q"
    with pytest.raises(ValueError):
        resolve_doh_url("http://insecure")
    assert make_resolver(False) is None and isinstance(make_resolver("quad9"), DoHResolver)


async def test_http_session_resolves_through_doh():
    runners, doh_url, port, queries = await _doh_and_target()
    try:
        s = HttpSession(dns_over_https=True)
        settings = Settings()
        await s.start(settings)
        s._resolver.url = doh_url  # plain-http DoH server for the test
        async with s.client.get(f"http://fake-host.test:{port}/") as r:
            assert (await r.text()).startswith("host=fake-host.test")
        assert ("fake-host.test", "A") in queries
        await s.close()
    finally:
        for r in runners:
            await r.cleanup()


async def test_fetcher_session_doh_and_http2_rejected():
    with pytest.raises(ValueError, match="http1"):
        AsyncFetcherSession(protocol="http2", dns_over_https=True)
    runners, doh_url, port, queries = await _doh_and_target()
    try:
        async with AsyncFetcherSession(dns_over_https=True, settings=Settings()) as f:
            f._backend._resolver.url = doh_url
            page = await f.get(f"http://other-host.test:{port}/")
            assert page.text.startswith("host=other-host.test")
    finally:
        for r in runners:
            await r.cleanup()


async def test_nxdomain_raises_and_fallback_optional():
    runners, doh_url, _port, _q = await _doh_and_target()
    try:
        r = DoHResolver("cloudflare")
        r.url = doh_url
        with pytest.raises(OSError, match="NXDOMAIN"):
            await r.resolve("nothing.nx", 80)
        assert (await r.resolve("127.0.0.1", 80))[0]["host"] == "127.0.0.1"
        loc = await r.resolve("localhost", 80, socket.AF_INET)
        assert loc and r.queries == 1
        await r.close()
    finally:
        for x in runners:
            await x.cleanup()


def test_dns_leak_protection_still_refused():
    from bitscrape.sessions import UNSUPPORTED_OPTIONS

    assert "dns_leak_protection" in UNSUPPORTED_OPTIONS and "dns_over_https" not in UNSUPPORTED_OPTIONS


# -- text helpers -----------------------------------------------------------------


def test_numbers_and_prices():
    assert textutil.extract_numbers("1,234.50 | 1.234,50 | 1 234,5 | -7 | 3,5") == [
        Decimal("1234.50"), Decimal("1234.50"), Decimal("1234.5"), Decimal("-7"), Decimal("3.5")]
    assert textutil.parse_price("Now only $1,299.00!") == (Decimal("1299.00"), "USD")
    assert textutil.parse_price("€ 12,50") == (Decimal("12.50"), "EUR")
    assert textutil.parse_price("£5")[1] == "GBP" and textutil.parse_price("free") is None


def test_clean_strip_slug_truncate_extract():
    assert textutil.clean_text("<p>a&nbsp;&nbsp;b\u200b</p>", strip_html=True) == "a b"
    assert textutil.strip_tags("<style>x{}</style>hi<!-- c -->&amp;") .strip() == "hi &"
    assert textutil.clean_text("ﬁne １２", accents=True) == "fine 12"
    assert textutil.strip_accents("Café") == "Cafe" and textutil.slugify("Hello, Wörld!") == "hello-world"
    assert textutil.truncate("the quick brown fox", 12) == "the quick…" and textutil.truncate("hi", 5) == "hi"
    assert textutil.extract_emails("A@b.com, a@B.com") == ["a@b.com"]
    assert textutil.extract_urls("see https://x.io/a, and (https://y.io).") == ["https://x.io/a", "https://y.io"]
    assert textutil.normalize_whitespace("a\n\n\n\nb  c", keep_newlines=True) == "a\n\nb c"


def test_selector_text_helpers():
    from bitscrape.core.models import Request, Response
    from bitscrape.parser.selector import ParsedResponse

    html = b"<html><body><p id=p> Price:\xc2\xa0 <b>$1,299.00</b> </p><script>var x=1</script>mail me@x.io</body></html>"
    page = ParsedResponse(Response(url="https://x.test", status=200, body=html, request=Request(url="https://x.test")))
    p = page.css("#p")[0]
    assert p.price() == (Decimal("1299.00"), "USD") and p.numbers() == [Decimal("1299.00")]
    assert p.clean_text() == "Price: $1,299.00"
    assert "var x" not in page.clean_text() and page.emails() == ["me@x.io"]
    assert page.re_first(r"(\d+),") == "1"


# -- json ---------------------------------------------------------------------------


def test_jsonx_roundtrip_and_fallbacks():
    data = {"a": 1, "s": {2, 1}, "big": 2**70, "u": "é", 1: "k", "b": b"hi"}
    out = jsonx.loads(jsonx.dumps(data))
    assert out["big"] == 2**70 and out["s"] == [1, 2] and out["u"] == "é" and out["1"] == "k" and out["b"] == "hi"
    assert jsonx.dumps({"a": [1]}, indent=True).startswith('{\n  "a"')
    assert jsonx.dumps({"b": 1, "a": 2}, sort_keys=True) == '{"a":2,"b":1}'
    with pytest.raises(ValueError):
        jsonx.loads("{bad")


def test_content_spider_alias():
    assert ContentSpider is SiteToMarkdownSpider
