"""
Tests for the "ready-made spider templates" upgrade:

* ``LinkExtractor.matches()`` (new) and ``Rule.priority`` (fixed: declared but
  never applied).
* ``OffsiteMiddleware`` + the Engine's dedicated ``DropItem`` handling (new:
  ``allowed_domains`` was previously documented but completely unenforced,
  and any ``DropItem`` -- e.g. from ``RobotsMiddleware`` -- was miscounted as
  a failure with an ERROR-level traceback).
* :class:`SitemapSpider`: robots.txt-driven discovery, ``Rule``-based
  dispatch, hreflang alternates, gzip-bomb protection.
* :class:`XMLFeedSpider`: the Atom/namespace bug (``findtext`` silently
  returning ``None``), the previously-dead ``namespaces`` setting, gzip.
* :class:`CSVFeedSpider`: ``quotechar``, gzip.
* :class:`ShopifySpider`: variant dedup, the richer field set, ``max_pages``.
* :class:`SiteToMarkdownSpider`: new template.

All driven end-to-end through ``build_engine()`` / ``Engine`` against a real
local aiohttp server.
"""

from __future__ import annotations

import gzip as gzip_mod
from typing import Any

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from bitscrape.core.models import Request
from bitscrape.core.spider import Spider
from bitscrape.crawl import CrawlSpider, LinkExtractor, Rule
from bitscrape.crawl.rules import RequestPriority
from bitscrape.engine import Engine
from bitscrape.factory import build_engine
from bitscrape.spiders.templates import (
    CSVFeedSpider,
    ShopifySpider,
    SitemapSpider,
    SiteToMarkdownSpider,
    XMLFeedSpider,
)

# ---------------------------------------------------------------------------
# LinkExtractor.matches() / Rule.priority
# ---------------------------------------------------------------------------


def test_matches_applies_allow_deny_regex():
    le = LinkExtractor(allow=r"/product/\d+$", deny=r"/product/999$")
    assert le.matches("https://example.com/product/1")
    assert not le.matches("https://example.com/product/999")
    assert not le.matches("https://example.com/blog/post")


def test_matches_applies_domain_filters():
    le = LinkExtractor(allow_domains=["example.com"])
    assert le.matches("https://sub.example.com/x")
    assert not le.matches("https://other.com/x")


async def test_matches_applies_extension_filter_and_canonicalizes():
    le = LinkExtractor()
    assert not le.matches("https://example.com/a.pdf")
    assert not le.matches("https://example.com/a.pdf?x=1")  # extension check ignores the query
    assert le.matches("https://example.com/a.html?x=1")
    canon = LinkExtractor(canonicalize=True)
    assert canon.matches("HTTPS://Example.com:443/x")  # canonicalized before matching


def test_matches_rejects_bad_scheme_or_relative_url():
    le = LinkExtractor()
    assert not le.matches("javascript:void(0)")
    assert not le.matches("/relative/path")


async def test_rule_priority_is_applied_to_followed_requests():
    from bitscrape.plugins import PluginManager

    app = web.Application()

    async def index(request: web.Request) -> web.Response:
        return web.Response(
            text='<html><body><a href="/a">A</a><a href="/b">B</a></body></html>',
            content_type="text/html",
        )

    async def leaf(request: web.Request) -> web.Response:
        return web.Response(text="<html><body>leaf</body></html>", content_type="text/html")

    app.router.add_get("/", index)
    app.router.add_get("/a", leaf)
    app.router.add_get("/b", leaf)
    server = TestServer(app)
    await server.start_server()
    base = str(server.make_url("/")).rstrip("/")

    seen_priorities: list[tuple[str, Any]] = []

    class PrioritySpider(CrawlSpider):
        name = "priority_demo"
        start_urls = [base]
        rules = (Rule(LinkExtractor(allow=r"/a$"), follow=True, priority=RequestPriority.HIGH),)

        async def parse_start_url(self, response):
            return
            yield

    def on_scheduled(request: Request, spider: Any) -> None:
        seen_priorities.append((request.url, request.priority))

    plugins = PluginManager()
    plugins.on("request_scheduled", on_scheduled)

    try:
        spider = PrioritySpider()
        await Engine(spider=spider, settings=spider.settings, plugin_manager=plugins).run()
    finally:
        await server.close()

    a_priority = next(p for u, p in seen_priorities if u.endswith("/a"))
    assert a_priority == RequestPriority.HIGH
    assert not any(u.endswith("/b") for u, _ in seen_priorities)  # outside the rule's allow=r"/a$"


