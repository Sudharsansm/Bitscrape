# Browser Rendering (Playwright)

## When you need this

Bitscrape's default `aiohttp` downloader only fetches the static, initial
HTML — it never executes JavaScript. If a page's content is populated
client-side (React/Vue/Angular apps, most modern SPAs, infinite-scroll
feeds), `curl`/view-source will show different content than what your
browser renders. That's the signal you need the Playwright path.

## Setup

```bash
pip install -e ".[playwright]"
playwright install chromium     # separate step -- downloads the actual browser binary
```

The Python package alone (`pip install playwright`) is not enough — the
browser binaries are a separate download. `use_playwright=True` will raise
`ImportError` at request time if the `playwright` package isn't installed,
and will fail differently (a launch error) if the package is installed but
`playwright install chromium` was never run.

## Basic usage

```python
async def parse(self, response):
    yield {"value": response.css(".metric::text").get()}

def start_requests(self):
    for url in self.start_urls:
        yield self.follow(url, use_playwright=True)
```

Or on a follow-up request from within a callback:
```python
yield self.follow(next_url, use_playwright=True)
```

Under the hood, `Downloader._fetch_playwright()` launches a browser (or
acquires one from the pool, see below), creates a context, navigates with
`wait_until="networkidle"`, optionally scrolls (see below), and returns the
fully-rendered HTML as the response body — everything downstream (your
`parse()` method, CSS/XPath selection) works exactly the same as with the
`aiohttp` path.

## Infinite scroll / lazy-loaded content

`scroll_to_bottom()` is a reusable scroll-and-wait loop for pages that load
more content as you scroll:

```python
yield self.follow(url, use_playwright=True, meta={"scroll": True})
```

`meta={"scroll": True}` uses the defaults (`max_scrolls=20, pause_ms=300,
stable_rounds=2`). Override any of them:

```python
yield self.follow(url, use_playwright=True, meta={
    "scroll": {"max_scrolls": 30, "pause_ms": 500, "click_selector": "button.load-more"}
})
```

- `max_scrolls` — hard cap on scroll rounds, regardless of whether the page
  is still growing.
- `pause_ms` — wait time between each scroll for lazy content to load.
- `stable_rounds` — stop once page height hasn't grown for this many
  consecutive rounds (i.e., the page has stopped loading more content).
- `click_selector` — if given (e.g. `"button.load-more"`), that element is
  clicked once per round (when present and visible) before measuring
  height — supports "click to load more" pagination in addition to pure
  scroll-triggered lazy loading. Safe if the button never appears (the
  click is skipped, not an error).

## Browser pooling

By default, every Playwright fetch launches a fresh browser process and
tears it down afterward — the most expensive part of the whole path
(typically 1-3+ seconds per launch). Enable pooling to reuse browser
**processes** across requests:

```python
from bitscrape.core.settings import Settings

settings = Settings(playwright_pool_enabled=True, playwright_pool_size=3)
```

With pooling on, only a browser **context** (cheap — isolated cookies and
storage per request) is created per fetch; the underlying browser process
is checked out from a pool of `playwright_pool_size` and returned when
done. This is verified with a concurrency-bounding test: with
`pool_size=2`, a 3rd concurrent request genuinely waits for a pool slot
rather than the pool being decorative.

## Proxies with Playwright

If `ProxyMiddleware` has assigned a proxy to a request (via
`request.meta["proxy"]`), it's passed through to Playwright automatically
— as a launch-time proxy in the unpooled path, or as a per-context proxy
(`browser.new_context(proxy=...)`) in the pooled path, so different
requests through the same pooled browser can use different proxies.

## Combining with other Settings

Nothing about JS rendering is exclusive of the rest of the framework —
`robotstxt_obey`, `respect_meta_robots`, `distributed_throttle_enabled`,
etc. all still apply to Playwright-rendered requests exactly as they do to
`aiohttp` ones, since middleware runs before the downloader is even
selected.

## Limitations / honest disclosure

- This project's automated tests exercise the Playwright *code paths*
  (context/page interaction, scroll logic, pool concurrency bounding)
  against fake-but-API-shaped Playwright objects, since no real browser
  binary was available to download in the environment this was built in
  (no network access to Playwright's CDN). The pooling and scroll logic
  itself is genuinely tested; a real end-to-end render against a real
  browser has not been run as part of this project's own test suite —
  run your own smoke test against a real page after
  `playwright install chromium` before depending on this in production.
- Playwright adds meaningful latency and memory overhead per request
  compared to the `aiohttp` path — use it only for pages that actually need
  JS execution, not as a default.

## See also

- [architecture/](../architecture/index.md) — where the Playwright path fits in the Downloader.
- [tutorials/](../tutorials/index.md) — Tutorial 3 (JS rendering) worked example.
- [api/index.md#downloader-bitscrapedownloaderdownloader](../api/index.md#downloader-bitscrapedownloaderdownloader) — full `BrowserPool`/`scroll_to_bottom` signatures.
