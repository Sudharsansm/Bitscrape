"""LinkExtractor filtering and CrawlSpider rule-driven crawling."""

from __future__ import annotations

import re

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from bitscrape.core.settings import Settings
from bitscrape.crawl import CrawlSpider, LinkExtractor, Rule
from bitscrape.engine import Engine

BASE = "https://shop.example/catalog/index.html"

PAGE = """<html><head><base href="https://shop.example/catalog/"></head><body>
<nav><a href="/">Home</a><a href="/about">About</a></nav>
<main>
  <a href="item-1.html?utm_source=x&b=2&a=1#reviews">Item 1</a>
  <a href="item-1.html?a=1&b=2">Item 1 again</a>
  <a href="item-2.html" rel="nofollow">Item 2</a>
  <a href="/files/spec.pdf">Spec</a>
  <a href="/img/pic.PNG">Pic</a>
  <a href="mailto:a@b.c">Mail</a>
  <a href="javascript:void(0)">JS</a>
  <a href="#top">Top</a>
  <a href="https://other.example/x">External</a>
  <a href="page-2.html">  Next
      page </a>
  <area href="map-target.html">
</main>
<footer><a href="/terms">Terms</a></footer>
</body></html>"""


def urls(links):
    return [lk.url for lk in links]


class TestLinkExtractor:
    def test_resolves_against_base_href_and_filters_junk_schemes(self):
        got = urls(LinkExtractor().extract_from_html(PAGE, BASE))
        assert "https://shop.example/catalog/page-2.html" in got
        assert "https://shop.example/catalog/map-target.html" in got  # <area>
        assert not any(u.startswith(("mailto", "javascript")) for u in got)
        assert not any(u.endswith("#top") for u in got)

    def test_canonicalisation_dedupes_tracking_params_fragments_and_order(self):
        got = urls(LinkExtractor().extract_from_html(PAGE, BASE))
        item1 = [u for u in got if "item-1" in u]
        assert len(item1) == 1, item1
        assert "utm_source" not in item1[0] and "#" not in item1[0]

    def test_unique_false_keeps_repeats(self):
        got = urls(LinkExtractor(unique=False, canonicalize=False).extract_from_html(PAGE, BASE))
        assert sum("item-1" in u for u in got) == 2

    def test_default_skips_binary_extensions_and_can_be_overridden(self):
        assert not any(
            u.endswith((".pdf", ".PNG"))
            for u in urls(LinkExtractor().extract_from_html(PAGE, BASE))
        )
        loose = urls(LinkExtractor(deny_extensions=()).extract_from_html(PAGE, BASE))
        assert any(u.endswith("spec.pdf") for u in loose)
        assert any(u.lower().endswith("pic.png") for u in loose)

    def test_allow_deny_regex(self):
        le = LinkExtractor(allow=r"item-\d", deny=r"item-2")
        assert [
            u.rsplit("/", 1)[-1].split("?")[0] for u in urls(le.extract_from_html(PAGE, BASE))
        ] == ["item-1.html"]

    def test_domain_filters_match_subdomains(self):
        html = '<a href="https://www.a.com/1">1</a><a href="https://b.org/2">2</a><a href="https://evil-a.com/3">3</a>'
        assert urls(
            LinkExtractor(allow_domains="a.com").extract_from_html(html, "https://a.com/")
        ) == ["https://www.a.com/1"]
        assert "https://b.org/2" not in urls(
            LinkExtractor(deny_domains=["b.org"]).extract_from_html(html, "https://a.com/")
        )

    def test_restrict_css_and_xpath(self):
        assert {
            u.rsplit("/", 1)[-1]
            for u in urls(LinkExtractor(restrict_css="nav").extract_from_html(PAGE, BASE))
        } == {"", "about"}
        got = urls(LinkExtractor(restrict_xpaths="//footer").extract_from_html(PAGE, BASE))
        assert got == ["https://shop.example/terms"]

    def test_text_nofollow_and_honor_nofollow(self):
        links = LinkExtractor().extract_from_html(PAGE, BASE)
        nxt = next(lk for lk in links if lk.url.endswith("page-2.html"))
        assert nxt.text == "Next page"  # whitespace collapsed
        assert next(lk for lk in links if lk.url.endswith("item-2.html")).nofollow
        strict = urls(LinkExtractor(honor_nofollow=True).extract_from_html(PAGE, BASE))
        assert not any(u.endswith("item-2.html") for u in strict)

    def test_process_value_can_unwrap_or_drop(self):
        html = '<a href="javascript:go(\'/real\')">x</a><a href="/keep">y</a><a href="/drop">z</a>'

        def pv(v: str):
            m = re.search(r"go\('(.*?)'\)", v)
            if m:
                return m.group(1)
            return None if v.endswith("drop") else v

        assert urls(LinkExtractor(process_value=pv).extract_from_html(html, "https://a.com/")) == [
            "https://a.com/real",
            "https://a.com/keep",
        ]

    def test_custom_tags_and_attrs(self):
        html = '<iframe src="/frame"></iframe><a href="/a">a</a>'
        got = urls(
            LinkExtractor(tags="iframe", attrs="src").extract_from_html(html, "https://a.com/")
        )
        assert got == ["https://a.com/frame"]

    def test_empty_and_garbage(self):
        assert LinkExtractor().extract_from_html("", BASE) == []
        assert LinkExtractor().extract_from_html("<<<", BASE) == []