# ---------------------------------------------------------------------------
# OffsiteMiddleware / allowed_domains / DropItem accounting
# ---------------------------------------------------------------------------


class _OffsiteApp:
    def __init__(self) -> None:
        self.app = web.Application()
        self.app.router.add_get("/", self.index)
        self.app.router.add_get("/local", self.local)

    async def index(self, request: web.Request) -> web.Response:
        return web.Response(
            text='<a href="/local">local</a><a href="https://EXTERNAL.example/x">external</a>',
            content_type="text/html",
        )

    async def local(self, request: web.Request) -> web.Response:
        return web.Response(text="ok")


@pytest.fixture
async def offsite_server():
    srv = TestServer(_OffsiteApp().app)
    await srv.start_server()
    try:
        yield srv
    finally:
        await srv.close()


class _OffsiteSpider(CrawlSpider):
    name = "offsite_demo"
    rules = (Rule(LinkExtractor(), follow=True),)

    def __init__(self, base: str, **kw: Any) -> None:
        self.allowed_domains = [base.split("://", 1)[1].split(":")[0]]  # the test server's bare host
        super().__init__(**kw)
        self.base = base
        self.fetched: list[str] = []

    @property
    def start_urls(self):  # type: ignore[override]
        return [self.base]

    async def parse_start_url(self, response):
        self.fetched.append(response.url)
        yield {"url": response.url}

    async def _rule_callback(self, response, rule_index=None):
        self.fetched.append(response.url)
        async for out in super()._rule_callback(response, rule_index):
            yield out
        yield {"url": response.url}


async def test_allowed_domains_drops_offsite_requests(offsite_server):
    base = str(offsite_server.make_url("/")).rstrip("/")
    spider = _OffsiteSpider(base)
    engine = await build_engine(spider, spider.settings)
    stats = await engine.run()
    assert all("external.example" not in u for u in spider.fetched)
    assert any(u.endswith("/local") for u in spider.fetched)
    assert stats.requests_offsite_dropped >= 1
    assert stats.requests_dropped >= stats.requests_offsite_dropped
    assert stats.requests_failed == 0  # the drop must NOT be miscounted as a failure


async def test_empty_allowed_domains_is_a_no_op(offsite_server):
    base = str(offsite_server.make_url("/")).rstrip("/")

    class NoRestriction(_OffsiteSpider):
        name = "no_restriction_demo"

        def __init__(self, base: str, **kw: Any) -> None:
            super().__init__(base, **kw)
            self.allowed_domains = []

    spider = NoRestriction(base)
    engine = await build_engine(spider, spider.settings)
    stats = await engine.run()
    assert stats.requests_offsite_dropped == 0


