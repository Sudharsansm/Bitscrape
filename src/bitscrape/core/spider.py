"""
Base Spider — all user spiders subclass this.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncGenerator
from contextvars import ContextVar
from typing import Any
from urllib.parse import urljoin, urlparse

from bitscrape.core.models import BaseItem, Request, Response
from bitscrape.core.settings import Settings

logger = logging.getLogger(__name__)

# A spider callback can yield Requests or Items (or dicts)
SpiderOutput = AsyncGenerator[Request | BaseItem | dict[str, Any], None]

# The response currently being parsed, so follow() can resolve relative
# URLs against it. A ContextVar (not a plain instance attribute) because
# the Engine processes multiple requests CONCURRENTLY via
# asyncio.create_task() against one shared Spider instance -- a plain
# `self._current_response = response` attribute would let two in-flight
# requests clobber each other's resolution context at an await point
# inside the callback. asyncio.create_task() copies the current context
# per task, so a ContextVar set within one task's execution is correctly
# isolated from sibling tasks processing other requests concurrently.
_current_response_var: ContextVar[Response | None] = ContextVar(
    "bitscrape_current_response", default=None
)


def _is_absolute_url(url: str) -> bool:
    """True if `url` already has a scheme (http://, https://, etc.) --
    i.e. doesn't need resolving against a base URL."""
    return bool(urlparse(url).scheme)


class Spider:
    """
    Base class for all Bitscrape spiders.

    Subclass and override:
      - ``name``: unique identifier string (required)
      - ``start_urls``: list of seed URLs
      - ``parse()``: async generator that receives a Response and yields
                     items or new Requests

    Example::

        class QuotesSpider(Spider):
            name = "quotes"
            start_urls = ["https://quotes.toscrape.com/"]

            async def parse(self, response: Response):
                for q in response.css("div.quote"):
                    yield {"text": q.css("span.text::text").get(),
                           "author": q.xpath("span/small/text()").get()}
                nxt = response.css("li.next a::attr(href)").get()
                if nxt:
                    yield self.follow(nxt)
    """

    name: str = ""
    start_urls: list[str] = []
    custom_settings: dict[str, Any] = {}

    def __init__(self, settings: Settings | None = None) -> None:
        if not self.name:
            raise ValueError(f"{self.__class__.__name__} must define a `name` attribute")
        self.settings = settings or Settings()
        self.logger = logging.getLogger(self.name)

    # ------------------------------------------------------------------
    # Lifecycle hooks (override as needed)
    # ------------------------------------------------------------------

    async def open_spider(self) -> None:
        """Called once before crawling starts."""

    async def close_spider(self) -> None:
        """Called once after crawling finishes."""

    # ------------------------------------------------------------------
    # Request helpers
    # ------------------------------------------------------------------

    def make_requests_from_url(self, url: str) -> Request:
        return Request(url=url, callback="parse")

    def start_requests(self) -> list[Request]:
        return [self.make_requests_from_url(u) for u in self.start_urls]

    def follow(
        self,
        url: str,
        callback: str = "parse",
        meta: dict[str, Any] | None = None,
        use_playwright: bool = False,
    ) -> Request:
        """
        Convenience: create a follow-up Request.

        If ``url`` is relative (no scheme, e.g. ``"/page/2"`` or
        ``"page/2"``) and this is called from within a callback (the
        Engine sets the current response via a context variable for the
        duration of each callback invocation), it's resolved against the
        current response's URL via ``urljoin`` -- so
        ``self.follow(response.css("a::attr(href)").get())`` works whether
        the extracted href is absolute or relative, matching what most
        spider authors expect. Called outside a callback (no current
        response, e.g. while building seed requests), a relative URL is
        passed through unresolved -- there's no page to resolve it against.
        """
        resolved_url = url
        current_response = _current_response_var.get()
        if current_response is not None and not _is_absolute_url(url):
            resolved_url = urljoin(current_response.url, url)
        return Request(
            url=resolved_url,
            callback=callback,
            meta=meta or {},
            use_playwright=use_playwright,
        )

    # ------------------------------------------------------------------
    # Default parse — must be overridden
    # ------------------------------------------------------------------

    async def parse(self, response: Response) -> SpiderOutput:
        raise NotImplementedError(f"Spider {self.name!r} must implement an async `parse` method")

    # ------------------------------------------------------------------
    # Error handling
    # ------------------------------------------------------------------

    async def errback(self, request: Request, exc: Exception) -> None:
        self.logger.error("Request failed: %s — %s", request.url, exc)

    def __repr__(self) -> str:
        return f"<Spider name={self.name!r}>"
