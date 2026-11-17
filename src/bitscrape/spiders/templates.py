"""
Spider templates
=================

Ready-made spiders for a few very common crawl shapes, so you don't
hand-roll the same boilerplate every time:

- :class:`SitemapSpider` -- seed a crawl from ``sitemap.xml`` *or*
  ``robots.txt`` (its ``Sitemap:`` directives are discovered and followed
  automatically), following nested sitemap indexes and gzip-compressed
  sitemaps, and dispatching matched URLs either with substring
  ``sitemap_rules`` or with regex/domain-aware
  :class:`~bitscrape.crawl.rules.Rule`\\ s.
- :class:`XMLFeedSpider` -- iterate over repeated nodes in an XML/RSS/Atom
  feed (e.g. every ``<item>``), one callback call per node, with matching
  namespaces stripped so ``node.findtext("title")`` works the same whether
  the feed uses them or not.
- :class:`CSVFeedSpider` -- iterate over rows of a CSV feed, one callback
  call per row.
- :class:`ShopifySpider` -- pull every product (and its variants) out of
  any storefront that exposes Shopify's public ``/products.json`` API,
  paginating automatically, deduplicated by variant id.
- :class:`SiteToMarkdownSpider` -- crawl a whole site (via
  :class:`~bitscrape.crawl.rules.CrawlSpider`) and turn every page into
  clean, LLM-ready Markdown -- for feeding a :class:`~bitscrape.rag.RagPipeline`
  or any other ingestion pipeline.

All five are plain :class:`~bitscrape.core.spider.Spider` subclasses --
they define ``start_requests``/``parse`` for you, but everything else
(settings, middleware, sessions, the engine) works exactly as it does for
a hand-written spider. XML/CSV feed and sitemap bodies are gzip-decoded
automatically regardless of file extension (magic-byte sniffed), capped
against decompression bombs.
"""

from __future__ import annotations

import csv
import gzip
import io
import re
from copy import deepcopy
from hashlib import sha1
from typing import Any, ClassVar
from urllib.parse import urlsplit

from bitscrape.core.models import Request, Response
from bitscrape.core.spider import Spider, SpiderOutput, iterate_output
from bitscrape.crawl.linkextract import LinkExtractor
from bitscrape.crawl.rules import CrawlSpider, Rule

# ---------------------------------------------------------------------------
# Shared: gzip-bomb-protected decompression
# ---------------------------------------------------------------------------

_GZIP_MAGIC = b"\x1f\x8b"
_GUNZIP_MAX_BYTES = 64 * 1024 * 1024  # 64 MiB cap, defends against gzip bombs


def _decompress(body: bytes, url: str = "", content_type: str = "") -> bytes:
    """Gunzip ``body`` when the URL, content-type, or magic bytes say it's
    gzipped; passes it through unchanged otherwise. Capped at
    ``_GUNZIP_MAX_BYTES`` of *decompressed* output so a malicious or broken
    feed can't be used to exhaust memory (a gzip bomb: a tiny compressed body
    that decompresses to gigabytes). Raises ``OSError`` if that cap is hit.
    """
    looks_gzipped = url.endswith(".gz") or "gzip" in content_type.lower() or body[:2] == _GZIP_MAGIC
    if not looks_gzipped:
        return body
    try:
        out = bytearray()
        with gzip.GzipFile(fileobj=io.BytesIO(body)) as f:
            while chunk := f.read(65536):
                out.extend(chunk)
                if len(out) > _GUNZIP_MAX_BYTES:
                    raise OSError(f"gunzip output exceeds {_GUNZIP_MAX_BYTES} bytes; refusing")
        return bytes(out)
    except OSError:
        if body[:2] != _GZIP_MAGIC:
            return body  # not actually gzipped despite the hint (e.g. a misleading extension)
        raise


# ---------------------------------------------------------------------------
# SitemapSpider
# ---------------------------------------------------------------------------

_SITEMAP_NS = {"sm": "http://www.sitemaps.org/schemas/sitemap/0.9"}
_XHTML_NS = {"x": "http://www.w3.org/1999/xhtml"}