async def test_dropitem_from_robots_is_not_counted_as_failure():
    """RobotsMiddleware raising DropItem must not land in requests_failed
    (a policy drop) nor trigger the errback (meant for real failures)."""
    app = web.Application()

    async def robots(request: web.Request) -> web.Response:
        return web.Response(text="User-agent: *\nDisallow: /blocked/\n")

    async def blocked(request: web.Request) -> web.Response:
        return web.Response(text="secret")

    app.router.add_get("/robots.txt", robots)
    app.router.add_get("/blocked/x", blocked)
    server = TestServer(app)
    await server.start_server()
    base = str(server.make_url("/")).rstrip("/")

    errback_calls: list[Any] = []

    class BlockedSpider(Spider):
        name = "blocked_demo"

        def start_requests(self):
            return [Request(url=f"{base}/blocked/x", errback="on_error", dont_filter=True)]

        async def parse(self, response):
            yield {"url": response.url}

        async def on_error(self, failure):
            errback_calls.append(failure)

    try:
        spider = BlockedSpider()
        engine = await build_engine(spider, spider.settings)
        stats = await engine.run()
    finally:
        await server.close()

    assert stats.requests_dropped == 1
    assert stats.requests_failed == 0
    assert errback_calls == []


# ---------------------------------------------------------------------------
# SitemapSpider
# ---------------------------------------------------------------------------

SITEMAP_INDEX = b"""<?xml version="1.0" encoding="UTF-8"?>
<sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <sitemap><loc>{base}/sitemap-products.xml</loc></sitemap>
  <sitemap><loc>{base}/sitemap-blog.xml</loc></sitemap>
</sitemapindex>
"""

SITEMAP_PRODUCTS = b"""<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9"
        xmlns:xhtml="http://www.w3.org/1999/xhtml">
  <url>
    <loc>{base}/product/1</loc>
    <xhtml:link rel="alternate" hreflang="fr" href="{base}/fr/product/1"/>
  </url>
  <url><loc>{base}/product/2</loc></url>
</urlset>
"""

SITEMAP_BLOG = b"""<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <url><loc>{base}/blog/hello-world</loc></url>
</urlset>
"""


class _SitemapApp:
    def __init__(self, base_holder: dict[str, str]) -> None:
        self.app = web.Application()
        self.base_holder = base_holder
        self.hits: list[str] = []
        self.app.router.add_get("/robots.txt", self.robots)
        self.app.router.add_get("/sitemap.xml", self.index)
        self.app.router.add_get("/sitemap-products.xml", self.products)
        self.app.router.add_get("/sitemap-blog.xml.gz", self.blog_gz)
        self.app.router.add_get("/product/{n}", self.leaf)
        self.app.router.add_get("/fr/product/{n}", self.leaf)
        self.app.router.add_get("/blog/{slug}", self.leaf)

    def _b(self, body: bytes) -> bytes:
        return body.replace(b"{base}", self.base_holder["base"].encode())

    async def robots(self, request: web.Request) -> web.Response:
        return web.Response(text=f"User-agent: *\nSitemap: {self.base_holder['base']}/sitemap.xml\n")

    async def index(self, request: web.Request) -> web.Response:
        idx = self._b(SITEMAP_INDEX).replace(b"sitemap-blog.xml<", b"sitemap-blog.xml.gz<")
        return web.Response(body=idx, content_type="application/xml")

    async def products(self, request: web.Request) -> web.Response:
        return web.Response(body=self._b(SITEMAP_PRODUCTS), content_type="application/xml")

    async def blog_gz(self, request: web.Request) -> web.Response:
        return web.Response(
            body=gzip_mod.compress(self._b(SITEMAP_BLOG)), content_type="application/x-gzip"
        )

    async def leaf(self, request: web.Request) -> web.Response:
        self.hits.append(request.path)
        return web.Response(text=f"<html><body>{request.path}</body></html>")


@pytest.fixture
async def sitemap_server():
    holder: dict[str, str] = {}
    app = _SitemapApp(holder)
    server = TestServer(app.app)
    await server.start_server()
    holder["base"] = str(server.make_url("/")).rstrip("/")
    server.app_obj = app  # type: ignore[attr-defined]
    server.base = holder["base"]  # type: ignore[attr-defined]
    try:
        yield server
    finally:
        await server.close()


