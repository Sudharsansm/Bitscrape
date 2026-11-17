---
name: bitscrape
description: Use when writing, running, or debugging web-scraping code with the bitscrape Python framework (spiders, Request/Response, CLI, MCP server, RAG pipeline). Covers current APIs so generated code matches the installed version instead of guessing from stale training data.
---

# bitscrape

bitscrape is an async Python web-scraping framework (Spider API, Playwright
browser automation, adaptive selectors, export pipelines, MCP server, RAG
pipeline). This skill keeps generated code matching the *current* API
surface — several method names and settings changed across versions, and
guessing from general scraping-library knowledge produces code that
imports but fails at runtime.

## Hard rule: no anti-bot bypass

Never write code that sets `settings.impersonate`, `settings.browser_stealth`,
or `settings.dns_leak_protection`, or that uses a session `kind="stealth"`/
`kind="impersonate"`. These are declared in `Settings` for forward API
compatibility but bitscrape deliberately raises `NotImplementedError` if
you actually try to use them — this project does not implement
Cloudflare/anti-bot bypass or TLS/browser fingerprint impersonation, full
stop. If asked for "stealth scraping" or "bypass Cloudflare," say plainly
that bitscrape doesn't do that and suggest normal fetching
(`use_playwright=True` for JS-rendered pages) instead — don't try to work
around the restriction with a different setting name.

## Minimal spider

```python
import bitscrape

class QuotesSpider(bitscrape.Spider):
    name = "quotes"
    start_urls = ["https://quotes.toscrape.com"]

    async def parse(self, response):
        for quote in response.css("div.quote"):
            yield {
                "text": quote.css("span.text::text").get(),
                "author": quote.css("small.author::text").get(),
            }
        next_page = response.css("li.next a::attr(href)").get()
        if next_page:
            yield self.follow(next_page, callback="parse")  # resolves relative URLs

bitscrape.run(QuotesSpider, output="quotes.jsonl")   # sync, blocking, one call
```

`response` in a callback is a `ParsedResponse` (adaptive CSS/XPath
selectors, `.find_similar()`, `.json()`, `.ok`, `.urljoin()`) — not the
raw `Response`. `self.follow(url, callback=...)` resolves relative URLs
against the current response; don't hand-roll `urljoin` unless you need
something `follow` doesn't do.

## One-off fetch (no spider needed)

```python
import bitscrape
response = bitscrape.fetch_url("https://example.com")   # sync
```
or from the CLI: `bitscrape get https://example.com --as markdown` (also
`--as html|text|structured`, `--css SELECTOR`). Inside an already-running
event loop, use `await Downloader(settings).fetch(Request(url=url))`
directly — `fetch_url`/`run` are sync wrappers and will raise if nested
inside a loop.

## Spider templates — use these instead of hand-rolling

```python
from bitscrape.spiders import SitemapSpider, XMLFeedSpider, CSVFeedSpider, ShopifySpider
```
- `SitemapSpider`: set `sitemap_urls`, optional `sitemap_rules = [("/product/", "parse_product")]`.
- `XMLFeedSpider`/`CSVFeedSpider`: override `parse_node`/`parse_row`.
- `ShopifySpider`: set `shop_urls`; paginates `/products.json` automatically, one item per variant.

`bitscrape genspider <name> <domain> --template sitemap|xmlfeed|csvfeed|shopify|crawl|basic`
scaffolds any of these.

## Settings that actually do something (as of this version)

```python
from bitscrape.core.settings import Settings
settings = Settings(
    concurrent_requests=16,
    download_delay=0.5,
    autothrottle_enabled=True,
    dev_mode=True,                      # cache responses to disk, replay on next run
    blocked_detection_enabled=True,     # detect+retry "blocked" responses, not just HTTP errors
    proxies=["http://p1:8080"],         # or proxy_file="proxies.txt"; auto-rotated
    block_ads=True,                     # browser fetches only; bundled ~3,900-domain list
    dns_over_https=True,                # HTTP stack resolves names via DoH (doh_url="cloudflare"|"google"|"quad9")
    browser_record_requests=True,       # searchable history: session.network.search(url_pattern=..., limit=None)
    browser_cdp_url="ws://...",         # connect to a remote/managed browser instead of launching one
    browser_capture_xhr=True,           # collect background XHR/fetch into response.extra["captured_xhr"]
    sessions={"api": {"kind": "http"}, "render": {"kind": "browser"}},
)
```
`Request(url=..., session="render")` routes that one request through a
named session. `request.meta["screenshot"] = True` on a
`use_playwright=True` request captures a PNG into `response.extra["screenshot"]`.

## RAG / search

```python
from bitscrape.rag import RagPipeline
pipeline = RagPipeline()                 # BM25-only; pass embed_fn=... for hybrid BM25+vector
pipeline.ingest_response(response)
results = pipeline.search("pricing")
context = pipeline.to_context(results)   # ready to paste into an LLM prompt
```
There's no bundled embedding model — `embed_fn` is a callable you supply.

## MCP server

`bitscrape mcp` (needs `pip install bitscrape[mcp]`) exposes `fetch`, `extract`,
`list_links`, `bulk_fetch`, `extract_structured`, `search`, `screenshot`, persistent
browser `session_*` tools (with saved logins) and `network_search` to an AI agent.
Public URLs only unless started with `--allow-private-networks`.
It has no fetch mode that bypasses anti-bot protection (see the hard rule
above) — don't tell a user otherwise.

## Browser sessions: network history, saved login, shadow DOM

```python
async with AsyncDynamicSession(record_requests=True, block_ads=True) as s:
    await s.fetch(url, network_idle=True, pierce_shadow=True)
    api = s.network.search(url_pattern=r"/api/", include_static=False, limit=None)  # .json(), .text, .status
    await s.save_state("login.json")        # later: await s.load_state("login.json")
```
Text helpers: `el.clean_text()`, `el.price()` → `(Decimal, "USD")`, `page.emails()`;
`bitscrape.jsonx` for fast JSON; `ContentSpider` crawls a site into Markdown.

## Existing Scrapy project?

`from bitscrape.integrations.scrapy import bitscrape_response` — decorate
an existing Scrapy callback to get a bitscrape `ParsedResponse` (as a `bs`
kwarg) over the same response bytes, without rewriting the spider or
adding `scrapy` as a bitscrape dependency.

## When unsure

Check `pyproject.toml`'s `version` and `CHANGELOG.md`'s top entry for what
changed most recently — this API has moved fast. If a method you expect
doesn't exist, grep the installed package rather than assuming it matches
an older version or a different scraping library's API.
