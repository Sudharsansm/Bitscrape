"""
Multi-session support
=====================

A *session* is a long-lived, independently configured way of fetching pages:
its own cookie jar / login state, headers, proxy, timeouts and (for browser
sessions) its own persistent browser context. One spider can register several
of them and route every request to the right one by **session id**::

    class ShopSpider(bitscrape.Spider):
        name = "shop"
        start_urls = ["https://shop.example.com/products"]

        def configure_sessions(self, manager):
            manager.add("api", bitscrape.HttpSession(headers={"Accept": "application/json"}))
            manager.add("rendered", bitscrape.BrowserSession(wait_selector=".price"), lazy=True)

        async def parse(self, response):
            for href in response.css("a.product::attr(href)").getall():
                # Detail pages need JavaScript: route them to the browser session.
                yield self.follow(href, callback="parse_product", session="rendered")

Two session kinds exist:

* :class:`HttpSession` -- an ``aiohttp`` client with its own cookie jar.
* :class:`BrowserSession` -- one Playwright browser + persistent context, shared
  by every request routed to it (so logins and cookies survive between pages,
  and the browser is launched once instead of once per request).

Sessions can also be declared as plain dicts in ``Settings.sessions`` or
``Spider.sessions`` (``{"api": {"kind": "http", "headers": {...}}}``).

What is deliberately **not** offered: TLS/browser-fingerprint impersonation,
"stealth" hardening, CAPTCHA/challenge solving and similar bot-detection
evasion. Declaring such a kind or option raises ``NotImplementedError`` with an
explanation -- see ``ROADMAP.md`` ("Explicit non-goals"). To react to being
blocked in a *legitimate* way (slower pace, different proxy, a rendered
session) use ``Spider.is_blocked()`` / ``Spider.retry_blocked_request()``.
"""

from __future__ import annotations

import asyncio
import logging
from abc import ABC, abstractmethod
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any, ClassVar

import aiohttp

from bitscrape.browser import BrowserRuntime, LaunchConfig
from bitscrape.core.settings import Settings

if TYPE_CHECKING:  # pragma: no cover
    from bitscrape.core.models import Request
    from bitscrape.downloader.blocking import DomainBlocker
    from bitscrape.downloader.proxy import ProxyRotator

# NB: ``bitscrape.downloader`` imports this module, so its classes are only
# imported lazily / for typing here to avoid a circular import.

logger = logging.getLogger(__name__)

#: Session id used for the built-in HTTP session that serves any request that
#: doesn't name one.
DEFAULT_SESSION_ID = "default"

#: Kinds that exist in the API surface but are deliberately never implemented.
UNSUPPORTED_KINDS = frozenset({"impersonate", "stealth"})

#: Option names that request bot-detection evasion. Rejected up front so a
#: spider author never believes a protection was applied when it wasn't.
UNSUPPORTED_OPTIONS = frozenset(
    {
        "stealth",
        "impersonate",
        "http3",
        "solve_cloudflare",
        "hide_canvas",
        "block_webrtc",
        "allow_webgl",
        "dns_leak_protection",
        "fingerprint",
        # habits from other libraries; refused wherever an option list is checked
        "google_search",  # fabricates a Google Referer so a visit looks like organic search traffic
        "stealthy_headers",
    }
)

_POLICY_NOTE = (
    "bitscrape deliberately does not implement bot-detection evasion "
    "(TLS/browser fingerprint impersonation, stealth hardening, challenge "
    "solving) -- see ROADMAP.md's 'Explicit non-goals'."
)


class SessionError(ValueError):
    """Raised for session configuration mistakes (unknown id, duplicate id...)."""


def _reject_unsupported(sid: str, options: dict[str, Any]) -> None:
    bad = sorted(k for k in options if k in UNSUPPORTED_OPTIONS)
    if bad:
        raise NotImplementedError(f"Session {sid!r} sets option(s) {bad}. {_POLICY_NOTE}")


# ---------------------------------------------------------------------------
# Base class
# ---------------------------------------------------------------------------


