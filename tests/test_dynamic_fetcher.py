"""
Tests for ``DynamicFetcher`` (``bitscrape.fetchers.dynamic``) and the shared
browser core it runs on (``bitscrape.browser``), which ``BrowserSession`` and
the spider ``Downloader`` use too.

Two halves:

* **No browser needed** -- option validation, the refused evasion options,
  error translation (against the *real* Playwright error text), proxy parsing,
  XHR-capture bookkeeping, and the plumbing, via fake Playwright objects.
* **A real Chromium** -- JavaScript rendering, hooks, scrolling, XHR capture,
  cookies, proxies (a real authenticating proxy), CDP, persistent profiles,
  resource blocking, failure modes, and process cleanup. These skip themselves
  when no browser can be launched, so the suite still runs in an environment
  without ``playwright install``.

The real-browser tests found three bugs that fakes had hidden (a streaming
XHR body recorded as ``None``, authenticated proxies failing with 407, and
browser failures taking the spider's "unexpected error" path); each has a
regression test below, marked ``Regression``.

Note on Chrome: in the sandbox this was developed in, ``/opt/google/chrome/chrome``
is a symlink to Playwright's Chromium, so ``real_chrome=True`` is tested for its
*plumbing* (the ``chrome`` channel is requested and a fetch works), not for
Chrome-specific behaviour.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import socket
import subprocess
import sys
import threading
import time
from collections import defaultdict
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import pytest
from aiohttp import web

from bitscrape.browser import (
    DISABLE_RESOURCES,
    BrowserRuntime,
    LaunchConfig,
    PageRequest,
    XhrCapture,
    cookie_list,
    load_page,
    playwright_proxy,
    translate_error,
    translate_launch_error,
)
from bitscrape.core.models import Request
from bitscrape.core.settings import Settings
from bitscrape.core.spider import Spider
from bitscrape.downloader.downloader import DownloadError, Downloader
from bitscrape.errors import BrowserLaunchError, DynamicFetchError, FetchError
from bitscrape.fetchers import (
    AsyncDynamicFetcher,
    AsyncDynamicSession,
    DynamicFetcher,
    DynamicSession,
)
from bitscrape.fetchers.dynamic import _wait_until
from bitscrape.parser.selector import ParsedResponse
from bitscrape.sessions import BrowserSession, SessionError, SessionManager
from tests.test_fetchers import _ThreadedServer
from tests.test_multi_session import _FakePW

# ---------------------------------------------------------------------------
# A real-browser availability gate
# ---------------------------------------------------------------------------


@pytest.fixture(scope="session")
def real_browser() -> None:
    """Skip real-browser tests when Playwright or its Chromium isn't available."""
    pytest.importorskip("playwright")

    async def probe() -> None:
        rt = BrowserRuntime(LaunchConfig())
        await rt.start()
        await rt.close()

    try:
        asyncio.run(probe())
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"no launchable Chromium here: {str(exc).splitlines()[0][:100]}")


@pytest.fixture(scope="session")
def chromium_path(real_browser: None) -> str:
    """Path of Playwright's Chromium binary (asked of a subprocess, so the sync
    API never touches this process's event loops)."""
    code = (
        "from playwright.sync_api import sync_playwright\n"
        "with sync_playwright() as p: print(p.chromium.executable_path)"
    )
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=60)
    path = out.stdout.strip().splitlines()[-1] if out.stdout.strip() else ""
    if not path or not Path(path).exists():
        pytest.skip("couldn't locate the Chromium executable")
    return path


# ---------------------------------------------------------------------------
# The test site
# ---------------------------------------------------------------------------

PRODUCTS_PAGE = """<html><head><title>Shop</title></head><body><ul id="list"></ul>
<script>
fetch('/api/products').then(r => r.json()).then(items => {
  document.getElementById('list').innerHTML = items.map(i => `<li class="product">${i}</li>`).join('');
});
</script></body></html>"""

SCROLL_PAGE = """<html><head><style>.item{height:400px}</style></head><body><div id="list"></div><script>
let n = 0;
function more() { for (let i = 0; i < 5; i++) { const d = document.createElement('div'); d.className = 'item';
  d.textContent = 'item' + (n++); document.getElementById('list').appendChild(d); } }
more();
let busy = false;
window.addEventListener('scroll', () => {
  if (!busy && n < 25 && window.innerHeight + scrollY >= document.body.scrollHeight - 5) {
    busy = true; setTimeout(() => { more(); busy = false; }, 100);
  }
});
</script></body></html>"""

XHR_PAGE = """<html><body><div id="done">pending</div><script>
Promise.all([fetch('/api/a').then(r => r.text()), fetch('/api/b').then(r => r.text()),
             fetch('/other.txt').then(r => r.text())]).then(() => {
  document.getElementById('done').textContent = 'all-done';
});
</script></body></html>"""

STREAM_PAGE = """<html><body><div id="done">pending</div><script>
fetch('/api/fast').then(r => r.text()).then(() => { document.getElementById('done').textContent = 'fast-done'; });
fetch('/api/streaming').then(r => r.text());
</script></body></html>"""

ENV_PAGE = """<html><body>
<div id="lang"></div><div id="tz"></div><div id="size"></div><div id="scheme"></div><div id="ua"></div>
<script>
document.getElementById('lang').textContent = navigator.language;
document.getElementById('tz').textContent = Intl.DateTimeFormat().resolvedOptions().timeZone;
document.getElementById('size').textContent = innerWidth + 'x' + innerHeight;
document.getElementById('scheme').textContent = matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light';
document.getElementById('ua').textContent = navigator.userAgent;
</script></body></html>"""

PNG_1X1 = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg=="
)


class _Site:
    def __init__(self) -> None:
        self.hits: dict[str, int] = defaultdict(int)
        self.in_flight = 0
        self.max_in_flight = 0


