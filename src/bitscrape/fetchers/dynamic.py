"""
Fetching JavaScript-driven pages with a real browser: ``DynamicFetcher``.

For pages whose content only exists after scripts run -- single-page apps,
lazy-loaded lists, content behind a click -- plain HTTP (``Fetcher``) sees an
empty shell. ``DynamicFetcher`` drives a real Chromium through Playwright and
returns the page as the browser ended up with it::

    from bitscrape import DynamicFetcher

    page = DynamicFetcher.fetch("https://example.com/app", wait_selector=".product")
    print(page.css(".product h2::text").getall())

Four classes, mirroring ``Fetcher``:

========================= ======== ============================================
class                     style    lifetime
========================= ======== ============================================
``DynamicFetcher``        sync     launches a browser per call, closes it after
``AsyncDynamicFetcher``   async    same
``DynamicSession``        sync     one browser for the ``with`` block: cookies
                                   and logins persist, no per-fetch launch cost
``AsyncDynamicSession``   async    same, for ``async with``
========================= ======== ============================================

Every call returns a :class:`~bitscrape.parser.selector.ParsedResponse` (the
same object a spider callback gets), whose body is the **rendered DOM**. The
exception is a URL that isn't an HTML document at all (a JSON endpoint, plain
text): there you get the real response body, not the browser's wrapper around it.

**Browsers.** By default Playwright's bundled Chromium
(``pip install bitscrape[playwright]`` then ``playwright install chromium``).
``real_chrome=True`` drives an installed **Google Chrome** instead;
``channel="msedge"`` (or any Playwright channel), ``executable_path=`` and
``cdp_url=`` (attach to a Chrome you started with
``--remote-debugging-port``) cover the rest. A browser that can't be started
raises :class:`~bitscrape.errors.BrowserLaunchError` saying how to fix it.

**Timeouts are in seconds**, and apply to each step separately: navigation,
the selector wait, and every Playwright call inside your hooks.

**Hooks.** ``page_setup(page)`` runs before navigation (register listeners,
routes); ``page_action(page)`` runs after it, before ``wait_selector`` (so an
action can be what makes the selector appear). ``page`` is Playwright's *async*
``Page`` -- write ``async def`` hooks, in the synchronous classes too::

    async def search(page):
        await page.fill("#q", "widgets")
        await page.click("button[type=submit]")

    DynamicFetcher.fetch(url, page_action=search, wait_selector=".results")

**What this is not.** It's an honest, automated browser: it doesn't patch the
fingerprint headless Chrome exposes, spoof a user agent, fabricate a Google
referer or solve challenges. ``stealth=``, ``hide_canvas=``, ``block_webrtc=``,
``solve_cloudflare=``, ``google_search=`` and similar raise
``NotImplementedError`` (ROADMAP.md, "Explicit non-goals"). Like ``Fetcher`` it
is a direct client -- no robots.txt check, no pacing; use a ``Spider`` for a
polite crawl (a spider can use browser sessions too: ``BrowserSession``).
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Coroutine
from pathlib import Path
from typing import Any, TypeVar

from bitscrape.browser import PageHook, PageRequest, load_page
from bitscrape.core.models import Request, Response
from bitscrape.core.settings import Settings
from bitscrape.fetchers.http import _ensure_no_running_loop, _reject_unsupported
from bitscrape.parser.selector import ParsedResponse
from bitscrape.sessions import BrowserSession, SessionError

T = TypeVar("T")

#: Options that configure the *browser* (so a one-shot call sends them to the
#: session constructor); everything else is per-fetch.
_SESSION_KEYS = frozenset(
    {
        "headless",
        "real_chrome",
        "channel",
        "executable_path",
        "cdp_url",
        "user_data_dir",
        "proxy",
        "extra_flags",
        "locale",
        "timezone",
        "viewport",
        "user_agent",
        "storage_state",
        "context_options",
        "init_script",
        "disable_resources",
        "block_ads",
        "blocked_domains",
        "block_resource_types",
        "allow_domains",
        "max_concurrency",
        "settings",
        "playwright_factory",
    }
)


def _wait_until(network_idle: bool, load_dom: bool) -> str:
    """``network_idle`` → wait for the network to go quiet; ``load_dom`` →
    the page's ``load`` event; neither → just the parsed DOM."""
    if network_idle:
        return "networkidle"
    return "load" if load_dom else "domcontentloaded"