class SitemapSpider(Spider):
    """Crawls a site by following its XML sitemap(s) -- or its robots.txt's
    declared sitemaps -- instead of (or in addition to) hyperlinks.

    Set ``sitemap_urls`` to one or more sitemap, sitemap-index, *or*
    robots.txt URLs (a URL ending in ``robots.txt`` is detected automatically
    and its ``Sitemap:`` directives are followed -- no separate mode needed).
    Nested sitemap indexes are followed recursively; ``sitemap_follow``
    restricts *which* nested sitemaps to descend into, by substring match on
    their URL (empty, the default, follows every nested sitemap).

    Two ways to route matched page URLs to callbacks, tried in this order:

    1. ``rules``: a sequence of :class:`~bitscrape.crawl.rules.Rule` (the same
       type :class:`~bitscrape.crawl.rules.CrawlSpider` uses), matched by
       ``rule.link_extractor.matches(url)`` -- supports regex, domain
       allow/deny, and ``process_request``. First match wins.
    2. ``sitemap_rules``: a simpler ``[(substring, callback_name), ...]`` list,
       tried if ``rules`` is empty or doesn't match.

    A URL matching neither falls back to ``parse``.

    Example (substring rules -- the simple case)::

        class ProductSitemap(SitemapSpider):
            name = "products"
            sitemap_urls = ["https://example.com/robots.txt"]  # or sitemap.xml directly
            sitemap_rules = [("/product/", "parse_product"), ("/blog/", "parse_blog")]

            async def parse_product(self, response):
                yield {"url": response.url, "title": response.css("h1::text").get()}

            async def parse_blog(self, response):
                yield {"url": response.url}

    Example (``Rule``-based -- regex/domain-aware)::

        from bitscrape.crawl import LinkExtractor, Rule

        class ProductSitemap(SitemapSpider):
            name = "products"
            sitemap_urls = ["https://example.com/sitemap.xml"]
            rules = (Rule(LinkExtractor(allow=r"/product/\\d+$"), callback="parse_product"),)

    Set ``sitemap_alternate_links = True`` to also route ``<xhtml:link
    rel="alternate" hreflang="...">`` URLs (translated variants of an entry)
    through the same dispatch, not just each entry's own ``<loc>``.
    """

    sitemap_urls: ClassVar[list[str]] = []
    sitemap_rules: ClassVar[list[tuple[str, str]]] = []
    sitemap_follow: ClassVar[list[str]] = []
    rules: ClassVar[tuple[Rule, ...]] = ()
    sitemap_alternate_links: ClassVar[bool] = False

    def start_requests(self) -> Any:
        return [Request(url=u, callback="_bitscrape_parse_sitemap") for u in self.sitemap_urls]

    async def _bitscrape_parse_sitemap(self, response: Response) -> SpiderOutput:
        if urlsplit(response.url).path.endswith("robots.txt"):
            async for out in self._bitscrape_parse_robots(response):
                yield out
            return

        from lxml import etree  # pyright: ignore[reportAttributeAccessIssue]

        try:
            body = _decompress(
                response.body, response.url, response.headers.get("Content-Type", "")
            )
        except OSError as exc:
            self.logger.warning("Refusing oversized gzip sitemap at %s: %s", response.url, exc)
            return
        try:
            root = etree.fromstring(body)
        except etree.XMLSyntaxError:
            self.logger.warning("Could not parse sitemap XML at %s", response.url)
            return

        tag = etree.QName(root.tag).localname
        if tag == "sitemapindex":
            for loc_el in root.findall(".//sm:sitemap/sm:loc", _SITEMAP_NS) or root.findall(
                ".//sitemap/loc"
            ):
                loc = (loc_el.text or "").strip()
                if not loc:
                    continue
                if self.sitemap_follow and not any(p in loc for p in self.sitemap_follow):
                    continue
                yield Request(url=loc, callback="_bitscrape_parse_sitemap")
            return

        # tag == "urlset" (or unrecognised -- try the same XPath anyway)
        for url_el in root.findall(".//sm:url", _SITEMAP_NS) or root.findall(".//url"):
            loc_el = url_el.find("sm:loc", _SITEMAP_NS)
            if loc_el is None:
                loc_el = url_el.find("loc")
            loc = (loc_el.text or "").strip() if loc_el is not None else ""
            if loc:
                request = self._bitscrape_dispatch(loc)
                if request is not None:
                    yield request
            if self.sitemap_alternate_links:
                for link_el in url_el.findall("x:link", _XHTML_NS):
                    if link_el.get("rel") != "alternate":
                        continue
                    href = (link_el.get("href") or "").strip()
                    if href:
                        request = self._bitscrape_dispatch(href)
                        if request is not None:
                            yield request

    async def _bitscrape_parse_robots(self, response: Response) -> SpiderOutput:
        """A ``sitemap_urls`` entry pointing at robots.txt: pull its
        ``Sitemap:`` directives (via the same Protego parser
        ``RobotsMiddleware`` uses) and follow each one."""
        from protego import Protego

        try:
            text = response.body.decode(response.encoding or "utf-8", errors="replace")
            sitemaps = list(Protego.parse(text).sitemaps)
        except Exception as exc:  # noqa: BLE001 -- a malformed robots.txt must not crash the crawl
            self.logger.warning("Could not parse robots.txt at %s: %s", response.url, exc)
            return
        if not sitemaps:
            self.logger.warning("No Sitemap: directives found in %s", response.url)
            return
        for loc in sitemaps:
            yield Request(url=loc, callback="_bitscrape_parse_sitemap")

    def _bitscrape_dispatch(self, url: str) -> Request | None:
        for rule in self.rules:
            assert rule.link_extractor is not None
            if not rule.link_extractor.matches(url):
                continue
            callback = rule.callback if isinstance(rule.callback, str) else "parse"
            request = Request(url=url, callback=callback, session=rule.session)
            if rule.priority is not None:
                request = request.model_copy(update={"priority": rule.priority})
            process_request = rule.process_request
            pr = (
                process_request
                if callable(process_request)
                else getattr(self, process_request or "", None)
            )
            if pr is not None:
                maybe = pr(request, None)
                if maybe is None:
                    return None
                request = maybe
            return request
        for pattern, callback in self.sitemap_rules:
            if pattern in url:
                return Request(url=url, callback=callback)
        return Request(url=url, callback="parse")