def _site_app(st: _Site) -> web.Application:
    def html(body: str, *, status: int = 200, **headers: str) -> web.Response:
        return web.Response(text=body, status=status, content_type="text/html", headers=headers or None)

    async def products(r: web.Request) -> web.Response:
        return html(PRODUCTS_PAGE)

    async def api_products(r: web.Request) -> web.Response:
        return web.json_response(["Widget", "Gadget", "Gizmo"])

    async def static(r: web.Request) -> web.Response:
        return html("<html><head><title>Static</title></head><body><h1>Static page</h1></body></html>")

    async def delayed(r: web.Request) -> web.Response:
        return html(
            "<div id='late'>pending</div><script>setTimeout(() => "
            "document.getElementById('late').textContent = 'arrived', 300)</script>"
        )

    async def slow_api_page(r: web.Request) -> web.Response:
        return html(
            "<div id='out'>pending</div><script>fetch('/api/slow').then(r => r.json())"
            ".then(j => document.getElementById('out').textContent = 'slow:' + j.v)</script>"
        )

    async def api_slow(r: web.Request) -> web.Response:
        await asyncio.sleep(0.4)
        return web.json_response({"v": 7})

    async def click_page(r: web.Request) -> web.Response:
        return html(
            "<button id='more'>more</button><div id='out'></div><script>"
            "document.getElementById('more').onclick = () => fetch('/api/more').then(r => r.text())"
            ".then(t => { document.getElementById('out').innerHTML = '<p class=revealed>' + t + '</p>'; });"
            "</script>"
        )

    async def api_more(r: web.Request) -> web.Response:
        return web.Response(text="revealed-by-click")

    async def search_page(r: web.Request) -> web.Response:
        return html(
            "<input id='q'><button id='go'>go</button><div id='results'></div><script>"
            "document.getElementById('go').onclick = () => document.getElementById('results')"
            ".textContent = 'results for ' + document.getElementById('q').value;</script>"
        )

    async def scroll(r: web.Request) -> web.Response:
        return html(SCROLL_PAGE)

    async def xhr(r: web.Request) -> web.Response:
        return html(XHR_PAGE)

    async def api_a(r: web.Request) -> web.Response:
        return web.json_response({"which": "a"})

    async def api_b(r: web.Request) -> web.Response:
        return web.json_response({"which": "b"})

    async def other_txt(r: web.Request) -> web.Response:
        return web.Response(text="plain-other", content_type="text/plain")

    async def stream_page(r: web.Request) -> web.Response:
        return html(STREAM_PAGE)

    async def api_fast(r: web.Request) -> web.Response:
        return web.json_response({"k": "fast"})

    async def api_streaming(r: web.Request) -> web.StreamResponse:
        resp = web.StreamResponse(headers={"Content-Type": "application/json"})
        await resp.prepare(r)  # headers go out now -> the browser's 'response' event fires
        await resp.write(b'{"k": "stre')
        await asyncio.sleep(0.8)  # ...while the body is still arriving
        await resp.write(b'aming"}')
        await resp.write_eof()
        return resp

    async def api_binary(r: web.Request) -> web.Response:
        return web.Response(body=bytes([0xFF, 0xFE, 0x00, 0x80]), content_type="application/octet-stream")

    async def binary_page(r: web.Request) -> web.Response:
        return html("<script>fetch('/api/binary').then(r => r.arrayBuffer())</script><p>x</p>")

    async def json_ep(r: web.Request) -> web.Response:
        return web.json_response({"hello": "world"})

    async def text_ep(r: web.Request) -> web.Response:
        return web.Response(text="plain text body", content_type="text/plain")

    async def download(r: web.Request) -> web.Response:
        return web.Response(
            body=b"file-bytes",
            content_type="application/octet-stream",
            headers={"Content-Disposition": 'attachment; filename="f.bin"'},
        )

    async def status(r: web.Request) -> web.Response:
        code = int(r.match_info["code"])
        return html(f"<h1>status {code}</h1>", status=code)

    async def redirect(r: web.Request) -> web.Response:
        raise web.HTTPFound("/static")

    async def js_redirect(r: web.Request) -> web.Response:
        return html("<script>location.replace('/static')</script>")

    async def meta_refresh(r: web.Request) -> web.Response:
        return html("<meta http-equiv='refresh' content='0;url=/static'>")

    async def redirect_loop(r: web.Request) -> web.Response:
        raise web.HTTPFound("/redirect-loop")

    async def set_cookie(r: web.Request) -> web.Response:
        resp = html("<p>cookie set</p>")
        resp.set_cookie(r.match_info["name"], r.match_info["value"])
        return resp

    async def set_persistent_cookie(r: web.Request) -> web.Response:
        resp = html("<p>cookie set</p>")
        resp.set_cookie(r.match_info["name"], r.match_info["value"], max_age=3600)
        return resp

    async def echo(r: web.Request) -> web.Response:
        return web.json_response(
            {"headers": {k.lower(): v for k, v in r.headers.items()}, "cookies": dict(r.cookies)}
        )

    async def echo_page(r: web.Request) -> web.Response:
        data = json.dumps({"headers": {k.lower(): v for k, v in r.headers.items()}, "cookies": dict(r.cookies)})
        return html(f"<pre id='data'>{data}</pre>")

    async def cookie_js(r: web.Request) -> web.Response:
        return html("<div id='c'></div><script>document.getElementById('c').textContent = document.cookie</script>")

    async def images(r: web.Request) -> web.Response:
        return html(
            "<link rel='stylesheet' href='/s.css'><body><h1 id='t'>images</h1>"
            "<img src='/img/1.png'><img src='/img/2.png'><img src='/img/3.png'></body>"
        )

    async def img(r: web.Request) -> web.Response:
        st.hits["img"] += 1
        return web.Response(body=PNG_1X1, content_type="image/png")

    async def css(r: web.Request) -> web.Response:
        st.hits["css"] += 1
        return web.Response(text="body{color:red}", content_type="text/css")

    async def third_party(r: web.Request) -> web.Response:
        port = r.url.port
        return html(
            f"<div id='t'>?</div><script src='http://localhost:{port}/tracker.js'></script>"
            "<script>document.getElementById('t').textContent = String(window.__tracked)</script>"
        )

    async def tracker(r: web.Request) -> web.Response:
        st.hits["tracker"] += 1
        return web.Response(text="window.__tracked = true;", content_type="application/javascript")

    async def env(r: web.Request) -> web.Response:
        return html(ENV_PAGE)

    async def init_flag(r: web.Request) -> web.Response:
        return html("<div id='f'></div><script>document.getElementById('f').textContent = String(window.__flag)</script>")

    async def slow(r: web.Request) -> web.Response:
        await asyncio.sleep(float(r.query.get("delay", "2")))
        return html("<p>finally</p>")

    async def unicode_page(r: web.Request) -> web.Response:
        return html("<meta charset='utf-8'><h1>日本語 ✓ ünïcode</h1>")

    async def latin1(r: web.Request) -> web.Response:
        return web.Response(
            body="<html><body><h1>café</h1></body></html>".encode("latin-1"),
            headers={"Content-Type": "text/html; charset=iso-8859-1"},
        )

    async def console_page(r: web.Request) -> web.Response:
        return html("<script>console.log('hello-console')</script><p>x</p>")

    async def mock_page(r: web.Request) -> web.Response:
        return html(
            "<div id='src'>?</div><script>fetch('/api/real').then(r => r.json())"
            ".then(j => document.getElementById('src').textContent = j.source)</script>"
        )

    async def api_real(r: web.Request) -> web.Response:
        return web.json_response({"source": "real"})

    async def xhr_redirect_page(r: web.Request) -> web.Response:
        return html("<script>fetch('/api/redir').then(r => r.text())</script><p>x</p>")

    async def api_redir(r: web.Request) -> web.Response:
        raise web.HTTPFound("/api/a")

    async def nav_source(r: web.Request) -> web.Response:
        # redirects itself shortly after load, to a target that is slow to respond
        return html("<h1>source</h1><script>setTimeout(() => { location.href = '/nav-target'; }, 30)</script>")

    async def nav_target(r: web.Request) -> web.Response:
        await asyncio.sleep(0.4)
        return html("<h1 id='t'>target</h1>")

    async def hold(r: web.Request) -> web.Response:
        st.in_flight += 1
        st.max_in_flight = max(st.max_in_flight, st.in_flight)
        await asyncio.sleep(0.5)
        st.in_flight -= 1
        return html(f"<p id='n'>{r.query.get('n', '')}</p>")

    async def many_xhr(r: web.Request) -> web.Response:
        return html("<script>fetch('/api/a').then(r => r.text()); fetch('/api/b').then(r => r.text());</script><p>x</p>")

    app = web.Application()
    for path, handler in [
        ("/", products),
        ("/api/products", api_products),
        ("/static", static),
        ("/delayed", delayed),
        ("/slow-api-page", slow_api_page),
        ("/api/slow", api_slow),
        ("/click", click_page),
        ("/api/more", api_more),
        ("/search", search_page),
        ("/scroll", scroll),
        ("/xhr", xhr),
        ("/api/a", api_a),
        ("/api/b", api_b),
        ("/other.txt", other_txt),
        ("/stream", stream_page),
        ("/api/fast", api_fast),
        ("/api/streaming", api_streaming),
        ("/api/binary", api_binary),
        ("/binary-page", binary_page),
        ("/json", json_ep),
        ("/text", text_ep),
        ("/download", download),
        ("/redirect", redirect),
        ("/js-redirect", js_redirect),
        ("/meta-refresh", meta_refresh),
        ("/redirect-loop", redirect_loop),
        ("/echo", echo),
        ("/echo-page", echo_page),
        ("/cookie-js", cookie_js),
        ("/images", images),
        ("/third-party", third_party),
        ("/tracker.js", tracker),
        ("/s.css", css),
        ("/env", env),
        ("/init-flag", init_flag),
        ("/slow", slow),
        ("/unicode", unicode_page),
        ("/latin1", latin1),
        ("/console", console_page),
        ("/mock", mock_page),
        ("/api/real", api_real),
        ("/xhr-redirect", xhr_redirect_page),
        ("/api/redir", api_redir),
        ("/hold", hold),
        ("/nav-source", nav_source),
        ("/nav-target", nav_target),
        ("/many-xhr", many_xhr),
    ]:
        app.router.add_get(path, handler)
    app.router.add_get("/img/{n}.png", img)
    app.router.add_get("/status/{code}", status)
    app.router.add_get("/set-cookie/{name}/{value}", set_cookie)
    app.router.add_get("/set-persistent-cookie/{name}/{value}", set_persistent_cookie)
    return app


@pytest.fixture(scope="module")
def site(real_browser: None):
    st = _Site()
    srv = _ThreadedServer(_site_app(st))
    srv.start()
    srv.state = st  # type: ignore[attr-defined]
    try:
        yield srv
    finally:
        srv.stop()


@pytest.fixture(scope="module")
def dyn(site: _ThreadedServer):
    """One shared browser for the tests that don't need isolation -- which is
    also how this class is meant to be used for more than a page or two."""
    with DynamicSession() as session:
        yield session


def _echo(page: ParsedResponse) -> dict[str, Any]:
    """The JSON an /echo-page fetch put inside <pre id=data>."""
    return json.loads(page.css("#data::text").get())


# ===========================================================================
# PART 1 -- no browser needed
# ===========================================================================

# ---------------------------------------------------------------------------
# Option validation and the refused evasion options
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "option",
    ["stealth", "hide_canvas", "block_webrtc", "allow_webgl", "solve_cloudflare", "google_search",
     "impersonate", "fingerprint", "stealthy_headers"],
)
def test_evasion_options_are_refused_everywhere(option):
    for call in (
        lambda: AsyncDynamicSession(**{option: True}),
        lambda: DynamicSession(**{option: True}),
        lambda: DynamicFetcher.fetch("http://127.0.0.1:1/", **{option: True}),
    ):
        with pytest.raises(NotImplementedError, match="Explicit non-goals"):
            call()


