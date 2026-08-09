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
import logging
import time
from collections import defaultdict
from typing import Any

import aiohttp

from bitscrape.core.models import Request, Response
from bitscrape.core.settings import Settings

logger = logging.getLogger(__name__)


class AutoThrottle:
    """
    Adaptive, latency-based per-domain delay -- the same idea as Scrapy's
    AUTOTHROTTLE: back off when a domain's responses get slow (it's under
    load, or rate-limiting us), speed back up as they recover, targeting a
    roughly constant number of requests in flight per domain rather than a
    fixed delay that's either too cautious on a fast day or too aggressive
    on a slow one.

    Algorithm: after each response, compute
        target_delay = latency_seconds / target_concurrency
    and move the domain's current delay halfway toward it:
        new_delay = (current_delay + target_delay) / 2
    clamped to [0, max_delay]. Starts every domain at ``start_delay``.
    """

    def __init__(
        self,
        start_delay: float = 1.0,
        max_delay: float = 60.0,
        target_concurrency: float = 2.0,
    ) -> None:
        self._start_delay = max(0.0, start_delay)
        self._max_delay = max(0.0, max_delay)
        self._target_concurrency = max(1e-6, target_concurrency)
        self._delays: dict[str, float] = {}

    def get_delay(self, domain: str) -> float:
        return self._delays.get(domain, self._start_delay)

    def update(self, domain: str, latency_seconds: float) -> None:
        target_delay = max(0.0, latency_seconds) / self._target_concurrency
        current = self._delays.get(domain, self._start_delay)
        new_delay = (current + target_delay) / 2
        self._delays[domain] = min(max(new_delay, 0.0), self._max_delay)

    def reset(self, domain: str | None = None) -> None:
        if domain is None:
            self._delays.clear()
        else:
            self._delays.pop(domain, None)


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

    def __init__(self, playwright_browser: str, headless: bool, pool_size: int) -> None:
        self._browser_type_name = playwright_browser
        self._headless = headless
        self._pool_size = max(1, pool_size)
        self._playwright: Any = None
        self._browsers: asyncio.Queue[Any] = asyncio.Queue()
        self._all_browsers: list[Any] = []
        self._started = False

    async def start(self) -> None:
        if self._started:
            return
        from playwright.async_api import async_playwright

        self._playwright = await async_playwright().start()
        browser_type = getattr(self._playwright, self._browser_type_name)
        for _ in range(self._pool_size):
            browser = await browser_type.launch(headless=self._headless)
            self._all_browsers.append(browser)
            await self._browsers.put(browser)
        self._started = True
        logger.info(
            "BrowserPool started: %d %s instance(s)", self._pool_size, self._browser_type_name
        )

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


def _parse_retry_after(value: str | None) -> float | None:
    """
    Parse a Retry-After header per RFC 9110: either an integer number of
    seconds, or an HTTP-date. Returns None if absent or unparseable.
    """
    if not value:
        return None
    value = value.strip()
    try:
        seconds = float(value)
        return max(0.0, seconds)
    except ValueError:
        pass
    try:
        from email.utils import parsedate_to_datetime

        target = parsedate_to_datetime(value)
        if target.tzinfo is None:
            from datetime import UTC

            target = target.replace(tzinfo=UTC)
        from datetime import UTC, datetime

        delta = (target - datetime.now(UTC)).total_seconds()
        return max(0.0, delta)
    except (ValueError, TypeError):
        return None


