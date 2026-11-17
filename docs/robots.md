# Robots.txt compliance

`Settings.robotstxt_obey` (default `True`) makes every request pass through
`RobotsMiddleware`, which enforces `Disallow`/`Allow`, folds `Crawl-delay` and
`Request-rate` into the request's pacing, and caches each domain's rules.

```python
from bitscrape import Settings

settings = Settings(robotstxt_obey=True)  # the default
```

Parsing is done by [Protego](https://github.com/scrapy/protego) (the parser
Scrapy ships), not the standard library's `RobotFileParser`, which does not
implement `Request-rate` and does not support `*`/`$` wildcard patterns in
`Disallow`/`Allow`.

## What's enforced

| Directive | Effect |
|---|---|
| `Disallow` / `Allow` | blocks the request (`DropItem`) if disallowed; supports `*` and end-of-URL `$` wildcards; the most specific matching `User-agent` group wins |
| `Crawl-delay` | folded into `request.meta["crawl_delay"]` (the domain's minimum delay -- see `docs/autothrottle.md`) |
| `Request-rate` | converted to `seconds/requests` and folded into the same `crawl_delay`, but **only while its optional time-of-day window applies** (checked at every request, not just when robots.txt was fetched); the raw directive is also put in `request.meta["request_rate"]` |
| `Sitemap` | collected into `request.meta["sitemaps"]` |

```python
request.meta["crawl_delay"]   # float seconds, or absent if robots.txt sets neither directive
request.meta["request_rate"]  # {"requests": 1, "seconds": 5, "delay": 5.0,
                               #  "start_time": "06:00:00", "end_time": "08:45:00", "in_window": True}
                               # (start_time/end_time are None when the directive has no window)
request.meta["sitemaps"]      # ["https://example.com/sitemap.xml", ...]
```

## Settings

| Setting | Default | Meaning |
|---|---|---|
| `robotstxt_obey` | `True` | enforce robots.txt at all |
| `robots_user_agent` | `None` (uses `user_agent`) | UA used to fetch robots.txt and to pick the matching group |
| `robots_respect_request_rate` | `True` | fold `Request-rate` in (`False`: `Crawl-delay` only) |
| `robots_cache_ttl` | `3600.0` | seconds before a domain's rules are re-checked |
| `robots_negative_cache_ttl` | `30.0` | seconds before retrying a domain whose robots.txt fetch failed |
| `robots_max_cache_domains` | `10000` | LRU cap on cached domains |
| `robots_max_content_bytes` | `512000` (500 KiB) | a larger robots.txt is truncated before parsing |
| `robots_timeout` | `10.0` | timeout for fetching robots.txt |

## Caching

* **Concurrency-safe.** The first several requests to a brand-new domain
  typically arrive together; they share one fetch instead of each fetching
  robots.txt themselves.
* **TTL + stale-while-revalidate.** A domain's rules are re-checked after
  `robots_cache_ttl` seconds. If that refresh fails, **the last known-good
  rules keep being enforced** rather than either opening the domain up or
  flipping it into fail-safe-block.
* **Bounded.** The cache evicts the least-recently-used domain past
  `robots_max_cache_domains`, so crawling many thousands of domains has a
  fixed memory ceiling.
* `mw.stats()` returns `{"domains_cached", "fetches", "fetch_failures", "cache_hits"}`.

## Fail-safe policy

* A 4xx robots.txt response (including 404) is the standard's own "nothing
  published": full access.
* Any other failure to fetch robots.txt for a domain **with no prior
  rules** -- timeout, connection error, 5xx, or a malformed body -- fails
  **safe**: requests to that domain are blocked (`DropItem`) until a
  successful fetch is cached. Unknown rules and no rules are different things.
* That failure is remembered for `robots_negative_cache_ttl` seconds before
  being retried, so a domain that's down is not refetched on every single
  request to it.

## Reusing a cache, or a custom fetcher

```python
from bitscrape import RobotsCache
from bitscrape.middleware.middleware import RobotsMiddleware

cache = RobotsCache(cache_ttl=1800.0, max_domains=50_000)
middleware = RobotsMiddleware(cache)
```

`RobotsCache(fetcher=...)` accepts anything with an
`async def fetch(url, *, user_agent, timeout) -> (status, body)` method, in
case you want robots.txt fetched through a particular session, proxy or pool
instead of the default throwaway `aiohttp` request.

## Scope

Bot-detection evasion is out of scope: this feature makes the crawler follow a
site's stated rules, not disguise that it is automated.