async def test_evasion_options_are_refused_by_the_async_one_shot_and_on_fetch():
    with pytest.raises(NotImplementedError, match="Explicit non-goals"):
        await AsyncDynamicFetcher.fetch("http://127.0.0.1:1/", stealth=True)
    session = AsyncDynamicSession()
    with pytest.raises(NotImplementedError, match="Explicit non-goals"):
        await session.fetch("http://127.0.0.1:1/", google_search=True)


def test_unknown_keyword_is_a_type_error():
    with pytest.raises(TypeError, match="bogus"):
        AsyncDynamicSession(bogus=1)


@pytest.mark.parametrize(
    "kwargs,match",
    [
        ({"timeout": 0}, "timeout"),
        ({"timeout": -3}, "timeout"),
        ({"wait": -1}, "wait"),
        ({"wait_selector_state": "gone"}, "wait_selector_state"),
        ({"channel": "chrome", "real_chrome": False, "executable_path": None, "cdp_url": None}, None),
    ],
)
def test_bad_defaults_fail_at_construction_not_after_a_browser_launch(kwargs, match):
    if match is None:
        AsyncDynamicSession(**kwargs)  # a valid combination must construct
        return
    with pytest.raises(ValueError, match=match):
        AsyncDynamicSession(**kwargs)


def test_real_chrome_and_a_different_channel_conflict():
    with pytest.raises(ValueError, match="real_chrome"):
        AsyncDynamicSession(real_chrome=True, channel="msedge")


def test_init_script_must_exist(tmp_path):
    with pytest.raises(FileNotFoundError, match="init_script"):
        AsyncDynamicSession(init_script=str(tmp_path / "missing.js"))


def test_wait_until_mapping():
    assert _wait_until(network_idle=True, load_dom=True) == "networkidle"
    assert _wait_until(network_idle=True, load_dom=False) == "networkidle"
    assert _wait_until(network_idle=False, load_dom=True) == "load"
    assert _wait_until(network_idle=False, load_dom=False) == "domcontentloaded"


def test_page_request_validation():
    with pytest.raises(ValueError, match="wait_until"):
        PageRequest(url="http://x/", wait_until="soon").validate()


async def test_sync_classes_refuse_a_running_event_loop():
    with pytest.raises(RuntimeError, match="AsyncDynamicFetcher"):
        DynamicFetcher.fetch("http://127.0.0.1:1/")
    with pytest.raises(RuntimeError, match="AsyncDynamicSession"):
        DynamicSession()


# ---------------------------------------------------------------------------
# Proxy parsing -- Regression: authenticated proxies failed with 407
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "given,expected",
    [
        (None, None),
        ("", None),
        ("http://proxy:3128", {"server": "http://proxy:3128"}),
        ("http://alice:s3cret@proxy:3128", {"server": "http://proxy:3128", "username": "alice", "password": "s3cret"}),
        ("socks5://u:p@10.0.0.1:1080", {"server": "socks5://10.0.0.1:1080", "username": "u", "password": "p"}),
        ("http://bob:p%40ss%3Aword%2F1@host:8080", {"server": "http://host:8080", "username": "bob", "password": "p@ss:word/1"}),
        ("http://onlyuser@host:8080", {"server": "http://host:8080", "username": "onlyuser", "password": ""}),
        ("http://u:p@[::1]:3128", {"server": "http://[::1]:3128", "username": "u", "password": "p"}),
        ("proxy.example:3128", {"server": "proxy.example:3128"}),
        ({"server": "http://p:1", "bypass": "*.internal"}, {"server": "http://p:1", "bypass": "*.internal"}),
    ],
)
def test_playwright_proxy_parsing(given, expected):
    assert playwright_proxy(given) == expected


def test_playwright_proxy_dict_is_copied_not_shared():
    original = {"server": "http://p:1"}
    out = playwright_proxy(original)
    assert out == original and out is not original


# ---------------------------------------------------------------------------
# Error translation, against Playwright's real error text
# ---------------------------------------------------------------------------


def _pw_exc(name: str, text: str) -> Exception:
    cls = type(name, (Exception,), {})
    cls.__module__ = "playwright._impl._errors"
    return cls(text)


@pytest.mark.parametrize(
    "name,text,retryable,fragment",
    [
        ("TimeoutError", "Page.goto: Timeout 30000ms exceeded.\nCall log:\n  - navigating", True, "Timed out"),
        ("Error", "Page.goto: net::ERR_CONNECTION_REFUSED at http://127.0.0.1:1/", True, "could not load"),
        ("Error", "Page.goto: net::ERR_NAME_NOT_RESOLVED at http://nope.invalid/", True, "could not load"),
        ("Error", "Page.goto: Download is starting", False, "file download"),
        ("Error", "Page.goto: Protocol error (Page.navigate): Cannot navigate to invalid URL", False, "Can't load"),
        ("Error", "Page.goto: net::ERR_CERT_AUTHORITY_INVALID at https://x/", False, "Can't load"),
        ("Error", "Page.goto: net::ERR_TOO_MANY_REDIRECTS at http://x/", False, "Can't load"),
        ("Error", "Page.goto: Target page, context or browser has been closed", False, "Create a new session"),
    ],
)
def test_translate_error(name, text, retryable, fragment):
    out = translate_error(_pw_exc(name, text), "http://x/")
    assert isinstance(out, DynamicFetchError) and isinstance(out, FetchError)
    assert out.retryable is retryable
    assert fragment.lower() in str(out).lower()
    assert out.url == "http://x/"


def test_translate_error_leaves_foreign_exceptions_alone():
    exc = ValueError("a bug in my own code")
    assert translate_error(exc, "http://x/") is exc
    already = DynamicFetchError("x")
    assert translate_error(already, "http://x/") is already


@pytest.mark.parametrize(
    "cfg,text,fragment",
    [
        (LaunchConfig(channel="msedge"), "BrowserType.launch: Chromium distribution 'msedge' is not found at /opt/microsoft/msedge/msedge\nRun \"playwright install msedge\"", "msedge"),
        (LaunchConfig(channel="chrome"), "BrowserType.launch: Chromium distribution 'chrome' is not found at /opt/google/chrome/chrome", "Google Chrome"),
        (LaunchConfig(channel="nonsense"), 'BrowserType.launch: Unsupported chromium channel "nonsense"', "Unsupported browser channel"),
        (LaunchConfig(executable_path="/no/such/chrome"), "BrowserType.launch: Failed to launch chromium because executable doesn't exist at /no/such/chrome", "/no/such/chrome"),
        (LaunchConfig(cdp_url="http://127.0.0.1:1"), "BrowserType.connect_over_cdp: connect ECONNREFUSED 127.0.0.1:1", "remote-debugging-port"),
        (LaunchConfig(), "BrowserType.launch: Executable doesn't exist at /x/chrome\nLooks like Playwright was just installed", "playwright install chromium"),
        (LaunchConfig(), "BrowserType.launch: something unforeseen", "Could not start the browser"),
    ],
)
def test_translate_launch_error(cfg, text, fragment):
    out = translate_launch_error(_pw_exc("Error", text), cfg)
    assert isinstance(out, BrowserLaunchError)
    assert out.retryable is False
    assert fragment in str(out)


def test_launch_error_leaves_foreign_exceptions_alone():
    exc = RuntimeError("nope")
    assert translate_launch_error(exc, LaunchConfig()) is exc


# ---------------------------------------------------------------------------
# XHR capture bookkeeping (fake responses)
# ---------------------------------------------------------------------------


class _FakeXhrRequest:
    def __init__(self, resource_type="xhr", method="GET"):
        self.resource_type, self.method = resource_type, method


class _FakeXhrResponse:
    def __init__(self, url, *, body=b"{}", resource_type="xhr", status=200, delay=0.0, error=None):
        self.url, self.status, self.headers = url, status, {"content-type": "application/json"}
        self.request = _FakeXhrRequest(resource_type)
        self._body, self._delay, self._error = body, delay, error

    async def body(self):
        await asyncio.sleep(self._delay)
        if self._error:
            raise self._error
        return self._body


class _FakeXhrPage:
    def __init__(self):
        self.handlers = []

    def on(self, event, handler):
        assert event == "response"
        self.handlers.append(handler)

    def emit(self, response):
        for h in self.handlers:
            h(response)


async def test_xhr_capture_keeps_arrival_order_and_filters_types_and_pattern():
    page, cap = _FakeXhrPage(), XhrCapture(r"/api/")
    cap.attach(page)
    page.emit(_FakeXhrResponse("http://x/api/slow", delay=0.05))
    page.emit(_FakeXhrResponse("http://x/api/fast"))
    page.emit(_FakeXhrResponse("http://x/other", body=b"no"))  # URL doesn't match
    page.emit(_FakeXhrResponse("http://x/api/img", resource_type="image"))  # not an XHR
    await cap.drain()
    assert [e["url"] for e in cap.entries] == ["http://x/api/slow", "http://x/api/fast"]
    assert all(e["body"] == "{}" and e["method"] == "GET" and "error" not in e for e in cap.entries)


async def test_xhr_capture_disabled_attaches_nothing():
    page, cap = _FakeXhrPage(), XhrCapture(None)
    cap.attach(page)
    assert page.handlers == [] and cap.entries == []


