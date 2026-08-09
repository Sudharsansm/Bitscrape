# Crawling

Core concepts around fetching pages: the request/response lifecycle,
retries, redirects, robots.txt/meta-robots compliance, and URL
canonicalization.

## Request and Response

`bitscrape.core.models.Request` and `.Response` are Pydantic models passed
through the whole pipeline (middleware → downloader → middleware → your
spider callback). Key `Request` fields you'll actually use:

- `url`, `method`, `headers`, `body` — the obvious ones.
- `meta: dict` — a free-form bag for passing data between requests/hooks
  (e.g. `{"scroll": True}` for Playwright, `{"proxy": "..."}` set by
  `ProxyMiddleware`, `{"session_id": 0}` set by `SessionPoolMiddleware`,
  `{"crawl_delay": 2.0}` set by `RobotsMiddleware`).
- `callback` — the spider method name to route the response to (defaults
  to `"parse"`).
- `priority` — a `RequestPriority` enum value; used by `MemoryQueue`/
  `RedisQueue` ordering.
- `max_retries` — per-request retry cap (default 3).
- `use_playwright` — route through the Playwright path instead of `aiohttp`.

`Response.css(selector)` / `.xpath(selector)` are how you extract data —
see [parser/](../parser/index.md). `response.ok` is `True` for 2xx status
codes; `response.text` decodes `response.body` using `response.encoding`.

## Retries

Controlled by `Settings.retry_http_codes` (default `[500, 502, 503, 504,
429]`) and `Request.max_retries` (default 3, settable per-request).

On a retry-eligible status or a network error (`TimeoutError`,
`aiohttp.ClientError`), the downloader waits and retries:

- If the response included a `Retry-After` header (seconds or an HTTP-date)
  and `Settings.respect_retry_after` is `True` (default), that value is
  honored — capped at `Settings.max_retry_after_seconds` (default 120s) so
  a misbehaving server can't stall a crawl indefinitely.
- Otherwise, capped exponential backoff: `min(2**attempt, 30)` seconds.

## Conditional GET (bandwidth efficiency)

If `Settings.conditional_get_enabled` (default `True`), the downloader
caches `ETag`/`Last-Modified` from `200` responses per URL, and sends
`If-None-Match`/`If-Modified-Since` on the next request to that same URL. A
`304 Not Modified` is served transparently from cache (the cached body is
returned, with `response.headers["X-Bitscrape-Not-Modified"] = "true"` so
your code can detect and skip re-processing if you want) — the origin
server only had to send headers, not the full page again.

## robots.txt

`RobotsMiddleware` (included whenever `Settings.robotstxt_obey=True`, the
default) fetches and caches `/robots.txt` per domain and:

- Blocks disallowed paths via the standard library's `RobotFileParser`.
- Surfaces `Crawl-delay` into `request.meta["crawl_delay"]` and discovered
  `Sitemap:` URLs into `request.meta["sitemaps"]`.
- **Fails safe, not open**: a 4xx (including 404) means "no robots.txt
  published" → unrestricted access (standard convention). Any other
  failure (timeout, DNS error, 5xx) blocks requests to that domain until a
  fetch succeeds — the real rules are unknown, not confirmed absent.

## Meta-robots (`noindex` / `nofollow`)

`MetaRobotsMiddleware` (included whenever
`Settings.respect_meta_robots=True`, the default) reads the `X-Robots-Tag`
HTTP header and `<meta name="robots" content="...">` (plus a UA-scoped
variant like `<meta name="googlebot" content="noindex">`), and sets
`request.meta["noindex"]`/`["nofollow"]`. The `Engine` then:

- Skips counting/exporting items from a `noindex` page (tracked in
  `stats.items_noindexed`, not `stats.items_scraped`).
- Skips enqueueing any links discovered on a `nofollow` page (tracked in
  `stats.links_nofollow_skipped`).
- Still fetches the page normally either way — only indexing/following is
  affected, matching real-world semantics (and matching what Googlebot/
  ClaudeBot/PerplexityBot-class crawlers do beyond robots.txt).

This is why a page correctly excluded by `noindex` does **not** trigger the
Engine's "0 items, check your selectors" diagnostic (see
[troubleshooting/](../troubleshooting/index.md)) — that's an intentional
skip, not a broken selector.

## Canonicalization and duplicate detection

See [extractors/](../extractors/index.md) for full detail on
`canonicalize_url()`, `resolve_redirect_chain()`, and SimHash-based
near-duplicate content detection (`compute_fingerprint()`/
`is_near_duplicate()`).

## A note on relative URLs

`self.follow()` does **not** resolve relative URLs against the current
page. If a link is `<a href="/next">`, resolve it yourself:

```python
from urllib.parse import urljoin
yield self.follow(urljoin(response.url, href))
```

See [user-guide/index.md#relative-urls-fixed-in-080](../user-guide/index.md#relative-urls-fixed-in-080)
for more.

## Politeness and rate limiting

- `Settings.download_delay` — a fixed minimum delay between requests to the
  same domain (single process).
- `Settings.autothrottle_enabled` — adaptive, latency-based per-domain
  delay instead of a fixed one (see [architecture/](../architecture/index.md)).
- `Settings.distributed_throttle_enabled` — the same guarantee, but
  enforced across a fleet of worker **processes** via Redis (see
  [scheduler/](../scheduler/index.md)).
- `bitscrape.frontier.Frontier` — an alternative, structurally-enforced
  single-process politeness mechanism (Mercator-style priority tiers +
  per-domain back-queues) — see [scheduler/](../scheduler/index.md).

## See also

- [scheduler/](../scheduler/index.md) — queues, dedup, and the Frontier.
- [user-guide/index.md#middleware](../user-guide/index.md#middleware) — the full middleware list.
- [troubleshooting/](../troubleshooting/index.md) — diagnosing crawl failures.
