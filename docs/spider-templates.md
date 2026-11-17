# Spider templates

Ready-made `Spider` subclasses for common crawl shapes, so you're not
hand-rolling the same boilerplate every time. All are plain
`bitscrape.core.spider.Spider` subclasses (or subclass one) — settings,
middleware, sessions, and the engine all work exactly as they do for a
hand-written spider.

```python
from bitscrape.spiders import (
    CrawlSpider, Rule, LinkExtractor,   # rule-based link following
    SitemapSpider,                       # sitemap / robots.txt-driven
    XMLFeedSpider, CSVFeedSpider,        # feed iteration
    ShopifySpider,                       # Shopify JSON API
    SiteToMarkdownSpider,                # whole-site → Markdown
)
```

## CrawlSpider + Rule + LinkExtractor

Follows links according to declarative `Rule`s — the usual way to crawl a
whole site:

```python
class Books(CrawlSpider):
    name = "books"
    start_urls = ["https://books.toscrape.com/"]
    allowed_domains = ["books.toscrape.com"]
    rules = (
        Rule(LinkExtractor(allow=r"/catalogue/page-\d+"), follow=True),
        Rule(LinkExtractor(allow=r"/catalogue/[^/]+_\d+/index\.html"), callback="parse_book"),
    )

    async def parse_book(self, response):
        yield {"title": response.css("h1::text").get()}
```

Every response is scanned with **all** rules; a URL is claimed by the first
rule whose `LinkExtractor` yields it. `follow` defaults to `True` when there's
no `callback`, `False` when there is one. The start pages go through
`parse_start_url` and always have their links followed.

`Rule` fields: `link_extractor`, `callback`, `follow`, `process_links`,
`process_request`, `cb_kwargs`, `session`, `priority` (a `RequestPriority` or
plain int — lower is higher priority; applied to every request the rule
builds, both followed links and, since this upgrade, `SitemapSpider`'s
`Rule`-based dispatch too).

`LinkExtractor(allow=, deny=, allow_domains=, deny_domains=, deny_extensions=,
restrict_css=, restrict_xpaths=, tags=, attrs=, canonicalize=True, unique=True,
honor_nofollow=False, process_value=, strip_query_params=)` extracts links from
a page (`.extract_links(response)`) — and, since this upgrade, can also test a
bare URL string against the same rules with `.matches(url)`, which is what
lets `SitemapSpider` reuse `Rule`/`LinkExtractor` for URLs that never came from
an `<a href>`.

### Staying on-site

