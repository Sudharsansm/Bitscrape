# Dynamic pages: `DynamicFetcher`

For pages whose content only exists after JavaScript runs -- single-page apps,
lazy-loaded lists, content behind a click -- plain HTTP sees an empty shell.
`DynamicFetcher` drives a real browser through Playwright and returns the page
as the browser ended up with it.

```python
from bitscrape import DynamicFetcher

page = DynamicFetcher.fetch("https://example.com/app", wait_selector=".product")
print(page.css(".product h2::text").getall())
```

Install: `pip install bitscrape[playwright]`, then `playwright install chromium`.

## Four classes

| Class | Style | Lifetime |
|---|---|---|
| `DynamicFetcher` | sync | launches a browser per call, closes it after |
| `AsyncDynamicFetcher` | async | same |
| `DynamicSession` | sync | one browser for the `with` block: cookies and logins persist, no per-fetch launch cost |
| `AsyncDynamicSession` | async | same, for `async with` |

A one-shot call costs about a second of browser launch. For more than a couple
of pages, or whenever cookies must carry over, use a session:

```python
from bitscrape import DynamicSession

with DynamicSession() as s:
    s.fetch("https://example.com/login", page_action=sign_in)
    account = s.fetch("https://example.com/account", wait_selector=".balance")
```

```python
from bitscrape import AsyncDynamicSession

async with AsyncDynamicSession(max_concurrency=4) as s:
    pages = await asyncio.gather(*[s.fetch(url) for url in urls])
```

Every call returns a `ParsedResponse` -- the same object a spider callback
gets (`.css()`, `.xpath()`, `.json()`, `.text`, `.status`, `.xhr`, `.extra`).
Its body is the **rendered DOM**, except for a URL that isn't an HTML
document at all (a JSON endpoint, plain text): there you get the real
response body, not the browser's wrapper around it, so `.json()` works.
`.http_version` is `None` -- the browser doesn't report it, and bitscrape
doesn't guess.

`DynamicFetcher`/`DynamicSession` are synchronous and raise a clear
`RuntimeError` inside a running event loop (an `async def`, a Jupyter cell);
use the `Async...` classes there.

## Which browser

| Option | Meaning |
|---|---|
| *(default)* | Playwright's bundled Chromium |
| `real_chrome=True` | an installed **Google Chrome** (`channel="chrome"`) |
| `channel="msedge"` | any other Playwright channel (`chrome-beta`, `msedge-dev`, ...) |
| `executable_path="/path/to/browser"` | a specific binary |
| `cdp_url="http://localhost:9222"` | attach to a browser you started with `--remote-debugging-port`; it's left running when the session closes |
| `user_data_dir="./profile"` | a persistent profile, so logins survive between runs |
| `headless=False` | show the window |

A browser that can't be started raises `BrowserLaunchError`, with a message
that says how to fix it: Playwright's browsers aren't installed (`playwright
install chromium`), Chrome isn't installed, `executable_path` doesn't exist,
`cdp_url` isn't reachable, an unknown `channel`.

## Waiting for the page

```python
DynamicFetcher.fetch(url, wait_selector=".results")                        # a CSS selector (the usual signal)
DynamicFetcher.fetch(url, wait_selector=".spinner", wait_selector_state="hidden")
DynamicFetcher.fetch(url, network_idle=True)                               # wait for the network to go quiet
DynamicFetcher.fetch(url, wait=2.0)                                        # sit on the page 2 more seconds
DynamicFetcher.fetch(url, load_dom=False)                                  # return as soon as the DOM is parsed
```

`wait_selector_state` is `"attached"` (default), `"visible"`, `"hidden"` or
`"detached"`. By default the fetch waits for the page's `load` event.
`network_idle=True` also waits for the network to go quiet, which is slow on
chatty pages -- `wait_selector` is usually the better signal.

**Timeouts are in seconds**, and apply to each step separately: navigation,
the selector wait, and every Playwright call inside your hooks.

## Hooks: clicking, typing, mocking

`page_setup(page)` runs before navigation; `page_action(page)` runs after it,
**before** `wait_selector` -- so an action can be what makes the selector
appear. `page` is Playwright's *async* `Page`, so write `async def` hooks, in
the synchronous classes too:

```python
async def search(page):
    await page.fill("#q", "widgets")
    await page.click("button[type=submit]")

DynamicFetcher.fetch(url, page_action=search, wait_selector=".results")
```

```python
async def mock_api(page):
    await page.route("**/api/prices", lambda route: route.fulfill(json={"price": 1}))

DynamicFetcher.fetch(url, page_setup=mock_api)
```

Hooks may also be plain functions. Anything a hook raises becomes a
`DynamicFetchError` that names the hook (`page_action raised KeyError: ...`)
and chains the original exception (`__cause__`); the tab is closed either way.

## Infinite scroll, screenshots, background requests