async def test_sitemap_spider_discovers_via_robots_txt(sitemap_server):
    class RobotsDriven(SitemapSpider):
        name = "robots_driven"
        sitemap_urls = [f"{sitemap_server.base}/robots.txt"]
        sitemap_rules = [("/product/", "parse_product"), ("/blog/", "parse_blog")]

        async def parse_product(self, response):
            yield {"kind": "product", "url": response.url}

        async def parse_blog(self, response):
            yield {"kind": "blog", "url": response.url}

    spider = RobotsDriven()
    engine = Engine(spider=spider, settings=spider.settings)
    stats = await engine.run()
    assert stats.items_scraped == 3  # 2 products + 1 blog (gzip'd nested sitemap)
    assert any(p.endswith("/blog/hello-world") for p in sitemap_server.app_obj.hits)


async def test_sitemap_spider_follows_index_and_alternate_links(sitemap_server):
    class WithAlternates(SitemapSpider):
        name = "with_alternates"
        sitemap_urls = [f"{sitemap_server.base}/sitemap.xml"]
        sitemap_rules = [("/product/", "parse_product"), ("/blog/", "parse_blog")]
        sitemap_alternate_links = True

        async def parse_product(self, response):
            yield {"kind": "product", "url": response.url}

        async def parse_blog(self, response):
            yield {"kind": "blog", "url": response.url}

    spider = WithAlternates()
    engine = Engine(spider=spider, settings=spider.settings)
    stats = await engine.run()
    # 2 products + 1 alternate (fr) + 1 blog
    assert stats.items_scraped == 4
    assert any(p == "/fr/product/1" for p in sitemap_server.app_obj.hits)


async def test_sitemap_spider_rule_based_dispatch(sitemap_server):
    class RuleDriven(SitemapSpider):
        name = "rule_driven"
        sitemap_urls = [f"{sitemap_server.base}/sitemap.xml"]
        rules = (
            Rule(LinkExtractor(allow=r"/product/\d+$"), callback="parse_product"),
            Rule(LinkExtractor(allow=r"/blog/"), callback="parse_blog"),
        )

        async def parse_product(self, response):
            yield {"kind": "product-via-rule"}

        async def parse_blog(self, response):
            yield {"kind": "blog-via-rule"}

    spider = RuleDriven()
    engine = Engine(spider=spider, settings=spider.settings)
    stats = await engine.run()
    assert stats.items_scraped == 3


async def test_sitemap_spider_falls_back_to_parse_with_no_rules(sitemap_server):
    class Bare(SitemapSpider):
        name = "bare"
        sitemap_urls = [f"{sitemap_server.base}/sitemap-products.xml"]

        async def parse(self, response):
            yield {"url": response.url}

    spider = Bare()
    engine = Engine(spider=spider, settings=spider.settings)
    stats = await engine.run()
    assert stats.items_scraped == 2


async def test_sitemap_gzip_bomb_is_refused():
    """A tiny compressed body that decompresses far past the cap must not be
    fully materialised in memory."""
    from bitscrape.spiders.templates import _decompress

    bomb = gzip_mod.compress(b"0" * (200 * 1024 * 1024))  # 200 MiB of zeros, compresses tiny
    with pytest.raises(OSError, match="exceeds"):
        _decompress(bomb, "https://example.com/sitemap.xml.gz")


def test_decompress_passes_through_non_gzip():
    from bitscrape.spiders.templates import _decompress

    assert _decompress(b"<xml/>", "https://example.com/sitemap.xml") == b"<xml/>"


def test_decompress_detects_by_magic_bytes_even_without_gz_extension():
    from bitscrape.spiders.templates import _decompress

    body = gzip_mod.compress(b"hello")
    assert _decompress(body, "https://example.com/feed", "") == b"hello"


# ---------------------------------------------------------------------------
# XMLFeedSpider
# ---------------------------------------------------------------------------

RSS_FEED = b"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0">
  <channel>
    <title>Example feed</title>
    <item><title>First post</title><link>https://example.com/1</link></item>
    <item><title>Second post</title><link>https://example.com/2</link></item>
  </channel>