async def test_xhr_body_that_cannot_be_read_says_why():
    page, cap = _FakeXhrPage(), XhrCapture(True)
    cap.attach(page)
    page.emit(_FakeXhrResponse("http://x/gone", error=RuntimeError("Response body is unavailable for redirect responses")))
    page.emit(_FakeXhrResponse("http://x/bin", body=bytes([0xFF, 0xFE, 0x80])))
    await cap.drain()
    gone, binary = cap.entries
    assert gone["body"] is None and "unavailable for redirect" in gone["error"]
    assert binary["body"] is None and "binary body (3 bytes)" in binary["error"]


async def test_xhr_drain_is_bounded_and_marks_what_never_finished():
    page, cap = _FakeXhrPage(), XhrCapture(True)
    cap.attach(page)
    page.emit(_FakeXhrResponse("http://x/forever", delay=60))
    t0 = time.monotonic()
    await cap.drain(timeout=0.1)
    assert time.monotonic() - t0 < 2
    assert cap.entries[0]["body"] is None and "still streaming" in cap.entries[0]["error"]


# ---------------------------------------------------------------------------
# Plumbing, via fake Playwright
# ---------------------------------------------------------------------------


def _fake_session(**kwargs: Any) -> tuple[AsyncDynamicSession, _FakePW]:
    pw = _FakePW()

    async def factory():
        return pw

    return AsyncDynamicSession(playwright_factory=factory, **kwargs), pw


async def test_real_chrome_requests_the_chrome_channel():
    session, pw = _fake_session(real_chrome=True)
    await session.start()
    try:
        assert pw.chromium.launches[0]["channel"] == "chrome"
    finally:
        await session.close()


async def test_channel_executable_flags_and_headless_reach_the_launch():
    session, pw = _fake_session(
        channel="msedge", executable_path="/opt/x/edge", extra_flags=["--no-sandbox"], headless=False
    )
    await session.start()
    try:
        launch = pw.chromium.launches[0]
        assert (launch["channel"], launch["executable_path"], launch["args"], launch["headless"]) == (
            "msedge", "/opt/x/edge", ["--no-sandbox"], False,
        )
    finally:
        await session.close()


async def test_proxy_credentials_are_split_before_launch():
    session, pw = _fake_session(proxy="http://alice:s3cret@proxy:3128")
    await session.start()
    try:
        assert pw.chromium.launches[0]["proxy"] == {
            "server": "http://proxy:3128", "username": "alice", "password": "s3cret",
        }
    finally:
        await session.close()


async def test_context_options_locale_viewport_and_headers_reach_the_context():
    session, pw = _fake_session(
        locale="de-DE", timezone="Europe/Berlin", viewport=(800, 600), user_agent="Mine/1",
        headers={"X-A": "1"}, context_options={"color_scheme": "dark"},
    )
    await session.start()
    try:
        kw = pw.chromium.contexts[0].kwargs
        assert kw["locale"] == "de-DE" and kw["timezone_id"] == "Europe/Berlin"
        assert kw["viewport"] == {"width": 800, "height": 600} and kw["user_agent"] == "Mine/1"
        assert kw["extra_http_headers"] == {"X-A": "1"} and kw["color_scheme"] == "dark"
    finally:
        await session.close()


async def test_disable_resources_blocks_only_dom_irrelevant_types():
    session, _ = _fake_session(disable_resources=True)
    blocker = session.browser.blocker
    assert blocker is not None  # (what it blocks is proven against a real browser below)
    assert {"image", "media", "font"} <= DISABLE_RESOURCES
    assert "stylesheet" not in DISABLE_RESOURCES and "script" not in DISABLE_RESOURCES


async def test_the_browser_is_launched_once_and_each_fetch_uses_its_own_tab():
    session, pw = _fake_session()
    async with session:
        for i in range(3):
            page = await session.fetch(f"http://example.test/{i}")
            assert page.status == 200
        runtime = session.browser.runtime
        assert len(pw.chromium.launches) == 1
        pages = pw.chromium.contexts[0].pages
        assert len(pages) == 3 and all(p.closed for p in pages)
        assert runtime is session.browser.runtime


async def test_a_failing_hook_becomes_dynamic_fetch_error_and_the_tab_still_closes():
    session, pw = _fake_session()

    async def boom(page):
        raise KeyError("missing-thing")

    async with session:
        with pytest.raises(DynamicFetchError, match=r"page_action raised KeyError") as exc:
            await session.fetch("http://example.test/", page_action=boom)
        assert exc.value.retryable is False
        assert isinstance(exc.value.__cause__, KeyError)  # the original is chained
        assert pw.chromium.contexts[0].pages[0].closed


async def test_hooks_run_in_order_and_sync_hooks_are_accepted():
    session, _ = _fake_session()
    calls: list[str] = []

    def setup(page):  # a plain function, not async
        calls.append("setup")

    async def action(page):
        calls.append("action")

    async with session:
        await session.fetch("http://example.test/", page_setup=setup, page_action=action)
    assert calls == ["setup", "action"]


async def test_the_browser_driver_setting_refuses_patched_drivers():
    session, _ = _fake_session(settings=Settings(browser_driver="patchright"))
    with pytest.raises(NotImplementedError, match="Explicit non-goals"):
        await session.start()


def test_cookie_list_shapes():
    assert cookie_list({"a": "1"}, "http://x/") == [{"name": "a", "value": "1", "url": "http://x/"}]
    given = [{"name": "b", "value": "2", "domain": "x", "path": "/"}]
    out = cookie_list(given, "http://x/")
    assert out == given and out[0] is not given[0]


def test_browser_session_real_chrome_conflict_is_a_session_error():
    with pytest.raises(SessionError, match="real_chrome"):
        BrowserSession(real_chrome=True, channel="msedge")


# ===========================================================================
# PART 2 -- a real Chromium
# ===========================================================================

# ---------------------------------------------------------------------------
# Rendering JavaScript
# ---------------------------------------------------------------------------


def test_renders_content_that_only_exists_after_javascript_runs(site, dyn):
    from bitscrape import Fetcher

    assert Fetcher.get(site.base + "/").css("li.product::text").getall() == []  # plain HTTP: an empty shell
    page = dyn.fetch(site.base + "/", wait_selector="li.product")
    assert isinstance(page, ParsedResponse)
    assert page.css("li.product::text").getall() == ["Widget", "Gadget", "Gizmo"]
    assert page.status == 200 and page.css("title::text").get() == "Shop"
    assert page.url == site.base + "/"
    assert page.response.elapsed_ms > 0
    assert page.http_version is None  # the browser doesn't report it; unknown, not guessed


def test_one_shot_dynamic_fetcher(site):
    page = DynamicFetcher.fetch(site.base + "/", wait_selector="li.product")
    assert len(page.css("li.product")) == 3


async def test_async_one_shot_and_session(site):
    page = await AsyncDynamicFetcher.fetch(site.base + "/", wait_selector="li.product")
    assert len(page.css("li.product")) == 3
    async with AsyncDynamicSession(wait_selector="li.product") as s:  # a session-wide default
        assert len((await s.fetch(site.base + "/")).css("li.product")) == 3


def test_static_pages_work_without_waiting(site, dyn):
    assert dyn.fetch(site.base + "/static").css("h1::text").get() == "Static page"


def test_unicode_is_preserved(site, dyn):
    assert dyn.fetch(site.base + "/unicode").css("h1::text").get() == "日本語 ✓ ünïcode"
    assert dyn.fetch(site.base + "/latin1").css("h1::text").get() == "café"  # a latin-1 page, decoded right


def test_parsed_response_helpers_work_on_browser_pages(site, dyn):
    page = dyn.fetch(site.base + "/", wait_selector="li.product")
    assert page.xpath("//li[@class='product']/text()").getall()[0] == "Widget"
    assert page.urljoin("/x") == site.base + "/x"


# ---------------------------------------------------------------------------
# Waiting
# ---------------------------------------------------------------------------


def test_without_a_wait_late_content_is_missed_and_wait_selector_catches_it(site, dyn):
    assert dyn.fetch(site.base + "/delayed").css("#late::text").get() == "pending"
    assert dyn.fetch(site.base + "/delayed", wait_selector="#late:has-text('arrived')").css("#late::text").get() == "arrived"


def test_wait_seconds_sits_on_the_page_before_reading_it(site, dyn):
    t0 = time.monotonic()
    page = dyn.fetch(site.base + "/delayed", wait=0.8)
    assert page.css("#late::text").get() == "arrived"
    assert time.monotonic() - t0 >= 0.8


def test_network_idle_waits_for_a_slow_api_that_load_does_not(site, dyn):
    assert dyn.fetch(site.base + "/slow-api-page").css("#out::text").get() == "pending"
    assert dyn.fetch(site.base + "/slow-api-page", network_idle=True).css("#out::text").get() == "slow:7"


@pytest.mark.parametrize(
    "state,selector",
    [("attached", "#late"), ("visible", "#late"), ("hidden", "#nope"), ("detached", "#nope")],
)
def test_wait_selector_states(site, dyn, state, selector):
    page = dyn.fetch(site.base + "/delayed", wait_selector=selector, wait_selector_state=state)
    assert page.status == 200


