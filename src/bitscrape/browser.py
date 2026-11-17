"""
Browser automation core, shared by every place bitscrape drives a browser:
:class:`~bitscrape.fetchers.dynamic.DynamicFetcher` (direct fetches),
:class:`~bitscrape.sessions.BrowserSession` (browser sessions inside a spider)
and the :class:`~bitscrape.downloader.downloader.Downloader` that serves them.

Keeping one implementation means a fix lands everywhere at once. Building it
against a real Chromium (not just fakes) turned up three bugs that had been
sitting in the earlier per-place copies, all fixed here:

* **Captured XHR bodies were silently lost.** Playwright fires ``response``
  when *headers* arrive; the body may still be streaming. The old handler read
  the body in the background, the page was closed before it finished, the read
  raised, a blanket ``except`` swallowed it -- and the entry was recorded with
  ``body=None``, indistinguishable from a genuinely empty or binary body.
  :class:`XhrCapture` now waits (bounded) for in-flight bodies before the page
  closes, and an entry whose body really can't be had says *why* in ``error``.
* **Authenticated proxies failed with 407.** ``http://user:pass@host:port`` was
  handed to Chromium whole as the proxy ``server``; Chromium ignores credentials
  embedded there. :func:`playwright_proxy` splits them into the separate
  ``username``/``password`` fields Playwright needs.
* **Browser failures took the "unexpected error" path** in a spider (ERROR log
  with a full traceback, no per-session failure count, no ``process_exception``
  middleware). They are now :class:`~bitscrape.errors.DynamicFetchError`\\ s.

None of this imitates a human or hides that a browser is automated: there is no
fingerprint patching, header spoofing or challenge solving here (see
ROADMAP.md, "Explicit non-goals").
"""

from __future__ import annotations

import asyncio
import contextlib
import inspect
import logging
import re
import time
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlsplit

from bitscrape.errors import BrowserLaunchError, DynamicFetchError

logger = logging.getLogger(__name__)

#: Resource types dropped by ``disable_resources=True``: things that make a page
#: heavy but never change its DOM text. Stylesheets and scripts are deliberately
#: *not* in the set -- blocking them changes layout/visibility and breaks the
#: very JavaScript a browser fetch exists to run.
DISABLE_RESOURCES: frozenset[str] = frozenset(
    {"image", "media", "font", "texttrack", "websocket", "eventsource", "manifest"}
)

SELECTOR_STATES = ("attached", "detached", "visible", "hidden")
WAIT_UNTIL = ("commit", "domcontentloaded", "load", "networkidle")
BROWSERS = ("chromium", "firefox", "webkit")

#: How long to wait, after the page is otherwise done, for captured XHR bodies
#: that were still streaming (seconds).
XHR_DRAIN_TIMEOUT = 5.0


# ---------------------------------------------------------------------------
# Proxy
# ---------------------------------------------------------------------------


def playwright_proxy(proxy: str | dict[str, str] | None) -> dict[str, str] | None:
    """A proxy as the dict Playwright wants (``server`` plus optional
    ``username``/``password``/``bypass``).

    Accepts the ``http://user:pass@host:port`` form the HTTP side of bitscrape
    uses everywhere, and splits the credentials out (percent-decoded), because
    Chromium ignores credentials embedded in ``server``. A dict is passed
    through untouched, so ``{"server": ..., "bypass": "*.internal"}`` works.
    """
    if not proxy:
        return None
    if isinstance(proxy, dict):
        return dict(proxy)
    if "://" not in proxy:  # "host:port" -- Playwright reads it as an HTTP proxy
        return {"server": proxy}
    parts = urlsplit(proxy)
    host = parts.hostname or ""
    if ":" in host:  # IPv6 literal: urlsplit strips the brackets
        host = f"[{host}]"
    server = f"{parts.scheme}://{host}" + (f":{parts.port}" if parts.port else "")
    out = {"server": server}
    if parts.username is not None:
        out["username"] = unquote(parts.username)
        out["password"] = unquote(parts.password or "")
    return out


# ---------------------------------------------------------------------------
# XHR capture
# ---------------------------------------------------------------------------