</rss>
"""

ATOM_FEED = b"""<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <title>Example Atom feed</title>
  <entry><title>Atom One</title><id>urn:1</id></entry>
  <entry><title>Atom Two</title><id>urn:2</id></entry>
</feed>
"""

MEDIA_FEED = b"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0" xmlns:media="http://search.yahoo.com/mrss/" xmlns:other="urn:other-content">
  <channel>
    <item>
      <title>Item with two "content" tags in different namespaces</title>
      <media:content url="https://example.com/media.jpg"/>
      <other:content url="https://example.com/should-not-match"/>
    </item>
  </channel>
</rss>
"""


@pytest.fixture
async def feed_server():
    app = web.Application()

    async def rss(request: web.Request) -> web.Response:
        return web.Response(body=RSS_FEED, content_type="application/rss+xml")

    async def atom(request: web.Request) -> web.Response:
        return web.Response(body=ATOM_FEED, content_type="application/atom+xml")

    async def media(request: web.Request) -> web.Response:
        return web.Response(body=MEDIA_FEED, content_type="application/rss+xml")

    async def rss_gz(request: web.Request) -> web.Response:
        return web.Response(body=gzip_mod.compress(RSS_FEED), content_type="application/gzip")

    app.router.add_get("/rss.xml", rss)
    app.router.add_get("/atom.xml", atom)
    app.router.add_get("/media.xml", media)
    app.router.add_get("/rss.xml.gz", rss_gz)
    server = TestServer(app)
    await server.start_server()
    try:
        yield server
    finally:
        await server.close()


async def test_xml_feed_spider_plain_rss(feed_server):
    class RSS(XMLFeedSpider):
        name = "rss"
        start_urls = [str(feed_server.make_url("/rss.xml"))]
        itertag = "item"

        async def parse_node(self, response, node):
            yield {"title": node.findtext("title"), "link": node.findtext("link")}

    spider = RSS()
    stats = await Engine(spider=spider, settings=spider.settings).run()
    assert stats.items_scraped == 2


async def test_xml_feed_spider_atom_namespace_bug_is_fixed(feed_server):
    """Regression: findtext("title") on a namespaced Atom <entry> used to
    silently return None because the node still carried the Atom namespace."""
    titles: list[str | None] = []

    class Atom(XMLFeedSpider):
        name = "atom"
        start_urls = [str(feed_server.make_url("/atom.xml"))]
        itertag = "entry"

        async def parse_node(self, response, node):
            titles.append(node.findtext("title"))
            yield {"title": node.findtext("title")}

    spider = Atom()
    stats = await Engine(spider=spider, settings=spider.settings).run()
    assert stats.items_scraped == 2
    assert titles == ["Atom One", "Atom Two"]
    assert None not in titles


async def test_xml_feed_spider_namespaces_disambiguates(feed_server):
    """Regression: the documented `namespaces` class attribute was declared
    but never actually consulted, so two same-named tags in different
    namespaces couldn't be told apart."""
    urls: list[str | None] = []

    class Media(XMLFeedSpider):
        name = "media"
        start_urls = [str(feed_server.make_url("/media.xml"))]
        itertag = "media:content"
        namespaces = {"media": "http://search.yahoo.com/mrss/"}

        async def parse_node(self, response, node):
            urls.append(node.get("url"))
            yield {"url": node.get("url")}

    spider = Media()
    stats = await Engine(spider=spider, settings=spider.settings).run()
    assert stats.items_scraped == 1
    assert urls == ["https://example.com/media.jpg"]  # not the "other:content" tag


def test_xml_feed_spider_unknown_namespace_prefix_raises():
    class Bad(XMLFeedSpider):
        name = "bad"
        itertag = "media:content"  # namespaces left empty

    with pytest.raises(ValueError, match="namespaces"):
        Bad()._wanted_tag()