def test_wait_selector_that_never_appears_times_out_with_a_retryable_error(site, dyn):
    t0 = time.monotonic()
    with pytest.raises(DynamicFetchError, match="Timed out") as exc:
        dyn.fetch(site.base + "/static", wait_selector="#does-not-exist", timeout=1)
    assert exc.value.retryable is True and exc.value.url == site.base + "/static"
    assert time.monotonic() - t0 < 5
    assert dyn.fetch(site.base + "/static").status == 200  # and the session is still healthy


def test_navigation_timeout(site, dyn):
    with pytest.raises(DynamicFetchError, match="Timed out"):
        dyn.fetch(site.base + "/slow?delay=5", timeout=0.5)


# ---------------------------------------------------------------------------
# Hooks
# ---------------------------------------------------------------------------


def test_page_action_click_reveals_content(site, dyn):
    async def click_more(page):
        await page.click("#more")

    page = dyn.fetch(site.base + "/click", page_action=click_more, wait_selector=".revealed")
    assert page.css(".revealed::text").get() == "revealed-by-click"


def test_page_action_runs_before_wait_selector_so_it_can_make_the_selector_appear(site, dyn):
    """Without the click the selector never exists; if the wait ran first it would time out."""

    async def click_more(page):
        await page.click("#more")

    page = dyn.fetch(site.base + "/click", page_action=click_more, wait_selector=".revealed", timeout=5)
    assert page.css(".revealed")


def test_page_action_can_fill_a_form(site, dyn):
    async def search(page):
        await page.fill("#q", "widgets")
        await page.click("#go")

    page = dyn.fetch(site.base + "/search", page_action=search)
    assert page.css("#results::text").get() == "results for widgets"


def test_page_setup_can_listen_and_mock_a_request(site, dyn):
    messages: list[str] = []

    async def setup(page):
        page.on("console", lambda m: messages.append(m.text))
        await page.route("**/api/real", lambda route: route.fulfill(json={"source": "mocked"}))

    assert dyn.fetch(site.base + "/mock").css("#src::text").get() in ("?", "real")
    page = dyn.fetch(site.base + "/mock", page_setup=setup, wait_selector="#src:has-text('mocked')")
    assert page.css("#src::text").get() == "mocked"
    dyn.fetch(site.base + "/console", page_setup=setup)
    assert "hello-console" in messages


def test_a_hook_that_raises_is_a_dynamic_fetch_error_naming_the_hook(site, dyn):
    async def broken(page):
        await page.click("#element-that-is-not-there", timeout=500)

    with pytest.raises(DynamicFetchError, match="page_action raised") as exc:
        dyn.fetch(site.base + "/static", page_action=broken)
    assert exc.value.retryable is False
    assert exc.value.__cause__ is not None
    assert dyn.fetch(site.base + "/static").status == 200  # the tab was cleaned up; the session lives on


# ---------------------------------------------------------------------------
# Scrolling and screenshots
# ---------------------------------------------------------------------------


def test_scroll_loads_infinite_scroll_content(site, dyn):
    assert len(dyn.fetch(site.base + "/scroll").css(".item")) == 5
    page = dyn.fetch(site.base + "/scroll", scroll={"pause_ms": 300, "max_scrolls": 20})
    assert len(page.css(".item")) == 25
    assert len(dyn.fetch(site.base + "/scroll", scroll=True).css(".item")) == 25


def test_screenshot_bytes(site, dyn):
    page = dyn.fetch(site.base + "/static", screenshot=True)
    shot = page.extra["screenshot"]
    assert isinstance(shot, bytes) and shot.startswith(b"\x89PNG")
    jpeg = dyn.fetch(site.base + "/static", screenshot={"type": "jpeg", "full_page": False}).extra["screenshot"]
    assert jpeg.startswith(b"\xff\xd8")
    assert "screenshot" not in dyn.fetch(site.base + "/static").extra


# ---------------------------------------------------------------------------
# XHR capture -- Regression: streaming bodies were recorded as None
# ---------------------------------------------------------------------------


def test_capture_xhr_everything(site, dyn):
    page = dyn.fetch(site.base + "/xhr", wait_selector="#done:has-text('all-done')", capture_xhr=True)
    by_url = {x["url"].rsplit("/", 1)[1]: x for x in page.xhr}
    assert set(by_url) == {"a", "b", "other.txt"}
    assert json.loads(by_url["a"]["body"]) == {"which": "a"}
    assert by_url["other.txt"]["body"] == "plain-other"
    assert by_url["a"]["status"] == 200 and by_url["a"]["method"] == "GET"
    assert page.captured_xhr == page.xhr


def test_capture_xhr_with_a_url_pattern(site, dyn):
    page = dyn.fetch(site.base + "/xhr", wait_selector="#done:has-text('all-done')", capture_xhr=r"/api/")
    assert sorted(x["url"].rsplit("/", 1)[1] for x in page.xhr) == ["a", "b"]
    assert [x["url"] for x in page.find_xhr(r"/api/a")] == [site.base + "/api/a"]


def test_no_capture_by_default(site, dyn):
    assert dyn.fetch(site.base + "/xhr", wait_selector="#done:has-text('all-done')").xhr == []


def test_regression_a_still_streaming_xhr_body_is_captured_not_recorded_as_none(site, dyn):
    """The page is read as soon as the *fast* XHR lands; the other body is still
    streaming for another ~0.8s. It used to come back as body=None."""
    page = dyn.fetch(site.base + "/stream", wait_selector="#done:has-text('fast-done')", capture_xhr=True)
    by_name = {x["url"].rsplit("/", 1)[1]: x for x in page.xhr}
    assert by_name["fast"]["body"] == '{"k": "fast"}'
    assert by_name["streaming"]["body"] == '{"k": "streaming"}'
    assert "error" not in by_name["streaming"]


def test_a_none_body_always_comes_with_a_reason(site, dyn):
    page = dyn.fetch(site.base + "/binary-page", wait=0.5, capture_xhr=True)
    (entry,) = page.xhr
    assert entry["body"] is None and "binary body (4 bytes)" in entry["error"]
    redirected = dyn.fetch(site.base + "/xhr-redirect", wait=0.5, capture_xhr=True)
    bodyless = [x for x in redirected.xhr if x["body"] is None]
    assert all(x.get("error") for x in bodyless)  # e.g. a redirect response has no readable body


# ---------------------------------------------------------------------------
# Non-HTML responses, status codes, redirects, downloads
# ---------------------------------------------------------------------------


def test_a_json_url_returns_the_real_body_not_the_browsers_wrapper(site, dyn):
    page = dyn.fetch(site.base + "/json")
    assert page.json() == {"hello": "world"}
    assert page.text.strip().startswith("{")  # no <html><pre> wrapper
    assert dyn.fetch(site.base + "/text").text == "plain text body"


@pytest.mark.parametrize("code", [201, 404, 500, 503])
def test_error_status_pages_are_returned_not_raised(site, dyn, code):
    page = dyn.fetch(site.base + f"/status/{code}")
    assert page.status == code
    assert page.css("h1::text").get() == f"status {code}"
    assert page.ok is (code < 400)


def test_http_redirect_reports_the_final_url(site, dyn):
    page = dyn.fetch(site.base + "/redirect")
    assert page.url == site.base + "/static" and page.css("h1::text").get() == "Static page"


def test_client_side_redirects_are_followed(site, dyn):
    for path in ("/js-redirect", "/meta-refresh"):
        page = dyn.fetch(site.base + path, wait_selector="h1")
        assert page.url == site.base + "/static", path
        assert page.css("h1::text").get() == "Static page"


def test_reading_a_page_that_is_mid_navigation_waits_for_it_instead_of_failing(site, dyn):
    """Playwright's page.content() raises 'the page is navigating and changing the content' if a
    client-side redirect is in flight (reproduced: ~80% of reads with a 30ms self-redirect). A
    fetch must come back with the *destination*, not fail or return the stale source page."""
    for _ in range(5):
        page = dyn.fetch(site.base + "/nav-source", load_dom=False, wait=0.1)
        assert page.css("#t::text").get() == "target"
        assert page.url == site.base + "/nav-target"


def test_redirect_loop_is_not_retryable(site, dyn):
    with pytest.raises(DynamicFetchError) as exc:
        dyn.fetch(site.base + "/redirect-loop", timeout=10)
    assert exc.value.retryable is False


def test_a_file_download_url_is_an_explanatory_error(site, dyn):
    with pytest.raises(DynamicFetchError, match="file download") as exc:
        dyn.fetch(site.base + "/download")
    assert exc.value.retryable is False
    assert dyn.fetch(site.base + "/static").status == 200


# ---------------------------------------------------------------------------
# Failures
# ---------------------------------------------------------------------------