# ---------------------------------------------------------------------------
# AsyncDynamicSession: the core
# ---------------------------------------------------------------------------


class AsyncDynamicSession:
    """A browser kept open across fetches: cookies, logins and local storage
    persist, and there's no per-fetch launch cost. Use as
    ``async with AsyncDynamicSession() as s:`` (it also starts itself on the
    first :meth:`fetch`; call :meth:`close` yourself if you don't use ``with``).
    Each fetch gets its own tab, closed afterwards.

    **Browser options** (fixed once the session starts):

    Args:
        headless: run without a visible window (default ``True``).
        real_chrome: drive an installed Google Chrome, not Playwright's Chromium.
        channel: another Playwright channel (``"msedge"``, ``"chrome-beta"`` ...).
        executable_path: path to a specific browser binary.
        cdp_url: attach to a running browser (``http://localhost:9222``) instead
            of launching one; the browser is left running when the session closes.
        user_data_dir: persistent profile directory (logins survive between runs).
        proxy: ``http://user:pass@host:port`` or a Playwright proxy dict.
        extra_flags: extra browser command-line flags.
        locale / timezone / viewport / user_agent: context settings; sent exactly
            as given (nothing is generated or rotated).
        headers: headers added to every request the browser makes.
        storage_state: Playwright storage-state JSON to start logged in.
        context_options: extra ``browser.new_context()`` keyword arguments
            (``geolocation``, ``permissions``, ``color_scheme``, ...).
        init_script: path to a JavaScript file run in every page before its scripts.
        disable_resources: drop images, media, fonts and similar heavy resources
            (stylesheets and scripts are kept: blocking them changes the page).
        block_ads / blocked_domains / block_resource_types: more request blocking.
        max_concurrency: tabs open at once (default 4).
        settings: a :class:`~bitscrape.core.settings.Settings` for defaults
            (timeout, ...).

    **Per-fetch defaults**, each overridable in :meth:`fetch`: ``timeout``,
    ``network_idle``, ``load_dom``, ``wait``, ``wait_selector``,
    ``wait_selector_state``, ``scroll``, ``screenshot``, ``capture_xhr``,
    ``page_setup``, ``page_action``.
    """

    def __init__(
        self,
        *,
        headless: bool = True,
        real_chrome: bool = False,
        channel: str | None = None,
        executable_path: str | None = None,
        cdp_url: str | None = None,
        user_data_dir: str | None = None,
        proxy: str | dict[str, str] | None = None,
        extra_flags: list[str] | None = None,
        locale: str | None = None,
        timezone: str | None = None,
        viewport: tuple[int, int] | None = None,
        user_agent: str | None = None,
        headers: dict[str, str] | None = None,
        storage_state: str | None = None,
        context_options: dict[str, Any] | None = None,
        init_script: str | None = None,
        disable_resources: bool = False,
        block_ads: bool = False,
        blocked_domains: list[str] | None = None,
        block_resource_types: list[str] | None = None,
        allow_domains: list[str] | None = None,
        record_requests: bool = False,
        max_recorded_requests: int = 1000,
        pierce_shadow: bool = False,
        max_concurrency: int = 4,
        timeout: float | None = None,
        network_idle: bool = False,
        load_dom: bool = True,
        wait: float = 0.0,
        wait_selector: str | None = None,
        wait_selector_state: str = "attached",
        scroll: bool | dict[str, Any] | None = None,
        screenshot: bool | dict[str, Any] | None = None,
        capture_xhr: bool | str | None = None,
        page_setup: PageHook | None = None,
        page_action: PageHook | None = None,
        settings: Settings | None = None,
        playwright_factory: Callable[[], Any] | None = None,
        **unsupported: Any,
    ) -> None:
        _reject_unsupported(unsupported, "AsyncDynamicSession()")
        self.settings = settings or Settings()
        try:
            self._browser = BrowserSession(
                headless=headless,
                real_chrome=real_chrome,
                channel=channel,
                executable_path=executable_path,
                cdp_url=cdp_url,
                user_data_dir=user_data_dir,
                proxy=proxy,
                args=extra_flags,
                locale=locale,
                timezone=timezone,
                viewport=viewport,
                user_agent=user_agent,
                headers=headers,
                storage_state=storage_state,
                context_options=context_options,
                init_script=init_script,
                disable_resources=disable_resources,
                block_ads=block_ads,
                block_domains=blocked_domains,
                block_resource_types=block_resource_types,
                allow_domains=allow_domains,
                record_requests=record_requests,
                max_recorded_requests=max_recorded_requests,
                max_concurrency=max_concurrency,
                playwright_factory=playwright_factory,
            )
        except SessionError as exc:
            raise ValueError(str(exc)) from exc
        # Validate everything now, so a typo fails here and not after a browser launch.
        self._browser.launch_config(self.settings).validate()
        self._defaults: dict[str, Any] = {
            "timeout": timeout,
            "network_idle": network_idle,
            "load_dom": load_dom,
            "wait": wait,
            "wait_selector": wait_selector,
            "wait_selector_state": wait_selector_state,
            "scroll": scroll,
            "screenshot": screenshot,
            "capture_xhr": capture_xhr,
            "pierce_shadow": pierce_shadow,
            "page_setup": page_setup,
            "page_action": page_action,
        }
        self._closed = False
        # Fail on bad per-fetch defaults at construction, too.
        self._page_request("about:blank", {})

    # -- lifecycle -------------------------------------------------------

    async def start(self) -> None:
        """Launch the browser (idempotent). Called for you by ``async with`` and
        by the first :meth:`fetch`."""
        if self._closed:
            raise RuntimeError("This browser session is closed.")
        await self._browser.start(self.settings)

    async def close(self) -> None:
        """Close the browser. Idempotent; the session can't be used afterwards."""
        self._closed = True
        await self._browser.close()

    async def __aenter__(self) -> AsyncDynamicSession:  # noqa: PYI034 -- typing.Self needs 3.11
        await self.start()
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.close()

    @property
    def browser(self) -> BrowserSession:
        """The underlying :class:`~bitscrape.sessions.BrowserSession` -- for
        ``await s.browser.cookies()``, ``s.browser.save_storage_state(path)`` ..."""
        return self._browser

    @property
    def network(self) -> Any:
        """The session's :class:`~bitscrape.network.NetworkRecorder`
        (needs ``record_requests=True``): ``s.network.search(url_pattern=..., limit=None)``."""
        return self._browser.network

    async def save_state(self, path: str | Path) -> None:
        """Save cookies, localStorage and IndexedDB to ``path``; see
        :meth:`~bitscrape.sessions.BrowserSession.save_state`. Call inside a started session."""
        await self._browser.save_state(path)

    async def load_state(self, path: str | Path) -> None:
        """Restore cookies and localStorage from a :meth:`save_state` file."""
        await self._browser.load_state(path)

    # -- fetching --------------------------------------------------------

    def _page_request(self, url: str, overrides: dict[str, Any]) -> PageRequest:
        opts = {**self._defaults, **{k: v for k, v in overrides.items() if v is not None}}
        timeout = opts["timeout"]
        request = PageRequest(
            url=url,
            timeout=float(timeout if timeout is not None else self.settings.browser_timeout),
            wait_until=_wait_until(opts["network_idle"], opts["load_dom"]),
            wait_selector=opts["wait_selector"],
            wait_selector_state=opts["wait_selector_state"],
            wait=float(opts["wait"]),
            scroll=opts["scroll"],
            screenshot=opts["screenshot"],
            capture_xhr=opts["capture_xhr"],
            pierce_shadow=bool(opts["pierce_shadow"]),
            headers=dict(overrides.get("headers") or {}),
            cookies=overrides.get("cookies"),
            page_setup=opts["page_setup"],
            page_action=opts["page_action"],
            blocker=self._browser.blocker,
        )
        request.validate()
        return request

    async def fetch(
        self,
        url: str,
        *,
        timeout: float | None = None,
        network_idle: bool | None = None,
        load_dom: bool | None = None,
        wait: float | None = None,
        wait_selector: str | None = None,
        wait_selector_state: str | None = None,
        scroll: bool | dict[str, Any] | None = None,
        screenshot: bool | dict[str, Any] | None = None,
        capture_xhr: bool | str | None = None,
        pierce_shadow: bool | None = None,
        page_setup: PageHook | None = None,
        page_action: PageHook | None = None,
        headers: dict[str, str] | None = None,
        cookies: dict[str, str] | list[dict[str, Any]] | None = None,
        **unsupported: Any,
    ) -> ParsedResponse:
        """Load ``url`` in a new tab and return the rendered page.

        Args:
            timeout: seconds, for navigation and the selector wait each.
            network_idle: also wait for the network to go quiet (slow on chatty
                pages; ``wait_selector`` is usually the better signal).
            load_dom: wait for the ``load`` event (default); ``False`` returns
                as soon as the DOM is parsed.
            wait: extra seconds to sit on the page before reading it.
            wait_selector / wait_selector_state: wait for a CSS selector
                (``"attached"`` default, ``"visible"``, ``"hidden"``, ``"detached"``).
            scroll: ``True`` (or a dict: ``max_scrolls``, ``pause_ms``,
                ``stable_rounds``, ``click_selector``) to scroll to the bottom
                until the page stops growing -- for infinite scroll.
            screenshot: ``True`` (full page PNG) or a dict of Playwright
                ``screenshot()`` options; the bytes are in ``response.extra["screenshot"]``.
            capture_xhr: ``True`` or a URL regex: record the page's background
                ``fetch``/XHR responses in ``response.extra["captured_xhr"]``
                (also ``response.xhr`` / ``response.find_xhr(pattern)``).
            page_setup / page_action: async hooks, see the module docstring.
            headers: extra headers for this page.
            cookies: ``{name: value}`` (scoped to ``url``) or Playwright cookie
                dicts, added to the browser's cookie jar -- where they **stay
                for the rest of the session** (a browser context has one jar).

        Raises:
            DynamicFetchError: navigation failed or timed out, a hook raised, or
                the URL is a file download.
            BrowserLaunchError: the browser couldn't be started.
        """
        _reject_unsupported(unsupported, "fetch()")
        if self._closed:
            raise RuntimeError("This browser session is closed.")
        page_request = self._page_request(
            url,
            {
                "timeout": timeout,
                "network_idle": network_idle,
                "load_dom": load_dom,
                "wait": wait,
                "wait_selector": wait_selector,
                "wait_selector_state": wait_selector_state,
                "scroll": scroll,
                "screenshot": screenshot,
                "capture_xhr": capture_xhr,
                "pierce_shadow": pierce_shadow,
                "page_setup": page_setup,
                "page_action": page_action,
                "headers": headers,
                "cookies": cookies,
            },
        )
        await self.start()
        semaphore = self._browser.semaphore
        if semaphore is not None:
            async with semaphore:
                result = await load_page(self._browser.runtime, page_request)
        else:
            result = await load_page(self._browser.runtime, page_request)
        self._browser.requests_count += 1
        return ParsedResponse(
            Response(
                url=result.url,
                status=result.status,
                headers=result.headers,
                body=result.body,
                request=Request(url=url, headers=dict(headers or {})),
                elapsed_ms=result.elapsed_ms,
                encoding=result.encoding,
                extra=result.extra,
            )
        )


