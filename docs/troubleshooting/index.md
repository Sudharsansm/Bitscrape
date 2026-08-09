# Troubleshooting

## "0 items scraped" (requests succeeded, no errors, but nothing scraped)

By far the most common thing that looks broken but isn't. If a crawl
finishes with something like `requests=1 failed=0 items=0`, check the
logs first -- as of a real fix in this project's history, the Engine now
logs an explicit warning identifying the URL, callback name, and response
size whenever a callback yields nothing at all from a non-empty response.
In order of likelihood:

1. **CSS/XPath selectors don't match the page.** The single most common
   cause, in any scraping framework. Check the real markup with
   `response.text` (or save `response.body` to a file and open it) and
   adjust your selectors. The bundled `examples/*.py` spiders use
   illustrative selectors (`div.listing-card`, etc.) meant to be adapted to
   your target site's actual HTML -- not working spiders for an arbitrary
   URL as-is.
2. **The content is JS-rendered.** If the data isn't in the raw HTML
   (view-source differs from what your browser shows), set
   `use_playwright=True` -- see [browser/](../browser/index.md). Add
   `meta={"scroll": True}` if it's also lazy-loaded on scroll.
3. **A redirect or robots.txt block served an unexpected page.** Check
   `response.url` and `response.status` in your callback against what you
   intended to fetch.
4. **Wrong callback name.** If `request.callback` doesn't match an actual
   method on your spider, the Engine logs `"Spider has no callback ..."` --
   distinct from the zero-yield warning above.
5. **The page was intentionally excluded, not broken.** If
   `stats.items_noindexed > 0` or `stats.links_nofollow_skipped > 0`, that
   page had a `<meta name="robots" content="noindex">`/`nofollow`
   directive (or the equivalent `X-Robots-Tag` header) and was correctly
   skipped -- see [crawling/](../crawling/index.md#meta-robots-noindex--nofollow).
   This does **not** trigger the zero-yield diagnostic, since it's an
   intentional skip.

## A crawl that should follow links stops after one page

As of 0.8.0, `self.follow()` resolves relative URLs against the current
page automatically — if you're seeing this on 0.8.0+, the more likely
causes are: the link selector itself doesn't match (check
`response.css("a::attr(href)").getall()` returns what you expect), or the
followed page is being excluded by `noindex`/`nofollow` (check
`stats.links_nofollow_skipped`).

**If you're on a version older than 0.8.0**: this was a real, known bug —
`self.follow()` didn't resolve relative URLs at all, so `<a href="/next">`
would request the literal string `/next` instead of the resolved absolute
URL. Upgrade, or work around it manually in the meantime:
```python
from urllib.parse import urljoin
yield self.follow(urljoin(response.url, href))
```
See [user-guide/index.md#relative-urls-fixed-in-080](../user-guide/index.md#relative-urls-fixed-in-080).

## `ERROR: ... does not appear to be a Python project`

`pip install -e .` needs to run from the exact directory containing
`pyproject.toml`. See [installation/](../installation/index.md#troubleshooting-installation).

## `use_playwright=True` raises `ImportError`

The `playwright` Python package isn't installed:
`pip install -e ".[playwright]"`. If the package is installed but you get
a different (launch) error, you likely skipped downloading the browser
binary: `playwright install chromium`. See [browser/](../browser/index.md).

## `DistributedThrottleMiddleware` / Redis features don't seem to be doing anything

Check that:
1. Redis is actually running and reachable at `Settings.redis_url`.
2. `distributed_throttle_enabled=True` is set.
3. A Redis client was actually available when `build_engine()` ran --
   if `distributed_throttle_enabled=True` but no client could be obtained
   (and `scheduler_use_redis` wasn't also set to trigger auto-creation), a
   warning is logged and the middleware is silently skipped rather than
   failing the whole crawl. Check your logs for
   `"skipping DistributedThrottleMiddleware"`.

## `PostgresStorageBackend` / `PostgresPipeline` behave unexpectedly

This backend is implemented against the real `asyncpg` API but was only
unit-tested with a mock connection in this project's build environment -- a
live PostgreSQL server wasn't available to verify it against for real. Run
your own integration test against your actual PostgreSQL instance before
relying on it. See [storage/](../storage/index.md).

## `MongoStorageBackend` / `ElasticsearchStorageBackend` raise `NotImplementedError`

This is intentional -- these are documented stubs, not broken
implementations. See their docstrings (or [storage/](../storage/index.md))
for the intended implementation shape if you want to build them out
yourself.

## `ruff check` reports errors on files I didn't touch

Check your installed `ruff` version against the one pinned in
`pyproject.toml`'s `dev` extra (`ruff==0.15.22` as of 0.7.0). An unpinned
or differently-pinned `ruff` can resolve to a version with different
default rules, which can flag pre-existing code the originally-verified
version never complained about. Reinstall with the exact pinned extras:
`pip install -e ".[all,dev,cli]"`.

## Tests hang when testing something that calls `bitscrape.run()`

If you're writing your own tests around `bitscrape.run()` (which manages
its own internal `asyncio.run()` event loop) and using
`aiohttp.test_utils.TestServer` for a local test server, the server can
silently stop processing requests once the loop that started it closes --
`TestServer` is bound to that specific event loop. Use a plain
`threading.Thread` + `http.server.HTTPServer` instead, which has no such
coupling (see `tests/test_package_api.py`'s `_ThreadedTestServer` for the
pattern this project's own test suite uses).

## mypy reports errors on bare `Settings()` calls

This is a known, accepted false positive from a `pydantic-settings`/`mypy`
plugin interaction, not a real bug -- confirmed at runtime by every single
test in this project's suite that constructs a `Settings()`. Not worth
"fixing" without configuring the pydantic mypy plugin project-wide (a
bigger, separate change).

## Still stuck?

- [faq/](../faq/index.md) -- shorter, more specific Q&A.
- [developer-guide/](../developer-guide/index.md) -- running the test suite yourself often narrows down whether something is your setup or an actual bug.