# ---------------------------------------------------------------------------
# XMLFeedSpider
# ---------------------------------------------------------------------------


def _strip_namespaces(node: Any) -> Any:
    """A deep copy of ``node`` with namespaces stripped from every tag and
    attribute, so ``node.findtext("title")`` and plain (non-namespace-aware)
    XPath work on any feed -- RSS (no namespace) or Atom/media-RSS
    (namespaced) alike -- without the caller needing a namespace map."""
    from lxml import etree  # pyright: ignore[reportAttributeAccessIssue]

    node = deepcopy(node)
    for el in node.iter():
        if isinstance(el.tag, str):
            el.tag = etree.QName(el.tag).localname
        for key in list(el.attrib):
            if isinstance(key, str) and key.startswith("{"):
                el.attrib[etree.QName(key).localname] = el.attrib.pop(key)
    etree.cleanup_namespaces(node)
    return node


class XMLFeedSpider(Spider):
    """Iterates over repeated nodes in an XML/RSS/Atom feed, calling
    :meth:`parse_node` once per matched node instead of parsing the whole
    document body yourself. Gzip-compressed feeds are decompressed
    automatically. Each node is passed namespace-stripped, so
    ``node.findtext("title")`` works the same on a plain RSS feed and on a
    namespaced Atom/media-RSS one.

    Override ``itertag`` (the node to iterate) and ``parse_node``. A plain
    name (``"item"``) matches that local name regardless of namespace; a
    prefixed name (``"media:content"``) matches only the namespace that
    prefix maps to in ``namespaces`` -- use this to disambiguate when two
    different namespaces share a local name.

    Example::

        class NewsFeed(XMLFeedSpider):
            name = "news"
            start_urls = ["https://example.com/rss.xml"]
            itertag = "item"

            async def parse_node(self, response, node):
                yield {
                    "title": node.findtext("title"),
                    "link": node.findtext("link"),
                }

    Example (disambiguating by namespace)::

        class MediaFeed(XMLFeedSpider):
            itertag = "media:content"
            namespaces = {"media": "http://search.yahoo.com/mrss/"}
    """

    itertag: ClassVar[str] = "item"
    namespaces: ClassVar[dict[str, str]] = {}

    async def parse(self, response: Response) -> SpiderOutput:  # type: ignore[override]
        from lxml import etree  # pyright: ignore[reportAttributeAccessIssue]

        try:
            body = _decompress(
                response.body, response.url, response.headers.get("Content-Type", "")
            )
        except OSError as exc:
            self.logger.warning("Refusing oversized gzip feed at %s: %s", response.url, exc)
            return
        try:
            root = etree.fromstring(body)
        except etree.XMLSyntaxError:
            self.logger.warning("Could not parse XML feed at %s", response.url)
            return

        uri, name = self._wanted_tag()
        for node in root.iter():
            if not isinstance(node.tag, str):
                continue  # skip comments/PIs, which have non-string tags
            qname = etree.QName(node.tag)
            if qname.localname == name and (uri is None or qname.namespace == uri):
                stripped = _strip_namespaces(node)
                async for out in iterate_output(self.parse_node(response, stripped)):
                    yield out

    def _wanted_tag(self) -> tuple[str | None, str]:
        """Resolve ``itertag`` into a ``(namespace uri or None, localname)``
        pair. A bare name (no ``prefix:``) matches any/no namespace."""
        prefix, sep, name = self.itertag.rpartition(":")
        if not sep:
            return None, name
        uri = self.namespaces.get(prefix)
        if not uri:
            raise ValueError(
                f"itertag={self.itertag!r} uses prefix {prefix!r}, which isn't defined in "
                f"{self.__class__.__name__}.namespaces"
            )
        return uri, name

    async def parse_node(self, response: Response, node: Any) -> SpiderOutput:
        raise NotImplementedError(
            f"{self.__class__.__name__} must implement parse_node(response, node)"
        )
        yield  # pragma: no cover -- makes this an async generator by shape


