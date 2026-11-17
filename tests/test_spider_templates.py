"""
Tests for the four spider templates, driven end-to-end through a real
``Engine`` against a real local aiohttp server -- proving each template
actually enqueues/parses correctly, not just that its class body imports.
"""

from __future__ import annotations

import gzip
import json

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from bitscrape.core.settings import Settings
from bitscrape.engine import Engine
from bitscrape.spiders.templates import (
    CSVFeedSpider,
    ShopifySpider,
    SitemapSpider,
    XMLFeedSpider,
)

SITEMAP_INDEX = b"""<?xml version="1.0" encoding="UTF-8"?>
<sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <sitemap><loc>{base}/sitemap-products.xml</loc></sitemap>
  <sitemap><loc>{base}/sitemap-blog.xml</loc></sitemap>
</sitemapindex>
"""

SITEMAP_PRODUCTS = b"""<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <url><loc>{base}/product/1</loc></url>
  <url><loc>{base}/product/2</loc></url>
</urlset>
"""

SITEMAP_BLOG = b"""<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <url><loc>{base}/blog/hello-world</loc></url>
</urlset>
"""

RSS_FEED = b"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0">
  <channel>
    <title>Example feed</title>
    <item><title>First post</title><link>https://example.com/1</link></item>
    <item><title>Second post</title><link>https://example.com/2</link></item>
  </channel>
