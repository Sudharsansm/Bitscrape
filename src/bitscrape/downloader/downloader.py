"""
Bitscrape Downloader
====================
Async HTTP downloader built on aiohttp.
- Per-domain concurrency semaphores (DOWNLOAD_DELAY support)
- Retry on transient errors via tenacity
- Optional Playwright passthrough for JS-rendered pages
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import Callable
from typing import Any

import aiohttp

from bitscrape.browser import (
    LaunchConfig,
    PageRequest,
    XhrCapture,
    load_page,
    playwright_proxy,
    scroll_to_bottom,
    translate_launch_error,
)
from bitscrape.core.models import Request, Response
from bitscrape.core.settings import Settings
from bitscrape.downloader.blocked import BlockedRequestDetector
from bitscrape.downloader.blocking import DomainBlocker
from bitscrape.downloader.devcache import DevModeCache
from bitscrape.downloader.proxy import ProxyRotator
from bitscrape.downloader.throttle import (
    AutoThrottle,
    DomainPacer,
    header_value,
    parse_retry_after,
)
from bitscrape.errors import DynamicFetchError
from bitscrape.sessions import (
    DEFAULT_SESSION_ID,
    BaseSession,
    BrowserSession,
    HttpSession,
    SessionManager,
    _BuiltinHttpSession,
)

logger = logging.getLogger(__name__)

_UNSET: Any = object()

#: Session "kinds" this downloader knows how to route. ``impersonate`` and
#: ``stealth`` are intentionally not in this set -- see
#: ``_resolve_session_kind`` for why.
_KNOWN_SESSION_KINDS = frozenset({"http", "browser"})

#: Session kinds that exist in ``Settings``/``Request`` as a declared option
#: (for forward API compatibility) but are deliberately unimplemented:
#: TLS/browser fingerprint impersonation and anti-detection "stealth"
#: hardening are tooling built specifically to defeat a site's anti-bot
#: protections, which this project has repeatedly declined to build (see
#: ROADMAP.md's "Explicit non-goals"). Raising a clear error here beats
#: silently downgrading to a plain request and letting a spider author
#: believe protection they didn't get was applied.
_UNIMPLEMENTED_SESSION_KINDS = frozenset({"impersonate", "stealth"})


class BrowserPool:
    """
    Reuses ``pool_size`` Playwright browser instances across requests instead
    of launching a fresh browser for every fetch. Launching a browser process
    is by far the most expensive part of the Playwright path (typically
    1-3+ seconds); a new browser CONTEXT (cheap, isolated cookies/storage
    per request) is still created per request so requests don't leak
    cookies/state into each other -- only the underlying browser PROCESS is
    shared and reused.

    Usage:
        pool = BrowserPool(playwright_browser="chromium", headless=True, pool_size=2)
        await pool.start()
        async with pool.acquire_context(proxy=..., user_agent=...) as context:
            page = await context.new_page()
            ...
        await pool.stop()
    """

    def __init__(
        self,
        playwright_browser: str,
        headless: bool,
        pool_size: int,
        launch_kwargs: dict[str, Any] | None = None,
    ) -> None:
        self._browser_type_name = playwright_browser
        self._headless = headless
        # Extra ``browser_type.launch()`` arguments applied to every pooled browser:
        # ``channel`` (e.g. "chrome"), ``executable_path``, ``args``. Previously the pool
        # launched with ``headless`` only, so ``Settings.browser_channel`` /
        # ``browser_executable_path`` / ``browser_args`` took effect with pooling off
        # and were silently ignored with it on.
        self._launch_kwargs = dict(launch_kwargs or {})
        self._pool_size = max(1, pool_size)
        self._playwright: Any = None
        self._browsers: asyncio.Queue[Any] = asyncio.Queue()
        self._all_browsers: list[Any] = []
        self._started = False

    async def start(self) -> None:
        if self._started:
            return
        from playwright.async_api import async_playwright

        try:
            self._playwright = await async_playwright().start()
            browser_type = getattr(self._playwright, self._browser_type_name)
            for _ in range(self._pool_size):
                browser = await browser_type.launch(headless=self._headless, **self._launch_kwargs)
                self._all_browsers.append(browser)
                await self._browsers.put(browser)
        except BaseException as exc:
            # Don't leak the browsers already launched, or the Playwright driver,
            # when a later launch fails; and say how to fix a launch problem.
            await self._abort_start()
            cfg = LaunchConfig(
                browser=self._browser_type_name,
                channel=self._launch_kwargs.get("channel"),
                executable_path=self._launch_kwargs.get("executable_path"),
            )
            raise translate_launch_error(exc, cfg) from exc
        self._started = True
        logger.info(
            "BrowserPool started: %d %s instance(s)", self._pool_size, self._browser_type_name
        )

    async def _abort_start(self) -> None:
        for browser in self._all_browsers:
            with contextlib.suppress(Exception):
                await browser.close()
        self._all_browsers.clear()
        self._browsers = asyncio.Queue()
        if self._playwright is not None:
            with contextlib.suppress(Exception):
                await self._playwright.stop()
            self._playwright = None

    async def stop(self) -> None:
        if not self._started:
            return
        for browser in self._all_browsers:
            await browser.close()
        if self._playwright is not None:
            await self._playwright.stop()
        self._all_browsers.clear()
        self._started = False
        logger.info("BrowserPool stopped")

    async def _acquire_browser(self) -> Any:
        return await self._browsers.get()

    async def _release_browser(self, browser: Any) -> None:
        await self._browsers.put(browser)

    def acquire_context(
        self, proxy: dict[str, str] | None = None, **context_kwargs: Any
    ) -> _PooledContext:
        return _PooledContext(self, proxy=proxy, **context_kwargs)

    @property
    def size(self) -> int:
        return self._pool_size

    @property
    def available(self) -> int:
        return self._browsers.qsize()


class _PooledContext:
    """Async context manager: checks out a pooled browser, yields a fresh
    BrowserContext on it, closes only the context (not the browser) on
    exit, and returns the browser to the pool."""

    def __init__(
        self, pool: BrowserPool, proxy: dict[str, str] | None, **context_kwargs: Any
    ) -> None:
        self._pool = pool
        self._proxy = proxy
        self._context_kwargs = context_kwargs
        self._browser: Any = None
        self._context: Any = None

    async def __aenter__(self) -> Any:
        self._browser = await self._pool._acquire_browser()
        kwargs = dict(self._context_kwargs)
        if self._proxy:
            kwargs["proxy"] = self._proxy
        self._context = await self._browser.new_context(**kwargs)
        return self._context

    async def __aexit__(self, *exc: object) -> None:
        if self._context is not None:
            await self._context.close()
        if self._browser is not None:
            await self._pool._release_browser(self._browser)


class DownloadError(Exception):
    """Raised when all retries are exhausted or an unrecoverable error occurs."""


class _RetryableStatus(Exception):
    """Internal signal: response status is in retry_http_codes."""

    def __init__(self, status: int, retry_after: float | None) -> None:
        self.status = status
        self.retry_after = retry_after
        super().__init__(f"HTTP {status}")


_parse_retry_after = parse_retry_after  # historical private name, kept for compatibility


def _settings_launch_kwargs(settings: Settings) -> dict[str, Any]:
    """``browser_executable_path`` / ``browser_channel`` / ``browser_args`` as
    ``browser_type.launch()`` keyword arguments (only those that are set)."""
    kw: dict[str, Any] = {}
    if settings.browser_executable_path:
        kw["executable_path"] = settings.browser_executable_path
    if settings.browser_channel:
        kw["channel"] = settings.browser_channel
    if settings.browser_args:
        kw["args"] = list(settings.browser_args)
    return kw


def _aiohttp_http_version(version: Any) -> str | None:
    """``aiohttp``'s ``HttpVersion(major, minor)`` as ``Response.http_version``
    text: ``"1.1"``, ``"1.0"``, ... (``None`` if it isn't a recognisable pair)."""
    try:
        return f"{version.major}.{version.minor}"
    except AttributeError:
        return None


class Downloader:
    """
    Manages a pool of aiohttp sessions and controls concurrency.
    Call ``open()`` before use and ``close()`` when done.
    """

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._session: aiohttp.ClientSession | None = None
        self._resolver: Any = None
        #: Registry/router of named fetch sessions; built by ``open()``.
        self.sessions: SessionManager | None = None
        # Per-domain semaphores to respect concurrent_requests_per_domain
        # (or a ``domain_concurrency`` override for that domain).
        self._domain_semaphores: dict[str, asyncio.Semaphore] = {}
        self._global_semaphore = asyncio.Semaphore(settings.concurrent_requests)
        # Spaces request start times per domain and enforces cool-downs.
        self._pacer = DomainPacer()
        # Conditional GET cache: url -> {"etag", "last_modified", "body",
        # "headers", "status"}. Lets repeat crawls send If-None-Match /
        # If-Modified-Since and skip re-downloading unchanged pages, the same
        # courtesy production crawlers extend to reduce load on crawled sites.
        # Keyed by (session id, url): two sessions (e.g. two logins) must never
        # be served each other's cached body.
        self._page_cache: dict[tuple[str | None, str], dict[str, Any]] = {}
        self._autothrottle = (
            AutoThrottle(
                start_delay=settings.autothrottle_start_delay,
                max_delay=settings.autothrottle_max_delay,
                target_concurrency=settings.autothrottle_target_concurrency,
                block_backoff=settings.autothrottle_block_backoff,
                backoff_factor=settings.autothrottle_backoff_factor,
                recovery=settings.autothrottle_recovery,
                max_cooldown=settings.max_retry_after_seconds,
            )
            if settings.autothrottle_enabled
            else None
        )
        self._browser_pool: BrowserPool | None = (
            BrowserPool(
                playwright_browser=settings.playwright_browser,
                headless=settings.playwright_headless,
                pool_size=settings.playwright_pool_size,
                launch_kwargs=_settings_launch_kwargs(settings),
            )
            if settings.playwright_pool_enabled
            else None
        )

        # --- Proxy rotation (opt-in: only built when proxies are configured) ---
        self._proxy_rotator: ProxyRotator | None = self._build_proxy_rotator(settings)

        # --- Dev mode: disk cache + replay ---------------------------------
        self._dev_cache: DevModeCache | None = (
            DevModeCache(
                cache_dir=settings.dev_cache_dir,
                ttl=settings.dev_cache_ttl,
                cacheable_statuses=settings.dev_cache_statuses,
            )
            if settings.dev_mode
            else None
        )

        # --- Blocked-request detection --------------------------------------
        self._blocked_detector: BlockedRequestDetector | None = (
            BlockedRequestDetector.from_settings(settings)
            if settings.blocked_detection_enabled
            else None
        )

        # --- Ad / domain / resource-type blocking (browser fetches only) ---
        self._domain_blocker: DomainBlocker | None = (
            DomainBlocker(
                block_ads=settings.block_ads,
                extra_domains=settings.block_domains,
                hosts_file=settings.block_hosts_file,
                block_resource_types=settings.block_resource_types,
            )
            if (
                settings.block_ads
                or settings.block_domains
                or settings.block_resource_types
                or settings.block_hosts_file
            )
            else None
        )

    @staticmethod
    def _build_proxy_rotator(settings: Settings) -> ProxyRotator | None:
        if settings.proxy_file:
            return ProxyRotator.from_file(
                settings.proxy_file,
                strategy=settings.proxy_strategy,
                ban_threshold=settings.proxy_ban_threshold,
                ban_seconds=settings.proxy_ban_seconds,
            )
        if settings.proxies:
            return ProxyRotator(
                settings.proxies,
                strategy=settings.proxy_strategy,
                ban_threshold=settings.proxy_ban_threshold,
                ban_seconds=settings.proxy_ban_seconds,
            )
        return None

    async def open(self, configure: Callable[[SessionManager], None] | None = None) -> None:
        """Open the shared client and build the session registry.

        ``configure`` (typically ``spider.configure_sessions``) is called with
        the ``SessionManager`` after the sessions declared in ``Settings.sessions``
        are registered and before the non-lazy ones are started.
        """
        from bitscrape.dns import make_resolver

        self._resolver = make_resolver(self.settings.dns_over_https, self.settings.doh_url)
        connector = aiohttp.TCPConnector(
            limit=self.settings.concurrent_requests,
            ttl_dns_cache=300,
            enable_cleanup_closed=True,
            **({"resolver": self._resolver} if self._resolver is not None else {}),
        )
        timeout = aiohttp.ClientTimeout(total=self.settings.download_timeout)
        self._session = aiohttp.ClientSession(
            connector=connector,
            timeout=timeout,
            headers={**self.settings.default_headers, "User-Agent": self.settings.user_agent},
        )
        try:
            if self._browser_pool is not None:
                await self._browser_pool.start()

            # Session registry: the built-in "default" session (wrapping the shared
            # client above) is registered first so it serves any request that names
            # no session, unless a session is declared with ``default=True``.
            self.sessions = SessionManager(self.settings)
            self.sessions.add(
                DEFAULT_SESSION_ID, _BuiltinHttpSession(self._session, self.settings), lazy=False
            )
            for sid, cfg in self.settings.sessions.items():
                self.sessions.add_from_config(sid, cfg)
            if configure is not None:
                configure(self.sessions)
            await self.sessions.open()
        except BaseException:
            # Something failed to start (the browser pool, a non-lazy session): don't
            # leak the HTTP client or whatever did start; the caller only sees the error.
            await self.close()
            raise
        logger.info(
            "Downloader opened (max_concurrent=%d, sessions=%s)",
            self.settings.concurrent_requests,
            self.sessions.session_ids,
        )

    async def close(self) -> None:
        if self.sessions is not None:
            await self.sessions.close()
            self.sessions = None
        if self._session:
            await self._session.close()
            self._session = None
        if getattr(self, "_resolver", None) is not None:
            await self._resolver.close()
            self._resolver = None
        if self._browser_pool is not None:
            await self._browser_pool.stop()
        logger.info("Downloader closed")

    # ------------------------------------------------------------------
    # Public fetch entry-point
    # ------------------------------------------------------------------

    async def fetch(self, request: Request) -> Response:
        """
        Fetch a request through the session it names (``request.session``), the
        default session, or -- for ``use_playwright=True`` without a session --
        a throw-away browser. Raises DownloadError if all retries fail.
        """
        session: BaseSession | None = None
        if self.sessions is not None:
            session = await self.sessions.resolve(request)
        use_playwright = (
            session.kind == "browser" if session is not None else request.use_playwright
        )
        if use_playwright and self.settings.browser_stealth:
            raise NotImplementedError(
                "settings.browser_stealth=True requests anti-detection 'stealth' "
                "hardening, which bitscrape deliberately does not implement -- "
                "see ROADMAP.md's 'Explicit non-goals'."
            )
        if not use_playwright and self.settings.impersonate:
            raise NotImplementedError(
                "settings.impersonate is set, requesting TLS/browser fingerprint "
                "impersonation, which bitscrape deliberately does not implement -- "
                "see ROADMAP.md's 'Explicit non-goals'."
            )
        if self.settings.dns_leak_protection:
            raise NotImplementedError(
                "settings.dns_leak_protection is set. Per its own docstring this "
                "means locking the browser's local DNS resolver and suppressing "
                "WebRTC to keep a proxy's identity from leaking -- i.e. evading "
                "site-side detection of automated/proxied traffic, which "
                "bitscrape deliberately does not implement -- see ROADMAP.md's "
                "'Explicit non-goals'."
            )

        sid = session.sid if session is not None else None

        if self._dev_cache is not None:
            cached = self._dev_cache.get(request.method, request.url, request.body)
            if cached is not None:
                logger.debug("Dev-mode cache hit for %s", request.url)
                return Response(
                    url=cached["url"],
                    status=cached["status"],
                    headers={**cached["headers"], "X-Bitscrape-Dev-Cache": "true"},
                    body=cached["body"],
                    request=request,
                    elapsed_ms=0.0,
                    encoding=cached["encoding"],
                    session_id=sid,
                )

        gate = session.semaphore if session is not None else None
        try:
            async with gate if gate is not None else contextlib.nullcontext():
                if session is None:
                    response = await self._fetch_browser_paced(
                        request, lambda: self._fetch_playwright(request)
                    )
                elif isinstance(session, BrowserSession):
                    response = await self._fetch_browser_paced(
                        request, lambda: self._fetch_browser_session(request, session)
                    )
                else:
                    response = await self._fetch_http(request, session)
        except Exception:
            if session is not None:
                session.failures_count += 1
            raise
        if session is not None:
            session.requests_count += 1
        response.session_id = sid

        if self._dev_cache is not None:
            self._dev_cache.put(
                request.method,
                request.url,
                response.status,
                response.headers,
                response.body,
                response.encoding,
                request_body=request.body,
            )
        return response

    # ------------------------------------------------------------------
    # HTTP fetch
    # ------------------------------------------------------------------

    async def _fetch_http(self, request: Request, session: BaseSession | None = None) -> Response:
        assert self._session is not None, "Call open() first"
        http = self._as_http_session(session)
        domain = _domain(request.url)

        # Pace *before* taking the global slot: a domain that is cooling down
        # must not hog capacity that other domains could be using.
        async with self._domain_semaphore(domain):
            await self._pace(request, domain)
            async with self._global_semaphore:
                return await self._fetch_http_with_blocked_retry(request, domain, http)

    def _as_http_session(self, session: BaseSession | None) -> HttpSession:
        if session is None:
            assert self.sessions is not None
            session = self.sessions.get(DEFAULT_SESSION_ID)
        assert isinstance(session, HttpSession), f"{session!r} is not an HTTP session"
        return session

    async def _fetch_http_with_blocked_retry(
        self, request: Request, domain: str, session: HttpSession
    ) -> Response:
        """Wraps ``_do_fetch`` (which already handles transient-error retries)
        with an outer retry loop for *blocked* responses -- rotating to a
        different proxy (if configured) and backing off between attempts."""
        proxy_url: str | None = None
        blocked_attempt = 0
        # Proxy precedence: request.meta["proxy"] > session_kwargs["proxy"] >
        # session.proxy > session.proxy_rotator > the downloader's global rotator.
        rotator = session.proxy_rotator or self._proxy_rotator
        static_proxy = request.session_kwargs.get("proxy") or session.proxy

        while True:
            if blocked_attempt:
                # A retry after a block is still a request to the same domain:
                # honour the (possibly just-doubled) delay and any cool-down.
                await self._pace(request, domain)
            req_to_use = request
            proxy_url = None
            if "proxy" not in request.meta:
                if static_proxy:
                    req_to_use = request.model_copy(
                        update={"meta": {**request.meta, "proxy": static_proxy}}
                    )
                elif rotator is not None:
                    proxy_url = rotator.get_proxy(domain)
                    if proxy_url is not None:
                        req_to_use = request.model_copy(
                            update={"meta": {**request.meta, "proxy": proxy_url}}
                        )

            try:
                response = await self._do_fetch(req_to_use, session)
            except DownloadError:
                if proxy_url is not None and rotator is not None:
                    rotator.report_failure(proxy_url)
                raise

            if self._blocked_detector is not None and self._blocked_detector.is_blocked(
                response.status, response.body
            ):
                if proxy_url is not None and rotator is not None:
                    rotator.report_failure(proxy_url)
                # A status already in autothrottle_backoff_codes was counted in
                # _do_fetch; this catches the rest (403, ban pages served as 200).
                if response.status not in self.settings.autothrottle_backoff_codes:
                    self._record_throttle(
                        domain,
                        request,
                        response.elapsed_ms / 1000,
                        status=response.status,
                        headers=response.headers,
                        blocked=True,
                    )
                blocked_attempt += 1
                if blocked_attempt > self._blocked_detector.max_retries:
                    logger.warning(
                        "Giving up on %s after %d blocked-response retries (status=%d)",
                        request.url,
                        blocked_attempt - 1,
                        response.status,
                    )
                    return response.model_copy(
                        update={
                            "headers": {**response.headers, "X-Bitscrape-Blocked": "true"},
                        }
                    )
                wait = self._blocked_detector.backoff_seconds(blocked_attempt)
                logger.warning(
                    "Blocked response for %s (status=%d) — retry %d/%d in %.0fs",
                    request.url,
                    response.status,
                    blocked_attempt,
                    self._blocked_detector.max_retries,
                    wait,
                )
                await asyncio.sleep(wait)
                continue

            if proxy_url is not None and rotator is not None:
                rotator.report_success(proxy_url)
            return response

    async def _fetch_browser_paced(self, request: Request, fetch: Callable[[], Any]) -> Response:
        """Run a browser fetch under the same per-domain pacing and AutoThrottle
        feedback as HTTP fetches (they previously bypassed both)."""
        domain = _domain(request.url)
        await self._pace(request, domain)
        t0 = time.monotonic()
        try:
            response: Response = await fetch()
        except Exception:
            self._record_throttle(domain, request, time.monotonic() - t0, error=True)
            raise
        self._record_throttle(
            domain,
            request,
            time.monotonic() - t0,
            status=response.status,
            headers=response.headers,
        )
        return response

    async def _do_fetch(self, request: Request, session: HttpSession | None = None) -> Response:
        http = self._as_http_session(session)
        client = http.client
        assert client is not None, "Call open() first"
        sk = request.session_kwargs
        cache_key = (http.sid, request.url)
        t0 = time.monotonic()
        attempt = 0
        last_exc: Exception | None = None
        domain = _domain(request.url)

        conditional_headers: dict[str, str] = {}
        cached = self._page_cache.get(cache_key)
        if cached and self.settings.conditional_get_enabled:
            if cached.get("etag"):
                conditional_headers["If-None-Match"] = cached["etag"]
            if cached.get("last_modified"):
                conditional_headers["If-Modified-Since"] = cached["last_modified"]

        # Redirect / cookie / timeout options: per-request session_kwargs >
        # session defaults > Settings.
        follow = sk.get("follow_redirects")
        if follow is None:
            follow = (
                http.follow_redirects
                if http.follow_redirects is not None
                else self.settings.follow_redirects
            )
        extra_kwargs: dict[str, Any] = {}
        cookies = {**request.cookies, **sk.get("cookies", {})}
        if cookies:
            extra_kwargs["cookies"] = cookies
        if sk.get("timeout"):
            extra_kwargs["timeout"] = aiohttp.ClientTimeout(total=float(sk["timeout"]))

        while attempt <= request.max_retries:
            # Latency of *this attempt* -- not the whole fetch, which would
            # fold earlier attempts' retry sleeps into AutoThrottle's estimate.
            attempt_t0 = time.monotonic()
            try:
                async with client.request(
                    method=request.method,
                    url=request.url,
                    headers={**sk.get("headers", {}), **request.headers, **conditional_headers},
                    data=request.body,
                    allow_redirects=follow,
                    max_redirects=self.settings.max_redirect_count,
                    proxy=request.meta.get("proxy"),
                    **extra_kwargs,
                ) as resp:
                    elapsed = (time.monotonic() - t0) * 1000

                    if resp.status == 304 and cached and self.settings.conditional_get_enabled:
                        # Not modified since our last crawl: serve the cached
                        # body instead of a (near-)empty 304 payload, so the
                        # spider sees the same content transparently, while
                        # the origin server only had to send headers.
                        logger.debug(
                            "GET %s → 304 Not Modified (served from cache, %.0fms)",
                            request.url,
                            elapsed,
                        )
                        self._record_throttle(
                            domain, request, time.monotonic() - attempt_t0, status=304
                        )
                        return Response(
                            url=str(resp.url),
                            status=200,
                            headers={**cached["headers"], "X-Bitscrape-Not-Modified": "true"},
                            body=cached["body"],
                            request=request,
                            elapsed_ms=elapsed,
                            encoding=cached.get("encoding", "utf-8"),
                        )

                    body = await resp.read()
                    response = Response(
                        url=str(resp.url),
                        status=resp.status,
                        headers=dict(resp.headers),
                        body=body,
                        request=request,
                        elapsed_ms=elapsed,
                        encoding=resp.charset or "utf-8",
                        http_version=_aiohttp_http_version(resp.version),
                    )
                    # Feed AutoThrottle first: a 429/503 must slow the whole
                    # domain down (and start any Retry-After cool-down) even
                    # when this particular request is about to be retried.
                    self._record_throttle(
                        domain,
                        request,
                        time.monotonic() - attempt_t0,
                        status=resp.status,
                        headers=resp.headers,
                    )
                    if resp.status in self.settings.retry_http_codes:
                        retry_after = _parse_retry_after(resp.headers.get("Retry-After"))
                        raise _RetryableStatus(resp.status, retry_after)

                    if (
                        resp.status == 200
                        and self.settings.conditional_get_enabled
                        and (resp.headers.get("ETag") or resp.headers.get("Last-Modified"))
                    ):
                        self._page_cache[cache_key] = {
                            "etag": resp.headers.get("ETag"),
                            "last_modified": resp.headers.get("Last-Modified"),
                            "body": body,
                            "headers": dict(resp.headers),
                            "encoding": resp.charset or "utf-8",
                        }

                    logger.debug("GET %s → %d (%.0fms)", request.url, resp.status, elapsed)
                    return response

            except _RetryableStatus as exc:
                attempt += 1
                last_exc = exc
                if attempt <= request.max_retries:
                    wait = self._backoff_seconds(attempt, exc.retry_after)
                    reason = (
                        f"HTTP {exc.status}, server requested {exc.retry_after:.0f}s"
                        if exc.retry_after is not None
                        else f"HTTP {exc.status}"
                    )
                    logger.warning(
                        "Retry %d/%d for %s (%s) — waiting %.0fs",
                        attempt,
                        request.max_retries,
                        request.url,
                        reason,
                        wait,
                    )
                    await asyncio.sleep(wait)

            except (TimeoutError, asyncio.TimeoutError, aiohttp.ClientError) as exc:
                self._record_throttle(domain, request, time.monotonic() - attempt_t0, error=True)
                attempt += 1
                last_exc = exc
                if attempt <= request.max_retries:
                    wait = self._backoff_seconds(attempt, None)
                    logger.warning(
                        "Retry %d/%d for %s (%s) — waiting %.0fs",
                        attempt,
                        request.max_retries,
                        request.url,
                        exc,
                        wait,
                    )
                    await asyncio.sleep(wait)

        raise DownloadError(
            f"All {request.max_retries} retries failed for {request.url}"
        ) from last_exc

    def _backoff_seconds(self, attempt: int, retry_after: float | None) -> float:
        """
        Prefer the server's own requested backoff (Retry-After) when present
        and enabled; otherwise fall back to capped exponential backoff.
        """
        if retry_after is not None and self.settings.respect_retry_after and retry_after >= 0:
            return min(retry_after, self.settings.max_retry_after_seconds)
        return min(2**attempt, 30)

    # ------------------------------------------------------------------
    # Browser-session fetch (shared, persistent context)
    # ------------------------------------------------------------------

    async def _fetch_browser_session(self, request: Request, session: BrowserSession) -> Response:
        """Fetch through a :class:`BrowserSession`: one tab in the session's
        long-lived context (cookies/logins persist), closed afterwards.

        Effective options: ``request.session_kwargs`` > session > ``Settings``.
        ``request.meta["scroll"]`` / ``["screenshot"]`` still work as before.
        The page work itself is :func:`bitscrape.browser.load_page`, shared with
        ``DynamicFetcher``. A browser failure becomes a ``DownloadError`` so it
        takes the engine's normal failure path (warning, errback, per-session
        failure count, ``process_exception`` middleware).
        """
        sk = request.session_kwargs
        opts = session.resolve_options(self.settings, sk)
        cookies = {**request.cookies, **sk.get("cookies", {})}
        page_request = PageRequest(
            url=request.url,
            timeout=float(opts["timeout"]),
            wait_until=opts["wait_until"],
            wait_selector=opts["wait_selector"],
            wait_selector_state=opts["wait_selector_state"],
            wait=float(opts["wait"]),
            scroll=sk.get("scroll", request.meta.get("scroll")),
            screenshot=sk.get("screenshot", request.meta.get("screenshot")),
            capture_xhr=opts["capture_xhr"],
            pierce_shadow=bool(sk.get("pierce_shadow", opts["pierce_shadow"])),
            headers={**sk.get("headers", {}), **request.headers},
            cookies=cookies or None,
            page_setup=session.page_setup,
            page_action=session.page_action,
            blocker=session.blocker if session.blocker is not None else self._domain_blocker,
        )
        try:
            result = await load_page(session.runtime, page_request)
        except DynamicFetchError as exc:
            # A session on a rotator's proxy can't switch off it mid-session
            # (see BrowserSession.proxy_rotator), but this still counts toward
            # the ban threshold, so the *next* launch avoids a proxy that's
            # started failing.
            if session.proxy_rotator is not None and session.resolved_proxy:
                session.proxy_rotator.report_failure(str(session.resolved_proxy))
            raise DownloadError(str(exc)) from exc
        if session.proxy_rotator is not None and session.resolved_proxy:
            session.proxy_rotator.report_success(str(session.resolved_proxy))
        return Response(
            url=result.url or request.url,
            status=result.status,
            headers=result.headers,
            body=result.body,
            request=request,
            elapsed_ms=result.elapsed_ms,
            encoding=result.encoding,
            extra=result.extra,
        )

    # ------------------------------------------------------------------
    # Playwright fetch
    # ------------------------------------------------------------------

    async def _fetch_playwright(self, request: Request) -> Response:
        t0 = time.monotonic()
        domain = _domain(request.url)

        proxy_url = request.meta.get("proxy")
        if proxy_url is None and self._proxy_rotator is not None:
            proxy_url = self._proxy_rotator.get_proxy(domain)
        proxy_kwarg = playwright_proxy(proxy_url)

        # Infinite-scroll support: request.meta["scroll"] = True (use defaults)
        # or a dict of overrides, e.g. {"max_scrolls": 20, "pause_ms": 500}.
        scroll_opts = request.meta.get("scroll")

        # Screenshot capture: request.meta["screenshot"] = True (full-page
        # PNG) or a dict of Playwright screenshot() kwargs, e.g.
        # {"full_page": False, "type": "jpeg"}.
        screenshot_opts = request.meta.get("screenshot")

        try:
            if self._browser_pool is not None:
                resp, body, captured_xhr, screenshot = await self._fetch_playwright_pooled(
                    request, proxy_kwarg, scroll_opts, screenshot_opts
                )
            else:
                resp, body, captured_xhr, screenshot = await self._fetch_playwright_unpooled(
                    request, proxy_kwarg, scroll_opts, screenshot_opts
                )
        except Exception:
            if proxy_url is not None and self._proxy_rotator is not None:
                self._proxy_rotator.report_failure(proxy_url)
            raise
        if proxy_url is not None and self._proxy_rotator is not None:
            self._proxy_rotator.report_success(proxy_url)

        elapsed = (time.monotonic() - t0) * 1000
        extra: dict[str, Any] = {}
        if captured_xhr:
            extra["captured_xhr"] = captured_xhr
        if screenshot is not None:
            extra["screenshot"] = screenshot
        return Response(
            url=request.url,
            status=resp.status if resp else 200,
            headers=dict(resp.headers) if resp else {},
            body=body,
            request=request,
            elapsed_ms=elapsed,
            encoding="utf-8",
            extra=extra,
        )

    def _browser_context_kwargs(self, request: Request) -> dict[str, Any]:
        """Shared ``new_context()``/persistent-context kwargs built from
        ``settings.browser_*``, independent of pooled vs. unpooled launch."""
        kwargs: dict[str, Any] = {
            "extra_http_headers": request.headers or {},
            "user_agent": self.settings.user_agent,
        }
        if self.settings.browser_locale:
            kwargs["locale"] = self.settings.browser_locale
        if self.settings.browser_timezone:
            kwargs["timezone_id"] = self.settings.browser_timezone
        if self.settings.browser_viewport:
            w, h = self.settings.browser_viewport
            kwargs["viewport"] = {"width": w, "height": h}
        if self.settings.browser_storage_state:
            kwargs["storage_state"] = self.settings.browser_storage_state
        return kwargs

    async def _maybe_apply_blocking_and_capture(
        self, page: Any, blocker: Any = _UNSET, capture: Any = _UNSET
    ) -> XhrCapture:
        """Wires up domain/ad/resource blocking and background XHR/fetch
        capture on a freshly-created page. Returns the :class:`XhrCapture`
        (``.entries`` is the live list; ``await .drain()`` before closing the
        page so bodies still streaming in aren't lost). ``blocker`` / ``capture``
        default to the global settings."""
        if blocker is _UNSET:
            blocker = self._domain_blocker
        if blocker is not None:
            await page.route("**/*", blocker.route_handler)
        xhr = XhrCapture(self.settings.browser_capture_xhr if capture is _UNSET else capture)
        xhr.attach(page)
        return xhr

    @staticmethod
    async def _maybe_screenshot(page: Any, screenshot_opts: Any) -> bytes | None:
        """Takes a screenshot if requested via ``request.meta["screenshot"]``
        (``True`` for full-page PNG defaults, or a dict of Playwright
        ``page.screenshot()`` kwargs). Returns the raw image bytes, or
        ``None`` if not requested."""
        if not screenshot_opts:
            return None
        opts = dict(screenshot_opts) if isinstance(screenshot_opts, dict) else {}
        opts.setdefault("full_page", True)
        result: bytes = await page.screenshot(**opts)
        return result

    async def _fetch_playwright_pooled(
        self,
        request: Request,
        proxy_kwarg: dict[str, str] | None,
        scroll_opts: Any,
        screenshot_opts: Any = None,
    ) -> tuple[Any, bytes, list[dict[str, Any]], bytes | None]:
        assert self._browser_pool is not None
        async with self._browser_pool.acquire_context(
            proxy=proxy_kwarg, **self._browser_context_kwargs(request)
        ) as context:
            page = await context.new_page()
            capture = await self._maybe_apply_blocking_and_capture(page)
            resp = await page.goto(request.url, wait_until=self.settings.browser_wait_until)
            if self.settings.browser_wait_selector:
                await page.wait_for_selector(
                    self.settings.browser_wait_selector,
                    timeout=self.settings.browser_timeout * 1000,
                )
            if scroll_opts:
                opts = scroll_opts if isinstance(scroll_opts, dict) else {}
                await scroll_to_bottom(page, **opts)
            screenshot = await self._maybe_screenshot(page, screenshot_opts)
            await capture.drain()
            body = (await page.content()).encode("utf-8")
        return resp, body, capture.entries, screenshot

    async def _fetch_playwright_unpooled(
        self,
        request: Request,
        proxy_kwarg: dict[str, str] | None,
        scroll_opts: Any,
        screenshot_opts: Any = None,
    ) -> tuple[Any, bytes, list[dict[str, Any]], bytes | None]:
        try:
            from playwright.async_api import async_playwright
        except ImportError as err:
            raise ImportError(
                "playwright is required for JS rendering. "
                "Install with: pip install bitscrape[playwright]"
            ) from err

        settings = self.settings
        async with async_playwright() as pw:
            # Connect to an already-running remote browser instead of
            # launching a local one -- e.g. a managed browser provider, or
            # one running on another host, addressed by CDP endpoint.
            if settings.browser_cdp_url:
                browser = await pw.chromium.connect_over_cdp(settings.browser_cdp_url)
                context = await browser.new_context(**self._browser_context_kwargs(request))
                # A remote/CDP browser is externally managed (e.g. by a
                # managed browser provider) -- close only the context we
                # created, never the shared remote browser process itself.
                owns_browser = False
            else:
                browser_type = getattr(pw, settings.playwright_browser)
                launch_kwargs: dict[str, Any] = {"headless": settings.playwright_headless}
                if proxy_kwarg:
                    launch_kwargs["proxy"] = proxy_kwarg
                launch_kwargs.update(_settings_launch_kwargs(settings))

                if settings.browser_user_data_dir:
                    # Persistent context: launch + context are one call, and
                    # the "browser" here is actually the context itself.
                    context = await browser_type.launch_persistent_context(
                        settings.browser_user_data_dir,
                        **launch_kwargs,
                        **self._browser_context_kwargs(request),
                    )
                    browser = context.browser
                    owns_browser = False  # persistent context owns its own lifecycle
                else:
                    browser = await browser_type.launch(**launch_kwargs)
                    context = await browser.new_context(**self._browser_context_kwargs(request))
                    owns_browser = True

            page = await context.new_page()
            capture = await self._maybe_apply_blocking_and_capture(page)
            resp = await page.goto(
                request.url,
                wait_until=settings.browser_wait_until,  # type: ignore[arg-type]
            )
            if settings.browser_wait_selector:
                await page.wait_for_selector(
                    settings.browser_wait_selector, timeout=settings.browser_timeout * 1000
                )

            if scroll_opts:
                opts = scroll_opts if isinstance(scroll_opts, dict) else {}
                await scroll_to_bottom(page, **opts)

            screenshot = await self._maybe_screenshot(page, screenshot_opts)
            await capture.drain()
            body = (await page.content()).encode("utf-8")
            await context.close()
            if owns_browser and browser is not None:
                await browser.close()
        return resp, body, capture.entries, screenshot

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    # ---- politeness: pacing + AutoThrottle feedback --------------------------

    def _domain_semaphore(self, domain: str) -> asyncio.Semaphore:
        sem = self._domain_semaphores.get(domain)
        if sem is None:
            limit = _override_for(self.settings.domain_concurrency, domain)
            sem = self._domain_semaphores[domain] = asyncio.Semaphore(
                max(1, int(limit))
                if limit is not None
                else self.settings.concurrent_requests_per_domain
            )
        return sem

    def _delay_floor(self, domain: str, request: Request | None = None) -> float:
        """Lowest delay allowed for ``domain``: the operator's ``domain_delays`` /
        ``download_delay`` or the site's own robots.txt ``Crawl-delay``
        (``request.meta["crawl_delay"]``), whichever is larger."""
        configured = _override_for(self.settings.domain_delays, domain)
        floor = float(configured) if configured is not None else self.settings.download_delay
        if request is not None:
            try:
                floor = max(floor, float(request.meta.get("crawl_delay") or 0.0))
            except (TypeError, ValueError):
                pass
        return max(0.0, floor)

    def _effective_delay(self, domain: str, request: Request | None = None) -> float:
        floor = self._delay_floor(domain, request)
        if self._autothrottle is not None:
            return self._autothrottle.delay_for(domain, floor)
        return floor

    async def _pace(self, request: Request, domain: str) -> None:
        """Wait until a request to ``domain`` may start. The delay is re-read when
        this request's turn comes, so a back-off recorded meanwhile applies."""
        await self._pacer.wait(domain, lambda: self._effective_delay(domain, request))

    def _record_throttle(
        self,
        domain: str,
        request: Request,
        latency: float,
        *,
        status: int | None = None,
        headers: Any = None,
        error: bool = False,
        blocked: bool = False,
    ) -> None:
        """Feed one finished attempt to AutoThrottle (no-op when it is disabled).

        A block signal is: a status in ``autothrottle_backoff_codes``, a response
        the blocked-request detector flagged, or (``autothrottle_backoff_on_errors``)
        a timeout / connection error. ``Retry-After`` on such a response becomes a
        domain-wide cool-down.
        """
        at = self._autothrottle
        if at is None:
            return
        settings = self.settings
        bad_status = status is not None and status in settings.autothrottle_backoff_codes
        bad_error = error and settings.autothrottle_backoff_on_errors
        ok = not (bad_status or bad_error or blocked)
        retry_after = None
        if not ok and settings.respect_retry_after:
            retry_after = parse_retry_after(header_value(headers, "Retry-After"))
        decision = at.record(
            domain,
            latency,
            ok=ok,
            retry_after=retry_after,
            floor=self._delay_floor(domain, request),
        )
        if decision.cooldown > 0:
            self._pacer.pause(domain, decision.cooldown)

    def throttle_info(self) -> dict[str, Any]:
        """Politeness state, for stats and debugging: learned delay + back-off
        count per domain, seconds spent waiting, cool-downs applied."""
        at = self._autothrottle
        return {
            "enabled": at is not None,
            "domains": at.snapshot() if at is not None else {},
            "backoffs": at.total_backoffs if at is not None else 0,
            "wait_seconds": round(self._pacer.total_wait, 3),
            "cooldowns": self._pacer.cooldowns,
        }


def _override_for(mapping: dict[str, float] | dict[str, int], domain: str) -> float | int | None:
    """Look ``domain`` (a ``host[:port]``) up in a ``{"example.com": value}``
    override map: exact ``host:port``, then bare host, then any parent domain."""
    if not mapping:
        return None
    if domain in mapping:
        return mapping[domain]
    host = domain.rsplit(":", 1)[0] if ":" in domain and not domain.endswith("]") else domain
    if host in mapping:
        return mapping[host]
    parts = host.split(".")
    for i in range(1, len(parts) - 1):
        parent = ".".join(parts[i:])
        if parent in mapping:
            return mapping[parent]
    return None


def _domain(url: str) -> str:
    from urllib.parse import urlparse

    return urlparse(url).netloc