# ---------------------------------------------------------------------------
# CSVFeedSpider
# ---------------------------------------------------------------------------


class CSVFeedSpider(Spider):
    """Iterates over rows of a CSV feed, calling :meth:`parse_row` once per
    row (as a ``dict`` keyed by header) instead of parsing the CSV yourself.
    Gzip-compressed feeds are decompressed automatically.

    Override ``delimiter`` if the feed isn't comma-separated, ``quotechar``
    if it doesn't use ``"``, and ``headers`` if the feed has no header row
    (supply the column names yourself, in order).

    Example::

        class PriceFeed(CSVFeedSpider):
            name = "prices"
            start_urls = ["https://example.com/prices.csv"]

            async def parse_row(self, response, row):
                yield {"sku": row["sku"], "price": row["price"]}
    """

    delimiter: ClassVar[str] = ","
    quotechar: ClassVar[str] = '"'
    headers: ClassVar[list[str] | None] = None

    async def parse(self, response: Response) -> SpiderOutput:  # type: ignore[override]
        try:
            body = _decompress(
                response.body, response.url, response.headers.get("Content-Type", "")
            )
        except OSError as exc:
            self.logger.warning("Refusing oversized gzip feed at %s: %s", response.url, exc)
            return
        text = body.decode(response.encoding or "utf-8", errors="replace")
        reader = csv.DictReader(
            io.StringIO(text),
            fieldnames=self.headers,
            delimiter=self.delimiter,
            quotechar=self.quotechar,
        )
        for row in reader:
            async for out in iterate_output(self.parse_row(response, dict(row))):
                yield out

    async def parse_row(self, response: Response, row: dict[str, Any]) -> SpiderOutput:
        raise NotImplementedError(
            f"{self.__class__.__name__} must implement parse_row(response, row)"
        )
        yield  # pragma: no cover -- makes this an async generator by shape


# ---------------------------------------------------------------------------
# ShopifySpider
# ---------------------------------------------------------------------------

_TAG_RE = re.compile(r"<[^>]+>")


def _strip_tags(html: str | None) -> str:
    return _TAG_RE.sub("", html or "").strip()