```python
page = DynamicFetcher.fetch(url, scroll=True)                              # scroll until the page stops growing
page = DynamicFetcher.fetch(url, scroll={"max_scrolls": 50, "pause_ms": 500, "click_selector": "button.more"})

page = DynamicFetcher.fetch(url, screenshot=True)
png = page.extra["screenshot"]                                             # bytes

page = DynamicFetcher.fetch(url, capture_xhr=r"/api/", wait_selector=".done")
for call in page.xhr:                                                      # also page.find_xhr(pattern)
    print(call["method"], call["url"], call["status"], call["body"])
```

Captured entries are `{"url", "method", "status", "headers", "body"}` in the
order responses arrived. A response whose body is still streaming when the
page is otherwise done is waited for (up to 5 s) rather than lost. If a body
genuinely can't be had, `body` is `None` **and the entry has an `"error"`
saying why** (`"binary body (4 bytes)"`, a redirect response, ...). A request
that hasn't responded at all by the time the page is read isn't captured --
give slow calls time with `wait_selector`, `wait` or `network_idle`.

## Headers, cookies, identity

```python
DynamicSession(headers={"X-Team": "data"})              # added to every request the browser makes
s.fetch(url, headers={"X-Page": "1"})                   # this page only
s.fetch(url, cookies={"sid": "abc"})                    # dict (scoped to the URL) or Playwright cookie dicts
DynamicSession(storage_state="state.json")              # start logged in
DynamicSession(user_agent="MyTool/2.0", locale="de-DE", timezone="Europe/Berlin", viewport=(800, 600))
DynamicSession(context_options={"color_scheme": "dark", "geolocation": {...}})
DynamicSession(init_script="hook.js")                   # your JavaScript, before the page's own
```

`cookies=` go into the browser's cookie jar, where they **stay for the rest of
the session** (a browser context has one jar, unlike a one-off HTTP request).
By default the browser identifies itself as `BitscrapeBot/...`; `user_agent=`
is sent exactly as given. Nothing is generated, rotated or invented: there is
no fabricated `Referer`.

## Proxies

```python
DynamicFetcher.fetch(url, proxy="http://user:pass@proxy.example:3128")
DynamicFetcher.fetch(url, proxy={"server": "http://proxy.example:3128", "bypass": "*.internal"})
```

The URL form with credentials works: they're split into the separate
`username`/`password` fields Chromium needs (it ignores credentials embedded
in the server URL and answers every request with a 407 -- a bug bitscrape's own
browser paths had until this was tested against a real authenticating proxy).
Percent-encoded credentials are decoded. Chromium never proxies `localhost` /
loopback addresses unless told to (`extra_flags=["--proxy-bypass-list=<-loopback>"]`).

## Blocking resources

```python
DynamicSession(disable_resources=True)                  # no images, media, fonts, ...
DynamicSession(blocked_domains=["tracker.example"])
DynamicSession(block_ads=True)
DynamicSession(block_resource_types=["image", "stylesheet"])
```

`disable_resources=True` drops resources that make a page heavy without
changing its DOM text (images, media, fonts, text tracks, websockets, event
streams, manifests). Stylesheets and scripts are deliberately kept: blocking
them changes layout and visibility and breaks the very JavaScript a browser
fetch exists to run.

## Errors

| Exception | When |
|---|---|
| `DynamicFetchError` (a `FetchError`) | navigation failed or timed out, a hook raised, a URL that triggers a file download |
| `BrowserLaunchError` (a `DynamicFetchError`) | the browser couldn't be started; never retryable |

Both carry `.url` and `.retryable` (`False` for things retrying can't fix: an
invalid URL, a certificate error, a redirect loop, a download, a port Chromium
refuses by policy, a browser that has closed) and chain the underlying
Playwright error as `__cause__`. An HTTP error status (404, 500) is **not** an
error: the page comes back with `.status` set, as a browser would show it.

A URL that triggers a file download raises `DynamicFetchError`; fetch files
with `Fetcher.get()`.

## In a spider

Browser fetching inside a crawl uses `BrowserSession` (see
[sessions.md](sessions.md)), which runs on the same core and takes the same
options (`real_chrome`, `wait_selector_state`, `wait`, `page_action`,
`page_setup`, `init_script`, `context_options`, `disable_resources`, ...):

```python
class AppSpider(Spider):
    name = "app"
    sessions = {"b": {"kind": "browser", "wait_selector": ".product", "real_chrome": True}}

    def start_requests(self):
        return [Request(url="https://example.com/app", session="b")]
```

Hooks are code, so they belong on a session built in `configure_sessions()`
(`BrowserSession(page_action=...)`), not in a request that has to be serialised.
A browser failure takes the engine's normal failure path (a warning, the
spider's errback, per-session failure counts).

## What this is not

It's an honest, automated browser. It does **not** patch the traits headless
Chrome exposes, spoof a user agent, fabricate a Google referer, or solve
challenges, and `stealth=`, `hide_canvas=`, `block_webrtc=`, `allow_webgl=`,
`solve_cloudflare=`, `google_search=`, `dns_leak_protection=` and similar raise
`NotImplementedError` (ROADMAP.md, "Explicit non-goals"). Patched drivers such
as `patchright` are refused for the same reason (`Settings.browser_driver`
accepts only `"playwright"`). Like `Fetcher` it is a direct client: no
robots.txt check and no per-domain pacing -- use a `Spider` for a polite crawl.
