"""
Base Spider — all user spiders subclass this.
"""

from __future__ import annotations

import inspect
import logging
from collections.abc import AsyncGenerator, AsyncIterator
from contextvars import ContextVar
from typing import TYPE_CHECKING, Any, ClassVar
from urllib.parse import urljoin, urlparse

from bitscrape.core.models import BaseItem, Request, Response
from bitscrape.core.settings import Settings

if TYPE_CHECKING:  # pragma: no cover
    from bitscrape.sessions import SessionManager

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


async def iterate_output(result: Any) -> AsyncIterator[Any]:
    """Normalise whatever a spider callback produced into an async stream of
    items/requests. Accepts an async generator (the classic form), a coroutine
    (``async def parse(...): return item``), a sync generator, a list/tuple, a
    single item/request, or ``None``."""
    if result is None:
        return
    if inspect.isasyncgen(result) or hasattr(result, "__aiter__"):
        async for out in result:
            yield out
        return
    if inspect.isawaitable(result):
        async for out in iterate_output(await result):
            yield out
        return
    if isinstance(result, (dict, str, bytes, BaseItem, Request)):
        yield result
        return
    if hasattr(result, "__iter__"):
        for out in result:
            yield out
        return
    yield result


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
    start_urls: ClassVar[list[str]] = []
    custom_settings: ClassVar[dict[str, Any]] = {}
    # Hosts this spider may visit (``example.com`` also allows ``www.example.com``).
    # Empty = no restriction. Off-site requests are dropped by
    # ``bitscrape.middleware.middleware.OffsiteMiddleware`` (always installed; a
    # no-op while this list is empty), which raises ``OffsiteDrop`` --
    # ``CrawlStats.requests_offsite_dropped``, not ``requests_failed``.
    allowed_domains: ClassVar[list[str]] = []
    # Named fetch sessions declared as plain dicts, e.g.
    #   sessions = {"api": {"kind": "http", "headers": {"Accept": "application/json"}},
    #               "rendered": {"kind": "browser", "wait_selector": ".price"}}
    # and selected per request with ``Request(session="rendered")`` /
    # ``self.follow(url, session="rendered")``. For session *objects* (or
    # anything needing code) override ``configure_sessions()`` instead.
    sessions: ClassVar[dict[str, dict[str, Any]]] = {}

    def __init__(self, settings: Settings | None = None) -> None:
        if not self.name:
            raise ValueError(f"{self.__class__.__name__} must define a `name` attribute")
        base = settings or Settings()
        # ``custom_settings`` override the run-wide settings for this spider.
        self.settings = (
            base.model_copy(update=dict(self.custom_settings)) if self.custom_settings else base
        )
        # Merge this spider's declared ``sessions`` on top of any sessions
        # already present in settings (settings.sessions wins on a name
        # collision, since it's the more specific, run-time configuration).
        if self.sessions:
            self.settings = self.settings.model_copy(
                update={"sessions": {**self.sessions, **self.settings.sessions}}
            )
        self.logger = logging.getLogger(self.name)

    # ------------------------------------------------------------------
    # Lifecycle hooks (override as needed)
    # ------------------------------------------------------------------

    async def open_spider(self) -> None:
        """Called once before crawling starts."""

    async def close_spider(self) -> None:
        """Called once after crawling finishes."""

    def configure_sessions(self, manager: SessionManager) -> None:
        """Register fetch sessions in code (called once, before the crawl starts).

        Sessions declared in ``Spider.sessions`` / ``Settings.sessions`` are
        already registered when this runs, so you can add to them or replace one
        with ``replace=True``::

            def configure_sessions(self, manager):
                manager.add("api", HttpSession(headers={"Accept": "application/json"}))
                manager.add("rendered", BrowserSession(wait_selector=".price"), lazy=True)
        """

    # ------------------------------------------------------------------
    # Blocked-response hooks
    # ------------------------------------------------------------------

    async def is_blocked(self, response: Response) -> bool:
        """Whether ``response`` is a block / rate-limit / interstitial page.

        The default trusts the downloader's blocked-request detector
        (``Settings.blocked_detection_enabled``), which marks a response it gave
        up on with ``X-Bitscrape-Blocked: true``. Override to add your own rules
        (a body marker, a redirect to ``/captcha`` ...).
        """
        return response.headers.get("X-Bitscrape-Blocked") == "true"

    async def retry_blocked_request(self, request: Request, response: Response) -> Request | None:
        """Return a request to retry instead of parsing a blocked response.

        The default returns ``None`` (parse the response as-is). Return a
        modified copy to *escalate* -- for instance move to a browser session, or
        to a session with a different proxy::

            async def retry_blocked_request(self, request, response):
                if request.session != "rendered":
                    return request.model_copy(update={"session": "rendered"})
                return None  # already rendered: give up

        A request is retried at most ``Settings.blocked_escalation_max`` times.
        This is for *legitimate* recovery (slower pace, a different proxy, a
        rendered session); bitscrape ships no evasion tooling.
        """
        return None

    # ------------------------------------------------------------------
    # Request helpers
    # ------------------------------------------------------------------

    def make_requests_from_url(self, url: str) -> Request:
        return Request(url=url, callback="parse")

    def start_requests(self) -> Any:
        """Seed requests. May return a list, a generator, or an *async*
        generator (useful when seeds come from a database or an API)."""
        return [self.make_requests_from_url(u) for u in self.start_urls]

    def follow(
        self,
        url: str,
        callback: str = "parse",
        meta: dict[str, Any] | None = None,
        use_playwright: bool = False,
        *,
        session: str | None = None,
        cb_kwargs: dict[str, Any] | None = None,
        dont_filter: bool = False,
        session_kwargs: dict[str, Any] | None = None,
        inherit_session: bool = True,
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

        # Session inheritance: a follow-up request stays on the session (and
        # keeps its per-request session_kwargs) of the page it was found on,
        # unless it names another one. Explicit values always win; pass
        # ``inherit_session=False`` to fall back to the default session.
        inherited_kwargs: dict[str, Any] = {}
        if inherit_session and current_response is not None:
            parent = current_response.request
            if session is None:
                session = parent.session
            if session is not None and session == parent.session:
                inherited_kwargs = dict(parent.session_kwargs)
        return Request(
            url=resolved_url,
            callback=callback,
            meta=meta or {},
            use_playwright=use_playwright,
            session=session,
            session_kwargs={**inherited_kwargs, **(session_kwargs or {})},
            cb_kwargs=cb_kwargs or {},
            dont_filter=dont_filter,
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
        """Called when a request ultimately fails (after retries). Override to
        log, record, or yield replacement Requests. The engine already logs the
        failure, so the default only records a debug line."""
        self.logger.debug("Request failed: %s — %s", request.url, exc)

    def __repr__(self) -> str:
        return f"<Spider name={self.name!r}>"