class ShopifySpider(Spider):
    """Pulls every product out of any Shopify storefront via its public,
    unauthenticated ``/products.json`` endpoint -- no HTML scraping needed.
    Paginates automatically until a page comes back empty (or ``max_pages``
    is hit), yielding one item per **variant** (a product with 3 sizes
    yields 3 items, each carrying the shared product fields plus that
    variant's own price/sku/options) since that's usually the unit you
    actually want (e.g. for a price-comparison or availability feed).
    Variants are deduplicated by ``(product_id, variant_id)`` so a store
    whose pagination overlaps (or which appears more than once in
    ``shop_urls``) never yields the same variant twice.

    Override ``shop_urls`` with one or more storefront base URLs (e.g.
    ``"https://example-shop.myshopify.com"`` or a custom domain that has
    the standard Shopify JSON API enabled). Override ``parse_variant`` to
    customise the yielded item shape; the default yields a flat dict.

    Example::

        class MyShop(ShopifySpider):
            name = "myshop"
            shop_urls = ["https://example-shop.myshopify.com"]
    """

    shop_urls: ClassVar[list[str]] = []
    page_size: ClassVar[int] = 250  # Shopify's documented max per page
    max_pages: ClassVar[int] = 0  # 0 = unlimited; a safety cap for a misbehaving store

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._bitscrape_seen_variants: set[tuple[Any, Any]] = set()

    def start_requests(self) -> Any:
        return [
            Request(
                url=f"{shop.rstrip('/')}/products.json?limit={self.page_size}&page=1",
                callback="_bitscrape_parse_products",
                meta={"shop": shop.rstrip("/"), "page": 1},
            )
            for shop in self.shop_urls
        ]

    async def _bitscrape_parse_products(self, response: Response) -> SpiderOutput:
        if not response.ok:
            self.logger.warning(
                "Shopify products.json returned %d for %s", response.status, response.url
            )
            return
        try:
            data = response.json()
        except ValueError:
            self.logger.warning("Non-JSON response from %s", response.url)
            return

        products = data.get("products", [])
        for product in products:
            async for out in iterate_output(self._bitscrape_expand_variants(response, product)):
                yield out

        page = response.meta["page"]
        if products and (self.max_pages == 0 or page < self.max_pages):
            shop = response.meta["shop"]
            next_page = page + 1
            yield Request(
                url=f"{shop}/products.json?limit={self.page_size}&page={next_page}",
                callback="_bitscrape_parse_products",
                meta={"shop": shop, "page": next_page},
            )

    async def _bitscrape_expand_variants(
        self, response: Response, product: dict[str, Any]
    ) -> SpiderOutput:
        variants = product.get("variants", []) or [{}]
        for variant in variants:
            key = (product.get("id"), variant.get("id"))
            if key in self._bitscrape_seen_variants:
                continue
            self._bitscrape_seen_variants.add(key)
            async for out in iterate_output(self.parse_variant(response, product, variant)):
                yield out

    async def parse_variant(
        self, response: Response, product: dict[str, Any], variant: dict[str, Any]
    ) -> SpiderOutput:
        """Override to customise the item shape. Default: a flat dict with
        the product's shared fields plus this variant's own fields."""
        images = product.get("images") or []
        price = float(variant.get("price") or 0)
        compare_at = variant.get("compare_at_price")
        old_price = compare_at if compare_at and float(compare_at) > price else None
        yield {
            "product_id": product.get("id"),
            "title": product.get("title"),
            "vendor": product.get("vendor"),
            "product_type": product.get("product_type"),
            "handle": product.get("handle"),
            "tags": product.get("tags"),
            "variant_id": variant.get("id"),
            "variant_title": variant.get("title"),
            "sku": variant.get("sku"),
            "price": variant.get("price"),
            "old_price": old_price,
            "available": variant.get("available"),
            "barcode": variant.get("barcode") or "",
            "option1": variant.get("option1"),
            "option2": variant.get("option2"),
            "option3": variant.get("option3"),
            "image_url": images[0].get("src") if images else None,
            "description": _strip_tags(product.get("body_html")),
            "source_url": response.meta["shop"] + "/products/" + str(product.get("handle", "")),
        }


# ---------------------------------------------------------------------------
# SiteToMarkdownSpider
# ---------------------------------------------------------------------------