class Downloader:
    """
    Manages a pool of aiohttp sessions and controls concurrency.
    Call ``open()`` before use and ``close()`` when done.
    """

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._session: aiohttp.ClientSession | None = None
        # Per-domain semaphores to respect concurrent_requests_per_domain
        self._domain_semaphores: dict[str, asyncio.Semaphore] = defaultdict(
            lambda: asyncio.Semaphore(settings.concurrent_requests_per_domain)
        )
        self._global_semaphore = asyncio.Semaphore(settings.concurrent_requests)
        self._last_request_time: dict[str, float] = {}
        # Conditional GET cache: url -> {"etag", "last_modified", "body",
        # "headers", "status"}. Lets repeat crawls send If-None-Match /
        # If-Modified-Since and skip re-downloading unchanged pages, the same
        # courtesy production crawlers extend to reduce load on crawled sites.
        self._page_cache: dict[str, dict[str, Any]] = {}
        self._autothrottle = (
            AutoThrottle(
                start_delay=settings.autothrottle_start_delay,
                max_delay=settings.autothrottle_max_delay,
                target_concurrency=settings.autothrottle_target_concurrency,
            )
            if settings.autothrottle_enabled
            else None
        )
        self._browser_pool: BrowserPool | None = (
            BrowserPool(
                playwright_browser=settings.playwright_browser,
                headless=settings.playwright_headless,
                pool_size=settings.playwright_pool_size,
            )
            if settings.playwright_pool_enabled
            else None
        )

    async def open(self) -> None:
        connector = aiohttp.TCPConnector(
            limit=self.settings.concurrent_requests,
            ttl_dns_cache=300,
            enable_cleanup_closed=True,
        )
        timeout = aiohttp.ClientTimeout(total=self.settings.download_timeout)
        self._session = aiohttp.ClientSession(
            connector=connector,
            timeout=timeout,
            headers={"User-Agent": self.settings.user_agent},
        )
        if self._browser_pool is not None:
            await self._browser_pool.start()
        logger.info("Downloader opened (max_concurrent=%d)", self.settings.concurrent_requests)

    async def close(self) -> None:
        if self._session:
            await self._session.close()
            self._session = None
        if self._browser_pool is not None:
            await self._browser_pool.stop()
        logger.info("Downloader closed")

    # ------------------------------------------------------------------
    # Public fetch entry-point
    # ------------------------------------------------------------------

    async def fetch(self, request: Request) -> Response:
        """
        Fetch a request.  Routes to Playwright for JS pages, otherwise aiohttp.
        Raises DownloadError if all retries fail.
        """
        if request.use_playwright:
            return await self._fetch_playwright(request)
        return await self._fetch_http(request)

    # ------------------------------------------------------------------
    # HTTP fetch
    # ------------------------------------------------------------------

    async def _fetch_http(self, request: Request) -> Response:
        assert self._session is not None, "Call open() first"
        domain = _domain(request.url)

        async with self._global_semaphore, self._domain_semaphores[domain]:
            await self._apply_delay(domain)
            return await self._do_fetch(request)

    async def _do_fetch(self, request: Request) -> Response:
        assert self._session is not None
        t0 = time.monotonic()
        attempt = 0
        last_exc: Exception | None = None
        domain = _domain(request.url)

        conditional_headers: dict[str, str] = {}
        cached = self._page_cache.get(request.url)
        if cached and self.settings.conditional_get_enabled:
            if cached.get("etag"):
                conditional_headers["If-None-Match"] = cached["etag"]
            if cached.get("last_modified"):
                conditional_headers["If-Modified-Since"] = cached["last_modified"]

        while attempt <= request.max_retries:
            try:
                async with self._session.request(
                    method=request.method,
                    url=request.url,
                    headers={**request.headers, **conditional_headers},
                    data=request.body,
                    allow_redirects=self.settings.follow_redirects,
                    max_redirects=self.settings.max_redirect_count,
                    proxy=request.meta.get("proxy"),
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
                        if self._autothrottle:
                            self._autothrottle.update(domain, elapsed / 1000)
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
                    )
                    if resp.status in self.settings.retry_http_codes:
                        retry_after = _parse_retry_after(resp.headers.get("Retry-After"))
                        raise _RetryableStatus(resp.status, retry_after)

                    if (
                        resp.status == 200
                        and self.settings.conditional_get_enabled
                        and (resp.headers.get("ETag") or resp.headers.get("Last-Modified"))
                    ):
                        self._page_cache[request.url] = {
                            "etag": resp.headers.get("ETag"),
                            "last_modified": resp.headers.get("Last-Modified"),
                            "body": body,
                            "headers": dict(resp.headers),
                            "encoding": resp.charset or "utf-8",
                        }

                    logger.debug("GET %s → %d (%.0fms)", request.url, resp.status, elapsed)
                    if self._autothrottle:
                        self._autothrottle.update(domain, elapsed / 1000)
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

            except (TimeoutError, aiohttp.ClientError) as exc:
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
    # Playwright fetch
    # ------------------------------------------------------------------

    async def _fetch_playwright(self, request: Request) -> Response:
        t0 = time.monotonic()

        proxy_url = request.meta.get("proxy")
        proxy_kwarg = {"server": proxy_url} if proxy_url else None

        # Infinite-scroll support: request.meta["scroll"] = True (use defaults)
        # or a dict of overrides, e.g. {"max_scrolls": 20, "pause_ms": 500}.
        scroll_opts = request.meta.get("scroll")

        if self._browser_pool is not None:
            resp, body = await self._fetch_playwright_pooled(request, proxy_kwarg, scroll_opts)
        else:
            resp, body = await self._fetch_playwright_unpooled(request, proxy_kwarg, scroll_opts)

        elapsed = (time.monotonic() - t0) * 1000
        return Response(
            url=request.url,
            status=resp.status if resp else 200,
            headers={},
            body=body,
            request=request,
            elapsed_ms=elapsed,
            encoding="utf-8",
        )

    async def _fetch_playwright_pooled(
        self,
        request: Request,
        proxy_kwarg: dict[str, str] | None,
        scroll_opts: Any,
    ) -> tuple[Any, bytes]:
        assert self._browser_pool is not None
        async with self._browser_pool.acquire_context(
            proxy=proxy_kwarg,
            extra_http_headers=request.headers or {},
            user_agent=self.settings.user_agent,
        ) as context:
            page = await context.new_page()
            resp = await page.goto(request.url, wait_until="networkidle")
            if scroll_opts:
                opts = scroll_opts if isinstance(scroll_opts, dict) else {}
                await scroll_to_bottom(page, **opts)
            body = (await page.content()).encode("utf-8")
        return resp, body

    async def _fetch_playwright_unpooled(
        self,
        request: Request,
        proxy_kwarg: dict[str, str] | None,
        scroll_opts: Any,
    ) -> tuple[Any, bytes]:
        try:
            from playwright.async_api import async_playwright
        except ImportError as err:
            raise ImportError(
                "playwright is required for JS rendering. "
                "Install with: pip install bitscrape[playwright]"
            ) from err

        async with async_playwright() as pw:
            browser_type = getattr(pw, self.settings.playwright_browser)
            launch_kwargs: dict[str, Any] = {"headless": self.settings.playwright_headless}
            if proxy_kwarg:
                launch_kwargs["proxy"] = proxy_kwarg
            browser = await browser_type.launch(**launch_kwargs)
            context = await browser.new_context(
                extra_http_headers=request.headers or {},
                user_agent=self.settings.user_agent,
            )
            page = await context.new_page()
            resp = await page.goto(request.url, wait_until="networkidle")

            if scroll_opts:
                opts = scroll_opts if isinstance(scroll_opts, dict) else {}
                await scroll_to_bottom(page, **opts)

            body = (await page.content()).encode("utf-8")
            await browser.close()
        return resp, body

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    async def _apply_delay(self, domain: str) -> None:
        delay = (
            self._autothrottle.get_delay(domain)
            if self._autothrottle
            else self.settings.download_delay
        )
        if delay <= 0:
            return
        last = self._last_request_time.get(domain, 0.0)
        elapsed = time.monotonic() - last
        if elapsed < delay:
            await asyncio.sleep(delay - elapsed)
        self._last_request_time[domain] = time.monotonic()