</rss>
"""

CSV_FEED = b"sku,price\nABC123,9.99\nXYZ789,19.99\n"


@pytest.mark.asyncio
async def test_sitemap_spider_follows_index_and_routes_by_rule():
    app = web.Application()

    async def index(request: web.Request) -> web.Response:
        base = f"http://{request.host}"
        return web.Response(body=SITEMAP_INDEX.replace(b"{base}", base.encode()), content_type="application/xml")

    async def products(request: web.Request) -> web.Response:
        base = f"http://{request.host}"
        return web.Response(body=SITEMAP_PRODUCTS.replace(b"{base}", base.encode()), content_type="application/xml")

    async def blog(request: web.Request) -> web.Response:
        base = f"http://{request.host}"
        return web.Response(body=SITEMAP_BLOG.replace(b"{base}", base.encode()), content_type="application/xml")

    async def leaf(request: web.Request) -> web.Response:
        return web.Response(text="ok")

    app.router.add_get("/sitemap.xml", index)
    app.router.add_get("/sitemap-products.xml", products)
    app.router.add_get("/sitemap-blog.xml", blog)
    app.router.add_get("/product/{id}", leaf)
    app.router.add_get("/blog/{slug}", leaf)

    server = TestServer(app)
    await server.start_server()
    try:
        base = f"http://{server.host}:{server.port}"
        seen: list[tuple[str, str]] = []  # (url, callback_used)

        class MySitemap(SitemapSpider):
            name = "sitemap-test"
            sitemap_urls = [f"{base}/sitemap.xml"]
            sitemap_rules = [("/product/", "parse_product")]

            async def parse(self, response):
                seen.append((response.url, "parse"))
                return
                yield  # pragma: no cover

            async def parse_product(self, response):
                seen.append((response.url, "parse_product"))
                return
                yield  # pragma: no cover

        engine = Engine(MySitemap(Settings(concurrent_requests=4)))
        await engine.run()

        product_calls = {url for url, cb in seen if cb == "parse_product"}
        blog_calls = {url for url, cb in seen if cb == "parse"}
        assert product_calls == {f"{base}/product/1", f"{base}/product/2"}
        assert blog_calls == {f"{base}/blog/hello-world"}
    finally:
        await server.close()


@pytest.mark.asyncio
async def test_sitemap_spider_handles_gzip():
    app = web.Application()

    async def gz_sitemap(request: web.Request) -> web.Response:
        base = f"http://{request.host}"
        body = SITEMAP_PRODUCTS.replace(b"{base}", base.encode())
        return web.Response(body=gzip.compress(body), content_type="application/gzip")

    async def leaf(request: web.Request) -> web.Response:
        return web.Response(text="ok")

    app.router.add_get("/sitemap.xml.gz", gz_sitemap)
    app.router.add_get("/product/{id}", leaf)
    server = TestServer(app)
    await server.start_server()
    try:
        base = f"http://{server.host}:{server.port}"
        seen: list[str] = []

        class GzSitemap(SitemapSpider):
            name = "sitemap-gz-test"
            sitemap_urls = [f"{base}/sitemap.xml.gz"]

            async def parse(self, response):
                seen.append(response.url)
                return
                yield  # pragma: no cover

        engine = Engine(GzSitemap(Settings()))
        await engine.run()
        assert set(seen) == {f"{base}/product/1", f"{base}/product/2"}
    finally:
        await server.close()


@pytest.mark.asyncio
async def test_xmlfeed_spider_iterates_items():
    app = web.Application()

    async def rss(request: web.Request) -> web.Response:
        return web.Response(body=RSS_FEED, content_type="application/rss+xml")

    app.router.add_get("/rss.xml", rss)
    server = TestServer(app)
    await server.start_server()
    try:
        url = f"http://{server.host}:{server.port}/rss.xml"
        titles: list[str] = []

        class NewsFeed(XMLFeedSpider):
            name = "news-test"
            start_urls = [url]
            itertag = "item"

            async def parse_node(self, response, node):
                titles.append(node.findtext("title"))
                return
                yield  # pragma: no cover

        engine = Engine(NewsFeed(Settings()))
        await engine.run()
        assert titles == ["First post", "Second post"]
    finally:
        await server.close()


@pytest.mark.asyncio
async def test_csvfeed_spider_iterates_rows():
    app = web.Application()

    async def csv_feed(request: web.Request) -> web.Response:
        return web.Response(body=CSV_FEED, content_type="text/csv")

    app.router.add_get("/prices.csv", csv_feed)
    server = TestServer(app)
    await server.start_server()
    try:
        url = f"http://{server.host}:{server.port}/prices.csv"
        rows: list[dict] = []

        class PriceFeed(CSVFeedSpider):
            name = "prices-test"
            start_urls = [url]

            async def parse_row(self, response, row):
                rows.append(row)
                return
                yield  # pragma: no cover

        engine = Engine(PriceFeed(Settings()))
        await engine.run()
        assert rows == [
            {"sku": "ABC123", "price": "9.99"},
            {"sku": "XYZ789", "price": "19.99"},
        ]
    finally:
        await server.close()


@pytest.mark.asyncio
async def test_shopify_spider_paginates_and_expands_variants():
    app = web.Application()
    page1 = {
        "products": [
            {
                "id": 1,
                "title": "T-Shirt",
                "handle": "t-shirt",
                "vendor": "Acme",
                "product_type": "Apparel",
                "tags": "cotton",
                "variants": [
                    {"id": 11, "title": "Small", "sku": "TS-S", "price": "19.99", "available": True},
                    {"id": 12, "title": "Large", "sku": "TS-L", "price": "21.99", "available": False},
                ],
            }
        ]
    }
    page2: dict = {"products": []}

    async def products_json(request: web.Request) -> web.Response:
        page = request.query.get("page", "1")
        body = page1 if page == "1" else page2
        return web.Response(text=json.dumps(body), content_type="application/json")

    app.router.add_get("/products.json", products_json)
    server = TestServer(app)
    await server.start_server()
    try:
        shop = f"http://{server.host}:{server.port}"
        collected: list[dict] = []

        class MyShop(ShopifySpider):
            name = "shop-test"
            shop_urls = [shop]

            async def parse_variant(self, response, product, variant):
                async for out in super().parse_variant(response, product, variant):
                    collected.append(out)
                    yield out

        engine = Engine(MyShop(Settings()))
        stats = await engine.run()

        assert stats.items_scraped == 2  # 2 variants from the one product, page 2 is empty (stop)
        assert len(collected) == 2
        assert {c["sku"] for c in collected} == {"TS-S", "TS-L"}
        by_sku = {c["sku"]: c for c in collected}
        assert by_sku["TS-S"]["price"] == "19.99"
        assert by_sku["TS-S"]["title"] == "T-Shirt"
        assert by_sku["TS-L"]["available"] is False
    finally:
        await server.close()