class BaseSession(ABC):
    """A named, long-lived fetch context. Subclass :class:`HttpSession` or
    :class:`BrowserSession` rather than this class directly."""

    kind: ClassVar[str] = ""
    #: Whether the session starts on first use by default (see ``SessionManager.add``).
    default_lazy: ClassVar[bool] = False

    def __init__(self, *, max_concurrency: int | None = None) -> None:
        if max_concurrency is not None and max_concurrency < 1:
            raise SessionError("max_concurrency must be >= 1")
        self.sid: str | None = None
        self.max_concurrency = max_concurrency
        self.started = False
        self.closed = False
        # Counters, updated by the Downloader.
        self.requests_count = 0
        self.failures_count = 0
        self._start_lock = asyncio.Lock()
        self._semaphore: asyncio.Semaphore | None = None

    @property
    def semaphore(self) -> asyncio.Semaphore | None:
        """Per-session concurrency cap (``None`` = unlimited)."""
        if self.max_concurrency is None:
            return None
        if self._semaphore is None:
            self._semaphore = asyncio.Semaphore(self.max_concurrency)
        return self._semaphore

    @abstractmethod
    async def _start(self, settings: Settings) -> None: ...

    @abstractmethod
    async def _close(self) -> None: ...

    async def start(self, settings: Settings) -> None:
        """Start the session (idempotent and safe under concurrent callers)."""
        if self.started:
            return
        async with self._start_lock:
            if self.started:
                return
            await self._start(settings)
            self.started = True
            self.closed = False
            logger.info("Session %r (%s) started", self.sid, self.kind)

    async def close(self) -> None:
        if not self.started:
            return
        try:
            await self._close()
        finally:
            self.started = False
            self.closed = True
            logger.info("Session %r (%s) closed", self.sid, self.kind)

    async def cookies(self, url: str | None = None) -> dict[str, str]:
        """Cookies currently held by this session, as ``{name: value}``."""
        return {}

    def describe(self) -> dict[str, Any]:
        return {
            "sid": self.sid,
            "kind": self.kind,
            "started": self.started,
            "requests": self.requests_count,
            "failures": self.failures_count,
            "max_concurrency": self.max_concurrency,
        }

    def __repr__(self) -> str:
        return f"<{type(self).__name__} sid={self.sid!r} started={self.started}>"


# ---------------------------------------------------------------------------
# HTTP session
# ---------------------------------------------------------------------------


