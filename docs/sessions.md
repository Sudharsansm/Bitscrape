# Multi-session support

One spider can drive several **named sessions** at once and route each request to
the right one by id. A session is a long-lived fetch context with its own cookie
jar / login state, headers, proxy, timeouts and (for browsers) its own persistent
context.

| Kind | Class | What it is |
|------|-------|------------|
| `"http"` | `HttpSession` | `aiohttp` client with its own cookie jar |
| `"browser"` | `BrowserSession` | one Playwright browser + shared context; a tab per request |

Every crawl also has a built-in `"default"` HTTP session, so spiders that never
mention sessions behave exactly as before.

## Declaring sessions

```python
import bitscrape
from bitscrape import BrowserSession, HttpSession

class ShopSpider(bitscrape.Spider):
    name = "shop"
    start_urls = ["https://shop.example.com/products"]

    # 1. Declaratively (also valid in Settings(sessions=...) and YAML/env config)
    sessions = {
        "api": {"kind": "http", "headers": {"Accept": "application/json"}},
    }

    # 2. In code (session objects, anything non-declarative)
    def configure_sessions(self, manager):
        manager.add("rendered", BrowserSession(wait_selector=".price"))  # lazy by default
        manager.add("account", HttpSession(cookies={"seed": "x"}), lazy=False)

    async def parse(self, response):
        for href in response.css("a.product::attr(href)").getall():
            yield self.follow(href, callback="parse_product", session="rendered")
```

`Request(session="rendered")` and `Request(sid="rendered")` are equivalent, and
`response.session_id` / `response.sid` tells a callback which session fetched a page.

### Options

`HttpSession`: `headers`, `cookies`, `user_agent`, `proxy` *or* `proxy_rotator`,
`timeout`, `verify_ssl`, `follow_redirects`, `max_concurrency`.

`BrowserSession`: `headless`, `browser`, `real_chrome`, `wait_until`,
`wait_selector`, `wait_selector_state`, `wait`, `timeout`, `locale`, `timezone`,
`viewport`, `user_agent`, `headers`, `proxy` (a URL -- `http://user:pass@host:port`
works -- or a Playwright proxy dict), `storage_state`, `user_data_dir`, `cdp_url`,
`executable_path`, `channel`, `args`, `context_options`, `init_script`,
`page_setup`, `page_action`, `capture_xhr`, `block_ads`, `block_domains`,
`block_resource_types`, `disable_resources`, `max_concurrency` (open tabs,
default 4). Anything left unset falls back to the matching `Settings.browser_*`
value. `page_setup`/`page_action` are `hook(page)` callables (sync or `async`)
that run before / after navigation; being code, they're set on the session
(in `configure_sessions()`), not in a serialisable request. The same options
power `DynamicFetcher` outside a spider -- see [dynamic-fetcher.md](dynamic-fetcher.md).

Declarative entries additionally accept `kind`, `lazy` and `default`.

## Routing rules

* `session=None` → the **default session**: the built-in one, or whichever was
  added with `default=True`.
* `use_playwright=True` with no session → the classic throw-away browser
  (unchanged), unless a default session was set explicitly.
* An unknown id raises `SessionError` (a `ValueError`) listing the known ids.
* **`follow()` inherits the current page's session** and its `session_kwargs`.
  Name another session to switch, or pass `inherit_session=False`.

## Per-request options

`Request.session_kwargs` overrides session defaults for one request (values must be
JSON-serialisable so requests survive checkpoints and Redis queues):

| Session | Keys |
|---------|------|
| HTTP | `headers`, `cookies`, `proxy`, `timeout`, `follow_redirects` |
| Browser | `wait_until`, `wait_selector`, `timeout`, `scroll`, `screenshot`, `capture_xhr`, `headers`, `cookies` |

Precedence: `Request.headers` > `session_kwargs` > session > `Settings`.
Proxy: `meta["proxy"]` > `session_kwargs["proxy"]` > `session.proxy` >
`session.proxy_rotator` > global rotator.

## Lifecycle

* Non-lazy sessions start with the crawl; **lazy** ones on first use (browser
  sessions are lazy by default so an unused browser is never launched).
* Concurrent first use starts a session exactly once.
* `max_concurrency` caps simultaneous requests through a session.
* All sessions are closed when the crawl ends.
* `await session.cookies()`, `HttpSession.clear_cookies()`,
  `BrowserSession.save_storage_state(path)` help with login flows.
* Conditional-GET caching is **per session**, so two logins never see each
  other's cached pages.

## Reacting to blocks

```python
class MySpider(bitscrape.Spider):
    sessions = {"plain": {"kind": "http"}, "rendered": {"kind": "browser"}}

    async def is_blocked(self, response):
        return response.status == 403 or b"captcha" in response.body

    async def retry_blocked_request(self, request, response):
        if request.session != "rendered":
            return request.model_copy(update={"session": "rendered"})
        return None   # already rendered: parse as-is
```

The default `is_blocked()` trusts the downloader's blocked-request detector.
A request is escalated at most `Settings.blocked_escalation_max` times (default 2);
the retry bypasses the dupefilter. `CrawlStats` reports `requests_blocked`,
`requests_escalated` and per-session `sessions_requests` / `sessions_failed` /
`sessions_blocked`.

## What is intentionally not included

TLS/browser-fingerprint impersonation, stealth hardening, challenge/CAPTCHA
solving and similar bot-detection evasion. Declaring such a kind (`"stealth"`,
`"impersonate"`) or option (`stealth=`, `solve_cloudflare=`, `hide_canvas=` ...)
raises `NotImplementedError` when used. See `ROADMAP.md`, "Explicit non-goals".