# ---------------------------------------------------------------------------
# DynamicSession: the same, synchronous
# ---------------------------------------------------------------------------


class DynamicSession:
    """The synchronous twin of :class:`AsyncDynamicSession`: one browser open
    for the ``with`` block, so a login in one fetch is still there in the next::

        with DynamicSession() as s:
            s.fetch("https://example.com/login", page_action=sign_in)
            account = s.fetch("https://example.com/account", wait_selector=".balance")

    Same constructor arguments as the async class. Not usable inside a running
    event loop (an ``async def``, a Jupyter cell) -- use the async class there.
    It owns a private event loop, so use one session per thread. Hooks are still
    ``async def`` (they drive Playwright's async ``Page``).
    """

    def __init__(self, **kwargs: Any) -> None:
        _ensure_no_running_loop("DynamicSession", "AsyncDynamicSession")
        self._inner = AsyncDynamicSession(**kwargs)
        self._loop: asyncio.AbstractEventLoop | None = asyncio.new_event_loop()

    def _run(self, coro: Coroutine[Any, Any, T]) -> T:
        _ensure_no_running_loop("DynamicSession", "AsyncDynamicSession")
        if self._loop is None:
            coro.close()  # don't leak "coroutine was never awaited"
            raise RuntimeError("This browser session is closed.")
        return self._loop.run_until_complete(coro)

    def __enter__(self) -> DynamicSession:  # noqa: PYI034 -- typing.Self needs 3.11
        self._run(self._inner.start())
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def close(self) -> None:
        """Close the browser and the private event loop. Idempotent."""
        if self._loop is None:
            return
        try:
            self._loop.run_until_complete(self._inner.close())
        finally:
            self._loop.close()
            self._loop = None

    @property
    def browser(self) -> BrowserSession:
        return self._inner.browser

    @property
    def network(self) -> Any:
        """The session's network history (local, synchronous reads); see
        :class:`~bitscrape.network.NetworkRecorder`."""
        return self._inner.network

    def save_state(self, path: str | Path) -> None:
        """Save cookies, localStorage and IndexedDB to ``path`` (inside the ``with`` block)."""
        self._run(self._inner.save_state(path))

    def load_state(self, path: str | Path) -> None:
        """Restore a :meth:`save_state` file, then fetch to use it."""
        self._run(self._inner.load_state(path))

    def fetch(self, url: str, **kwargs: Any) -> ParsedResponse:
        """Load ``url`` and return the rendered page; see :meth:`AsyncDynamicSession.fetch`."""
        return self._run(self._inner.fetch(url, **kwargs))


