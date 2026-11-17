"""
Rule-driven crawling
====================

``CrawlSpider`` follows links according to declarative :class:`Rule`\\ s -- the
usual way to crawl a whole site::

    class Books(CrawlSpider):
        name = "books"
        start_urls = ["https://books.toscrape.com/"]
        allowed_domains = ["books.toscrape.com"]
        rules = (
            Rule(LinkExtractor(allow=r"/catalogue/page-\\d+"), follow=True),
            Rule(LinkExtractor(allow=r"/catalogue/[^/]+_\\d+/index\\.html"), callback="parse_book"),
        )

        async def parse_book(self, response):
            yield {"title": response.css("h1::text").get()}

Semantics (matching Scrapy's, so existing knowledge transfers):

* Every response is scanned with **all** rules; a URL is claimed by the
  *first* rule whose extractor yields it.
* ``follow`` defaults to ``True`` when there is no callback and ``False`` when
  there is one.
* The start pages go through :meth:`CrawlSpider.parse_start_url` and always
  have their links followed.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable, Sequence
from dataclasses import dataclass, field
from typing import Any, ClassVar

from bitscrape.core.models import Request, RequestPriority
from bitscrape.core.spider import Spider, iterate_output
from bitscrape.crawl.linkextract import LinkExtractor


@dataclass
class Rule:
    """One crawl rule.

    Args:
        link_extractor: which links this rule matches (default: all links).
        callback: spider method name (or callable) to parse matched pages.
        follow: keep extracting links from matched pages.
        process_links: ``(links) -> links`` hook to filter/reorder extracted links.
        process_request: ``(request, response) -> Request | None`` hook; return
            ``None`` to drop the request.
        cb_kwargs: extra keyword arguments passed to ``callback``.
        session: fetch session name for requests created by this rule.
        priority: request priority (``RequestPriority.HIGH``/``NORMAL``/``LOW``,
            or a plain int -- lower value = higher priority). Documented since
            the initial version of this dataclass but not actually applied to
            the requests it built; it now is.
    """

    link_extractor: LinkExtractor | None = None
    callback: str | Callable[..., Any] | None = None
    follow: bool | None = None
    process_links: str | Callable[[list[Any]], list[Any]] | None = None
    process_request: str | Callable[[Request, Any], Request | None] | None = None
    cb_kwargs: dict[str, Any] = field(default_factory=dict)
    session: str | None = None
    priority: RequestPriority | int | None = None

    def __post_init__(self) -> None:
        if self.link_extractor is None:
            self.link_extractor = LinkExtractor()
        if self.follow is None:
            self.follow = self.callback is None


class CrawlSpider(Spider):
    """A spider driven by :attr:`rules`. Override :meth:`parse_start_url` to
    scrape the seed pages themselves."""

    rules: ClassVar[Sequence[Rule]] = ()

    async def parse_start_url(self, response: Any) -> Any:
        """Called for each start URL response (yield items/requests)."""
        return None

    # -- engine entry points ----------------------------------------------

    # Async-generator callbacks are the Bitscrape norm; the base class stub is a plain
    # coroutine, hence the override ignore.
    async def parse(self, response: Any) -> AsyncIterator[Any]:  # type: ignore[override]
        async for out in self._parse_response(response, self.parse_start_url, {}, follow=True):
            yield out

    async def _rule_callback(
        self, response: Any, rule_index: int | None = None
    ) -> AsyncIterator[Any]:
        idx = rule_index if rule_index is not None else response.meta.get("_rule")
        if idx is None or not (0 <= int(idx) < len(self.rules)):
            return
        rule = self.rules[int(idx)]
        cb = self._resolve(rule.callback)
        kwargs = dict(rule.cb_kwargs)
        async for out in self._parse_response(response, cb, kwargs, follow=bool(rule.follow)):
            yield out

    # -- machinery -----------------------------------------------------------

    def _resolve(self, ref: str | Callable[..., Any] | None) -> Callable[..., Any] | None:
        if ref is None:
            return None
        if callable(ref):
            return ref
        fn = getattr(self, ref, None)
        if fn is None:
            raise AttributeError(f"{self.__class__.__name__} has no callback {ref!r}")
        return fn

    async def _parse_response(
        self,
        response: Any,
        callback: Callable[..., Any] | None,
        cb_kwargs: dict[str, Any],
        follow: bool,
    ) -> AsyncIterator[Any]:
        if callback is not None:
            async for out in iterate_output(callback(response, **cb_kwargs)):
                yield out
        if follow:
            for request in self._requests_to_follow(response):
                yield request

    def _requests_to_follow(self, response: Any) -> list[Request]:
        seen: set[str] = set()
        out: list[Request] = []
        for idx, rule in enumerate(self.rules):
            assert rule.link_extractor is not None
            links = [lk for lk in rule.link_extractor.extract_links(response) if lk.url not in seen]
            pl = self._resolve(rule.process_links)
            if pl is not None:
                links = pl(links)
            for link in links:
                seen.add(link.url)
                request_kwargs: dict[str, Any] = {
                    "url": link.url,
                    "callback": "_rule_callback",
                    "meta": {"_rule": idx, "link_text": link.text},
                    "session": rule.session,
                    "cb_kwargs": {},
                }
                if rule.priority is not None:
                    request_kwargs["priority"] = rule.priority
                request = Request(**request_kwargs)
                pr = self._resolve(rule.process_request)
                if pr is not None:
                    maybe = pr(request, response)
                    if maybe is None:
                        continue
                    request = maybe
                out.append(request)
        return out