class SiteToMarkdownSpider(CrawlSpider):  # also exported as ``ContentSpider``
    """Crawls a whole site and converts every page to clean, LLM-ready
    Markdown -- built on :class:`~bitscrape.crawl.rules.CrawlSpider`, so
    everything that works there (``rules``, sessions, ``process_request``)
    works here too. Yields one item per page with ``url``, ``title`` and
    ``markdown`` keys; feed the output straight into a
    :class:`~bitscrape.rag.RagPipeline` or write it to disk.

    ``allowed_domains`` is required, so the crawl stays bound to the target
    site(s) (enforced by ``OffsiteMiddleware`` when run through
    :func:`~bitscrape.factory.build_engine`). By default every link on a page
    is followed and converted (via a default ``rules`` entry); override
    ``rules`` with your own :class:`~bitscrape.crawl.rules.Rule`\\ s -- keep
    ``callback="parse_page"`` on any rule whose matched pages should also be
    converted, not just followed through -- to narrow the crawl.

    Args:
        css_selector: convert only the elements matching this CSS selector.
        main_content_only: convert only ``<body>``'s content (drops nav/footer
            boilerplate). Default ``True``.
        output_dir: when set, each page is *also* written to this directory
            as a ``.md`` file named after its URL (collisions get a short
            hash suffix). Writing happens off the event loop thread.
        max_pages: stop converting further pages, and stop following links
            from them, after this many. Requests already in flight when the
            cap is hit may still complete, but aren't converted or followed.
            ``0`` (default) means unlimited.

    Example::

        class Docs(SiteToMarkdownSpider):
            name = "docs-to-md"
            start_urls = ["https://docs.example.com/"]
            allowed_domains = ["docs.example.com"]
            output_dir = "./docs-markdown"
    """

    css_selector: ClassVar[str | None] = None
    main_content_only: ClassVar[bool] = True
    output_dir: ClassVar[str | None] = None
    max_pages: ClassVar[int] = 0
    rules: ClassVar[tuple[Rule, ...]] = ()

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        if not self.allowed_domains:
            raise ValueError(
                f"{self.__class__.__name__} must set `allowed_domains` to bound the crawl "
                "to the target website(s)"
            )
        self._bitscrape_page_count = 0
        self._bitscrape_used_names: set[str] = set()
        if not self.rules:
            # Instance-level default (not a shared mutable ClassVar): follow
            # and convert every link. `callback="parse_page"` is what makes
            # matched (non-start) pages get converted, not just followed.
            # mypy: CrawlSpider.rules is a ClassVar; overriding it per-instance
            # here is deliberate (each spider instance can compute its own
            # default) and matches how `_requests_to_follow` reads `self.rules`.
            self.rules = (Rule(LinkExtractor(), callback="parse_page", follow=True),)  # type: ignore[misc]

    async def parse_start_url(self, response: Any) -> SpiderOutput:  # type: ignore[override]
        async for out in self._bitscrape_convert(response):
            yield out

    async def parse_page(self, response: Any) -> SpiderOutput:
        async for out in self._bitscrape_convert(response):
            yield out

    async def _rule_callback(self, response: Any, rule_index: int | None = None) -> SpiderOutput:
        """As ``CrawlSpider._rule_callback``, except link-following also stops
        once ``max_pages`` is reached (the base class always follows a
        ``follow=True`` rule regardless of what its callback does)."""
        idx = rule_index if rule_index is not None else response.meta.get("_rule")
        if idx is None or not (0 <= int(idx) < len(self.rules)):
            return
        rule = self.rules[int(idx)]
        cb = self._resolve(rule.callback)
        at_cap = bool(self.max_pages) and self._bitscrape_page_count >= self.max_pages
        async for out in self._parse_response(
            response, cb, dict(rule.cb_kwargs), follow=bool(rule.follow) and not at_cap
        ):
            yield out

    async def _bitscrape_convert(self, response: Any) -> SpiderOutput:
        if self.max_pages and self._bitscrape_page_count >= self.max_pages:
            return
        self._bitscrape_page_count += 1
        markdown = (
            self._bitscrape_markdown_for_selector(response)
            if self.css_selector
            else response.markdown(main_content=self.main_content_only)
        )
        item = {
            "url": response.url,
            "title": (response.css("title::text").get() or "").strip(),
            "markdown": markdown,
        }
        if self.output_dir:
            await self._bitscrape_write(item)
        yield item

    def _bitscrape_markdown_for_selector(self, response: Any) -> str:
        from bitscrape.markdown import html_to_markdown

        assert self.css_selector is not None
        scoped_html = "".join(el.get() or "" for el in response.css(self.css_selector))
        return html_to_markdown(
            scoped_html or response.text, base_url=response.url, main_content=False
        )

    async def _bitscrape_write(self, item: dict[str, Any]) -> None:
        import asyncio
        from pathlib import Path

        assert self.output_dir is not None
        name = self._bitscrape_filename_for(item["url"])
        path = Path(self.output_dir) / f"{name}.md"

        def _write() -> None:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(item["markdown"], encoding="utf-8")

        await asyncio.to_thread(_write)

    def _bitscrape_filename_for(self, url: str) -> str:
        """Build a unique filesystem-safe name from the URL, suffixing a hash
        on collisions (e.g. two pages that only differ by query string)."""
        parsed = urlsplit(url)
        raw = f"{parsed.netloc}{parsed.path}".strip("/")
        name = re.sub(r"[^A-Za-z0-9._-]+", "-", raw).strip("-")[:150] or "index"
        if name in self._bitscrape_used_names:
            name = f"{name}-{sha1(url.encode(), usedforsecurity=False).hexdigest()[:8]}"
        self._bitscrape_used_names.add(name)
        return name


#: Scrapling-style name for :class:`SiteToMarkdownSpider`.
ContentSpider = SiteToMarkdownSpider