def _closed_port() -> int:
    """A port nothing listens on (so: connection refused), and that Chromium doesn't reject outright."""
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def test_connection_refused(dyn):
    with pytest.raises(DynamicFetchError, match="ERR_CONNECTION_REFUSED") as exc:
        dyn.fetch(f"http://127.0.0.1:{_closed_port()}/", timeout=5)
    assert exc.value.retryable is True


def test_chromium_unsafe_ports_are_not_retryable(dyn):
    """Chromium refuses ports like 1 by policy; retrying can never help."""
    with pytest.raises(DynamicFetchError, match="ERR_UNSAFE_PORT") as exc:
        dyn.fetch("http://127.0.0.1:1/", timeout=5)
    assert exc.value.retryable is False


def test_invalid_url_is_not_retryable(dyn):
    with pytest.raises(DynamicFetchError) as exc:
        dyn.fetch("not a url")
    assert exc.value.retryable is False


def test_dynamic_fetch_error_is_a_fetch_error(dyn):
    with pytest.raises(FetchError):
        dyn.fetch(f"http://127.0.0.1:{_closed_port()}/", timeout=5)


# ---------------------------------------------------------------------------
# Headers, cookies and the browser's identity
# ---------------------------------------------------------------------------


def test_default_user_agent_is_honest_and_user_agent_is_sent_as_given(site):
    with DynamicSession() as s:
        assert "BitscrapeBot" in s.fetch(site.base + "/env").css("#ua::text").get()
    with DynamicSession(user_agent="MyTool/2.0") as s:
        assert s.fetch(site.base + "/env").css("#ua::text").get() == "MyTool/2.0"


def test_no_default_headers_are_fabricated(site, dyn):
    headers = _echo(dyn.fetch(site.base + "/echo-page"))["headers"]
    assert "referer" not in headers  # no invented "came from Google"
    assert "x-forwarded-for" not in headers


def test_session_wide_and_per_fetch_headers(site):
    with DynamicSession(headers={"X-Session": "s"}) as s:
        base = _echo(s.fetch(site.base + "/echo-page"))["headers"]
        over = _echo(s.fetch(site.base + "/echo-page", headers={"X-Page": "p"}))["headers"]
    assert base["x-session"] == "s" and "x-page" not in base
    assert over["x-session"] == "s" and over["x-page"] == "p"


def test_session_keeps_cookies_between_fetches(site):
    with DynamicSession() as s:
        s.fetch(site.base + "/set-cookie/sid/abc")
        assert _echo(s.fetch(site.base + "/echo-page"))["cookies"] == {"sid": "abc"}
        assert s.fetch(site.base + "/cookie-js").css("#c::text").get() == "sid=abc"  # JS sees it too


def test_one_shot_fetches_share_nothing(site):
    DynamicFetcher.fetch(site.base + "/set-cookie/sid/abc")
    assert _echo(DynamicFetcher.fetch(site.base + "/echo-page"))["cookies"] == {}


def test_per_fetch_cookies_dict_and_playwright_cookie_dicts(site):
    with DynamicSession() as s:
        assert _echo(s.fetch(site.base + "/echo-page", cookies={"a": "1"}))["cookies"] == {"a": "1"}
    with DynamicSession() as s:
        host = urlsplit(site.base).hostname
        page = s.fetch(
            site.base + "/echo-page",
            cookies=[{"name": "b", "value": "2", "domain": host, "path": "/"}],
        )
        assert _echo(page)["cookies"] == {"b": "2"}


def test_per_fetch_cookies_stay_in_the_session_jar(site):
    """Documented: a browser context has one cookie jar, so these persist."""
    with DynamicSession() as s:
        s.fetch(site.base + "/echo-page", cookies={"a": "1"})
        assert _echo(s.fetch(site.base + "/echo-page"))["cookies"] == {"a": "1"}


def test_browser_session_cookie_accessors(site):
    with DynamicSession() as s:
        s.fetch(site.base + "/set-cookie/sid/abc")
        jar = s._run(s.browser.cookies(site.base))
        assert jar == {"sid": "abc"}


def test_storage_state_starts_logged_in(site, tmp_path):
    state = tmp_path / "state.json"
    host = urlsplit(site.base).hostname
    state.write_text(json.dumps({
        "cookies": [{"name": "s", "value": "v", "domain": host, "path": "/", "expires": -1,
                     "httpOnly": False, "secure": False, "sameSite": "Lax"}],
        "origins": [],
    }))
    with DynamicSession(storage_state=str(state)) as s:
        assert _echo(s.fetch(site.base + "/echo-page"))["cookies"] == {"s": "v"}


def test_persistent_profile_survives_between_sessions(site, tmp_path):
    profile = str(tmp_path / "profile")
    with DynamicSession(user_data_dir=profile) as s:
        s.fetch(site.base + "/set-persistent-cookie/p/1")
    with DynamicSession(user_data_dir=profile) as s:
        assert _echo(s.fetch(site.base + "/echo-page"))["cookies"] == {"p": "1"}
    with DynamicSession() as s:  # a fresh, non-persistent session has none of it
        assert _echo(s.fetch(site.base + "/echo-page"))["cookies"] == {}


def test_locale_timezone_viewport_and_context_options(site):
    with DynamicSession(
        locale="de-DE", timezone="Europe/Berlin", viewport=(800, 600),
        context_options={"color_scheme": "dark"},
    ) as s:
        page = s.fetch(site.base + "/env")
    assert page.css("#lang::text").get() == "de-DE"
    assert page.css("#tz::text").get() == "Europe/Berlin"
    assert page.css("#size::text").get() == "800x600"
    assert page.css("#scheme::text").get() == "dark"


def test_init_script_runs_before_the_pages_own_scripts(site, tmp_path):
    script = tmp_path / "init.js"
    script.write_text("window.__flag = 'set-by-init-script';")
    with DynamicSession(init_script=str(script)) as s:
        assert s.fetch(site.base + "/init-flag").css("#f::text").get() == "set-by-init-script"
    with DynamicSession() as s:
        assert s.fetch(site.base + "/init-flag").css("#f::text").get() == "undefined"


# ---------------------------------------------------------------------------
# Resource blocking
# ---------------------------------------------------------------------------


def test_disable_resources_stops_images_but_keeps_the_page_working(site):
    st = site.state
    before = st.hits["img"]
    with DynamicSession() as s:
        s.fetch(site.base + "/images")
    loaded = st.hits["img"] - before
    assert loaded == 3
    before = st.hits["img"]
    css_before = st.hits["css"]
    with DynamicSession(disable_resources=True) as s:
        page = s.fetch(site.base + "/images")
    assert page.css("#t::text").get() == "images"
    assert st.hits["img"] - before == 0  # images were never requested
    assert st.hits["css"] - css_before == 1  # but the stylesheet still was


def test_blocked_domains_stop_third_party_requests(site):
    st = site.state
    before = st.hits["tracker"]
    with DynamicSession() as s:
        assert s.fetch(site.base + "/third-party").css("#t::text").get() == "true"
    assert st.hits["tracker"] - before == 1
    before = st.hits["tracker"]
    with DynamicSession(blocked_domains=["localhost"]) as s:
        assert s.fetch(site.base + "/third-party").css("#t::text").get() == "undefined"
    assert st.hits["tracker"] - before == 0


# ---------------------------------------------------------------------------
# Sessions: concurrency, lifecycle, cleanup
# ---------------------------------------------------------------------------


async def test_max_concurrency_limits_open_tabs(site):
    st = site.state
    st.max_in_flight = 0
    async with AsyncDynamicSession(max_concurrency=2) as s:
        pages = await asyncio.gather(*[s.fetch(site.base + f"/hold?n={i}", wait_selector="#n") for i in range(6)])
    assert [p.css("#n::text").get() for p in pages] == [str(i) for i in range(6)]
    assert st.max_in_flight == 2


async def test_more_tabs_run_in_parallel_when_allowed(site):
    st = site.state
    st.max_in_flight = 0
    async with AsyncDynamicSession(max_concurrency=6) as s:
        await asyncio.gather(*[s.fetch(site.base + f"/hold?n={i}") for i in range(6)])
    assert st.max_in_flight >= 4


def test_session_starts_lazily_and_closes_for_good(site):
    s = DynamicSession()
    assert not s.browser.started
    assert s.fetch(site.base + "/static").status == 200  # started itself on first use
    assert s.browser.started
    s.close()
    s.close()  # idempotent
    with pytest.raises(RuntimeError, match="closed"):
        s.fetch(site.base + "/static")


async def test_async_session_close_is_final(site):
    s = AsyncDynamicSession()
    await s.fetch(site.base + "/static")
    await s.close()
    await s.close()
    with pytest.raises(RuntimeError, match="closed"):
        await s.fetch(site.base + "/static")