class XhrCapture:
    """Records the page's background ``fetch``/``XMLHttpRequest`` responses.

    ``spec`` is ``True`` (everything) or a regex matched against the URL.
    Entries are appended in the order responses *arrive*; each is
    ``{"url", "method", "status", "headers", "body"}`` plus ``"error"`` when
    the body couldn't be had (so ``body=None`` always comes with a reason).
    Call :meth:`drain` before closing the page so bodies still streaming in
    aren't lost.
    """

    def __init__(self, spec: bool | str | None) -> None:
        self.entries: list[dict[str, Any]] = []
        self.enabled = bool(spec)
        self._pattern = re.compile(spec) if isinstance(spec, str) and spec else None
        self._tasks: dict[asyncio.Task[None], dict[str, Any]] = {}

    def attach(self, page: Any) -> None:
        if self.enabled:
            page.on("response", self._on_response)

    def _on_response(self, response: Any) -> None:
        try:
            request = response.request
            if request.resource_type not in ("xhr", "fetch"):
                return
            if self._pattern is not None and not self._pattern.search(response.url):
                return
            entry: dict[str, Any] = {
                "url": response.url,
                "method": request.method,
                "status": response.status,
                "headers": dict(response.headers),
                "body": None,
            }
        except Exception:
            logger.debug("Could not record an XHR response", exc_info=True)
            return
        self.entries.append(entry)
        task = asyncio.ensure_future(self._read_body(response, entry))
        self._tasks[task] = entry
        task.add_done_callback(lambda t: self._tasks.pop(t, None))

    @staticmethod
    async def _read_body(response: Any, entry: dict[str, Any]) -> None:
        try:
            raw = await response.body()
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 -- e.g. the page closed, or a redirect body
            entry["error"] = (
                f"body unavailable: {str(exc).splitlines()[0][:200] if str(exc) else type(exc).__name__}"
            )
            return
        try:
            entry["body"] = raw.decode("utf-8")
        except UnicodeDecodeError:
            entry["error"] = f"binary body ({len(raw)} bytes)"

    async def drain(self, timeout: float = XHR_DRAIN_TIMEOUT) -> None:
        """Wait up to ``timeout`` seconds for bodies still being read; any still
        unfinished are cancelled and marked with an ``error`` saying so."""
        pending = list(self._tasks)
        if not pending:
            return
        _done, not_done = await asyncio.wait(pending, timeout=timeout)
        for task in not_done:
            entry = self._tasks.get(task)
            task.cancel()
            if entry is not None and entry.get("body") is None:
                entry["error"] = f"body still streaming after {timeout:g}s; not captured"
        if not_done:
            await asyncio.gather(*not_done, return_exceptions=True)


# ---------------------------------------------------------------------------
# Scrolling
# ---------------------------------------------------------------------------