class HttpSession(BaseSession):
    """An ``aiohttp`` client with its own cookie jar and defaults.

    Args:
        headers: default headers sent with every request in this session.
        cookies: initial cookies (``{name: value}``).
        user_agent: overrides ``Settings.user_agent`` for this session.
        proxy: fixed proxy URL for this session.
        proxy_rotator: a :class:`~bitscrape.downloader.proxy.ProxyRotator` used
            instead of the global one (cannot be combined with ``proxy``).
        timeout: total request timeout in seconds (default: ``Settings.download_timeout``).
        verify_ssl: verify TLS certificates (default: ``Settings.verify_ssl``).
        follow_redirects: default: ``Settings.follow_redirects``.
        max_concurrency: cap on simultaneous requests through this session.
        dns_over_https: resolve hostnames through DNS-over-HTTPS instead of the
            system resolver: ``True`` (default provider / ``Settings.doh_url``) or a
            provider name / ``https://`` URL. See :mod:`bitscrape.dns`.
    """

    kind = "http"

    def __init__(
        self,
        *,
        headers: dict[str, str] | None = None,
        cookies: dict[str, str] | None = None,
        user_agent: str | None = None,
        proxy: str | None = None,
        proxy_rotator: ProxyRotator | None = None,
        timeout: float | None = None,
        verify_ssl: bool | None = None,
        follow_redirects: bool | None = None,
        max_concurrency: int | None = None,
        dns_over_https: bool | str | None = None,
    ) -> None:
        super().__init__(max_concurrency=max_concurrency)
        if proxy is not None and proxy_rotator is not None:
            raise SessionError("Use either `proxy` or `proxy_rotator`, not both.")
        self.dns_over_https = dns_over_https
        self._resolver: Any = None
        self.headers = dict(headers or {})
        self.initial_cookies = dict(cookies or {})
        self.user_agent = user_agent
        self.proxy = proxy
        self.proxy_rotator = proxy_rotator
        self.timeout = timeout
        self.verify_ssl = verify_ssl
        self.follow_redirects = follow_redirects
        self.client: aiohttp.ClientSession | None = None
        self._settings: Settings | None = None

    async def _start(self, settings: Settings) -> None:
        self._settings = settings
        verify = settings.verify_ssl if self.verify_ssl is None else self.verify_ssl
        from bitscrape.dns import make_resolver

        doh = settings.dns_over_https if self.dns_over_https is None else self.dns_over_https
        self._resolver = make_resolver(doh, settings.doh_url)
        connector = aiohttp.TCPConnector(
            limit=self.max_concurrency or settings.concurrent_requests,
            ttl_dns_cache=300,
            enable_cleanup_closed=True,
            ssl=bool(verify),
            **({"resolver": self._resolver} if self._resolver is not None else {}),
        )
        timeout = aiohttp.ClientTimeout(total=self.timeout or settings.download_timeout)
        headers = {
            **settings.default_headers,
            "User-Agent": self.user_agent or settings.user_agent,
            **self.headers,
        }
        self.client = aiohttp.ClientSession(
            connector=connector,
            timeout=timeout,
            headers=headers,
            # unsafe=True: accept cookies from IP-address hosts (e.g. an
            # internal service or a local test server); aiohttp's default jar
            # silently drops them, which would make the session look stateless.
            cookie_jar=aiohttp.CookieJar(unsafe=True),
            cookies=self.initial_cookies or None,
        )

    async def _close(self) -> None:
        if self.client is not None:
            await self.client.close()
            self.client = None
        if self._resolver is not None:
            await self._resolver.close()
            self._resolver = None

    async def cookies(self, url: str | None = None) -> dict[str, str]:
        if self.client is None:
            return {}
        if url:
            from yarl import URL

            morsels = self.client.cookie_jar.filter_cookies(URL(url))
        else:
            morsels = {m.key: m for m in self.client.cookie_jar}  # type: ignore[assignment]
        return {k: m.value for k, m in morsels.items()}

    def clear_cookies(self) -> None:
        """Forget every cookie (e.g. to log out)."""
        if self.client is not None:
            self.client.cookie_jar.clear()


class _BuiltinHttpSession(HttpSession):
    """The always-present ``"default"`` session. It wraps the ``Downloader``'s own
    shared client (created by ``Downloader.open()``), so a spider that never
    mentions sessions behaves exactly as before."""

    def __init__(self, client: aiohttp.ClientSession, settings: Settings) -> None:
        super().__init__()
        self.client = client
        self._settings = settings
        self.started = True

    async def _start(self, settings: Settings) -> None:  # already running
        return None

    async def _close(self) -> None:
        # The Downloader owns and closes the shared client.
        self.client = None


# ---------------------------------------------------------------------------
# Browser session
# ---------------------------------------------------------------------------