async def test_xml_feed_spider_decompresses_gzip(feed_server):
    class RSS(XMLFeedSpider):
        name = "rss_gz"
        start_urls = [str(feed_server.make_url("/rss.xml.gz"))]
        itertag = "item"

        async def parse_node(self, response, node):
            yield {"title": node.findtext("title")}

    spider = RSS()
    stats = await Engine(spider=spider, settings=spider.settings).run()
    assert stats.items_scraped == 2


# ---------------------------------------------------------------------------
# CSVFeedSpider
# ---------------------------------------------------------------------------


@pytest.fixture
async def csv_server():
    app = web.Application()
    csv_body = b"sku,price\nABC123,9.99\nXYZ789,19.99\n"
    semicolon_body = b'sku;note\nABC123;"has; a semicolon"\n'

    async def csv_route(request: web.Request) -> web.Response:
        return web.Response(body=csv_body, content_type="text/csv")

    async def semicolon_route(request: web.Request) -> web.Response:
        return web.Response(body=semicolon_body, content_type="text/csv")

    async def csv_gz(request: web.Request) -> web.Response:
        return web.Response(body=gzip_mod.compress(csv_body), content_type="application/gzip")

    app.router.add_get("/prices.csv", csv_route)
    app.router.add_get("/semicolon.csv", semicolon_route)
    app.router.add_get("/prices.csv.gz", csv_gz)
    server = TestServer(app)
    await server.start_server()
    try:
        yield server
    finally:
        await server.close()


async def test_csv_feed_spider_basic(csv_server):
    rows: list[dict[str, str]] = []

    class Prices(CSVFeedSpider):
        name = "prices"
        start_urls = [str(csv_server.make_url("/prices.csv"))]

        async def parse_row(self, response, row):
            rows.append(row)
            yield row

    spider = Prices()
    stats = await Engine(spider=spider, settings=spider.settings).run()
    assert stats.items_scraped == 2
    assert rows[0] == {"sku": "ABC123", "price": "9.99"}


async def test_csv_feed_spider_custom_delimiter_and_quotechar(csv_server):
    rows: list[dict[str, str]] = []

    class Semicolon(CSVFeedSpider):
        name = "semicolon"
        start_urls = [str(csv_server.make_url("/semicolon.csv"))]
        delimiter = ";"
        quotechar = '"'

        async def parse_row(self, response, row):
            rows.append(row)
            yield row

    spider = Semicolon()
    await Engine(spider=spider, settings=spider.settings).run()
    assert rows == [{"sku": "ABC123", "note": "has; a semicolon"}]


async def test_csv_feed_spider_decompresses_gzip(csv_server):
    rows: list[dict[str, str]] = []

    class GzPrices(CSVFeedSpider):
        name = "gz_prices"
        start_urls = [str(csv_server.make_url("/prices.csv.gz"))]

        async def parse_row(self, response, row):
            rows.append(row)
            yield row

    spider = GzPrices()
    await Engine(spider=spider, settings=spider.settings).run()
    assert len(rows) == 2


# ---------------------------------------------------------------------------
# ShopifySpider
# ---------------------------------------------------------------------------


def _product(pid: int, title: str, variants: list[dict[str, Any]], **extra: Any) -> dict[str, Any]:
    base = {
        "id": pid,
        "title": title,
        "vendor": "Acme",
        "product_type": "Widget",
        "handle": title.lower().replace(" ", "-"),
        "tags": "a,b",
        "body_html": "<p>Nice <b>widget</b></p>",
        "images": [{"src": "https://cdn.example.com/img.jpg"}],
        "variants": variants,
    }
    base.update(extra)
    return base