`allowed_domains = ["example.com"]` makes `OffsiteMiddleware` (always
installed, a no-op while empty) drop any request to a different host — a bare
host also allows its subdomains. This applies to every spider, not just
`CrawlSpider`; see [crawling/](crawling/index.md#staying-on-site-alloweddomains).

## SitemapSpider

Crawls a site by following its XML sitemap(s) — **or its robots.txt's declared
sitemaps**, discovered and followed automatically:

```python
class Products(SitemapSpider):
    name = "products"
    sitemap_urls = ["https://example.com/robots.txt"]  # or a sitemap.xml URL directly
    sitemap_rules = [("/product/", "parse_product"), ("/blog/", "parse_blog")]

    async def parse_product(self, response):
        yield {"url": response.url, "title": response.css("h1::text").get()}

    async def parse_blog(self, response):
        yield {"url": response.url}
```

A `sitemap_urls` entry ending in `robots.txt` is detected automatically — its
`Sitemap:` directives (parsed the same way `RobotsMiddleware` does) are
followed, no separate mode needed. Nested sitemap indexes are followed
recursively; `sitemap_follow` (substring list) restricts which nested
sitemaps to descend into. Gzip-compressed sitemaps (`.xml.gz`, or detected by
content-type/magic-bytes regardless of extension) are decompressed
automatically, capped against decompression bombs.

Two ways to route a matched page URL to a callback, tried in this order:

1. **`rules`** — a sequence of the same `Rule` type `CrawlSpider` uses,
   matched with `rule.link_extractor.matches(url)`: supports regex,
   domain allow/deny, and `process_request`.
2. **`sitemap_rules`** — a simpler `[(substring, callback_name), ...]` list.

A URL matching neither falls back to `parse`. Set
`sitemap_alternate_links = True` to also route `<xhtml:link rel="alternate"
hreflang="...">` URLs (translated page variants) through the same dispatch.

## XMLFeedSpider

Iterates over repeated nodes in an XML/RSS/Atom feed, calling `parse_node`
once per matched node:

```python
class NewsFeed(XMLFeedSpider):
    name = "news"
    start_urls = ["https://example.com/rss.xml"]
    itertag = "item"

    async def parse_node(self, response, node):
        yield {"title": node.findtext("title"), "link": node.findtext("link")}
```

Each node is passed with namespaces **stripped**, so `node.findtext("title")`
works the same on a plain RSS feed and a namespaced Atom/media-RSS one — a
bare `itertag` (`"item"`) matches that local name regardless of namespace. A
prefixed `itertag` (`"media:content"`) matches only the namespace mapped in
`namespaces`, for disambiguating when two different namespaces share a local
name:

```python
class MediaFeed(XMLFeedSpider):
    itertag = "media:content"
    namespaces = {"media": "http://search.yahoo.com/mrss/"}
```

Gzip-compressed feeds are decompressed automatically (same protection as
`SitemapSpider`).

## CSVFeedSpider

Iterates over rows of a CSV feed, calling `parse_row` once per row (a `dict`
keyed by header):

```python
class PriceFeed(CSVFeedSpider):
    name = "prices"
    start_urls = ["https://example.com/prices.csv"]

    async def parse_row(self, response, row):
        yield {"sku": row["sku"], "price": row["price"]}
```

Override `delimiter` (default `,`), `quotechar` (default `"`), or `headers`
(supply column names yourself for a feed with no header row). Gzip-compressed
feeds are decompressed automatically.

## ShopifySpider

Pulls every product out of any Shopify storefront via its public,
unauthenticated `/products.json` endpoint — no HTML scraping needed:

```python
class MyShop(ShopifySpider):
    name = "myshop"
    shop_urls = ["https://example-shop.myshopify.com"]
```

Paginates automatically until a page comes back empty (or `max_pages` is
hit — a safety cap for a misbehaving store; `0`, the default, means
unlimited). Yields **one item per variant** (a product with 3 sizes yields 3
items, each carrying the shared product fields plus that variant's own
price/sku/options), deduplicated by `(product_id, variant_id)` so overlapping
pagination or a repeated entry in `shop_urls` never yields the same variant
twice.

Default item fields: `product_id`, `title`, `vendor`, `product_type`,
`handle`, `tags`, `variant_id`, `variant_title`, `sku`, `price`, `old_price`
(the variant's `compare_at_price` when it's actually higher than `price`,
else `None`), `available`, `barcode`, `option1`/`option2`/`option3`,
`image_url` (the product's first image), `description` (`body_html` with HTML
tags stripped), `source_url`. Override `parse_variant(response, product,
variant)` to customise the shape.

## SiteToMarkdownSpider

Crawls a whole site (built on `CrawlSpider`) and converts every page to
clean, LLM-ready Markdown — feed the output into a `RagPipeline` (see
[rag/](rag/index.md)) or write it to disk:

```python
class Docs(SiteToMarkdownSpider):
    name = "docs-to-md"
    start_urls = ["https://docs.example.com/"]
    allowed_domains = ["docs.example.com"]  # required
    output_dir = "./docs-markdown"          # optional: also write .md files
```

Yields one item per page: `{"url", "title", "markdown"}`. `allowed_domains` is
required (`OffsiteMiddleware` keeps the crawl bound to it). By default every
link is followed and converted; override `rules` with your own `Rule`s —
keep `callback="parse_page"` on any rule whose matched pages should also be
converted, not just followed through, to narrow the crawl.

Options: `css_selector` (convert only matching elements), `main_content_only`
(default `True` — drops nav/footer boilerplate), `output_dir` (writes each
page as a `.md` file named after its URL, off the event loop thread;
collisions get a short hash suffix), `max_pages` (stop converting *and*
following links past this many pages — requests already in flight may still
complete but aren't converted or followed; `0` means unlimited).

## See also

- [crawling/](crawling/index.md) — robots.txt, retries, conditional GET,
  `allowed_domains`.
- [autothrottle.md](autothrottle.md) / [sessions.md](sessions.md) — pacing and
  multi-session routing, which all of these templates use like any other spider.
- [rag/](rag/index.md) — turning crawled pages (e.g. from
  `SiteToMarkdownSpider`) into a retrieval index.