# ---------------------------------------------------------------------------
# DynamicFetcher / AsyncDynamicFetcher: one-shot facades
# ---------------------------------------------------------------------------


def _split(kwargs: dict[str, Any], where: str) -> tuple[dict[str, Any], dict[str, Any]]:
    from bitscrape.sessions import UNSUPPORTED_OPTIONS

    _reject_unsupported({k: v for k, v in kwargs.items() if k in UNSUPPORTED_OPTIONS}, where)
    session_kwargs = {k: kwargs.pop(k) for k in list(kwargs) if k in _SESSION_KEYS}
    return session_kwargs, kwargs


class DynamicFetcher:
    """One-shot synchronous browser fetch -- ``DynamicFetcher.fetch(url)`` --
    each call starting its own browser and closing it again (about a second of
    launch cost per call; use :class:`DynamicSession` for more than a couple of
    pages, or when cookies must carry over).

    Browser options (``real_chrome=``, ``proxy=``, ``headless=``, ...) and fetch
    options (``wait_selector=``, ``page_action=``, ...) are all plain keyword
    arguments; see :class:`AsyncDynamicSession` and :meth:`AsyncDynamicSession.fetch`.
    """

    @classmethod
    def fetch(cls, url: str, **kwargs: Any) -> ParsedResponse:
        _ensure_no_running_loop("DynamicFetcher", "AsyncDynamicFetcher")
        session_kwargs, fetch_kwargs = _split(kwargs, "DynamicFetcher.fetch()")
        with DynamicSession(**session_kwargs) as session:
            return session.fetch(url, **fetch_kwargs)


class AsyncDynamicFetcher:
    """One-shot asynchronous browser fetch -- ``await AsyncDynamicFetcher.fetch(url)``.
    Same options as :class:`DynamicFetcher`. For many pages open one
    :class:`AsyncDynamicSession` and share it."""

    @classmethod
    async def fetch(cls, url: str, **kwargs: Any) -> ParsedResponse:
        session_kwargs, fetch_kwargs = _split(kwargs, "AsyncDynamicFetcher.fetch()")
        async with AsyncDynamicSession(**session_kwargs) as session:
            return await session.fetch(url, **fetch_kwargs)


__all__ = [
    "AsyncDynamicFetcher",
    "AsyncDynamicSession",
    "DynamicFetcher",
    "DynamicSession",
]