@pytest.fixture
async def shopify_server():
    app = web.Application()
    page1 = {
        "products": [
            _product(
                1,
                "Tee Shirt",
                [
                    {"id": 10, "title": "Small", "sku": "TS-S", "price": "19.99", "available": True},
                    {
                        "id": 11,
                        "title": "Large",
                        "sku": "TS-L",
                        "price": "19.99",
                        "compare_at_price": "24.99",
                        "available": False,
                        "barcode": "12345",
                    },
                ],
            ),
            _product(2, "No Variant Product", []),  # variants=[] -> synthetic {} variant
        ]
    }
    page2 = {"products": [_product(3, "Mug", [{"id": 30, "sku": "MUG", "price": "5.00"}])]}
    pages = {1: page1, 2: page2, 3: {"products": []}}

    async def products(request: web.Request) -> web.Response:
        page = int(request.query.get("page", "1"))
        return web.json_response(pages.get(page, {"products": []}))

    app.router.add_get("/products.json", products)
    server = TestServer(app)
    await server.start_server()
    try:
        yield server
    finally:
        await server.close()


async def test_shopify_spider_paginates_and_yields_one_item_per_variant(shopify_server):
    items: list[dict[str, Any]] = []

    class Shop(ShopifySpider):
        name = "shop"
        shop_urls = [str(shopify_server.make_url("/")).rstrip("/")]
        page_size = 2

        async def parse_variant(self, response, product, variant):
            async for out in super().parse_variant(response, product, variant):
                items.append(out)
                yield out

    spider = Shop()
    stats = await Engine(spider=spider, settings=spider.settings).run()
    # 2 variants (tee) + 1 synthetic (no-variant product) + 1 (mug) = 4
    assert stats.items_scraped == 4
    by_sku = {i["sku"]: i for i in items if i["sku"]}
    assert by_sku["TS-S"]["price"] == "19.99"
    assert by_sku["TS-S"]["title"] == "Tee Shirt"
    assert by_sku["TS-L"]["available"] is False
    assert by_sku["TS-L"]["old_price"] == "24.99"  # compare_at_price > price
    assert by_sku["TS-S"]["old_price"] is None
    assert by_sku["TS-L"]["barcode"] == "12345"
    assert by_sku["TS-S"]["image_url"] == "https://cdn.example.com/img.jpg"
    assert by_sku["TS-S"]["description"] == "Nice widget"  # HTML tags stripped


async def test_shopify_spider_no_variant_product_still_yields_one_item(shopify_server):
    class Shop(ShopifySpider):
        name = "shop2"
        shop_urls = [str(shopify_server.make_url("/")).rstrip("/")]
        page_size = 2

    spider = Shop()
    stats = await Engine(spider=spider, settings=spider.settings).run()
    assert stats.items_scraped == 4  # confirms the variant-less product wasn't dropped


async def test_shopify_spider_dedupes_variants_across_overlapping_shops(shopify_server):
    """Two entries in shop_urls pointing at the same store must not double-yield."""
    shop = str(shopify_server.make_url("/")).rstrip("/")

    class Shop(ShopifySpider):
        name = "dup_shop"
        shop_urls = [shop, shop]

    spider = Shop()
    stats = await Engine(spider=spider, settings=spider.settings).run()
    assert stats.items_scraped == 4  # not 8


async def test_shopify_spider_max_pages_caps_pagination(shopify_server):
    class Shop(ShopifySpider):
        name = "capped_shop"
        shop_urls = [str(shopify_server.make_url("/")).rstrip("/")]
        page_size = 2
        max_pages = 1

    spider = Shop()
    stats = await Engine(spider=spider, settings=spider.settings).run()
    assert stats.items_scraped == 3  # only page 1's products (tee x2 + no-variant)


async def test_shopify_spider_handles_non_ok_status():
    class Shop(ShopifySpider):
        name = "broken_shop"
        shop_urls = ["http://127.0.0.1:1/notreal"]

    spider = Shop()
    stats = await Engine(spider=spider, settings=spider.settings).run()
    assert stats.items_scraped == 0
    assert stats.requests_failed >= 1


# ---------------------------------------------------------------------------
# SiteToMarkdownSpider
# ---------------------------------------------------------------------------


