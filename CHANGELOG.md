# Changelog

All notable changes to this fork/patch are documented here.
Format loosely follows [Keep a Changelog](https://keepachangelog.com/).

## [0.8.1]

### Added
- **Network history** (`bitscrape.network`): `record_requests=True` on browser
  sessions saves every completed request into a bounded, searchable
  `session.network` (`search`/`get`/`clear`/`find_json`/`to_har`); text bodies
  only, 1,000 entries / 1 MiB per body / 20 MiB total, ids never reused.
  `Settings.browser_record_requests`, `browser_max_recorded_requests`.
- **Saved login state**: `BrowserSession.save_state()/load_state()` (also on
  `AsyncDynamicSession`/`DynamicSession`); `save_state` includes IndexedDB.
- **Shadow DOM**: `pierce_shadow=True` inlines open shadow roots as
  `<shadow-root>`; `Settings.browser_pierce_shadow`.
- **Ad blocking**: bundled core list of ~3,900 ad/tracker domains (was 39),
  O(labels) matching, `allow_domains`, `blocked_count`, `parse_blocklist`,
  `update_blocklist()` and `bitscrape blocklist update|info`.
- **DNS-over-HTTPS** (`bitscrape.dns.DoHResolver`): `dns_over_https=` on
  `HttpSession`, `AsyncFetcherSession` and `Settings`; Cloudflare/Google/Quad9
  or a custom URL; TTL cache; no silent fallback.
- **MCP server**: 10 new tools (`extract`, `list_links`, `bulk_fetch`, six
  `session_*` tools, `network_search`), `css_selector` on `fetch`, SSRF guard
  (`allow_private_networks`), `--cdp-url`, bearer-token HTTP transport
  (`--transport streamable-http --auth-token`), owner-only saved state.
- `bitscrape.jsonx` (orjson with stdlib fallback; `orjson` is now a core
  dependency), `bitscrape.textutil` and `clean_text()/numbers()/price()/emails()`
  /`re()` on selectors and responses, `ContentSpider` alias, `RecordedResponse`.
- Docker: `--target browsers` image with Chromium/Firefox/WebKit; CI builds and
  smoke-tests it, and runs mypy + pyright.

### Fixed
- Whole codebase now passes pyright (was 46 errors: `Field(...)` defaults pyright
  couldn't see, untyped `**kwargs`, TypedDict reads, `lxml` imports) as well as mypy.

### Changed
- `dns_over_https` is no longer in the refused-options list (it is a privacy
  feature). `dns_leak_protection`, `stealth`, `impersonate`, `solve_cloudflare`
  and friends remain refused (ROADMAP.md, "Explicit non-goals").
- `selectolax` pinned `<1` (1.x removed the Modest backend).
- MCP server refuses loopback/private targets unless `allow_private_networks`.

## [Unreleased]

### Added — multi-session support (`docs/sessions.md`)
- New `bitscrape.sessions` module: `SessionManager`, `HttpSession`,
  `BrowserSession`. Sessions are real long-lived objects with their own cookie
  jar, headers, proxy, timeout and concurrency cap; a `BrowserSession` launches
  one browser and shares one persistent context (logins survive between pages,
  no per-request launch cost).
- `Spider.configure_sessions(manager)` for code-defined sessions; declarative
  `Spider.sessions` / `Settings.sessions` now accept the full option set plus
  `lazy` and `default`.
- `Request.session_kwargs` (per-request overrides), `Request.sid` alias,
  `Response.session_id`, and `response.session_id` / `.sid` in callbacks.
- `Spider.follow()` inherits the parent page's session and `session_kwargs`
  (`inherit_session=False` to opt out).
- `Spider.is_blocked()` / `Spider.retry_blocked_request()` hooks for escalating a
  blocked request (bounded by `Settings.blocked_escalation_max`).
- `CrawlStats`: `sessions_requests`, `sessions_failed`, `sessions_blocked`,
  `requests_escalated`.

### Fixed
- Conditional-GET cache is now keyed per session, so two logins can't be served
  each other's cached body.
- `Request.cookies` was declared but never sent; it now is (HTTP and browser).
- `ParsedResponse.xhr` read `extra["xhr"]` while the downloader stored
  `extra["captured_xhr"]`, so captured XHR never reached callbacks.

### Changed / Added — AutoThrottle (`docs/autothrottle.md`)
- **Reacts to blocks.** A status in `autothrottle_backoff_codes` (default 429/503),
  a response the blocked-request detector flags, or a timeout / connection error
  now doubles the domain's delay (`autothrottle_backoff_factor`). Before, only
  successful responses fed the throttle, so a 429 never slowed the crawl down.
- **`Retry-After` pauses the whole domain** (numeric or HTTP-date, capped by
  `max_retry_after_seconds`) instead of only delaying the one failing request.
- **Recovers on its own**: each healthy response closes `autothrottle_recovery`
  of the gap to the sustainable delay; a block never speeds the crawl up.
- New module `bitscrape.downloader.throttle` (`AutoThrottle`, `DomainPacer`,
  `parse_retry_after`); `AutoThrottle.get_delay/update/reset` keep working.
- New settings: `autothrottle_block_backoff`, `autothrottle_backoff_factor`,
  `autothrottle_recovery`, `autothrottle_backoff_codes`,
  `autothrottle_backoff_on_errors`; `autothrottle_max_delay < start_delay` is now
  rejected when enabled.
- `CrawlStats`: `throttle_backoffs`, `throttle_cooldowns`, `throttle_wait_seconds`,
  `throttle_delays`; `Downloader.throttle_info()`.
- Browser (Playwright) fetches are now paced and feed AutoThrottle too.

### Fixed — politeness
- Per-domain delay was check-then-sleep, so N concurrent workers all saw "enough
  time has passed" and fired together. Requests are now spaced by a FIFO
  `DomainPacer`, and the delay is re-read when a queued request's turn comes.
- The delay was applied while holding the *global* concurrency slot; a
  cooling-down domain no longer starves other domains.
- AutoThrottle measured latency from the start of the whole fetch, folding
  earlier attempts' retry sleeps into the delay it learned; it now uses the
  latency of the attempt.
- With autothrottle on, `download_delay` was ignored; it (and `domain_delays`,
  and robots.txt `Crawl-delay`) is now the floor the learned delay never drops below.
- `domain_delays` and `domain_concurrency` were declared but never read; they
  now work. robots.txt `Crawl-delay` was only honoured by the Redis-distributed
  middleware; the local downloader now honours it too.

### Changed / Added — robots.txt compliance (`docs/robots.md`)
- Parsing moved from the standard library's `RobotFileParser` to
  [Protego](https://github.com/scrapy/protego): adds `*`/`$` wildcard support in
  `Disallow`/`Allow` and, crucially, **`Request-rate`**, which `RobotFileParser`
  doesn't implement at all.
- `Request-rate` is converted to a `seconds/requests` delay and folded into
  `request.meta["crawl_delay"]` alongside `Crawl-delay` (the larger of the two
  wins); its optional time-of-day window is evaluated at **every** request, not
  baked in when robots.txt was fetched. The raw directive is also available as
  `request.meta["request_rate"]`.
- New `bitscrape.middleware.robots` module (`RobotsCache`, `RobotsInfo`,
  `RobotsFetcher`); `RobotsMiddleware` is now a thin `process_request` adapter over it
  and accepts an optional `RobotsCache` (`RobotsMiddleware(cache)`), so a custom
  fetcher/session, or a pre-warmed cache, can be supplied.
- Per-domain fetching is now concurrency-safe: the first several requests to a
  brand-new domain share one fetch instead of each triggering its own.
- The cache now has a TTL (`robots_cache_ttl`, default 1h) instead of caching a
  domain's rules for the whole crawl, with stale-while-revalidate: a failed
  refresh keeps enforcing the last known-good rules rather than either opening
  the domain up or falling back to fail-safe-block.
- The cache is now bounded (`robots_max_cache_domains`, LRU eviction) and caps
  how much of an oversized robots.txt it will parse (`robots_max_content_bytes`,
  500 KiB default, truncated rather than rejected).
- New settings: `robots_user_agent`, `robots_respect_request_rate`,
  `robots_cache_ttl`, `robots_negative_cache_ttl`, `robots_max_cache_domains`,
  `robots_max_content_bytes`, `robots_timeout`.
- `mw.stats()`: `domains_cached`, `fetches`, `fetch_failures`, `cache_hits`.
- New dependency: `protego>=0.3`.

### Fixed — robots.txt
- A domain whose robots.txt fetch failed was refetched on **every single
  request** to it (the old fail-safe path deliberately didn't cache the
  failure). It is now cached for `robots_negative_cache_ttl` seconds, so an
  outage produces one fetch attempt per interval instead of one per request.
- The UA used to pick a robots.txt group was matched by raw substring, so a
  User-agent string containing another bot's name as a substring could match
  the wrong group; the product token (before the first `/`) is matched now.
- robots.txt was fetched with no `User-Agent` header; some sites require one to
  serve rules at all. It is now sent (`robots_user_agent` or `Settings.user_agent`).

### Added — cookie and state management for `FetcherSession` / `AsyncFetcherSession` (`docs/fetchers.md`)
- `cookies=` constructor argument to seed a session's jar at creation (e.g.
  with cookies restored from a previous run).
- `session.cookies(url=None)`, `.set_cookies({...})` (add cookies without a
  real `Set-Cookie` header -- a login API that returns a token in its JSON
  body, say), `.clear_cookies()`.
- `session.save_cookies(path)` / `.load_cookies(path)`: a flat `{name: value}`
  JSON snapshot, for resuming a logged-in session later or in a different
  process -- the same simplification `HttpSession.cookies()` (the spider-side
  equivalent) already uses. `load_cookies` adds to what the session already
  has rather than replacing it.
- All five methods exist on both the async and sync classes
  (`AsyncFetcherSession`/`FetcherSession`), and on both protocol backends
  (`protocol="http1"` and `"http2"`); `DynamicSession`'s equivalents
  (`.browser.cookies()`, `.browser.save_storage_state()`) already covered this
  for browser sessions, so this closes the same gap on the HTTP side.

### Added — `DynamicFetcher`: real-browser fetching of JavaScript-driven pages (`docs/dynamic-fetcher.md`)
- New `DynamicFetcher` / `AsyncDynamicFetcher` (one-shot) and `DynamicSession` /
  `AsyncDynamicSession` (one browser kept open, so cookies and logins persist):
  `DynamicFetcher.fetch(url, wait_selector=...)` returns a `ParsedResponse` whose
  body is the rendered DOM (or the real body, for a JSON/text URL).
  Browsers: Playwright's Chromium, installed Google Chrome (`real_chrome=True`),
  any Playwright `channel`, `executable_path`, or a running browser over
  `cdp_url`; `user_data_dir` for a persistent profile.
- Waiting and driving the page: `wait_selector` (+ `wait_selector_state`),
  `network_idle`, `load_dom`, `wait`; `page_setup(page)` / `page_action(page)`
  hooks (sync or async; `page_action` runs *before* `wait_selector` so an action
  can make the selector appear); `scroll=` for infinite scroll; `screenshot=`;
  `capture_xhr=` (`response.xhr`, `.find_xhr()`); per-fetch `headers=` /
  `cookies=`; `init_script=`, `context_options=`, `locale` / `timezone` /
  `viewport`, `storage_state`; `disable_resources`, `blocked_domains`, `block_ads`.
- New `bitscrape.errors` (`FetchError` moved here; re-exported everywhere it was
  importable before) with `DynamicFetchError` (a `FetchError`: navigation failure
  or timeout, a hook that raised, a URL that triggers a download) and
  `BrowserLaunchError` (the browser can't start: the message says whether to run
  `playwright install chromium`, install Chrome, fix `executable_path`, or start
  Chrome with `--remote-debugging-port`). Both carry `.url` / `.retryable`.
- `BrowserSession` (browser sessions in a spider) gained `real_chrome`,
  `wait_selector_state`, `wait`, `page_setup`, `page_action`, `init_script`,
  `context_options` and `disable_resources`, and now runs on the same new core
  (`bitscrape.browser`: `BrowserRuntime`, `load_page`, `XhrCapture`) as
  `DynamicFetcher`, so fixes land in one place.
- The "refuse evasion options" list now also refuses `google_search` and
  `stealthy_headers`. `stealth=`, `hide_canvas=`, `block_webrtc=`,
  `solve_cloudflare=` and the rest raise `NotImplementedError` on the new classes
  too, and `Settings.browser_driver="patchright"` (a patched driver that exists
  to hide automation; the setting was declared but never read) now raises
  `NotImplementedError` instead of being silently ignored.

### Fixed — found by testing against a real Chromium (the earlier tests used fakes)
- **Captured XHR bodies were silently lost.** Playwright fires `response` when
  *headers* arrive; the body can still be streaming. The handler read it in the
  background, the page closed first, the read raised, a blanket `except`
  swallowed it, and the entry was recorded with `body=None` -- indistinguishable
  from an empty or binary body. In-flight bodies are now waited for (up to 5 s),
  and an entry whose body can't be had carries an `"error"` saying why
  (`"binary body (4 bytes)"`, ...). Fixed in all three browser paths.
- **Authenticated proxies failed with 407.** `http://user:pass@host:port` was
  handed to Chromium whole as the proxy `server`; Chromium ignores credentials
  embedded there. They're now split into `username` / `password` (percent-decoded)
  in every browser path, so the same proxy URL works for HTTP and browser fetches.
- **Browser failures took a spider's "unexpected error" path** (ERROR log with a
  full traceback, not counted in per-session failure stats, errback skipped).
  A navigation failure in a browser session is now a `DownloadError`, like any
  other network failure.
- **`BrowserPool` ignored `browser_channel`, `browser_executable_path` and
  `browser_args`** whenever pooling was on (only the unpooled path applied them).
  It also leaked the browsers it had already launched, and the Playwright driver,
  if a later launch failed. Both fixed; launch errors are translated like the rest.
- `Downloader.open()` leaked its HTTP client (and any sessions already started)
  when the browser pool or a non-lazy browser session failed to start.
- A browser page that is mid-redirect when read no longer fails with Playwright's
  "page is navigating" error (retried after the load settles).
- Chromium's refusal of certain ports (`ERR_UNSAFE_PORT`) is no longer marked
  retryable; neither is a redirect loop.
- The "can't run a synchronous API inside an event loop" error now names the
  right async class for each (`AsyncDynamicFetcher`, `AsyncFetcherSession`, ...).
- `docs/browser/index.md` claimed no real-browser run had ever been done; it now
  states what is verified against a real Chromium and what is not (genuine
  Chrome, Firefox/WebKit, headed mode, SOCKS, Windows/macOS).

### Changed — CI
- CI now runs `playwright install --with-deps chromium` before the tests.
  Without a downloaded browser `tests/test_dynamic_fetcher.py` skips its
  real-browser half rather than fail, which would quietly hide a regression.
  (Not run from here; that step is the standard Playwright command.)

### Added — custom proxy rotation strategies, and `ProxyRotator` on every session type (`docs/proxies.md`)
- `ProxyRotator(strategy=...)` now accepts a plain function -- `(available, domain) -> ProxyState` -- not just the four
  built-in names. `register_proxy_strategy(name, fn)` / `unregister_proxy_strategy()` / `list_proxy_strategies()` let a
  custom strategy be selected by name too, including from `Settings.proxy_strategy` (a plain string field, which can't
  hold a function). `ProxyStrategyError` when a strategy raises or returns something that isn't one of the proxies it
  was actually given.
- `ProxyState` is now public (was private `_ProxyState`; the old name is kept as an alias). All four built-in
  strategies (`random`, `round_robin`, `least_used`, `sticky`) are unchanged.
- **`BrowserSession.proxy_rotator`** and **`AsyncFetcherSession`/`FetcherSession`/`Fetcher`.proxy_rotator**: neither
  had any rotator support before (only a fixed `proxy=`). An HTTP session (`protocol="http1"`) draws a fresh proxy
  **per request** (and per retry attempt); a browser session or an HTTP/2 session draws **once**, since neither a
  browser nor an httpx client can switch proxy mid-connection -- each new session (or one-shot `DynamicFetcher` call,
  which launches a fresh browser every time) gets a turn from the rotator instead. Every fetch reports success/failure
  back to the rotator either way, so a session stuck on a proxy that starts failing gets it banned for the *next*
  launch.

### Fixed
- `ProxyRotator`'s new custom-strategy validation initially checked a returned `ProxyState` by equality, not identity;
  since `ProxyState` is a plain dataclass, a strategy returning a *different* but field-equal object (e.g. a fresh
  default-valued one) would have passed. Fixed to check identity, so a strategy must return one of the actual objects
  it was handed.

### Added — direct HTTP requests: `Fetcher` / `AsyncFetcher` / `FetcherSession` (`docs/fetchers.md`)
- New `bitscrape.fetchers` module: `Fetcher.get/post/put/patch/delete/head/options(url, ...)`
  (one-shot, synchronous) and the matching `AsyncFetcher`, plus
  `FetcherSession`/`AsyncFetcherSession` for reusing cookies and connections
  across several calls -- a lightweight client for hitting a URL or an API
  directly, without a spider/engine. Every call returns a `ParsedResponse`,
  the same object a spider callback gets (`.css()`, `.xpath()`, `.json()`,
  `.http_version`, ...).
- `protocol="http2"` offers real, ALPN-negotiated HTTP/2 via `httpx`
  (`pip install bitscrape[http2]`), falling back to HTTP/1.1 (visibly, via
  `response.http_version`) where the server doesn't offer it. HTTP/3 is not
  supported (`protocol="http3"` raises a clear error).
- Retries with exponential backoff and `Retry-After` support, restricted by
  default to idempotent methods (GET/HEAD/OPTIONS/PUT/DELETE) so a retried
  request can't repeat a POST's side effects; `retry_methods=` widens this,
  `retry_statuses=` customises which responses are retried.
  `bitscrape.FetchError` (`.url`, `.retryable`, `__cause__`) for exhausted
  network-level failures; a redirect loop or malformed URL is never retried.
- New `Settings.default_headers` (`Accept`, `Accept-Language`): ordinary HTTP
  hygiene sent by every request -- `Fetcher`, the full spider/engine path
  (`Downloader`, `HttpSession`), and `BrowserSession` all read it. This is a
  single fixed default, not a per-browser profile -- overridable/clearable
  per `Settings`, per session, or per request as already supported.
- `Response.http_version` (declared since the field's introduction but never
  populated) is now set from aiohttp's negotiated protocol on every HTTP
  fetch (`"1.1"`; `"2"` via the new HTTP/2 backend). Left `None` for browser
  (Playwright) fetches, which don't expose it cleanly -- honest "unknown"
  rather than a guess.
- `impersonate=`, `stealthy_headers=`, `http3=`, `fingerprint=` and similar
  keyword arguments raise `NotImplementedError` naming ROADMAP.md's
  "Explicit non-goals" wherever they're passed to `Fetcher`/`AsyncFetcher`/
  `FetcherSession` -- the same policy `sessions.py` already applies to
  `HttpSession`/`BrowserSession`, extended to this new entry point rather
  than left as a gap a differently-named parameter could slip through.
- Renamed the internal `RobotsMiddleware` fetch-abstraction `Protocol` from
  `Fetcher` to `RobotsFetcher` to free the name `Fetcher` for this public
  class; it was only ever used internally (`RobotsCache(fetcher=...)`),
  so nothing public changes name.

### Added — 13 new export formats + in-memory `ItemList` (`docs/exporters.md`)
- **New formats**, all dispatched through the existing `get_exporter()` (and
  therefore the CLI's `--format` and `bitscrape.run(fmt=...)`) alongside the
  original JSON/JSONL/CSV/XML: **Parquet** (`.parquet`), **ORC** (`.orc`),
  **Avro** (`.avro`), **YAML** (`.yaml`/`.yml`), **TOML** (`.toml`), **INI**
  (`.ini`), **Protocol Buffers** (`.proto`/`.protobuf`), **MessagePack**
  (`.msgpack`), **BSON** (`.bson`), **Markdown** (`.md`), **WARC** (`.warc`),
  **PDF** (`.pdf`), **Word** (`.docx`/`.doc`). Each lives behind its own
  optional dependency (`pip install bitscrape[parquet]`, etc., or
  `bitscrape[export-formats]` for all thirteen, or `bitscrape[all]`); a
  format whose dependency isn't installed raises a clear `ImportError` naming
  the right extra, not a bare `ModuleNotFoundError`.
- **New** `bitscrape.ItemList` (`stats.items` when `Settings.collect_items=True`,
  or standalone): a `list` subclass with a `.to_X()` method per format --
  `result.items.to_json()`, `.to_parquet()`, `.to_avro(schema=...)`, etc.
  Omit the `path` argument to get the content back directly (`str`/`bytes`)
  instead of writing to disk. `Settings.collect_items` is off by default (a
  large crawl shouldn't silently buffer every item in memory); `stats.items`
  holds items *after* every pipeline ran, in scrape order.
- `bitscrape.exporters.warc.response_to_item(response)`: archive a real
  fetched page (actual status/headers/body) as a WARC `response` record by
  yielding it from a spider callback like any other item; a plain dict item
  becomes a generic `resource` record instead, so WARC export never requires
  changing what an existing spider yields.
- New module layout: `bitscrape.exporters._base` (shared `normalize_rows()` /
  `BufferedExporter` / `require()`), `.columnar`, `.structured`, `.binary`,
  `.documents`, `.warc`, `.registry` (registers the 13 new classes into
  `feed._EXPORTERS` in place, so every existing `get_exporter` call site
  picks them up with no change to `feed.py` itself).

### Fixed (found while building the above)
- **Silent column loss**: handing heterogeneous items straight to
  `pyarrow.Table.from_pylist()` uses only the *first* item's keys as the
  schema and silently drops any key that first appears later. `normalize_rows()`
  computes the union of keys across every item up front (missing = `null`;
  nested `dict`/`list` values are JSON-encoded) before it reaches Parquet,
  ORC, or Avro.
- `fpdf2`'s `multi_cell()` doesn't reset the x-position to the left margin
  between calls by default, so a second consecutive field on a `PDFExporter`
  page could raise "Not enough horizontal space to render a single character"
  for a normal multi-field item; every call now passes `new_x="LMARGIN",
  new_y="NEXT"` explicitly.

### Added — spider templates upgrade (`docs/spider-templates.md`)
- `CrawlSpider`/`Rule`/`LinkExtractor` are now re-exported from
  `bitscrape.spiders` alongside the other four templates, all under one
  import path.
- **New** `SiteToMarkdownSpider`: crawls a whole site (via `CrawlSpider`) and
  converts every page to clean, LLM-ready Markdown, optionally writing `.md`
  files to disk. `css_selector`, `main_content_only`, `output_dir`, `max_pages`.
- **New** `LinkExtractor.matches(url)`: tests a bare URL string against the
  extractor's allow/deny/domain/extension rules without needing an HTML
  document -- what lets `SitemapSpider` reuse `Rule`/`LinkExtractor` for
  `<loc>` URLs, which never came from an `<a href>`.
- `SitemapSpider`:
  - **robots.txt-driven discovery**: a `sitemap_urls` entry ending in
    `robots.txt` is detected automatically and its `Sitemap:` directives
    (parsed via Protego, same as `RobotsMiddleware`) are followed -- the
    "or robots.txt-driven crawls" half of this feature, previously absent.
  - `Rule`-based dispatch (regex/domain-aware) as an alternative to the
    existing substring `sitemap_rules`, tried first when set.
  - `sitemap_alternate_links`: follow `<xhtml:link rel="alternate"
    hreflang="...">` URLs too.
  - Gzip sitemaps are now capped at 64 MiB decompressed (gzip-bomb protection
    -- previously unbounded).
- `ShopifySpider`: richer default item fields (`old_price`, `barcode`,
  `option1/2/3`, `image_url`, `description` with HTML stripped); variants are
  now deduplicated by `(product_id, variant_id)` so overlapping pagination or
  a repeated `shop_urls` entry can't double-yield; new `max_pages` safety cap.
- New settings-adjacent module: `Rule.priority` (was declared in the
  docstring since the initial version of this dataclass but never applied to
  the requests it built -- it now is, for both `CrawlSpider` and
  `SitemapSpider`'s `Rule`-based dispatch).

### Added / Fixed -- `allowed_domains` now actually does something
- **New** `OffsiteMiddleware` (always installed, a no-op while
  `Spider.allowed_domains` is empty): drops any request to a host outside it.
  `allowed_domains` has been a documented `Spider` attribute -- even used in
  `CrawlSpider`'s own docstring example -- since the initial version, but
  nothing anywhere enforced it; a comment in `core/spider.py` asserted
  "off-site requests are dropped by the scheduler", which was not true. It
  now is enforced, first in the middleware chain (before robots.txt,
  sessions, or proxy selection do any work on a request headed nowhere).
  Tracked as `CrawlStats.requests_dropped` / `requests_offsite_dropped`.
- **Fixed**: a `DropItem` raised by a `process_request` middleware (robots.txt
  disallow, and now offsite) was previously caught by the Engine's generic
  exception handler: counted in `requests_failed`, logged at ERROR level with
  a full traceback, and passed to the spider's `errback` -- all wrong for a
  deliberate policy drop, not a failure. The Engine now handles `DropItem` /
  `OffsiteDrop` specifically: `CrawlStats.requests_dropped` (and
  `requests_offsite_dropped` for the latter), an INFO-level log line, a new
  `request_dropped` plugin event, and no errback call.

### Fixed -- XMLFeedSpider
- **Namespace bug**: `node.findtext("title")` silently returned `None` for
  any namespaced feed (Atom, media-RSS) because the matched node still
  carried its namespace and `findtext` without a namespace map can't see
  into it. Nodes are now passed with namespaces stripped, so plain,
  namespace-agnostic calls work the same on RSS and Atom/media-RSS alike.
- **Dead setting**: the documented `namespaces: ClassVar[dict[str, str]]`
  class attribute was never actually consulted when matching `itertag` --
  two same-named tags in different namespaces (e.g. `<media:content>` vs.
  some other `<x:content>`) couldn't be told apart. A prefixed `itertag`
  (`"media:content"`) now matches only the namespace `namespaces` maps that
  prefix to; a bare `itertag` still matches any/no namespace as before.
- Gzip-compressed feeds are now decompressed automatically (previously only
  `SitemapSpider` did this); capped at 64 MiB like sitemaps.

### Fixed -- CSVFeedSpider
- `quotechar` is now a configurable class attribute (previously hardcoded to
  Python's `csv` module default with no way to override).
- Gzip-compressed feeds are now decompressed automatically.

### Fixed -- ParsedResponse
- Added `ParsedResponse.encoding` (proxying the underlying `Response`, like
  its other passthrough properties `.status`/`.headers`/`.body` already do) --
  previously the raw `Response.encoding` wasn't reachable from a spider
  callback at all, since callbacks receive the parsed wrapper.
- Added `ParsedResponse.session_id` / `.sid` (see the multi-session work
  above) usage note: `.captured_xhr` alias for `.xhr`.

### Fixed -- stale documentation
- `docs/crawling/index.md` still described robots.txt matching via the
  standard library's `RobotFileParser`; updated for the Protego-based
  rewrite (`Request-rate`, wildcards) from this same upgrade pass.
- `docs/user-guide/index.md` flatly asserted `self.follow()` "does not
  resolve relative URLs for you" a few lines above a section titled
  "Relative URLs (fixed in 0.8.0)" that correctly describes the opposite --
  a leftover from before that fix landed. Corrected.

### Notes
- `Downloader.open()` takes an optional `configure` callback; `_resolve_session_kind`
  was replaced by `SessionManager`. Unknown ids still raise `ValueError`
  (now `SessionError`), unsupported kinds still raise `NotImplementedError`.
- Bot-detection evasion (stealth, fingerprint impersonation, challenge solving)
  remains a non-goal; such session kinds/options are rejected with a clear error.

## [0.9.0] — 2026-09-21

Implements the crawl-reliability and spider-template settings that
checkpoint 0.8.0 had already declared in `Settings` (`dev_mode`,
`blocked_detection_enabled`, `proxy_strategy`, `sessions`, `block_ads`,
`browser_cdp_url`, and friends) but that had no code behind them yet.
Every item below is backed by a real, passing test against a real local
server (or, for pure logic like proxy rotation, direct unit tests) — see
`tests/test_downloader_v09_features.py` and `tests/test_spider_templates.py`.

### Test environment (was reported as "49 failed / 18 errors")

That wasn't a code regression — it was this environment missing the
optional extras and live services `docs/developer-guide/index.md` already
documents as required. Installing `.[all,dev]`, then starting a local
Redis on port 6390 and a local PostgreSQL with a `bitscrape_test` database
exactly per that guide's recipe, took the suite from `49 failed / 507
passed / 18 errors` to **`616 passed, 0 failed, 0 errors`**. `ruff` and
`mypy` are both fully clean too (mypy's previous 5 errors were the same
missing-optional-dep issue — `prometheus_client`/`opentelemetry` weren't
installed; they resolve cleanly once `.[all]` is installed).

### Added (this round — closing remaining gaps against the original request)

- **`bitscrape shell <url>`** (`[--playwright]`): fetches a URL and drops
  into an IPython shell with `response`/`r` (a `ParsedResponse`) already
  loaded and a `fetch(url)` helper to load another page without leaving
  the shell. Falls back to the standard library's `code.interact` if
  IPython isn't installed. This was the explicitly-requested "CLI plus
  IPython shell" that had no implementation yet.
- **`genspider` templates for the new spider types**: `--template
  xmlfeed`, `csvfeed`, and `shopify`, alongside the existing
  `basic`/`crawl`/`sitemap`. Also fixed the existing `sitemap` template,
  which had gone stale: it used to hand-roll `loc::text` CSS against the
  raw sitemap XML (no sitemap-index following, no gzip support) instead
  of the real `SitemapSpider` added earlier in 0.9.0 — it now generates a
  proper `SitemapSpider` subclass.
- **Scrapy integration** (`bitscrape.integrations.scrapy.bitscrape_response`):
  a decorator for people who already have a Scrapy project and just want
  bitscrape's parser (adaptive selectors, `find_similar`, structured
  extraction) on responses they're *already fetching with Scrapy* — no
  rewrite, no `scrapy` dependency added to bitscrape itself (it duck-types
  against any response with `.url`/`.body`). Matches Scrapling's "drop-in
  Scrapy integration" feature.

### Corrected (not "added" — a course-correction)

- `settings.dns_leak_protection` / `settings.doh_url` now raise the same
  explicit `NotImplementedError` as `impersonate`/`browser_stealth`,
  instead of remaining silently unimplemented. On closer reading of their
  own docstring ("lock the browser's local DNS resolver ... stop WebRTC
  from exposing the real IP"), these aren't a neutral privacy feature —
  they're proxy-identity concealment specifically for evading site-side
  detection of automated/proxied traffic, the same category as
  fingerprint impersonation. Filed under the same policy rather than
  built.

### Added

- **Proxy rotation** (`bitscrape.ProxyRotator`): random / round-robin /
  least-used / sticky-per-domain strategies, automatic cooldown after N
  consecutive failures, `ProxyRotator.from_file()`. Wired into the
  downloader automatically whenever `settings.proxies` or
  `settings.proxy_file` is set — no per-request wiring needed, though
  `request.meta["proxy"]` still wins if you set it explicitly.
- **Dev mode** (`bitscrape.DevModeCache`, `settings.dev_mode=True`):
  caches every response to disk on first fetch and replays it on later
  runs, so you can iterate on `parse()` without re-hitting the target
  server. Works for both HTTP and Playwright-rendered fetches.
  `settings.dev_cache_ttl` (0 = never expires) and
  `settings.dev_cache_statuses` control what gets cached and for how long.
- **Blocked-request detection** (`bitscrape.BlockedRequestDetector`,
  `settings.blocked_detection_enabled=True`): flags a response as blocked
  by status code (`settings.blocked_http_codes`) and/or a body marker
  (`settings.blocked_extra_markers`, regex, case-insensitive) — catching
  the "200 OK but it's actually an interstitial" case that plain HTTP
  retry codes miss. Retries with exponential backoff
  (`settings.blocked_retry_backoff` / `blocked_max_backoff`), rotating to
  a different proxy each attempt if one is configured, and gives up
  cleanly after `settings.blocked_max_retries` rather than looping
  forever. `CrawlStats.requests_blocked` and `.cache_hits` (declared but
  unused since 0.8.0) now actually populate.
- **Domain / ad / resource-type blocking** for Playwright fetches
  (`bitscrape.DomainBlocker`, `settings.block_ads` / `block_domains` /
  `block_hosts_file` / `block_resource_types`): a small curated built-in
  ad/tracker domain list (not the ~3,500-domain lists some commercial
  blocklists ship — see the module docstring for why), your own domain
  list, a hosts-file loader, and outright blocking of resource types like
  images/fonts/media.
- **Multi-session routing** (`Spider.sessions` / `settings.sessions`,
  `Request(session=...)`): declare named sessions with `kind="http"` or
  `kind="browser"` and route individual requests to them within the same
  spider. `Spider.sessions` now actually merges into `settings.sessions`
  (previously declared as a `ClassVar` and documented, but never read
  anywhere).
- **Spider templates** (`bitscrape.SitemapSpider`, `XMLFeedSpider`,
  `CSVFeedSpider`, `ShopifySpider` — new `bitscrape.spiders` module):
  ready-made spiders for the four most common non-hyperlink-following
  crawl shapes. `SitemapSpider` follows sitemap indexes (including
  gzip-compressed sitemaps) and routes matched URLs to different
  callbacks by substring rule; `ShopifySpider` paginates a storefront's
  public `/products.json` API and yields one item per variant.
- **Extended browser-fetch settings** actually wired up (previously
  declared in `Settings` but unused): `browser_cdp_url` (connect to an
  already-running remote browser instead of launching one locally),
  `browser_executable_path` / `browser_channel` / `browser_args`,
  `browser_user_data_dir` (persistent profiles via
  `launch_persistent_context`), `browser_storage_state`, `browser_locale`
  / `browser_timezone` / `browser_viewport`, `browser_wait_until` /
  `browser_wait_selector`, and `browser_capture_xhr` (background XHR/fetch
  responses collected onto `response.extra["captured_xhr"]` without
  reverse-engineering the API yourself).
- CI now actually builds `deploy/Dockerfile` on every push/PR
  (`.github/workflows/docker.yml`) — the Dockerfile, `docker-compose.yml`,
  and Kubernetes manifests under `deploy/` existed since before this
  release but nothing had ever built or validated them, so a broken
  Dockerfile could ship silently. The workflow also validates
  `deploy/docker-compose.yml` with `docker compose config`. It does
  **not** push the image anywhere by default — this project currently
  only publishes to GitHub and PyPI; the push step is present but
  commented out for whenever that changes. A root-level `.dockerignore`
  was added to keep the build context (which is the repo root, per
  `docker-compose.yml`'s `context: ..`) small.
- `ParsedResponse` (what spider callbacks actually receive) now also
  proxies `.ok` from the underlying `Response` — it already had `.json()`
  and `.urljoin()`, but `.ok` was missing, so callbacks that wanted to
  check `response.ok` had to reach through `response.response.ok`. Found
  and fixed while writing `ShopifySpider`, which checks `.ok` in its
  pagination callback.

### Explicitly not implemented (by design, not oversight)

`settings.impersonate`, `settings.browser_stealth`, and the `"impersonate"`
/ `"stealth"` session kinds remain declared (for forward API
compatibility with anyone already reading them) but now raise a clear
`NotImplementedError` pointing here instead of silently doing nothing.
TLS/browser fingerprint impersonation and anti-detection "stealth"
hardening are tooling built specifically to defeat a site's anti-bot
protections — this project has repeatedly declined to build that (see
`ROADMAP.md`'s "Explicit non-goals"), and 0.9.0 continues that policy
rather than reversing it quietly. Everything else requested alongside it
(autothrottle already existed since checkpoint 0.8.0; proxy rotation,
blocked-request retry, dev-mode caching, ad/domain blocking, spider
templates, multi-session, and extended browser controls above) is
implemented in full.

### Added (closing the last two items from the original request: MCP server + RAG pipeline)

- **`bitscrape.rag.RagPipeline`**: ties `html_to_markdown` +
  `chunk_markdown` + `BM25Index`/`VectorIndex`/`HybridSearcher` (all of
  which already existed separately) into one object —
  `pipeline.ingest_response(response)` / `.ingest_html(html, url=...)`,
  then `.search(query)`, then `.to_context(results)` to get a
  ready-to-paste LLM prompt context, each chunk labelled with its
  source/heading. BM25-only by default; pass `embed_fn` for full
  BM25+vector hybrid search via RRF — embeddings are still bring-your-own
  (same scope note as `ranking.py`: no network access here to call an
  embedding API or download a model).
- **MCP server** (`bitscrape.mcp_server`, `bitscrape mcp
  [--transport stdio|sse|streamable-http]`, new optional `mcp` extra):
  three tools for AI agents — `fetch` (URL → clean Markdown, optionally
  through a headless browser), `extract_structured` (JSON-LD/Microdata/
  RDFa/OpenGraph/meta/links as JSON), and `search` (hybrid search, via
  `RagPipeline`, over every page fetched through the server so far).
  Deliberately **excludes** any stealth/anti-bot-bypass fetch mode — the
  server builds its own internal `Settings` and forces
  `impersonate=None` / `browser_stealth=False` / `dns_leak_protection=False`
  regardless of what's passed in, so an AI agent can't reach for evasion
  tooling through this surface even indirectly. Prompt-injection handling
  is scoped honestly in the module docstring: content is passed through
  `html_to_markdown(strip_hidden=True)`, which is a real, tested defense
  against the common "hidden instructions for the AI" pattern (verified in
  `tests/test_mcp_server.py` against a page with exactly that), plus a
  length cap — not a claim that all injection is caught, since visible
  page text is unavoidably passed through as-is.

### Added (verification pass — closing gaps found by re-auditing every line item)

Re-checked every feature/technology bullet in the original request against
the actual code (not assumptions) and closed what was still missing:

- **Screenshots**: `request.meta["screenshot"] = True` (or a dict of
  Playwright `page.screenshot()` kwargs) on a `use_playwright=True` request
  now captures a PNG into `response.extra["screenshot"]`, wired through
  both the pooled and unpooled browser-fetch paths. New `screenshot` MCP
  tool (base64-encoded PNG). Was previously mentioned only in a comment on
  the `response.extra` dict, with no actual capture code behind it.
- **Fast JSON export**: `orjson` was installed as a dependency but only
  used by the Redis scheduler code — the actual export path
  (`JSONExporter`/`JSONLExporter`, where it matters most for large
  crawls) was still on stdlib `json`. Now uses `orjson` when available
  (optional import, stdlib fallback, so it stays an optional accelerant
  rather than a hard dependency).
- **curl → Request** (`bitscrape.curlparse.curl_to_request`): paste a
  browser's "Copy as cURL" output, get back a `Request` with the same
  method/headers/body/cookies. Exposed in `bitscrape shell` as
  `from_curl(cmd)`.
- **View in browser**: `bitscrape shell` now has a `view()` helper that
  opens the fetched (or any) response's HTML in your actual browser for
  visual inspection.
- **`bitscrape get <url>`**: one-shot fetch-and-print, no spider and no
  interactive shell needed — `--as markdown|html|text|structured` and
  `--css SELECTOR`. This was the explicitly-requested "scrape a URL
  without writing a single line of code."
- **`bitscrape.fetch_url()`**: a synchronous single-request helper
  alongside the existing `bitscrape.run()` (which is sync-for-a-whole-
  spider; this is sync-for-one-request) — closes the "sync and async
  APIs" technology requirement, which previously only had the async
  `Downloader`/`Engine` plus the whole-spider sync `run()`.
- **Agent Skill** (`agent-skill/SKILL.md`): a ready-to-install skill
  teaching a coding agent bitscrape's current API (spiders, `Request`/
  `Response`, settings that actually do something, the CLI, MCP server,
  RAG pipeline) so generated code matches this version instead of
  guessing from general scraping-library knowledge or a stale training
  snapshot. States the no-anti-bot-bypass rule explicitly so an agent
  using this skill doesn't try to route around it via a differently-named
  setting.

Every item above has real, passing tests (`tests/test_screenshot_capture.py`,
`tests/test_curlparse.py`, `tests/test_fetch_url.py`, new cases in
`tests/test_cli_end_to_end.py` and `tests/test_mcp_server.py`) — 659
passing overall, up from 633, with `ruff`/`mypy` both still fully clean.
Coverage is 92% overall (`pytest --cov`), including `cli/main.py` at 93%
and `exporters/feed.py` at 95% — both were flagged as coverage gaps
(36% and 61% respectively) in `BITSCRAPE_QA_REPORT.md` from an earlier
version.

Also re-verified `BITSCRAPE_QA_REPORT.md`'s one open bug (`Spider.follow()`
not resolving relative URLs) is in fact already fixed and covered by
`tests/test_follow_relative_urls.py` — it was fixed before this line of
work started, so no code change was needed here, just confirmation.

### Known follow-ups (not in this release)

Nothing outstanding from the original feature request. The remaining
gaps are the same ones flagged throughout this changelog as deliberate
non-goals (stealth/anti-bot bypass, TLS/browser fingerprint impersonation,
DNS-leak/proxy-identity concealment) rather than things left undone.

## [0.8.0] — 2026-07-27

Follow-up to an external QA pass (verified independently before acting on
it — see the "How this release was produced" note below) that closed all
five flagged coverage gaps and, in the process of writing real tests for
each, found and fixed **four genuine application bugs** plus **one real
packaging bug**. As always: every fix below has a real, passing test
behind it; nothing here is a documentation-only change pretending to be a
code fix.

### Fixed — real bugs found while closing test coverage gaps

- **`self.follow()` didn't resolve relative URLs against the current
  page.** A long-standing, previously-documented limitation
  (`self.follow("/next")` enqueued the literal string `/next` instead of
  the resolved absolute URL) — now fixed for real. The Engine tracks the
  response currently being parsed via a `contextvars.ContextVar` (set for
  the duration of each callback), and `follow()` resolves relative URLs
  against it with `urljoin()`. **Deliberately not a plain instance
  attribute**: the Engine processes multiple requests *concurrently* via
  `asyncio.create_task()` against one shared `Spider` instance, so a plain
  `self._current_response = response` would let two in-flight requests
  clobber each other's resolution context at an `await` point inside a
  callback. `ContextVar` is correctly isolated per `asyncio` task. Verified
  with a dedicated concurrency-safety test: two pages with *different*
  relative links, deliberately raced against each other (one artificially
  slow), each resolve against their own URL, not the other's.
  (`bitscrape/core/spider.py`, `bitscrape/engine.py`,
  `bitscrape/workflow/graph.py` — the same fix applied to the optional
  LangGraph workflow path for consistency)
- **`XMLExporter` produced invalid, unparseable XML for any item value
  containing `&`, `<`, `>`, or `"`.** (e.g. a scraped title like "Smith &
  Sons" broke the whole output file.) Fixed with `xml.sax.saxutils.escape`.
  Also fixed: dict keys with spaces or special characters (e.g. `"2024
  price ($)"`) produced invalid XML element names — now sanitized into
  valid tag names. (`bitscrape/exporters/feed.py`)
- **`parsel` was never declared as a project dependency**, despite
  `.xpath()` requiring it, being documented throughout `docs/`, and being
  used in the bundled `examples/polite_sitemap_spider.py`. A fresh
  `pip install -e ".[all,cli]"` left `.xpath()` broken with
  `ImportError: XPath requires parsel: pip install parsel` on the default
  `selectolax` backend (which has no native XPath support). Fixed by
  adding `parsel>=1.9` to base dependencies. (`pyproject.toml`)
- **The parsel-native XPath backend (used when `selectolax` is
  unavailable) silently returned no results for `text()`/`@attr`
  queries.** `isinstance(m, str)` was checked against the wrong object —
  `parsel.Selector.xpath()` always wraps results in a `Selector`, even for
  text/attribute queries; the actual string lives at `m.root`, not `m`
  itself. Fixed by checking `m.root`. Only affects installs where
  `selectolax` is genuinely absent (not the default), but a real,
  previously-undetected bug in a documented fallback path.
  (`bitscrape/parser/selector.py`)
- **The optional LangGraph workflow path misclassified follow-up
  `Request`s as scraped items.** `parse_node`'s classification checked
  `hasattr(out, "model_dump")` to detect items, but `Request` is *also* a
  pydantic `BaseModel` (so it also has `model_dump`) — every `Request`
  yielded by a spider was being routed into `items` instead of
  `new_requests`. Fixed by checking `isinstance(out, Request)` first.
  (`bitscrape/workflow/graph.py`)

### Added — real implementations replacing documented stubs / gaps

- **`PostgresStorageBackend` and `PostgresPipeline` are now verified
  against a real, live PostgreSQL server** (previously mock-connection-
  tested only, since no live server was available in earlier build
  environments). A live server became available; added
  `tests/test_postgres_live.py` (connection pooling, real JSONB storage
  and querying, persistence across backend-instance/process restarts,
  concurrent writes, upsert-on-conflict) and expanded
  `tests/test_pipelines.py` (real inserts, real `ON CONFLICT` upserts,
  connection-failure handling).
- **`MongoStorageBackend` is now a real implementation**, no longer a
  `NotImplementedError` stub. Implemented against `motor` (MongoDB's
  official async driver) and tested against `mongomock_motor` — a genuine
  async MongoDB API emulator (the same category of tool as `moto` for
  S3), injected via a new `client=` constructor parameter. A live MongoDB
  server wasn't available (MongoDB needs its own separate apt repository,
  not present in this build environment's package mirror), so this is
  "verified against a real emulator," the same honest tier Postgres was at
  before a live server became available for it. `ElasticsearchStorageBackend`
  remains a documented stub — no comparable, easily-installable ES emulator
  exists to test a real implementation against.
- **`workflow/graph.py` (the optional LangGraph orchestration path) is now
  tested end-to-end against a real local HTTP server** — previously 0%
  coverage since it needs `langgraph` installed, and was untested even
  when the package was present. `langgraph` added as an optional
  `workflow` extra.
- **`cli/main.py`'s subcommands are now tested end-to-end via
  `click.testing.CliRunner`** — `crawl` (JSONL/CSV output, concurrency
  flag, stats summary, failure on a nonexistent spider file), `list`,
  `genspider` (all three templates, output confirmed to be valid Python),
  and `startproject` (structure, valid Python settings file, failure on an
  existing directory).

### Fixed — tooling/config

- **Bumped `[tool.mypy] python_version` from 3.11 to 3.12.** `numpy`
  (pulled in transitively via `networkx`, used by `LinkGraph`) ships inline
  type stubs using newer type-statement syntax that `mypy` can't parse
  under a 3.11 target — a hard parse error that halted the whole `mypy`
  run, not something `ignore_missing_imports` could paper over (the stub
  *is* found; it just can't be parsed at the configured version). 3.12 is
  within this project's supported range (`requires-python = ">=3.11"`) and
  matches the actual runtime used throughout this project's own testing.
- Added `numpy`, `scipy`, `motor`, `mongomock_motor` to the existing
  `ignore_missing_imports` override list, consistent with the pattern
  already used for other optional third-party dependencies.
- Added `pytest-cov` to the `dev` extra for coverage reporting, and
  `mongomock-motor` for testing the new `MongoStorageBackend`.

### Testing

- **440 tests total, all passing** (418 verified-QA baseline + 22 new in
  `tests/test_pipelines.py`), up from 332 at the last verified baseline.
  Overall coverage 93%, up from 80–81%. Per-module improvements on the
  specific gaps flagged: `cli/main.py` 36%→94%, `exporters/feed.py`
  61%→99%, `parser/selector.py` 46%→73%, `pipeline/pipelines.py`
  41%→98%, `workflow/graph.py` 0%→100%.
- ruff clean on the whole tree; mypy clean (`Success: no issues found in
  36 source files`).

### How this release was produced

An external QA report (`BITSCRAPE_QA_REPORT.md`) claimed 332 passing
tests, a mypy fix (missing `pydantic.mypy` plugin), and 5 flagged coverage
gaps. Rather than accepting the report at face value, every claim was
independently re-verified before building on it: the mypy fix was
confirmed genuine by toggling the plugin config off and back on and
watching the errors reappear/disappear; the diff was checked file-by-file
against the claimed change list; two of the nine new tests were read in
full to confirm they measured real behavior, not decorative assertions;
and the full suite was re-run from a genuinely fresh install rather than
trusted from the report's own numbers. Only after that independent
verification passed did this release build on top of it.

## [0.7.0] — 2026-07-24

Simplifying the developer-facing ecosystem: found and fixed a real
consistency bug in the existing top-level API while doing this, on top of
adding a genuinely simpler entry point.

### Fixed

- **`bitscrape.run()` had its own separate, simplified middleware wiring
  that had drifted out of sync with everything else** — most importantly,
  it never included `MetaRobotsMiddleware`, so a page marked
  `<meta name="robots" content="noindex">` would have had its item counted
  as scraped via `bitscrape.run()`, while the CLI (already on
  `build_engine()` since 0.6.0) would have correctly excluded it. Same
  bug-class as the CLI's stale hardcoded `--version` string fixed
  previously. Fixed by making `run()` delegate to `build_engine()` — the
  one function now used by both the CLI and the top-level API, so they
  can't drift apart again. Proven with a regression test: the exact
  noindex scenario, run through `bitscrape.run()`, now correctly reports
  `items_scraped=0, items_noindexed=1`.
- **`bitscrape.__version__` was hardcoded to a stale `"0.1.0"`** — the same
  bug class, at the package level this time instead of just the CLI. Fixed
  to read the real installed version via `importlib.metadata`, matching
  the CLI's existing fix.

### Added

- **`@bitscrape.spider(name=..., start_urls=[...])`** — a decorator that
  turns a plain `async def parse(response): yield ...` function into a full
  `Spider` subclass. The simplest possible entry point for the common case
  (one callback, no spider-level state): no class boilerplate, no `self`.
  For anything needing multiple callbacks or `self.follow()`, a full
  `class MySpider(Spider):` is still there and unchanged.
- **The full 0.3.0–0.6.0 feature set is now importable from the top-level
  `bitscrape` package** — `build_engine`, `Frontier`, `LinkGraph`,
  `RecrawlScheduler`, `canonicalize_url`/`resolve_redirect_chain`/
  `compute_fingerprint`, `BM25Index`/`VectorIndex`/`HybridSearcher`,
  `EntityResolver`, `KnowledgeGraph`, `CrawlMetrics`/`CrawlTracer`/
  `AlertManager`, `StatsMonitor`, `PluginManager`/`BasePlugin`, and the
  storage backends. Previously these needed knowing the exact submodule
  path (`from bitscrape.link_analysis import LinkGraph`); now
  `from bitscrape import LinkGraph` works directly, matching the
  project's existing "one import" design. None of these add a hard
  dependency on optional packages (aiosqlite, boto3, networkx, etc.) at
  `import bitscrape` time — those imports stay lazy, inside the
  functions/methods that actually need them, exactly as before.

### Testing

- 11 new tests in `tests/test_package_api.py`, including the two real
  regressions found and fixed (stale version, missing meta-robots
  middleware in `run()`), and both the function-decorator and class-based
  spider paths verified end-to-end against a real local HTTP server.
- Switched these specific tests to a plain `threading` + `http.server`
  based test server instead of `aiohttp.test_utils.TestServer`, after
  discovering the real reason a first draft of these tests hung: aiohttp's
  `TestServer` is bound to whichever asyncio event loop started it, and
  stops actually processing requests once that loop closes — which is
  exactly what happens when it's accessed from `bitscrape.run()`'s own,
  separate internal `asyncio.run()` call. A plain thread-based server has
  no such event-loop coupling.
- 323 tests total, all passing. ruff clean on the whole tree; mypy clean
  aside from the long-documented pydantic-settings/mypy false positive
  (now also appearing at the two new bare `Settings()` call sites in
  `__init__.py`/`factory.py` — same non-issue, confirmed at runtime by
  every single test in this project that calls `Settings()`).

## [0.6.0] — 2026-07-24

### Architecture: a factory, not a rewrite

A proposal came in to restructure the project around five stacked patterns
at once: Clean/Hexagonal Architecture, an internal event-driven runtime, an
actor-style concurrency model, plugin-first design, and a distributed
scheduler layer. Evaluated and declined, for concrete reasons:

- An event bus adds indirection, not speed — the actual performance here
  comes from `asyncio` + semaphores + connection pooling, already in place.
- Plugin-first and distributed scheduling were already delivered in 0.4.0
  (`PluginManager`, `RedisQueue`, `RedisDupeFilter`,
  `DistributedThrottleMiddleware`) — restating them as a new architecture
  layer would be relabeling, not new capability.
- An actor runtime (mailboxes, supervision trees) solves fault-isolation
  problems at a scale this project isn't at, and would mean maintaining two
  competing concurrency models for no demonstrated benefit over the
  existing `asyncio` task model.
- Stacking five architectural patterns simultaneously is a real risk to a
  312-test, working codebase, for terminology, not a concrete gap.

**What was real in the proposal**: "the same spider runs unchanged from a
laptop to a distributed cluster." That property already existed in pieces
(`Settings` toggles selecting backends, `Scheduler.from_settings()`) but
required hand-assembling ~6 subsystems every time — exactly what the CLI
was doing ad hoc, and what every library user would otherwise have to
copy-paste. That's a real, worth-fixing gap, and it doesn't need a new
pattern to fix — it needs a factory.

### Added

- **`bitscrape.factory.build_engine()`** — the single function that builds
  a fully-wired `Engine` from one `Settings` object: proxy rotation,
  session pooling vs. cookie jar, distributed throttling, robots.txt +
  meta-robots handling, live monitoring, and Prometheus metrics are all
  selected automatically from config toggles. No new abstraction layers —
  it's a plain factory over the dependency injection the codebase already
  uses.
- **`bitscrape.factory.build_middlewares()`** — the middleware-selection
  logic, exposed standalone so it can be inspected/extended without
  reimplementing the selection rules.
- **`Engine.stats`** — a public property for live crawl stats (previously
  only accessible via the private `_stats` attribute), needed so
  `StatsMonitor` can poll a running crawl through the factory wiring.
- New `Settings` fields powering the factory: `proxies`, `proxy_rotate`,
  `monitoring_enabled`/`monitoring_port`, `metrics_enabled`/`metrics_port`.
- **The CLI now uses `build_engine()` itself**, replacing its previous
  ad-hoc middleware assembly — the CLI is now just a caller of the same
  public API a library user gets, rather than a separate, undocumented
  wiring path that could drift from it.

### Testing

- 13 new tests in `tests/test_factory.py`, including the actual claim being
  tested end-to-end: the **same spider class**, run through
  `build_engine()` twice — once with default (in-memory) `Settings`, once
  with `scheduler_use_redis=True` + `distributed_throttle_enabled=True`
  against a real local Redis — both complete a real crawl against a real
  local server with `items_scraped=1, requests_failed=0`. Also verified the
  CLI's new `build_engine()`-based path with a real end-to-end crawl.
- 312 tests total, all passing. ruff clean on the whole tree; mypy clean
  (aside from the long-documented pydantic-settings/mypy false positive in
  `engine.py`, unrelated to this change).

## [0.5.0] — 2026-07-23

Hyperscale search infrastructure: the surrounding systems a large search
engine needs beyond the crawler itself. As throughout this project, every
algorithm claimed as working has a real test behind it. The genuinely
infrastructure-dependent pieces (live Kubernetes clusters, real embedding
models, real alerting-vendor integrations) are shipped as real, valid
artifacts with honest disclosure of what's verified vs. what needs your own
environment — not faked as "tested" when they weren't.

### Added — URL Frontier

- **`Frontier`** (`bitscrape/frontier.py`) — a proper Mercator-style
  two-level frontier: N priority tiers of front queues + one per-domain
  back queue with its own next-allowed-time, so politeness (minimum
  spacing between requests to the same domain) is enforced structurally,
  not just hoped for under concurrency. 17 tests, including real-time
  politeness enforcement and 20-domain round-robin fairness. Complements
  (doesn't replace) `bitscrape.scheduler` — use `Frontier` when you need
  real per-domain politeness guarantees at scale; combine with
  `DistributedThrottleMiddleware` (0.4.0) for the same guarantee across
  worker *processes*, not just within one.

### Added — Link Analysis

- **`LinkGraph`** (`bitscrape/link_analysis.py`) — PageRank and HITS over
  the crawl's discovered hyperlink graph, built on `networkx`'s real
  algorithms (not reimplemented). 14 tests verifying correct relative
  ordering (frequently-cited pages outrank orphans, symmetric graphs score
  symmetrically, scores sum to ~1.0).

### Added — Incremental Recrawling

- **`RecrawlScheduler`** (`bitscrape/recrawl.py`) — schedules next-visit
  time from importance (e.g. a PageRank score) and an estimated
  change-frequency (Laplace-smoothed observed-change-rate, in the spirit of
  Cho & Garcia-Molina's freshness estimation), clamped to configurable
  min/max intervals. 17 tests confirming important and frequently-changing
  pages get shorter intervals than unimportant/static ones.

### Added — Canonicalization

- **`canonicalize_url()`, `resolve_redirect_chain()`,
  `compute_fingerprint()`/`is_near_duplicate()`** (`bitscrape/canonicalize.py`)
  — URL normalization (scheme/host case, default ports, tracking-param
  stripping, fragment/trailing-slash handling), redirect-loop detection,
  and SimHash-based near-duplicate content detection (survives minor edits
  like a changed footer timestamp, unlike exact-hash comparison). 29 tests.

### Added — Semantic Ranking

- **`BM25Index`, `VectorIndex`, `reciprocal_rank_fusion()`, `HybridSearcher`**
  (`bitscrape/ranking.py`) — a correct BM25 implementation, a brute-force
  cosine-similarity vector index, and Reciprocal Rank Fusion to combine
  them. 22 tests including known-correct BM25 properties (rare terms
  weighted higher, more occurrences score higher) and RRF's key property
  (consistent moderate ranking beats one great signal + one absence).
  **Scope note**: doesn't compute embeddings itself — no network access to
  download an embedding model or call an embedding API in this build
  environment. Bring your own embeddings (any model) into `VectorIndex`;
  the fusion/reranking logic is what's reusable and tested here.

### Added — Entity Resolution

- **`EntityResolver`, `similarity()`** (`bitscrape/entity_resolution.py`) —
  heuristic clustering of entity mentions ("Jon Smith" / "John Smith" /
  "J. Smith" -> one cluster) via normalized string similarity + an
  initials-abbreviation check, NOT a trained entity-linking model. 19
  tests. Pairs with `bitscrape.knowledge_graph` (0.3.0): resolve mentions
  first, then use the canonical name as the graph node identity.

### Added — Large-scale Observability

New `bitscrape/observability.py`, on real industry-standard libraries:

- **Metrics**: `CrawlMetrics` (`prometheus_client` counters/histograms) +
  `serve_metrics()` (a real `/metrics` HTTP endpoint). Verified by actually
  scraping it with a real HTTP client and checking Prometheus text format.
- **Tracing**: `CrawlTracer` (OpenTelemetry SDK spans, swappable exporter
  for Jaeger/Tempo/Honeycomb/etc.). Verified with OTel's own in-memory
  exporter — real spans, real parent/child relationships, real status
  codes, not a hand-rolled tracing stand-in.
- **Structured logging**: `JSONLogFormatter` — standard JSON-per-line shape
  for ELK/Loki/Datadog ingestion.
- **Alerting**: `AlertManager` — threshold-rule evaluation with per-rule
  cooldowns and callback dispatch. Decides *when* to alert; delivering to a
  specific vendor (PagerDuty, Slack, a webhook) is your callback's job —
  this isn't a full Alertmanager/PagerDuty client.

19 tests total for this module.

### Added — Cloud-native Infrastructure

New `deploy/` directory:

- **`Dockerfile`** — multi-stage build, non-root user, separate builder
  stage so build toolchains don't bloat the final image.
- **`docker-compose.yml`** — local multi-worker + Redis stack for dev/
  integration testing, with a documented `--scale worker=N` path.
- **`k8s/deployment.yaml`** — Deployment with resource requests/limits,
  readiness/liveness probes, and zone-spread `topologySpreadConstraints`
  (the practical starting point for multi-region resilience before you're
  running fully separate per-region clusters).
- **`k8s/hpa.yaml`** — HorizontalPodAutoscaler on CPU/memory, with
  scale-down stabilization to avoid flapping, plus a documented (commented)
  path to scale on actual crawl-queue-depth instead of just CPU.
- **`k8s/service.yaml`** — Service, ConfigMap, and PodDisruptionBudget.

**Honest disclosure**: no Docker daemon or Kubernetes cluster was available
in this build environment to `docker build` or `kubectl apply
--dry-run=server` against. What IS verified (13 tests in
`tests/test_deploy_manifests.py`): every manifest is syntactically valid
YAML, has the fields a production workload needs (resource limits, health
probes, PDB, HPA stabilization), and that label selectors actually match
across files (a very common real-world misconfiguration). Run `docker
compose config` and `kubectl apply --dry-run=server` against your own
environment before deploying.

### Testing

- **299 tests total, all passing** (149 from 0.4.0 + 150 new), across 8 new
  test files. ruff clean; mypy clean on every new file.
- **Fixed a reproducibility gap found during this release's own
  verification**: `pyproject.toml`'s `dev` extras weren't version-pinned, so
  a fresh install could grab a newer `ruff` with stricter default rules,
  producing lint findings on *pre-existing, untouched* code that the
  originally-verified version didn't flag. Pinned exact versions
  (`ruff==0.15.22`, `mypy==2.3.0`, `pytest==9.1.1`, `pytest-asyncio==1.4.0`,
  `moto[s3]==5.2.2`, `pyyaml==6.0.3`) so `pip install -e ".[dev]"` now
  reproduces the exact toolchain this release was verified against.

## [0.4.0] — 2026-07-22

A large feature release covering distributed crawling, performance, storage,
extensibility, monitoring, and knowledge-graph support. As throughout this
project, everything claimed as "working" has a real test behind it — against
a real local Redis instance, a real local HTTP server, a real on-disk
SQLite database, or a real S3 API emulation (moto), not just mocks. Where
something couldn't be verified for real in this build environment (no
network access to a package mirror for Postgres/MongoDB/Elasticsearch, no
browser binary for Playwright), that's disclosed explicitly rather than
claimed as tested.

Explicitly NOT included, on purpose: fingerprint/TLS spoofing, canvas/WebGL
evasion, or CAPTCHA-solving/anti-bot integrations. These are tooling whose
specific purpose is defeating a site's anti-bot protections against its
wishes -- a different category from the legitimate infrastructure below,
and not something this project ships regardless of packaging or framing.

### Added — Distributed crawling

- **`DistributedThrottleMiddleware`** — a Redis-backed per-domain lease so
  multiple worker *processes* sharing the same Redis don't independently
  hammer the same domain. Verified with two independent middleware
  instances (standing in for two workers) sharing a real local Redis: one
  genuinely waits out the other's lease. `Settings.distributed_throttle_enabled`.
- Confirmed and test-covered the existing `RedisDupeFilter` as the real
  distributed-dedup mechanism: 8 tests against real Redis, including two
  independent filter instances agreeing on already-seen URLs — the actual
  cross-worker guarantee "distributed crawling" needs.
  (`bitscrape/scheduler/dupefilter.py`)

### Added — Performance

- **`AutoThrottle`** — adaptive, latency-based per-domain delay (same idea
  as Scrapy's AUTOTHROTTLE). Verified live: hitting a server that's
  deliberately slow then fast, the effective delay measurably rises during
  the slow phase and falls back down during the fast phase.
  `Settings.autothrottle_enabled`, `autothrottle_start_delay`,
  `autothrottle_max_delay`, `autothrottle_target_concurrency`.
  (`bitscrape/downloader/downloader.py`)
- **`SessionPoolMiddleware`** — a pool of N independent cookie-jar sessions
  per domain (round-robin or sticky via `request.meta["session_id"]`),
  with optional auto-rotation after N requests. Supersedes the single-jar
  `CookieMiddleware` when you need several parallel sessions.
  `Settings.session_pool_size`, `session_rotate_every`.
- **`BrowserPool`** — reuses `playwright_pool_size` browser *processes*
  across requests instead of launching a fresh one every time (only a
  cheap browser *context* is created per request, keeping cookie/storage
  isolation). Verified with fake-but-API-shaped Playwright objects
  (no browser binary was available in this build environment), including a
  genuine concurrency-bounding test proving a 3rd concurrent request
  actually waits for a pool slot rather than the pool being decorative.
  `Settings.playwright_pool_enabled`. (`bitscrape/downloader/downloader.py`)

### Added — Storage backends

New `bitscrape/storage/backends.py`, a pluggable `BaseStorageBackend`
interface:

- **`SQLiteStorageBackend`** — fully tested against a real on-disk database
  (via `aiosqlite`). The reference implementation.
- **`S3StorageBackend`** — tested against a real S3 API emulation (`moto`,
  not a hand-rolled mock), including verifying the actual objects written.
  Works with any S3-compatible endpoint (AWS, MinIO, R2, etc.) via
  `endpoint_url`.
- **`PostgresStorageBackend`** — implemented against the real `asyncpg` API
  (JSONB column, connection pooling) and unit-tested with a mock connection.
  No live Postgres server was available in this build environment's package
  mirror to test against for real — treat as "implemented, needs your own
  integration test," not "verified end-to-end" like SQLite/S3.
- **`MongoStorageBackend` / `ElasticsearchStorageBackend`** — honest
  documented stubs. Each raises `NotImplementedError` with a full
  implementation sketch in its docstring (the real `motor`/
  `elasticsearch-py` calls to wire up), rather than pretending to work.
  Their engines require package repositories outside this build
  environment's allowed mirror.

### Added — Extensibility

- **Plugin/hook architecture** (`bitscrape/plugins.py`): `PluginManager`
  fires named lifecycle events (`spider_opened`, `spider_closed`,
  `request_scheduled`, `response_received`, `item_scraped`, `item_dropped`,
  `error`); `BasePlugin` for convenient subclassing. Wired into the real
  `Engine` — verified with a live end-to-end crawl against a real local
  server, confirming every hook actually fires in the right order.
  A broken plugin callback is logged and does not stop the crawl.
- Two example plugins built on this architecture: **`BearerTokenAuthPlugin`**
  (attaches an `Authorization: Bearer` header to requests for a given
  domain — a legitimate "I already have credentials for a site I'm
  authorized to access" helper) and **`StorageConnectorPlugin`** (streams
  every scraped item into any `BaseStorageBackend` as it's scraped).
  Deliberately not included: CAPTCHA-solving or anti-bot-evasion plugins.

### Added — Monitoring

New `bitscrape/monitoring.py`: `StatsMonitor` serves a live JSON feed
(`/stats.json`) and an auto-refreshing HTML view (`/`) of the current
`CrawlStats` plus real process CPU/RAM (via `psutil`). This is a small
local `aiohttp.web` server you run alongside one crawl — explicitly NOT a
hosted, multi-crawl dashboard product. Verified with a real HTTP client
against a real running server, including that stats reflect live changes
(not a frozen snapshot) and that `stop()` actually releases the port.
Wire it up via `monitor.as_plugin()` to start/stop with the spider
lifecycle, or point your own Prometheus/Grafana scraper at `/stats.json`
for real production dashboarding.

### Added — Knowledge graph support

New `bitscrape/knowledge_graph.py`, scoped honestly:

- `extract_entities()` — a heuristic capitalized-phrase extractor (the
  classic "proper noun sequence" baseline). Documented as a rough signal,
  not real NER — it will over- and under-match; pair it with a real NER
  model/LLM for production entity extraction.
- `KnowledgeGraph` — a real, working directed graph (built on `networkx`)
  recording (subject, predicate, object) triples, either reliably from
  structured item fields (`add_item()`) or roughly from free text
  (`add_entities_from_text()`, using the heuristic above). Exports to JSON
  or GraphML (openable in Gephi/Cytoscape for real graph analysis/viz,
  rather than reinventing that here).

### Testing

- **149 tests total, all passing** (52 from 0.3.0 + 97 new), across 8 new
  test files. Notably includes: real local Redis integration (dupefilter +
  distributed throttle), a real local HTTP server proving AutoThrottle
  backs off and recovers, real moto-emulated S3, real on-disk SQLite with
  cross-process persistence, and a full engine-level integration test
  proving plugin hooks fire during an actual crawl.
- ruff clean; mypy clean on every new/modified file (aside from the
  pre-existing pydantic-settings/mypy false-positives already documented
  as unrelated in earlier releases).

## [0.3.0] — 2026-07-22

Added the legitimate crawler-etiquette features that production search/AI
crawlers (Googlebot, ClaudeBot, PerplexityBot) implement beyond basic
robots.txt — as opposed to bot-detection evasion (CAPTCHA solving, TLS/JA3
fingerprint spoofing), which is deliberately out of scope: that's tooling
built to defeat a site's anti-bot protections against its wishes, which sits
in legally gray territory regardless of framing, and isn't something this
patch will implement.

### Added

- **Conditional GET (ETag / Last-Modified).** The downloader now caches
  `ETag`/`Last-Modified` from 200 responses and sends `If-None-Match` /
  `If-Modified-Since` on repeat crawls of the same URL. A `304 Not Modified`
  is served transparently from cache (marked with
  `response.headers["X-Bitscrape-Not-Modified"] = "true"`), so the origin
  server only has to send headers, not the full page again — the same
  courtesy production crawlers extend to sites they crawl repeatedly.
  Controlled by `Settings.conditional_get_enabled` (default `True`).
  (`bitscrape/downloader/downloader.py`)
- **Retry-After-aware backoff.** On a `429`/`503` (or any status in
  `retry_http_codes`), the downloader now reads the server's own
  `Retry-After` header (both delay-seconds and HTTP-date forms, per RFC
  9110) and waits that long instead of blind exponential backoff — capped by
  `Settings.max_retry_after_seconds` (default 120s) so a misbehaving server
  can't stall a crawl indefinitely. Controlled by
  `Settings.respect_retry_after` (default `True`).
  (`bitscrape/downloader/downloader.py`)
- **Meta-robots directives (`noindex`/`nofollow`).** New
  `MetaRobotsMiddleware` reads the `X-Robots-Tag` HTTP header and
  `<meta name="robots" content="...">` (plus a UA-scoped variant like
  `<meta name="googlebot" content="noindex">`) and sets
  `request.meta["noindex"]` / `request.meta["nofollow"]`. The Engine now
  honours these: `noindex` skips counting/exporting items from that page
  (tracked in new `stats.items_noindexed`); `nofollow` skips enqueueing any
  links discovered on that page (tracked in new
  `stats.links_nofollow_skipped`). Fetching itself is never blocked by
  these — only indexing/following — matching real-world semantics.
  Controlled by `Settings.respect_meta_robots` (default `True`). Wired into
  the CLI's default middleware stack. (`bitscrape/middleware/middleware.py`,
  `bitscrape/engine.py`, `bitscrape/cli/main.py`)
- New `CrawlStats.items_noindexed` / `CrawlStats.links_nofollow_skipped`
  fields, surfaced in the CLI's Crawl Stats table.

### Testing

- 52 tests total, all passing (33 from 0.2.1 + 19 new across
  `tests/test_downloader_features.py` and `tests/test_meta_robots.py`).
- Verified end-to-end against a real local server with a real spider: a
  page with `<meta name="robots" content="noindex, nofollow">` produced
  `items_noindexed=1`, `links_nofollow_skipped=1`, `items_scraped=0` —
  and, critically, did **not** trigger the 0.2.1 "0 items, check your
  selectors" diagnostic, since this is an intentional skip, not a broken
  selector.

### Explicitly not implemented (and why)

- CAPTCHA solving, TLS/JA3 fingerprint spoofing, mouse/keystroke simulation,
  or any other bot-detection evasion. Real search/AI crawlers don't need
  these because sites generally welcome them; building tooling whose
  purpose is defeating a site's anti-bot measures is a different thing
  entirely and isn't something this project will add regardless of how
  the request is framed.

## [0.2.1] — 2026-07-22

### Investigated: "Requests: 1, Items scraped: 0" reports

A user reported a crawl completing with `requests=1 failed=0 items=0` (large
download, tens of seconds elapsed). Investigated end-to-end against a real
local server with a real spider:

- **The core crawl → download → parse → item pipeline works correctly.**
  Verified live: a spider with matching selectors against a test page
  produced `requests=1 failed=0 items=2` and wrote both items to the output
  file, in 0.06s. This isn't a regression from the 0.2.0 fixes.
- **Root cause of the reported pattern: CSS/XPath selectors don't match the
  target page.** Reproduced the *exact* `requests=1 failed=0 items=0`
  signature by pointing a spider with mismatched selectors (e.g. the
  placeholder `div.listing-card` from `examples/infinite_scroll_spider.py`)
  at a real page. This is the single most common cause of "0 items" in any
  scraping framework — the download succeeds, the parse callback runs
  without error, it just finds nothing to select.
- The example spiders in `examples/` use illustrative selectors
  (`div.listing-card`, `h2::text`, etc.) that are **not tied to any real
  site** — they're templates to adapt, not working spiders for
  `example.com`. Pointing them at a real URL as-is will always yield 0
  items regardless of framework correctness.

### Added

- **Zero-yield diagnostic in the engine.** Previously, a callback that ran
  successfully but yielded no items and no follow-up requests produced no
  signal at all beyond the final `items: 0` in the stats table — no
  warning, no hint of why. The engine now logs an explicit warning
  identifying the URL, the callback name, and the response size, with a
  pointer to likely causes (selector mismatch, JS-rendered content needing
  `use_playwright=True`, or an unexpected redirect/robots.txt page).
  Spiders that only yield follow-up `Request`s (e.g. sitemap crawlers) are
  correctly not flagged. (`bitscrape/engine.py`)
- New README "Troubleshooting: 0 items scraped" section (see below).
- `tests/test_engine_diagnostics.py` — 3 new tests covering the warning
  firing, not firing on a normal yield, and not firing on request-only
  yields.

### Testing

- 33 tests total, all passing (30 from 0.2.0 + 3 new).

## [0.2.0] — 2026-07-22

### Fixed

- **Critical: `RobotsMiddleware` was a silent no-op.** The code called
  `parser.feed(text)`, but `urllib.robotparser.RobotFileParser` has no
  `feed()` method — only `.parse(lines)`. Every fetch raised `AttributeError`,
  silently swallowed by a broad `except Exception`, so `robotstxt_obey=True`
  never actually blocked a single request on any site, regardless of the
  site's rules. Fixed by calling `.parse()` correctly.
  (`bitscrape/middleware/middleware.py`)
- **`MemoryQueue` crashed on concurrent same-priority requests.**
  `asyncio.PriorityQueue` fell through to comparing `Request` objects
  directly whenever two requests shared a priority tier, and `Request`
  defines no `__lt__`, raising
  `TypeError: '<' not supported between instances of 'Request' and 'Request'`.
  This broke the most common crawl pattern (homepage → many same-priority
  links). Fixed with a monotonic tiebreaker counter:
  `(priority, counter, request)`. (`bitscrape/scheduler/scheduler.py`)
- **robots.txt fetch failures now fail safe, not open.** A 4xx (including
  404) is still treated as "no robots.txt published" → unrestricted access.
  Any other failure (timeout, DNS error, 5xx) now blocks requests to that
  domain until a fetch succeeds, instead of silently disabling enforcement.

- **`bitscrape --version` reported a stale hardcoded string.** It printed
  `"0.1.0"` unconditionally regardless of what was actually installed (e.g.
  still showing `0.1.0` after installing this `0.2.0` fork). Fixed to read
  the real installed distribution version via `importlib.metadata`.
  (`bitscrape/cli/main.py`)

### Added

- `Crawl-delay` and `Sitemap` directives are now surfaced from robots.txt via
  `RobotFileParser`'s own native `.crawl_delay()` / `.site_maps()` methods,
  populated into `request.meta["crawl_delay"]` / `request.meta["sitemaps"]`.
- `ProxyMiddleware` — per-request proxy rotation (round-robin or random),
  mirroring `UserAgentMiddleware`'s interface. Wired into both the `aiohttp`
  and Playwright fetch paths via `request.meta["proxy"]`.
- `scroll_to_bottom()` helper in `bitscrape/downloader/downloader.py` — a
  reusable infinite-scroll / "load more" driver for the Playwright path, with
  optional `click_selector` support for button-triggered pagination. Enabled
  per-request via `request.meta["scroll"] = True` (or a dict of overrides).
- `Settings.queue_max_size` — optional backpressure cap for `MemoryQueue`
  (default `0` = unbounded, unchanged behaviour).
- `ProxyMiddleware.add_proxy()` / `.remove_proxy()` for runtime pool
  management (e.g. dropping a proxy after repeated failures).

### Testing

- 30 new tests added across 5 files, all passing:
  `tests/test_scheduler_queue.py`, `tests/test_robots_middleware.py`,
  `tests/test_proxy_middleware.py`, `tests/test_scroll_to_bottom.py`,
  `tests/test_cli_version.py`.

### Not included (out of scope for this release)

- CAPTCHA-solving / bot-detection evasion, TLS/JA3 fingerprint spoofing,
  mouse/keystroke simulation — deliberately excluded; building tooling
  explicitly meant to defeat a site's anti-bot protections is legally
  gray depending on the target site and jurisdiction.
- Distributed lock/coordination for multi-worker `RedisQueue` deployments —
  a real gap, but a bigger design change (lease/heartbeat protocol) than a
  patch release should take on.
- Credential vault / OAuth / CSRF flows for authenticated crawling —
  application-specific and out of scope for a general-purpose fix.

## [0.1.6] — prior release

Baseline this fork is patched against. See upstream
[README](https://github.com/Sudharsansm/Bitscrape) for original feature set.
