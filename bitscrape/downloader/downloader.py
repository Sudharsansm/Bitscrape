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
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from bitscrape.core.models import Request, Response
from bitscrape.core.settings import Settings

logger = logging.getLogger(__name__)


class DownloadError(Exception):
    """Raised when all retries are exhausted or an unrecoverable error occurs."""


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
        logger.info("Downloader opened (max_concurrent=%d)", self.settings.concurrent_requests)

    async def close(self) -> None:
        if self._session:
            await self._session.close()
            self._session = None
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

        async with self._global_semaphore:
            async with self._domain_semaphores[domain]:
                await self._apply_delay(domain)
                return await self._do_fetch(request)

    async def _do_fetch(self, request: Request) -> Response:
        assert self._session is not None
        t0 = time.monotonic()
        attempt = 0
        last_exc: Exception | None = None

        while attempt <= request.max_retries:
            try:
                async with self._session.request(
                    method=request.method,
                    url=request.url,
                    headers=request.headers,
                    data=request.body,
                    allow_redirects=self.settings.follow_redirects,
                    max_redirects=self.settings.max_redirect_count,
                ) as resp:
                    body = await resp.read()
                    elapsed = (time.monotonic() - t0) * 1000
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
                        raise aiohttp.ClientResponseError(
                            resp.request_info,
                            resp.history,
                            status=resp.status,
                        )
                    logger.debug("GET %s → %d (%.0fms)", request.url, resp.status, elapsed)
                    return response

            except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
                attempt += 1
                last_exc = exc
                if attempt <= request.max_retries:
                    wait = min(2 ** attempt, 30)
                    logger.warning(
                        "Retry %d/%d for %s (%s) — waiting %ds",
                        attempt, request.max_retries, request.url, exc, wait,
                    )
                    await asyncio.sleep(wait)

        raise DownloadError(
            f"All {request.max_retries} retries failed for {request.url}"
        ) from last_exc

    # ------------------------------------------------------------------
    # Playwright fetch
    # ------------------------------------------------------------------

    async def _fetch_playwright(self, request: Request) -> Response:
        try:
            from playwright.async_api import async_playwright
        except ImportError:
            raise ImportError(
                "playwright is required for JS rendering. "
                "Install with: pip install bitscrape[playwright]"
            )
        t0 = time.monotonic()
        async with async_playwright() as pw:
            browser_type = getattr(pw, self.settings.playwright_browser)
            browser = await browser_type.launch(headless=self.settings.playwright_headless)
            context = await browser.new_context(
                extra_http_headers=request.headers or {},
                user_agent=self.settings.user_agent,
            )
            page = await context.new_page()
            resp = await page.goto(request.url, wait_until="networkidle")
            body = (await page.content()).encode("utf-8")
            await browser.close()

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

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    async def _apply_delay(self, domain: str) -> None:
        delay = self.settings.download_delay
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