@pytest.fixture
async def site_server():
    app = web.Application()

    async def index(request: web.Request) -> web.Response:
        return web.Response(
            text=(
                "<html><head><title>Home</title></head><body>"
                '<nav>nav junk</nav><main><h1>Welcome</h1>'
                '<p>Hello <b>world</b></p><a href="/about">About</a>'
                '<a href="https://other.example/x">External</a></main>'
                "</body></html>"
            ),
            content_type="text/html",
        )

    async def about(request: web.Request) -> web.Response:
        return web.Response(
            text="<html><head><title>About</title></head><body><h1>About us</h1></body></html>",
            content_type="text/html",
        )

    app.router.add_get("/", index)
    app.router.add_get("/about", about)
    server = TestServer(app)
    await server.start_server()
    try:
        yield server
    finally:
        await server.close()


async def test_site_to_markdown_requires_allowed_domains():
    class Bad(SiteToMarkdownSpider):
        name = "bad_md"
        start_urls = ["https://example.com/"]

    with pytest.raises(ValueError, match="allowed_domains"):
        Bad()


async def test_site_to_markdown_converts_every_page_and_stays_onsite(site_server):
    base = str(site_server.make_url("/")).rstrip("/")
    host = site_server.host

    class Docs(SiteToMarkdownSpider):
        name = "docs_md"
        start_urls = [base]
        allowed_domains = [host]

    spider = Docs()
    engine = await build_engine(spider, spider.settings)
    items: list[dict[str, Any]] = []
    orig_process_item = engine._pipeline_manager.process_item

    async def spy(item: Any, sp: Any) -> Any:
        items.append(item)
        return await orig_process_item(item, sp)

    engine._pipeline_manager.process_item = spy
    stats = await engine.run()

    assert stats.items_scraped == 2  # home + about, external link never fetched
    urls = {i["url"] for i in items}
    assert urls == {base + "/", base + "/about"} or urls == {base, base + "/about"}
    home = next(i for i in items if i["title"] == "Home")
    assert "Welcome" in home["markdown"]
    assert "nav junk" not in home["markdown"]  # main_content_only default


async def test_site_to_markdown_max_pages_caps_conversion(site_server):
    base = str(site_server.make_url("/")).rstrip("/")

    class Docs(SiteToMarkdownSpider):
        name = "capped_md"
        start_urls = [base]
        allowed_domains = [site_server.host]
        max_pages = 1

    spider = Docs()
    stats = await Engine(spider=spider, settings=spider.settings).run()
    assert stats.items_scraped == 1


async def test_site_to_markdown_writes_files(tmp_path, site_server):
    base = str(site_server.make_url("/")).rstrip("/")
    out = tmp_path / "md"

    class Docs(SiteToMarkdownSpider):
        name = "writing_md"
        start_urls = [base]
        allowed_domains = [site_server.host]
        output_dir = str(out)

    spider = Docs()
    stats = await Engine(spider=spider, settings=spider.settings).run()
    assert stats.items_scraped == 2
    files = list(out.glob("*.md"))
    assert len(files) == 2
    assert any("Welcome" in f.read_text() for f in files)


async def test_site_to_markdown_css_selector_scopes_conversion(site_server):
    base = str(site_server.make_url("/")).rstrip("/")

    class Docs(SiteToMarkdownSpider):
        name = "scoped_md"
        start_urls = [base]
        allowed_domains = [site_server.host]
        css_selector = "h1"
        max_pages = 1

    spider = Docs()
    items: list[dict[str, Any]] = []
    orig_convert = Docs._bitscrape_convert

    async def spy(self: Docs, response: Any) -> Any:
        async for out in orig_convert(self, response):
            items.append(out)
            yield out

    Docs._bitscrape_convert = spy
    await Engine(spider=spider, settings=spider.settings).run()
    assert "Welcome" in items[0]["markdown"]
    assert "Hello" not in items[0]["markdown"]  # <p> was outside the h1 selector