def test_no_browser_processes_are_left_behind(site):
    psutil = pytest.importorskip("psutil")

    def browser_procs() -> list[str]:
        names = []
        for child in psutil.Process().children(recursive=True):
            try:
                if any(k in " ".join(child.cmdline()).lower() for k in ("chrom", "headless_shell", "playwright")):
                    names.append(child.name())
            except psutil.Error:
                continue
        return names

    baseline = browser_procs()  # the module-scoped `dyn` browser may be running; account for it
    DynamicFetcher.fetch(site.base + "/static")
    with DynamicSession() as s:
        s.fetch(site.base + "/static")
    with pytest.raises(DynamicFetchError):
        DynamicFetcher.fetch(f"http://127.0.0.1:{_closed_port()}/", timeout=5)  # a failed fetch cleans up too
    deadline = time.monotonic() + 5
    while browser_procs() != baseline and time.monotonic() < deadline:
        time.sleep(0.1)
    assert len(browser_procs()) <= len(baseline)


# ---------------------------------------------------------------------------
# Which browser: executable_path, CDP, the chrome channel, launch failures
# ---------------------------------------------------------------------------


def test_executable_path(site, chromium_path):
    page = DynamicFetcher.fetch(site.base + "/static", executable_path=chromium_path)
    assert page.css("h1::text").get() == "Static page"


def test_real_chrome_requests_the_chrome_channel_and_fetches(site):
    """In this sandbox the 'chrome' channel resolves to a symlink to Playwright's Chromium,
    so this checks the plumbing, not Chrome-specific behaviour. Either outcome is valid
    on another machine: Chrome is installed and it works, or it isn't and the error says so."""
    try:
        page = DynamicFetcher.fetch(site.base + "/static", real_chrome=True)
    except BrowserLaunchError as exc:
        assert "Google Chrome" in str(exc) and "playwright install chrome" in str(exc)
    else:
        assert page.css("h1::text").get() == "Static page"


def test_an_uninstalled_or_unknown_channel_explains_itself(site):
    with pytest.raises(BrowserLaunchError, match="Unsupported browser channel") as exc:
        DynamicFetcher.fetch(site.base + "/static", channel="nonsense")
    assert exc.value.retryable is False


def test_wrong_executable_path(site):
    with pytest.raises(BrowserLaunchError, match=r"/no/such/chrome"):
        DynamicFetcher.fetch(site.base + "/static", executable_path="/no/such/chrome")


def test_unreachable_cdp_url(site):
    with pytest.raises(BrowserLaunchError, match="remote-debugging-port"):
        DynamicFetcher.fetch(site.base + "/static", cdp_url="http://127.0.0.1:1")


def test_missing_playwright_browsers_say_how_to_install_them(site, monkeypatch):
    monkeypatch.setenv("PLAYWRIGHT_BROWSERS_PATH", "/nonexistent-browsers")
    with pytest.raises(BrowserLaunchError, match="playwright install chromium"):
        DynamicFetcher.fetch(site.base + "/static")


def test_a_failed_launch_leaves_no_session_half_started(site):
    s = DynamicSession(executable_path="/no/such/chrome")
    with pytest.raises(BrowserLaunchError):
        s.fetch(site.base + "/static")
    assert not s.browser.started
    s.close()