class BrowserSession(BaseSession):
    """One Playwright browser + persistent context shared by all requests routed to it.

    Unlike the per-request browser used for plain ``use_playwright=True``
    requests, a ``BrowserSession`` is launched **once**: cookies, local storage
    and logins persist across pages, and there is no per-request launch cost.
    Each request gets its own tab, closed when the request finishes. (The
    launching, page loading and XHR capture live in :mod:`bitscrape.browser`,
    shared with :class:`~bitscrape.fetchers.dynamic.DynamicFetcher`.)

    Args:
        headless: run without a visible window (default: ``Settings.playwright_headless``).
        browser: ``"chromium"`` | ``"firefox"`` | ``"webkit"`` (default: ``Settings.playwright_browser``).
        real_chrome: drive an installed Google Chrome instead of Playwright's
            bundled Chromium (sets ``channel="chrome"``).
        wait_until: ``"load"`` | ``"domcontentloaded"`` | ``"networkidle"`` | ``"commit"``.
        wait_selector / wait_selector_state: CSS selector to wait for after
            navigation, and the state to wait for (``"attached"`` (default),
            ``"visible"``, ``"hidden"``, ``"detached"``).
        wait: extra seconds to sit on the page after everything else, before reading it.
        timeout: seconds; applies to navigation and to the selector wait separately.
        locale / timezone / viewport / user_agent: context settings. ``user_agent``
            is sent as given; nothing is generated or rotated for you.
        headers: extra headers sent by every page in the session.
        proxy: proxy URL (``http://user:pass@host:port`` works) or a Playwright
            proxy dict, for the whole browser (browsers can't switch per tab).
        proxy_rotator: a :class:`~bitscrape.downloader.proxy.ProxyRotator` to
            draw the proxy from instead of a fixed ``proxy=``. Drawn **once**,
            the first time the browser launches -- unlike an HTTP session, a
            browser can't change proxy mid-session, so "rotation" here means
            each new ``BrowserSession``/``DynamicSession`` (or each one-shot
            ``DynamicFetcher`` call, which launches a fresh browser every
            time) gets a turn from the rotator, not that one long-lived
            session switches proxies partway through. Each fetch through the
            session still reports success/failure back to the rotator, so a
            session stuck on a proxy that starts failing gets it banned for
            the *next* launch, even though this session keeps using it (see
            ``docs/proxies.md``).
        storage_state: Playwright storage-state JSON path to start logged in.
        user_data_dir: persistent profile directory (keeps logins across runs).
        cdp_url: attach to an already-running browser instead of launching one.
        executable_path / channel / args: how to launch the browser.
        context_options: extra keyword arguments for Playwright's
            ``browser.new_context()`` (``geolocation``, ``permissions``,
            ``color_scheme``, ``ignore_https_errors``, ...).
        init_script: path to a JavaScript file run in every page before its own scripts.
        page_setup / page_action: hooks, ``hook(page)`` (sync or ``async``), run
            before navigation / after it. Code-defined, so they belong on the
            session (not in a serialisable request).
        capture_xhr: ``True`` (all XHR/fetch) or a URL regex; results land in
            ``response.extra["captured_xhr"]``.
        block_ads / block_domains / block_resource_types / disable_resources:
            request blocking (``disable_resources`` drops images, media, fonts and
            similar heavy, DOM-irrelevant resources).
        max_concurrency: max simultaneously open tabs (default 4).

    Options left as ``None`` fall back to the matching ``Settings.browser_*``.
    """

    kind = "browser"
    default_lazy = True  # don't launch a browser nobody uses

    _OPTION_NAMES = (
        "headless",
        "browser",
        "wait_until",
        "wait_selector",
        "wait_selector_state",
        "wait",
        "timeout",
        "locale",
        "timezone",
        "viewport",
        "user_agent",
        "headers",
        "proxy",
        "storage_state",
        "user_data_dir",
        "cdp_url",
        "executable_path",
        "channel",
        "args",
        "capture_xhr",
        "pierce_shadow",
        "record_requests",
        "max_recorded_requests",
    )

    def __init__(
        self,
        *,
        headless: bool | None = None,
        browser: str | None = None,
        real_chrome: bool = False,
        wait_until: str | None = None,
        wait_selector: str | None = None,
        wait_selector_state: str | None = None,
        wait: float | None = None,
        timeout: float | None = None,
        locale: str | None = None,
        timezone: str | None = None,
        viewport: tuple[int, int] | None = None,
        user_agent: str | None = None,
        headers: dict[str, str] | None = None,
        proxy: str | dict[str, str] | None = None,
        proxy_rotator: ProxyRotator | None = None,
        storage_state: str | None = None,
        user_data_dir: str | None = None,
        cdp_url: str | None = None,
        executable_path: str | None = None,
        channel: str | None = None,
        args: list[str] | None = None,
        context_options: dict[str, Any] | None = None,
        init_script: str | None = None,
        page_setup: Callable[[Any], Any] | None = None,
        page_action: Callable[[Any], Any] | None = None,
        capture_xhr: bool | str | None = None,
        record_requests: bool | None = None,
        max_recorded_requests: int | None = None,
        pierce_shadow: bool | None = None,
        block_ads: bool = False,
        block_domains: list[str] | None = None,
        block_resource_types: list[str] | None = None,
        allow_domains: list[str] | None = None,
        disable_resources: bool = False,
        max_concurrency: int | None = 4,
        playwright_factory: Callable[[], Any] | None = None,
    ) -> None:
        super().__init__(max_concurrency=max_concurrency)
        if real_chrome:
            if channel not in (None, "chrome"):
                raise SessionError("Pass either real_chrome=True or channel=..., not both.")
            channel = "chrome"
        if proxy is not None and proxy_rotator is not None:
            raise SessionError("Use either `proxy` or `proxy_rotator`, not both.")
        self.headless = headless
        self.browser = browser
        self.wait_until = wait_until
        self.wait_selector = wait_selector
        self.wait_selector_state = wait_selector_state
        self.wait = wait
        self.timeout = timeout
        self.locale = locale
        self.timezone = timezone
        self.viewport = viewport
        self.user_agent = user_agent
        self.headers = dict(headers or {})
        self.proxy = proxy
        self.proxy_rotator = proxy_rotator
        #: The proxy actually in use, once resolved -- ``None`` until then.
        #: Drawn from ``proxy_rotator`` (once -- a browser can't switch proxy
        #: mid-session) the first time :meth:`launch_config` runs; static
        #: ``proxy=`` otherwise. Read by the downloader to report success/
        #: failure for each fetch back to the rotator.
        self.resolved_proxy: str | dict[str, str] | None = proxy
        self.storage_state = storage_state
        self.user_data_dir = user_data_dir
        self.cdp_url = cdp_url
        self.executable_path = executable_path
        self.channel = channel
        self.args = list(args or [])
        self.context_options = dict(context_options or {})
        self.init_script = init_script
        self.page_setup = page_setup
        self.page_action = page_action
        self.capture_xhr = capture_xhr
        self.record_requests = record_requests
        self.max_recorded_requests = max_recorded_requests
        self.pierce_shadow = pierce_shadow
        self._recorder: Any = None
        if record_requests:
            from bitscrape.network import NetworkRecorder

            self._recorder = NetworkRecorder(max_entries=max_recorded_requests or 1000)
        self.blocker: DomainBlocker | None = None
        blocked_types = list(block_resource_types or [])
        if disable_resources:
            from bitscrape.browser import DISABLE_RESOURCES

            blocked_types = sorted({*blocked_types, *DISABLE_RESOURCES})
        if block_ads or block_domains or blocked_types:
            from bitscrape.downloader.blocking import DomainBlocker

            self.blocker = DomainBlocker(
                block_ads=block_ads,
                extra_domains=list(block_domains or []),
                block_resource_types=blocked_types,
                allow_domains=list(allow_domains or []),
            )
        self._playwright_factory = playwright_factory
        self._runtime: BrowserRuntime | None = None
        self._settings: Settings | None = None

    # -- option resolution ---------------------------------------------------

    def resolve_options(
        self, settings: Settings, overrides: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        """Effective options: per-request ``overrides`` > session > ``Settings``."""
        base: dict[str, Any] = {
            "headless": settings.playwright_headless,
            "browser": settings.playwright_browser,
            "wait_until": settings.browser_wait_until,
            "wait_selector": settings.browser_wait_selector,
            "wait_selector_state": "attached",
            "wait": 0.0,
            "timeout": settings.browser_timeout,
            "locale": settings.browser_locale,
            "timezone": settings.browser_timezone,
            "viewport": settings.browser_viewport,
            "user_agent": settings.user_agent,
            "headers": {},
            "proxy": None,
            "storage_state": settings.browser_storage_state,
            "user_data_dir": settings.browser_user_data_dir,
            "cdp_url": settings.browser_cdp_url,
            "executable_path": settings.browser_executable_path,
            "channel": settings.browser_channel,
            "args": list(settings.browser_args),
            "capture_xhr": settings.browser_capture_xhr,
            "pierce_shadow": settings.browser_pierce_shadow,
            "record_requests": settings.browser_record_requests,
            "max_recorded_requests": settings.browser_max_recorded_requests,
        }
        for name in self._OPTION_NAMES:
            value = getattr(self, name)
            if value is not None and value != [] and value != {}:
                base[name] = value
        for name, value in (overrides or {}).items():
            if name in base and value is not None:
                base[name] = value
        return base

    def launch_config(self, settings: Settings) -> LaunchConfig:
        """The browser launch/context configuration for this session.

        Resolves ``proxy_rotator`` into a concrete proxy the first time this
        runs (memoized in :attr:`resolved_proxy`), so calling this more than
        once -- it's also called once up front just to validate options --
        never draws a second proxy for the same session.
        """
        opts = self.resolve_options(settings)
        proxy = opts["proxy"]
        if self.proxy_rotator is not None:
            if self.resolved_proxy is None:
                self.resolved_proxy = self.proxy_rotator.get_proxy()
            proxy = self.resolved_proxy
        if self._recorder is None and opts["record_requests"]:
            from bitscrape.network import NetworkRecorder

            self._recorder = NetworkRecorder(max_entries=int(opts["max_recorded_requests"]))
        return LaunchConfig(
            recorder=self._recorder,
            headless=opts["headless"],
            browser=opts["browser"],
            channel=opts["channel"],
            executable_path=opts["executable_path"],
            cdp_url=opts["cdp_url"],
            user_data_dir=opts["user_data_dir"],
            proxy=proxy,
            args=list(opts["args"]),
            user_agent=opts["user_agent"],
            locale=opts["locale"],
            timezone=opts["timezone"],
            viewport=opts["viewport"],
            headers=dict(opts["headers"]),
            storage_state=opts["storage_state"],
            context_options=dict(self.context_options),
            init_scripts=[self.init_script] if self.init_script else [],
        )

    # -- lifecycle -----------------------------------------------------------

    async def _start(self, settings: Settings) -> None:
        self._settings = settings
        if settings.browser_driver != "playwright":
            raise NotImplementedError(
                f"settings.browser_driver={settings.browser_driver!r}: only 'playwright' is "
                "supported. Patched drivers such as 'patchright' exist to hide automation "
                "from bot-detection, which bitscrape deliberately does not implement -- "
                "see ROADMAP.md's 'Explicit non-goals'."
            )
        runtime = BrowserRuntime(self.launch_config(settings), self._playwright_factory)
        await runtime.start()
        self._runtime = runtime

    async def _close(self) -> None:
        if self._runtime is not None:
            await self._runtime.close()
            self._runtime = None

    @property
    def runtime(self) -> BrowserRuntime:
        if self._runtime is None:
            raise RuntimeError(f"BrowserSession {self.sid!r} is not started")
        return self._runtime

    @property
    def context(self) -> Any:
        return self.runtime.context

    def page(self, extra_headers: dict[str, str] | None = None) -> Any:
        """Open a tab in the shared context (an async context manager); it is closed on exit."""
        return self.runtime.page(extra_headers)

    async def add_cookies(self, cookies: dict[str, str], url: str) -> None:
        """Set cookies (``{name: value}``) for ``url`` in this session's context."""
        await self.context.add_cookies(
            [{"name": k, "value": v, "url": url} for k, v in cookies.items()]
        )

    async def cookies(self, url: str | None = None) -> dict[str, str]:
        if self._runtime is None:
            return {}
        raw = await self.context.cookies([url] if url else None)
        return {c["name"]: c["value"] for c in raw}

    @property
    def network(self) -> Any:
        """The :class:`~bitscrape.network.NetworkRecorder` holding this session's
        request history. Requires ``record_requests=True`` (or
        ``Settings.browser_record_requests``)."""
        if self._recorder is None:
            raise SessionError(
                f"BrowserSession {self.sid!r} is not recording requests; "
                "pass record_requests=True (or set Settings.browser_record_requests)."
            )
        return self._recorder

    async def save_state(self, path: str | Path) -> None:
        """Save cookies, localStorage and IndexedDB to a JSON file (Playwright's
        storage-state format, so it also works as ``storage_state=``). The parent
        directory must exist; an existing file is overwritten."""
        try:
            await self.context.storage_state(path=str(path), indexed_db=True)
        except TypeError:  # older Playwright: no indexed_db parameter
            await self.context.storage_state(path=str(path))

    async def load_state(self, path: str | Path) -> None:
        """Replace this session's cookies and localStorage with a file written by
        :meth:`save_state`. Call it inside a started session, then fetch (loading
        doesn't navigate open tabs).

        Cookies are restored directly. localStorage is restored per origin by an
        init script that runs when a page of that origin is created. IndexedDB
        can't be injected into a live context -- if the file has any, pass the file as
        ``storage_state=`` when creating the session instead (a warning says so).
        """
        import json as _json

        state = _json.loads(Path(path).read_text(encoding="utf-8"))
        if not isinstance(state, dict):
            raise ValueError(f"{path}: not a Playwright storage-state file")
        ctx = self.context
        await ctx.clear_cookies()
        cookies = state.get("cookies") or []
        if cookies:
            await ctx.add_cookies(cookies)
        origins = state.get("origins") or []
        local = {
            o["origin"]: {i["name"]: i["value"] for i in (o.get("localStorage") or [])}
            for o in origins
            if o.get("origin") and o.get("localStorage")
        }
        if local:
            payload = _json.dumps(local)
            await ctx.add_init_script(
                "(() => { try { const data = " + payload + "[location.origin];"
                " if (data) for (const [k, v] of Object.entries(data)) localStorage.setItem(k, v);"
                " } catch (e) {} })();"
            )
        if any(o.get("indexedDB") for o in origins):
            logger.warning(
                "load_state(%s): IndexedDB data was not restored on a live context; "
                "pass storage_state=%r when creating the session to include it.",
                path,
                str(path),
            )

    async def save_storage_state(self, path: str) -> None:
        """Write cookies + local storage to ``path`` (reusable as ``storage_state=``).
        Alias of :meth:`save_state`."""
        await self.save_state(path)


# ---------------------------------------------------------------------------
# Unsupported placeholder
# ---------------------------------------------------------------------------


class _UnsupportedSession(BaseSession):
    """Registered for a declared-but-unsupported kind so that the error surfaces
    clearly *when a request tries to use it* (and not for a spider that
    declares it yet never routes to it)."""

    def __init__(self, message: str) -> None:
        super().__init__()
        self.message = message

    async def _start(self, settings: Settings) -> None:
        raise NotImplementedError(self.message)

    async def _close(self) -> None:
        return None


# ---------------------------------------------------------------------------
# Manager
# ---------------------------------------------------------------------------


class SessionManager:
    """Registry and router for a crawl's sessions.

    ``add()`` a session under an id; requests select one via ``Request.session``
    (``session="rendered"``). The first session added -- or the one added with
    ``default=True`` -- serves requests that don't name a session.
    """

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or Settings()
        self._sessions: dict[str, BaseSession] = {}
        self._lazy: dict[str, bool] = {}
        self._default: str | None = None
        self._explicit_default = False
        self._opened = False

    # -- registry ------------------------------------------------------------

    def add(
        self,
        sid: str,
        session: BaseSession,
        *,
        default: bool = False,
        lazy: bool | None = None,
        replace: bool = False,
    ) -> SessionManager:
        """Register ``session`` under ``sid``.

        ``lazy=True`` delays starting it (e.g. launching a browser) until a
        request first uses it; ``lazy=None`` uses the session type's default
        (browser sessions are lazy, HTTP sessions are not).
        """
        if not sid or not isinstance(sid, str):
            raise SessionError("A session id must be a non-empty string")
        existing = self._sessions.get(sid)
        if existing is not None and not replace and not isinstance(existing, _BuiltinHttpSession):
            raise SessionError(f"A session named {sid!r} is already registered")
        if self._opened and lazy is False:
            raise SessionError(
                "Sessions added after the crawl started must be lazy; "
                "use manager.start_session(sid) to start one eagerly."
            )
        session.sid = sid
        self._sessions[sid] = session
        self._lazy[sid] = session.default_lazy if lazy is None else lazy
        if default:
            self._default = sid
            self._explicit_default = True
        elif self._default is None:
            self._default = sid
        return self

    def remove(self, sid: str) -> None:
        """Unregister ``sid`` (the session must be closed by the caller)."""
        self.pop(sid)

    def pop(self, sid: str) -> BaseSession:
        session = self.get(sid)
        del self._sessions[sid]
        self._lazy.pop(sid, None)
        if self._default == sid:
            self._default = next(iter(self._sessions), None)
            self._explicit_default = False
        return session

    def get(self, sid: str) -> BaseSession:
        try:
            return self._sessions[sid]
        except KeyError:
            raise SessionError(
                f"Request references session {sid!r}, but it isn't declared in "
                f"settings.sessions, Spider.sessions or Spider.configure_sessions(). "
                f"Known sessions: {sorted(self._sessions)}"
            ) from None

    def __contains__(self, sid: object) -> bool:
        return sid in self._sessions

    def __len__(self) -> int:
        return len(self._sessions)

    def __iter__(self) -> Any:
        return iter(self._sessions.values())

    @property
    def session_ids(self) -> list[str]:
        return list(self._sessions)

    @property
    def default_session_id(self) -> str:
        if self._default is None:
            raise SessionError("No sessions are registered")
        return self._default

    def is_lazy(self, sid: str) -> bool:
        return self._lazy.get(sid, False)

    # -- declarative config --------------------------------------------------

    def add_from_config(self, sid: str, config: dict[str, Any]) -> None:
        """Build and register a session from a ``Settings.sessions`` entry."""
        cfg = dict(config)
        kind = cfg.pop("kind", "http")
        default = bool(cfg.pop("default", False))
        lazy = cfg.pop("lazy", None)
        if kind in UNSUPPORTED_KINDS:
            self.add(
                sid,
                _UnsupportedSession(f"Session {sid!r} declares kind={kind!r}. {_POLICY_NOTE}"),
                default=default,
                lazy=True,
            )
            return
        _reject_unsupported(sid, cfg)
        session: BaseSession
        if kind == "http":
            session = HttpSession(**cfg)
        elif kind == "browser":
            session = BrowserSession(**cfg)
        else:
            raise SessionError(
                f"Session {sid!r} declares unrecognised kind={kind!r}; "
                "expected 'http' or 'browser'."
            )
        self.add(sid, session, default=default, lazy=lazy)

    @classmethod
    def from_settings(cls, settings: Settings) -> SessionManager:
        manager = cls(settings)
        for sid, cfg in settings.sessions.items():
            manager.add_from_config(sid, cfg)
        return manager

    # -- lifecycle -----------------------------------------------------------

    async def open(self) -> None:
        """Start every non-lazy session (lazy ones start on first use)."""
        self._opened = True
        for sid, session in list(self._sessions.items()):
            if not self._lazy.get(sid, False):
                await session.start(self.settings)

    async def start_session(self, sid: str) -> BaseSession:
        """Start ``sid`` now if it isn't running (used for lazy sessions)."""
        session = self.get(sid)
        if not session.started:
            await session.start(self.settings)
        return session

    async def close(self) -> None:
        """Close every started session, continuing past individual failures."""
        for session in list(self._sessions.values()):
            try:
                await session.close()
            except Exception:
                logger.exception("Error closing session %r", session.sid)
        self._opened = False

    # -- routing -------------------------------------------------------------

    async def resolve(self, request: Request) -> BaseSession | None:
        """Session that should serve ``request`` (started if lazy).

        Returns ``None`` for a legacy ``use_playwright=True`` request that names
        no session -- the Downloader keeps its per-request-browser path for those.
        """
        sid = request.session
        if sid is None:
            if request.use_playwright and not self._explicit_default:
                return None
            sid = self.default_session_id
        return await self.start_session(sid)

    def info(self) -> list[dict[str, Any]]:
        """A description of every session (id, kind, started, counters)."""
        return [
            {**s.describe(), "default": sid == self._default, "lazy": self._lazy.get(sid, False)}
            for sid, s in self._sessions.items()
        ]

    async def __aenter__(self) -> SessionManager:  # noqa: PYI034
        await self.open()
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.close()


__all__ = [
    "DEFAULT_SESSION_ID",
    "BaseSession",
    "BrowserSession",
    "HttpSession",
    "SessionError",
    "SessionManager",
]
