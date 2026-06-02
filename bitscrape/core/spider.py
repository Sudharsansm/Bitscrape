"""
Base Spider — all user spiders subclass this.
"""
from __future__ import annotations

import logging
from typing import Any, AsyncGenerator

from bitscrape.core.models import BaseItem, Request, Response
from bitscrape.core.settings import Settings

logger = logging.getLogger(__name__)

# A spider callback can yield Requests or Items (or dicts)
SpiderOutput = AsyncGenerator[Request | BaseItem | dict[str, Any], None]


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
        """Convenience: create a follow-up Request."""
        return Request(
            url=url,
            callback=callback,
            meta=meta or {},
            use_playwright=use_playwright,
        )

    # ------------------------------------------------------------------
    # Default parse — must be overridden
    # ------------------------------------------------------------------

    async def parse(self, response: Response) -> SpiderOutput:  # type: ignore[return]
        raise NotImplementedError(
            f"Spider {self.name!r} must implement an async `parse` method"
        )

    # ------------------------------------------------------------------
    # Error handling
    # ------------------------------------------------------------------

    async def errback(self, request: Request, exc: Exception) -> None:
        self.logger.error("Request failed: %s — %s", request.url, exc)

    def __repr__(self) -> str:
        return f"<Spider name={self.name!r}>"