def test_cdp_attaches_to_a_running_browser_and_leaves_it_running(site, chromium_path, tmp_path):
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    proc = subprocess.Popen(
        [chromium_path, "--headless=new", "--no-sandbox", "--disable-gpu", f"--remote-debugging-port={port}",
         f"--user-data-dir={tmp_path / 'cdp'}", "about:blank"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    try:
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            try:
                socket.create_connection(("127.0.0.1", port), timeout=0.2).close()
                break
            except OSError:
                time.sleep(0.1)
        cdp = f"http://127.0.0.1:{port}"
        with DynamicSession(cdp_url=cdp) as s:
            assert s.fetch(site.base + "/", wait_selector="li.product").css("li.product::text").getall()[0] == "Widget"
            assert s.fetch(site.base + "/static").css("h1::text").get() == "Static page"
        assert proc.poll() is None  # closing our session did not kill a browser we don't own
        assert DynamicFetcher.fetch(site.base + "/static", cdp_url=cdp).status == 200  # still attachable
    finally:
        proc.terminate()
        proc.wait(10)


# ---------------------------------------------------------------------------
# Proxies, against a real authenticating proxy -- Regression: 407
# ---------------------------------------------------------------------------


class _AuthProxy:
    """A minimal plain-HTTP forward proxy that demands Basic Proxy-Authorization."""

    def __init__(self, user: str, password: str) -> None:
        self.expected = "Basic " + base64.b64encode(f"{user}:{password}".encode()).decode()
        self.proxied: list[str] = []
        self.rejected = 0
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._loop.run_forever, daemon=True)
        self.port = 0
        self._server: asyncio.AbstractServer | None = None

    def start(self) -> None:
        self._thread.start()

        async def up() -> int:
            self._server = await asyncio.start_server(self._handle, "127.0.0.1", 0)
            return int(self._server.sockets[0].getsockname()[1])

        self.port = asyncio.run_coroutine_threadsafe(up(), self._loop).result(10)

    def stop(self) -> None:
        async def down() -> None:
            if self._server is not None:
                self._server.close()

        asyncio.run_coroutine_threadsafe(down(), self._loop).result(10)
        self._loop.call_soon_threadsafe(self._loop.stop)
        self._thread.join(5)

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            head = await reader.readuntil(b"\r\n\r\n")
            lines = head.decode("latin1").split("\r\n")
            method, target, _ = lines[0].split(" ", 2)
            hdrs = {ln.split(":", 1)[0].lower(): ln.split(":", 1)[1].strip() for ln in lines[1:] if ":" in ln}
            if hdrs.get("proxy-authorization") != self.expected:
                self.rejected += 1
                writer.write(
                    b'HTTP/1.1 407 Proxy Authentication Required\r\nProxy-Authenticate: Basic realm="t"\r\n'
                    b"Content-Length: 0\r\nConnection: close\r\n\r\n"
                )
                await writer.drain()
                writer.close()
                return
            self.proxied.append(target)
            url = urlsplit(target)
            up_r, up_w = await asyncio.open_connection(url.hostname, url.port or 80)
            path = (url.path or "/") + (f"?{url.query}" if url.query else "")
            out = [f"{method} {path} HTTP/1.1"]
            out += [ln for ln in lines[1:] if ln and not ln.lower().startswith(("proxy-", "connection:"))]
            up_w.write("\r\n".join([*out, "Connection: close", "", ""]).encode())
            await up_w.drain()
            while chunk := await up_r.read(65536):
                writer.write(chunk)
                await writer.drain()
            up_w.close()
            writer.close()
        except Exception:  # noqa: BLE001
            writer.close()


# Chromium never proxies loopback addresses unless told to; the target here is 127.0.0.1.
_PROXY_LOOPBACK = ["--proxy-bypass-list=<-loopback>"]


@pytest.fixture
def auth_proxy(real_browser):
    proxy = _AuthProxy("alice", "s3cret")
    proxy.start()
    try:
        yield proxy
    finally:
        proxy.stop()


def test_regression_authenticated_proxy_url_works(site, auth_proxy):
    """http://user:pass@host:port used to be passed to Chromium whole, which ignores the
    embedded credentials: every fetch got a 407."""
    page = DynamicFetcher.fetch(
        site.base + "/static",
        proxy=f"http://alice:s3cret@127.0.0.1:{auth_proxy.port}",
        extra_flags=_PROXY_LOOPBACK,
    )
    assert page.status == 200 and page.css("h1::text").get() == "Static page"
    assert auth_proxy.proxied == [site.base + "/static"]  # it really went through the proxy


def test_proxy_dict_form(site, auth_proxy):
    page = DynamicFetcher.fetch(
        site.base + "/static",
        proxy={"server": f"http://127.0.0.1:{auth_proxy.port}", "username": "alice", "password": "s3cret"},
        extra_flags=_PROXY_LOOPBACK,
    )
    assert page.status == 200 and len(auth_proxy.proxied) == 1


def test_percent_encoded_proxy_credentials(site, real_browser):
    proxy = _AuthProxy("bob", "p@ss:word/1")
    proxy.start()
    try:
        page = DynamicFetcher.fetch(
            site.base + "/static",
            proxy=f"http://bob:p%40ss%3Aword%2F1@127.0.0.1:{proxy.port}",
            extra_flags=_PROXY_LOOPBACK,
        )
        assert page.status == 200 and len(proxy.proxied) == 1
    finally:
        proxy.stop()


def test_wrong_proxy_credentials_do_not_succeed(site, auth_proxy):
    try:
        page = DynamicFetcher.fetch(
            site.base + "/static",
            proxy=f"http://alice:WRONG@127.0.0.1:{auth_proxy.port}",
            extra_flags=_PROXY_LOOPBACK,
            timeout=5,
        )
    except DynamicFetchError:
        pass
    else:
        assert page.status == 407
    assert auth_proxy.proxied == []


# ---------------------------------------------------------------------------
# The shared core, used directly
# ---------------------------------------------------------------------------


async def test_load_page_directly(site):
    rt = BrowserRuntime(LaunchConfig())
    await rt.start()
    try:
        result = await load_page(rt, PageRequest(url=site.base + "/", wait_selector="li.product"))
        assert b"Widget" in result.body and result.status == 200 and result.encoding == "utf-8"
    finally:
        await rt.close()
    assert not rt.started


async def test_load_page_rejects_bad_options_before_touching_the_browser(site):
    rt = BrowserRuntime(LaunchConfig())
    with pytest.raises(ValueError, match="timeout"):
        await load_page(rt, PageRequest(url=site.base + "/", timeout=0))  # the runtime was never started


# ---------------------------------------------------------------------------
# The spider path: BrowserSession through the Downloader and Engine
# ---------------------------------------------------------------------------


async def _downloader(configure: Any, settings: Settings | None = None) -> Downloader:
    d = Downloader(settings or Settings())
    await d.open(configure=configure)
    return d


async def test_downloader_browser_session_renders_and_captures(site):
    def configure(m: SessionManager) -> None:
        m.add("b", BrowserSession(wait_selector="#done:has-text('all-done')", capture_xhr=r"/api/"))

    d = await _downloader(configure)
    try:
        r = await d.fetch(Request(url=site.base + "/xhr", session="b"))
    finally:
        await d.close()
    assert r.status == 200 and "all-done" in r.text
    assert sorted(x["url"].rsplit("/", 1)[1] for x in r.extra["captured_xhr"]) == ["a", "b"]
    assert r.session_id == "b"


async def test_browser_session_hooks_and_new_options_work_in_a_spider_session(site):
    clicked: list[str] = []

    async def click_more(page):
        clicked.append("yes")
        await page.click("#more")

    def configure(m: SessionManager) -> None:
        m.add("b", BrowserSession(page_action=click_more, wait_selector=".revealed", wait_selector_state="visible"))

    d = await _downloader(configure)
    try:
        r = await d.fetch(Request(url=site.base + "/click", session="b"))
    finally:
        await d.close()
    assert clicked == ["yes"] and "revealed-by-click" in r.text


async def test_regression_streaming_xhr_through_the_spider_session(site):
    def configure(m: SessionManager) -> None:
        # wait_until="load": the default ("networkidle") would itself wait out the
        # streaming body, hiding the very race this test exists to catch.
        m.add(
            "b",
            BrowserSession(wait_until="load", wait_selector="#done:has-text('fast-done')", capture_xhr=True),
        )

    d = await _downloader(configure)
    try:
        r = await d.fetch(Request(url=site.base + "/stream", session="b"))
    finally:
        await d.close()
    streaming = next(x for x in r.extra["captured_xhr"] if x["url"].endswith("/streaming"))
    assert streaming["body"] == '{"k": "streaming"}'


async def test_browser_failure_is_a_download_error_in_the_spider_path(site):
    def configure(m: SessionManager) -> None:
        m.add("b", BrowserSession(timeout=5))

    d = await _downloader(configure)
    try:
        with pytest.raises(DownloadError, match="ERR_CONNECTION_REFUSED") as exc:
            await d.fetch(Request(url=f"http://127.0.0.1:{_closed_port()}/", session="b"))
        assert isinstance(exc.value.__cause__, DynamicFetchError)
    finally:
        await d.close()


async def test_regression_browser_failures_take_the_normal_failure_path_in_a_spider(site, caplog):
    """A navigation failure used to surface as a raw Playwright error: logged at ERROR with
    a full traceback as an 'unexpected error', not counted per session, errback skipped."""
    from bitscrape.engine import Engine

    errbacks: list[Any] = []

    class BrowserSpider(Spider):
        name = "browser_failure"
        sessions = {"b": {"kind": "browser", "timeout": 5}}

        def start_requests(self):
            url = f"http://127.0.0.1:{_closed_port()}/"
            return [Request(url=url, session="b", errback="on_error", max_retries=0)]

        async def parse(self, response):
            yield {"never": True}

        async def on_error(self, request, exc):
            errbacks.append(exc)

    spider = BrowserSpider()
    with caplog.at_level(logging.WARNING):
        stats = await Engine(spider=spider, settings=spider.settings).run()
    assert stats.requests_failed == 1
    assert stats.sessions_failed == {"b": 1}  # counted against the session
    assert len(errbacks) == 1  # the spider's errback ran, and was handed the failure
    assert "ERR_CONNECTION_REFUSED" in str(errbacks[0])
    assert not [r for r in caplog.records if r.levelno >= logging.ERROR]  # no ERROR + traceback


async def test_spider_browser_session_renders_pages_end_to_end(site):
    from bitscrape.engine import Engine

    class ShopSpider(Spider):
        name = "browser_shop"
        sessions = {"b": {"kind": "browser", "wait_selector": "li.product"}}

        def start_requests(self):
            return [Request(url=site.base + "/", session="b")]

        async def parse(self, response):
            for name in response.css("li.product::text").getall():
                yield {"name": name}

    spider = ShopSpider()
    settings = spider.settings.model_copy(update={"collect_items": True})
    stats = await Engine(spider=spider, settings=settings).run()
    assert [i["name"] for i in stats.items] == ["Widget", "Gadget", "Gizmo"]


# ---------------------------------------------------------------------------
# The legacy per-request path (``use_playwright=True``, pooled and unpooled),
# against the real browser. It shares the XHR capture and proxy handling with
# the paths above, and had the same bugs.
# ---------------------------------------------------------------------------


async def _legacy_fetch(settings: Settings, request: Request) -> Any:
    d = Downloader(settings)
    await d.open()
    try:
        return await d.fetch(request)
    finally:
        await d.close()


@pytest.mark.parametrize("pooled", [False, True], ids=["unpooled", "pooled"])
async def test_legacy_use_playwright_renders_javascript(site, pooled):
    settings = Settings(playwright_pool_enabled=pooled, playwright_pool_size=1, browser_wait_selector="li.product")
    r = await _legacy_fetch(settings, Request(url=site.base + "/", use_playwright=True))
    assert r.status == 200 and "Widget" in r.text and "Gizmo" in r.text


async def test_legacy_pool_reuses_one_browser_for_many_fetches(site):
    d = Downloader(Settings(playwright_pool_enabled=True, playwright_pool_size=1, browser_wait_selector="li.product"))
    await d.open()
    try:
        browser_before = d._browser_pool._all_browsers[0]
        for _ in range(3):
            r = await d.fetch(Request(url=site.base + "/", use_playwright=True, dont_filter=True))
            assert "Widget" in r.text
        assert d._browser_pool._all_browsers == [browser_before]  # same process, three fetches
        assert d._browser_pool.available == 1  # and it was returned each time
    finally:
        await d.close()


@pytest.mark.parametrize("pooled", [False, True], ids=["unpooled", "pooled"])
async def test_regression_legacy_path_captures_a_streaming_xhr_body(site, pooled):
    settings = Settings(
        playwright_pool_enabled=pooled,
        playwright_pool_size=1,
        browser_wait_until="load",
        browser_wait_selector="#done:has-text('fast-done')",
        browser_capture_xhr=True,
    )
    r = await _legacy_fetch(settings, Request(url=site.base + "/stream", use_playwright=True))
    by_name = {x["url"].rsplit("/", 1)[1]: x for x in r.extra["captured_xhr"]}
    assert by_name["streaming"]["body"] == '{"k": "streaming"}'


@pytest.mark.parametrize("pooled", [False, True], ids=["unpooled", "pooled"])
async def test_regression_legacy_path_authenticated_proxy(site, auth_proxy, pooled):
    settings = Settings(
        playwright_pool_enabled=pooled,
        playwright_pool_size=1,
        browser_args=_PROXY_LOOPBACK,
        browser_wait_until="load",
    )
    request = Request(
        url=site.base + "/static",
        use_playwright=True,
        meta={"proxy": f"http://alice:s3cret@127.0.0.1:{auth_proxy.port}"},
    )
    r = await _legacy_fetch(settings, request)
    assert r.status == 200 and "Static page" in r.text
    assert auth_proxy.proxied == [site.base + "/static"]


async def test_regression_pooled_browsers_honor_the_browser_launch_settings(site):
    """The pool used to launch with ``headless`` only, silently ignoring
    browser_executable_path / browser_channel / browser_args."""
    d = Downloader(Settings(playwright_pool_enabled=True, browser_executable_path="/no/such/chrome"))
    with pytest.raises(BrowserLaunchError, match="/no/such/chrome"):
        await d.open()


async def test_a_failed_pool_start_leaves_nothing_running(site):
    psutil = pytest.importorskip("psutil")

    def procs() -> int:
        n = 0
        for child in psutil.Process().children(recursive=True):
            try:
                if any(k in " ".join(child.cmdline()).lower() for k in ("chrom", "headless_shell", "playwright")):
                    n += 1
            except psutil.Error:
                continue
        return n

    baseline = procs()
    d = Downloader(Settings(playwright_pool_enabled=True, playwright_pool_size=2, browser_executable_path="/no/such/chrome"))
    with pytest.raises(BrowserLaunchError):
        await d.open()
    assert d.sessions is None and d._session is None  # open() cleaned up after itself
    deadline = time.monotonic() + 5
    while procs() > baseline and time.monotonic() < deadline:
        await asyncio.sleep(0.1)
    assert procs() <= baseline