# ---------------------------------------------------------------- CrawlSpider end to end


def _site() -> web.Application:
    def html(body: str) -> web.Response:
        return web.Response(text=f"<html><body>{body}</body></html>", content_type="text/html")

    async def home(_):
        return html('<a href="/list/1">L1</a><a href="/about">About</a><a href="/p/1">direct</a>')

    async def listing(r):
        n = int(r.match_info["n"])
        nxt = f'<a href="/list/{n + 1}">next</a>' if n < 3 else ""
        prods = "".join(f'<a href="/p/{n * 10 + i}">p</a>' for i in range(2))
        return html(f"<h1>list {n}</h1>{prods}{nxt}")

    async def product(r):
        return html(f'<h1>product {r.match_info["n"]}</h1><a href="/list/1">back</a>')

    async def about(_):
        return html("<h1>about</h1>")

    app = web.Application()
    app.router.add_get("/", home)
    app.router.add_get("/list/{n}", listing)
    app.router.add_get("/p/{n}", product)
    app.router.add_get("/about", about)
    return app


@pytest.mark.asyncio
async def test_crawlspider_follows_rules_dispatches_callbacks_and_first_rule_wins():
    async with TestServer(_site()) as srv:
        base = f"http://127.0.0.1:{srv.port}"
        seen_kwargs = []

        class Site(CrawlSpider):
            name = "site"
            start_urls = [f"{base}/"]
            rules = (
                Rule(LinkExtractor(allow=r"/list/\d+"), follow=True),  # follow only
                Rule(
                    LinkExtractor(allow=r"/p/\d+"),
                    callback="parse_product",
                    cb_kwargs={"kind": "prod"},
                ),
                Rule(LinkExtractor(allow=r"/about"), callback="parse_about"),
                Rule(LinkExtractor(allow=r"/p/1$"), callback="never_called"),  # shadowed by rule 2
            )

            async def parse_start_url(self, response):
                yield {"kind": "start", "url": response.url}

            async def parse_product(self, response, kind):
                seen_kwargs.append(kind)
                yield {"kind": "product", "title": response.css("h1::text").get()}

            async def parse_about(self, response):
                return {"kind": "about"}  # plain coroutine callback

            async def never_called(self, response):  # pragma: no cover
                raise AssertionError("rule 4 must be shadowed by rule 2")

        settings = Settings(robotstxt_obey=False, concurrent_requests=4)
        spider = Site(settings)
        engine = Engine(spider=spider, settings=settings)
        items = []

        async def on_item(item, spider):
            items.append(dict(item))

        engine._plugins.on("item_scraped", on_item)
        await engine.run()

        kinds = sorted(i["kind"] for i in items)
        titles = sorted(i["title"] for i in items if i["kind"] == "product")
        # products: /p/1 (from home) + list1:10,11 + list2:20,21 + list3:30,31
        assert titles == sorted(f"product {n}" for n in (1, 10, 11, 20, 21, 30, 31))
        assert kinds.count("start") == 1 and kinds.count("about") == 1
        assert set(seen_kwargs) == {"prod"}


@pytest.mark.asyncio
async def test_process_links_process_request_and_allowed_domains():
    async with TestServer(_site()) as srv:
        base = f"http://127.0.0.1:{srv.port}"
        dropped = []

        class Filtered(CrawlSpider):
            name = "filtered"
            start_urls = [f"{base}/"]
            rules = (
                Rule(
                    LinkExtractor(),
                    callback="parse_page",
                    follow=True,  # a rule with a callback does NOT follow unless asked to
                    process_links="only_lists_and_about",
                    process_request="tag_request",
                ),
            )

            def only_lists_and_about(self, links):
                return [lk for lk in links if "/p/" not in lk.url]

            def tag_request(self, request, response):
                if request.url.endswith("/about"):
                    dropped.append(request.url)
                    return None  # drop this one
                request.meta["tagged"] = True
                return request

            async def parse_page(self, response):
                yield {"url": response.url, "tagged": response.meta.get("tagged")}

        settings = Settings(robotstxt_obey=False)
        engine = Engine(spider=Filtered(settings), settings=settings)
        items = []

        async def on_item(item, spider):
            items.append(dict(item))

        engine._plugins.on("item_scraped", on_item)
        await engine.run()
        got = {i["url"].replace(base, "") for i in items}
        assert got == {"/list/1", "/list/2", "/list/3"}
        assert all(i["tagged"] for i in items)
        assert len(dropped) == 1 and dropped[0].endswith("/about")


def test_rule_follow_default_depends_on_callback():
    assert Rule().follow is True
    assert Rule(callback="x").follow is False
    assert Rule(callback="x", follow=True).follow is True


def test_parsed_response_links_helper():
    from bitscrape.core.models import Request, Response
    from bitscrape.parser.selector import ParsedResponse

    resp = Response(url=BASE, status=200, headers={}, body=PAGE.encode(), request=Request(url=BASE))
    page = ParsedResponse(resp)
    assert "https://shop.example/catalog/page-2.html" in page.links()
    assert page.links(restrict_css="footer") == ["https://shop.example/terms"]