async def scroll_to_bottom(
    page: Any,
    max_scrolls: int = 20,
    pause_ms: int = 300,
    stable_rounds: int = 2,
    click_selector: str | None = None,
) -> int:
    """
    Reusable infinite-scroll / "load more" driver for browser fetches.

    A plain HTTP fetch only sees the initial DOM, so a site that lazy-loads on
    scroll returns nothing beyond that. This repeatedly scrolls the page to the
    bottom, waiting ``pause_ms`` between scrolls for lazy content to load, and
    stops once the page height hasn't grown for ``stable_rounds`` consecutive
    scrolls (or ``max_scrolls`` is hit, whichever comes first).

    If ``click_selector`` is given (e.g. ``"button.load-more"``), that element is
    clicked once per round (when present and visible) before measuring page
    height -- supporting "click to load more" pagination as well as pure
    scroll-triggered lazy loading.

    Returns the number of scroll rounds actually performed. Safe to call on
    pages with no scrollable content: it detects no height change and returns
    after ``stable_rounds`` rounds.
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
            except Exception:  # noqa: BLE001 -- best-effort: a missing/covered button just ends the loop
                logger.debug("click_selector %r not clickable this round", click_selector)

        await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        await page.wait_for_timeout(pause_ms)

        new_height = await page.evaluate("document.body.scrollHeight")
        if new_height == last_height:
            stable_count += 1
            if stable_count >= stable_rounds:
                break
        else:
            stable_count = 0
            last_height = new_height

    return rounds


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


def _is_playwright_error(exc: BaseException) -> bool:
    return type(exc).__module__.split(".", 1)[0] == "playwright"


def _first_line(exc: BaseException, limit: int = 300) -> str:
    text = str(exc).strip()
    line = text.splitlines()[0] if text else type(exc).__name__
    return line[:limit]


def translate_error(exc: BaseException, url: str) -> BaseException:
    """A Playwright error as a :class:`DynamicFetchError` (with ``retryable``
    set from what actually went wrong); anything else -- including a
    :class:`DynamicFetchError` already -- is returned unchanged."""
    if isinstance(exc, DynamicFetchError) or not _is_playwright_error(exc):
        return exc
    msg = _first_line(exc)
    low = str(exc).lower()
    if type(exc).__name__ == "TimeoutError":
        return DynamicFetchError(f"Timed out loading {url}: {msg}", url=url, retryable=True)
    if "download is starting" in low or ("net::err_aborted" in low and "download" in low):
        return DynamicFetchError(
            f"{url} triggers a file download, not a page. Use Fetcher.get() to fetch files.",
            url=url,
            retryable=False,
        )
    if any(
        s in low
        for s in (
            "invalid url",
            "err_invalid_url",
            "err_unknown_url_scheme",
            "err_cert_",
            "err_too_many_redirects",
            "err_unsafe_port",  # Chromium refuses certain ports (1, 22, 25, ...) outright
        )
    ):
        return DynamicFetchError(f"Can't load {url}: {msg}", url=url, retryable=False)
    if "has been closed" in low or "browser has disconnected" in low:
        return DynamicFetchError(
            f"The browser closed or crashed while loading {url} ({msg}). "
            "Create a new session to continue.",
            url=url,
            retryable=False,
        )
    return DynamicFetchError(f"Browser could not load {url}: {msg}", url=url, retryable=True)


def translate_launch_error(exc: BaseException, cfg: LaunchConfig) -> BaseException:
    """A Playwright launch/connect failure as a :class:`BrowserLaunchError`
    that says how to fix it; non-Playwright errors are returned unchanged."""
    if isinstance(exc, BrowserLaunchError) or not _is_playwright_error(exc):
        return exc
    text = str(exc)
    low = text.lower()
    if cfg.cdp_url and any(
        s in low for s in ("econnrefused", "connect_over_cdp", "websocket", "err_connection")
    ):
        return BrowserLaunchError(
            f"Couldn't connect to a browser at cdp_url={cfg.cdp_url!r}. Start Chrome with "
            "--remote-debugging-port=<port> first, and check the URL. "
            f"({_first_line(exc, 160)})"
        )
    if cfg.executable_path and any(
        s in low for s in ("executable doesn't exist", "enoent", "no such file")
    ):
        return BrowserLaunchError(
            f"No browser executable at executable_path={cfg.executable_path!r}. ({_first_line(exc, 160)})"
        )
    if "unsupported chromium channel" in low:
        return BrowserLaunchError(
            f"Unsupported browser channel {cfg.channel!r}. Playwright knows 'chrome', "
            "'chrome-beta', 'chrome-dev', 'chrome-canary', 'msedge', 'msedge-beta', "
            "'msedge-dev' and 'msedge-canary'."
        )
    if cfg.channel and ("is not found at" in low or "distribution" in low):
        which = "Google Chrome" if cfg.channel == "chrome" else f"the {cfg.channel!r} browser"
        return BrowserLaunchError(
            f"{which} (channel={cfg.channel!r}) isn't installed. Install it, or run "
            f"`playwright install {cfg.channel}`, or drop real_chrome/channel to use "
            "Playwright's bundled Chromium."
        )
    if "executable doesn't exist" in low or "playwright install" in low:
        return BrowserLaunchError(
            f"Playwright's {cfg.browser} browser isn't installed. Run: playwright install {cfg.browser}"
        )
    return BrowserLaunchError(f"Could not start the browser: {_first_line(exc)}")


# ---------------------------------------------------------------------------
# Shadow DOM
# ---------------------------------------------------------------------------

#: Serialises the live document to HTML, inlining every *open* shadow root as a
#: ``<shadow-root>`` child of its host. ``<slot>`` elements keep their tag and
#: attributes and contain their assigned nodes (or fallback content if nothing
#: is assigned); a shadow host's unassigned light-DOM children are omitted.
#: Closed roots and iframe documents are not traversed. The live page is not modified.
SHADOW_DOM_JS = """() => {
  const VOID = new Set(['area','base','br','col','embed','hr','img','input','link','meta','param','source','track','wbr']);
  const RAW = new Set(['script','style']);
  const esc = s => s.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
  const escA = s => s.replace(/&/g,'&amp;').replace(/"/g,'&quot;');
  const out = [];
  const kids = (nodes, raw) => { for (const n of nodes) ser(n, raw); };
  function ser(n, raw) {
    if (n.nodeType === 3) { out.push(raw ? n.data : esc(n.data)); return; }
    if (n.nodeType !== 1) return;
    const tag = n.localName;
    out.push('<' + tag);
    for (const a of n.attributes) out.push(' ' + a.name + '="' + escA(a.value) + '"');
    out.push('>');
    if (VOID.has(tag)) return;
    if (tag === 'slot') {
      const assigned = n.assignedNodes ? n.assignedNodes() : [];
      kids(assigned.length ? assigned : n.childNodes, false);
    } else if (n.shadowRoot) {
      out.push('<shadow-root>');
      kids(n.shadowRoot.childNodes, false);
      out.push('</shadow-root>');
    } else {
      kids(n.childNodes, RAW.has(tag));
    }
    out.push('</' + tag + '>');
  }
  ser(document.documentElement, false);
  return '<!DOCTYPE html>' + out.join('');
}"""


# ---------------------------------------------------------------------------
# Launching
# ---------------------------------------------------------------------------


@dataclass
class LaunchConfig:
    """Everything needed to start a browser and its one shared context."""

    headless: bool = True
    browser: str = "chromium"
    channel: str | None = None
    executable_path: str | None = None
    cdp_url: str | None = None
    user_data_dir: str | None = None
    proxy: str | dict[str, str] | None = None
    args: list[str] = field(default_factory=list)
    user_agent: str | None = None
    locale: str | None = None
    timezone: str | None = None
    viewport: tuple[int, int] | None = None
    headers: dict[str, str] = field(default_factory=dict)
    storage_state: str | None = None
    context_options: dict[str, Any] = field(default_factory=dict)
    init_scripts: list[str] = field(default_factory=list)
    #: A :class:`~bitscrape.network.NetworkRecorder` to attach to the context (or ``None``).
    recorder: Any = None

    def validate(self) -> None:
        if self.browser not in BROWSERS:
            raise ValueError(f"browser must be one of {BROWSERS}, got {self.browser!r}")
        if self.channel and self.browser != "chromium":
            raise ValueError("channel (e.g. 'chrome') only applies to the chromium browser")
        for path in self.init_scripts:
            if not Path(path).is_file():
                raise FileNotFoundError(f"init_script not found: {path}")

    def context_kwargs(self) -> dict[str, Any]:
        kw: dict[str, Any] = dict(self.context_options)
        if self.user_agent:
            kw["user_agent"] = self.user_agent
        if self.headers:
            kw["extra_http_headers"] = dict(self.headers)
        if self.locale:
            kw["locale"] = self.locale
        if self.timezone:
            kw["timezone_id"] = self.timezone
        if self.viewport:
            kw["viewport"] = {"width": self.viewport[0], "height": self.viewport[1]}
        if self.storage_state:
            kw["storage_state"] = self.storage_state
        return kw

    def launch_kwargs(self) -> dict[str, Any]:
        kw: dict[str, Any] = {"headless": self.headless}
        proxy = playwright_proxy(self.proxy)
        if proxy:
            kw["proxy"] = proxy
        if self.executable_path:
            kw["executable_path"] = self.executable_path
        if self.channel:
            kw["channel"] = self.channel
        if self.args:
            kw["args"] = list(self.args)
        return kw


class BrowserRuntime:
    """One browser process and one browser context, started once and reused
    for every page loaded through it (so cookies and logins persist, and there's
    no per-fetch launch cost). Each page is its own tab, closed after use."""

    def __init__(
        self, cfg: LaunchConfig, playwright_factory: Callable[[], Any] | None = None
    ) -> None:
        cfg.validate()
        self.cfg = cfg
        self._factory = playwright_factory
        self._pw: Any = None
        self._browser: Any = None
        self._context: Any = None
        self._owns_browser = True

    @property
    def started(self) -> bool:
        return self._context is not None

    @property
    def recorder(self) -> Any:
        """The :class:`~bitscrape.network.NetworkRecorder` attached to this context, if any."""
        return self.cfg.recorder

    @property
    def context(self) -> Any:
        if self._context is None:
            raise RuntimeError("The browser is not started")
        return self._context

    async def start(self) -> None:
        cfg = self.cfg
        try:
            if self._factory is not None:
                self._pw = await self._factory()
            else:
                try:
                    from playwright.async_api import async_playwright
                except ImportError as err:
                    raise ImportError(
                        "Browser fetching requires Playwright. Install with: "
                        "pip install bitscrape[playwright] && playwright install chromium"
                    ) from err
                self._pw = await async_playwright().start()

            browser_type = getattr(self._pw, cfg.browser)
            ctx_kwargs = cfg.context_kwargs()
            if cfg.cdp_url:
                # An externally managed browser: reuse it, never close it.
                self._browser = await browser_type.connect_over_cdp(cfg.cdp_url)
                self._context = await self._browser.new_context(**ctx_kwargs)
                self._owns_browser = False
            elif cfg.user_data_dir:
                self._context = await browser_type.launch_persistent_context(
                    cfg.user_data_dir, **cfg.launch_kwargs(), **ctx_kwargs
                )
                self._browser = None
                self._owns_browser = False
            else:
                self._browser = await browser_type.launch(**cfg.launch_kwargs())
                self._context = await self._browser.new_context(**ctx_kwargs)
                self._owns_browser = True
            for script in cfg.init_scripts:
                await self._context.add_init_script(path=script)
            if cfg.recorder is not None:
                cfg.recorder.attach(self._context)
        except BaseException as exc:
            await self._teardown()
            raise translate_launch_error(exc, cfg) from exc

    async def close(self) -> None:
        await self._teardown()

    async def _teardown(self) -> None:
        with contextlib.suppress(Exception):
            if self._context is not None:
                await self._context.close()
        with contextlib.suppress(Exception):
            if self._owns_browser and self._browser is not None:
                await self._browser.close()
        with contextlib.suppress(Exception):
            if self._pw is not None:
                await self._pw.stop()
        self._context = self._browser = self._pw = None

    @contextlib.asynccontextmanager
    async def page(self, extra_headers: dict[str, str] | None = None) -> AsyncIterator[Any]:
        """A new tab in the shared context, closed on exit."""
        page = await self.context.new_page()
        try:
            if extra_headers:
                await page.set_extra_http_headers(extra_headers)
            yield page
        finally:
            with contextlib.suppress(Exception):
                await page.close()


# ---------------------------------------------------------------------------
# Loading one page
# ---------------------------------------------------------------------------

PageHook = Callable[[Any], Any]


@dataclass
class PageRequest:
    """One page load. ``timeout`` (seconds) applies to each step separately --
    navigation, the selector wait, and every Playwright call inside your hooks."""

    url: str
    timeout: float = 30.0
    wait_until: str = "load"
    wait_selector: str | None = None
    wait_selector_state: str = "attached"
    wait: float = 0.0
    scroll: bool | dict[str, Any] | None = None
    screenshot: bool | dict[str, Any] | None = None
    capture_xhr: bool | str | None = None
    pierce_shadow: bool = False
    headers: dict[str, str] = field(default_factory=dict)
    cookies: dict[str, str] | list[dict[str, Any]] | None = None
    page_setup: PageHook | None = None
    page_action: PageHook | None = None
    blocker: Any = None

    def validate(self) -> None:
        if self.wait_until not in WAIT_UNTIL:
            raise ValueError(f"wait_until must be one of {WAIT_UNTIL}, got {self.wait_until!r}")
        if self.wait_selector_state not in SELECTOR_STATES:
            raise ValueError(
                f"wait_selector_state must be one of {SELECTOR_STATES}, got {self.wait_selector_state!r}"
            )
        if self.timeout <= 0:
            raise ValueError("timeout must be > 0")
        if self.wait < 0:
            raise ValueError("wait must be >= 0")


@dataclass
class PageResult:
    status: int
    headers: dict[str, str]
    body: bytes
    url: str
    encoding: str
    elapsed_ms: float
    extra: dict[str, Any] = field(default_factory=dict)


def cookie_list(cookies: dict[str, str] | list[dict[str, Any]], url: str) -> list[dict[str, Any]]:
    """Cookies in Playwright's shape. A ``{name: value}`` dict is scoped to
    ``url``; a list of dicts is passed through (each needs ``url`` or ``domain``)."""
    if isinstance(cookies, dict):
        return [{"name": k, "value": v, "url": url} for k, v in cookies.items()]
    return [dict(c) for c in cookies]


async def _run_hook(name: str, hook: PageHook, page: Any, url: str) -> None:
    """Call a user hook (sync or async), turning whatever it raises into a
    :class:`DynamicFetchError` that names the hook and chains the original."""
    try:
        result = hook(page)
        if inspect.isawaitable(result):
            await result
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        raise DynamicFetchError(
            f"{name} raised {type(exc).__name__}: {_first_line(exc)}", url=url, retryable=False
        ) from exc


def _charset(content_type: str) -> str | None:
    match = re.search(r"charset=([\w.-]+)", content_type, re.IGNORECASE)
    return match.group(1) if match else None


async def _read_body(page: Any, resp: Any, pierce_shadow: bool = False) -> tuple[bytes, str]:
    """The DOM as it is now -- except when the URL isn't an HTML document at all
    (JSON, plain text, ...), where the browser's view is just a wrapper and the
    real response body is what the caller wants."""
    if resp is not None:
        ctype = (resp.headers.get("content-type") or "").lower()
        if ctype and "html" not in ctype and "xml" not in ctype:
            with contextlib.suppress(Exception):
                return await resp.body(), _charset(ctype) or "utf-8"
    last: Exception | None = None
    for _ in range(3):  # page.content() fails if a client-side redirect is mid-flight
        try:
            if pierce_shadow:
                return str(await page.evaluate(SHADOW_DOM_JS)).encode("utf-8"), "utf-8"
            return (await page.content()).encode("utf-8"), "utf-8"
        except Exception as exc:
            if "navigating" not in str(exc).lower():
                raise
            last = exc
            with contextlib.suppress(Exception):
                await page.wait_for_load_state("load")
    assert last is not None
    raise last


async def load_page(runtime: BrowserRuntime, req: PageRequest) -> PageResult:
    """Load ``req.url`` in a fresh tab and return what the browser ended up with.

    Steps, in order: cookies → tab opens → resource blocking / XHR capture →
    ``page_setup`` → navigation → ``page_action`` → ``wait_selector`` → scrolling
    → ``wait`` → screenshot → let in-flight XHR bodies finish → read the DOM.
    ``page_action`` runs before ``wait_selector`` so an action (a click, a
    search) can be what makes the selector appear.

    Raises :class:`DynamicFetchError` for navigation failures, timeouts and
    hook errors; anything that isn't a Playwright error propagates unchanged.
    """
    req.validate()
    t0 = time.monotonic()
    timeout_ms = req.timeout * 1000
    try:
        if req.cookies:
            await runtime.context.add_cookies(cookie_list(req.cookies, req.url))
        async with runtime.page(req.headers or None) as page:
            page.set_default_timeout(timeout_ms)
            capture = XhrCapture(req.capture_xhr)
            if req.blocker is not None:
                await page.route("**/*", req.blocker.route_handler)
            capture.attach(page)
            if req.page_setup is not None:
                await _run_hook("page_setup", req.page_setup, page, req.url)
            resp = await page.goto(req.url, wait_until=req.wait_until, timeout=timeout_ms)
            if req.page_action is not None:
                await _run_hook("page_action", req.page_action, page, req.url)
            if req.wait_selector:
                await page.wait_for_selector(
                    req.wait_selector, state=req.wait_selector_state, timeout=timeout_ms
                )
            if req.scroll:
                await scroll_to_bottom(page, **(req.scroll if isinstance(req.scroll, dict) else {}))
            if req.wait:
                await asyncio.sleep(req.wait)
            extra: dict[str, Any] = {}
            if req.screenshot:
                shot_opts = dict(req.screenshot) if isinstance(req.screenshot, dict) else {}
                shot_opts.setdefault("full_page", True)
                extra["screenshot"] = await page.screenshot(**shot_opts)
            await capture.drain(min(XHR_DRAIN_TIMEOUT, req.timeout))
            if runtime.recorder is not None:
                await runtime.recorder.drain(min(XHR_DRAIN_TIMEOUT, req.timeout))
            if capture.entries:
                extra["captured_xhr"] = capture.entries
            body, encoding = await _read_body(page, resp, req.pierce_shadow)
            final_url = page.url or req.url
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        translated = translate_error(exc, req.url)
        if translated is exc:
            raise
        raise translated from exc
    return PageResult(
        status=resp.status if resp else 200,
        headers=dict(resp.headers) if resp else {},
        body=body,
        url=final_url,
        encoding=encoding,
        elapsed_ms=(time.monotonic() - t0) * 1000,
        extra=extra,
    )


__all__ = [
    "DISABLE_RESOURCES",
    "BrowserRuntime",
    "LaunchConfig",
    "PageRequest",
    "PageResult",
    "SHADOW_DOM_JS",
    "XhrCapture",
    "cookie_list",
    "load_page",
    "playwright_proxy",
    "scroll_to_bottom",
    "translate_error",
    "translate_launch_error",
]