def _domain(url: str) -> str:
    from urllib.parse import urlparse

    return urlparse(url).netloc


async def scroll_to_bottom(
    page: Any,
    max_scrolls: int = 20,
    pause_ms: int = 300,
    stable_rounds: int = 2,
    click_selector: str | None = None,
) -> int:
    """
    Reusable infinite-scroll / "load more" driver for the Playwright path.

    Bitscrape's HTTP downloader only fetches the static, initial DOM, so any
    site that lazy-loads content on scroll returns nothing beyond that. The
    Playwright path launches a real browser but previously had no built-in
    scroll loop, so callers had to hand-roll this for every site.

    Repeatedly scrolls the page to the bottom, waiting ``pause_ms`` between
    scrolls for lazy content to load, and stops once the page height hasn't
    grown for ``stable_rounds`` consecutive scrolls (or ``max_scrolls`` is
    hit, whichever comes first).

    If ``click_selector`` is given (e.g. ``"button.load-more"``), that
    element is clicked once per round (when present and visible) before
    measuring page height — supporting "click to load more" pagination in
    addition to pure scroll-triggered lazy loading.

    Returns the number of scroll rounds actually performed. Safe to call on
    pages with no scrollable content: it will simply detect no height change
    and return after ``stable_rounds`` rounds.
    """
    last_height = await page.evaluate("document.body.scrollHeight")
    stable_count = 0
    rounds = 0

    for _ in range(max_scrolls):
        rounds += 1

        if click_selector:
            try:
                locator = page.locator(click_selector)
                if await locator.count() > 0 and await locator.first.is_visible():
                    await locator.first.click(timeout=1000)
            except Exception:  # noqa: BLE001, S110 -- button absent/unclickable is expected, keep scrolling
                pass

        await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        await page.wait_for_timeout(pause_ms)

        new_height = await page.evaluate("document.body.scrollHeight")
        if new_height <= last_height:
            stable_count += 1
            if stable_count >= stable_rounds:
                break
        else:
            stable_count = 0
        last_height = new_height

    return rounds
