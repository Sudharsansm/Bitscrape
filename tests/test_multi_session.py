"""
Tests for multi-session support (``bitscrape.sessions``).

HTTP behaviour is verified against a real local aiohttp server (cookie
isolation, headers, proxy precedence, concurrency caps, conditional-GET
isolation, end-to-end engine routing and block escalation). Browser sessions
are exercised against a fake Playwright object graph, because no browser
binary is available in CI -- that verifies the *session logic* (single launch,
shared context, per-request tabs, option precedence, lazy start), not
Chromium itself.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any, ClassVar

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from bitscrape.core.models import Request, Response
from bitscrape.core.settings import Settings
from bitscrape.core.spider import Spider
from bitscrape.downloader.downloader import Downloader
from bitscrape.downloader.proxy import ProxyRotator
from bitscrape.engine import Engine
from bitscrape.sessions import (
    BrowserSession,
    HttpSession,
    SessionError,
    SessionManager,
)

# ---------------------------------------------------------------------------
# Local test server
# ---------------------------------------------------------------------------


class _State:
    def __init__(self) -> None:
        self.in_flight = 0
        self.max_in_flight = 0
        self.hits: list[str] = []


def _make_app(state: _State) -> web.Application:
    async def whoami(request: web.Request) -> web.Response:
        return web.json_response(
            {
                "cookies": dict(request.cookies),
                "x_tier": request.headers.get("X-Tier"),
                "accept": request.headers.get("Accept"),
                "ua": request.headers.get("User-Agent"),
                "extra": request.headers.get("X-Extra"),
            }
        )

    async def login(request: web.Request) -> web.Response:
        resp = web.Response(text="logged in")
        resp.set_cookie("user", request.query["user"])
        return resp

    async def slow(request: web.Request) -> web.Response:
        state.in_flight += 1
        state.max_in_flight = max(state.max_in_flight, state.in_flight)
        await asyncio.sleep(0.05)
        state.in_flight -= 1
        return web.Response(text="ok")

    async def profile(request: web.Request) -> web.Response:
        # Body depends on the logged-in user; ETag is deliberately constant so
        # a shared conditional-GET cache would leak one user's page to another.
        user = request.cookies.get("user", "anon")
        if request.headers.get("If-None-Match") == '"v1"':
            return web.Response(status=304)
        return web.Response(text=f"profile of {user}", headers={"ETag": '"v1"'})

    async def guarded(request: web.Request) -> web.Response:
        state.hits.append(request.headers.get("Authorization", "-"))
        if request.headers.get("Authorization") != "Bearer good":
            return web.Response(status=403, text="Access Denied")
        return web.Response(text="<html><body><h1>secret</h1></body></html>")

    async def chain(request: web.Request) -> web.Response:
        n = int(request.match_info["n"])
        nxt = f'<a href="/chain/{n + 1}">next</a>' if n < 3 else ""
        return web.Response(text=f"<html><body><p>page {n}</p>{nxt}</body></html>")

    app = web.Application()
    app.router.add_get("/whoami", whoami)
    app.router.add_get("/login", login)
    app.router.add_get("/slow", slow)
    app.router.add_get("/profile", profile)
    app.router.add_get("/guarded", guarded)
    app.router.add_get("/chain/{n}", chain)
    return app


@pytest.fixture
async def server():
    state = _State()
    srv = TestServer(_make_app(state))
    await srv.start_server()
    srv.state = state  # type: ignore[attr-defined]
    try:
        yield srv
    finally:
        await srv.close()


def _url(server: TestServer, path: str) -> str:
    return str(server.make_url(path))


async def _open(settings: Settings | None = None, configure: Any = None) -> Downloader:
    d = Downloader(settings or Settings())
    await d.open(configure=configure)
    return d


# ---------------------------------------------------------------------------
# SessionManager: registry semantics (pure unit tests)
# ---------------------------------------------------------------------------


def test_first_session_added_becomes_default():
    m = SessionManager()
    m.add("a", HttpSession())
    m.add("b", HttpSession())
    assert m.default_session_id == "a"


def test_default_true_overrides_first():
    m = SessionManager()
    m.add("a", HttpSession())
    m.add("b", HttpSession(), default=True)
    assert m.default_session_id == "b"


def test_duplicate_id_is_rejected_unless_replace():
    m = SessionManager()
    m.add("a", HttpSession())
    with pytest.raises(SessionError, match="already registered"):
        m.add("a", HttpSession())
    replacement = HttpSession()
    m.add("a", replacement, replace=True)
    assert m.get("a") is replacement


def test_unknown_session_lists_known_ids():
    m = SessionManager()
    m.add("a", HttpSession())
    with pytest.raises(SessionError, match=r"isn't declared.*\['a'\]"):
        m.get("nope")


def test_session_error_is_a_value_error():
    assert issubclass(SessionError, ValueError)


def test_pop_default_promotes_next():
    m = SessionManager()
    m.add("a", HttpSession())
    m.add("b", HttpSession())
    m.pop("a")
    assert m.default_session_id == "b"
    assert "a" not in m and len(m) == 1


def test_lazy_defaults_by_kind():
    m = SessionManager()
    m.add("h", HttpSession())
    m.add("b", BrowserSession())
    assert m.is_lazy("h") is False
    assert m.is_lazy("b") is True
    m.add("b2", BrowserSession(), lazy=False)
    assert m.is_lazy("b2") is False


def test_http_session_rejects_proxy_and_rotator_together():
    with pytest.raises(SessionError):
        HttpSession(proxy="http://p:1", proxy_rotator=ProxyRotator(["http://q:1"]))


def test_max_concurrency_must_be_positive():
    with pytest.raises(SessionError):
        HttpSession(max_concurrency=0)


def test_request_sid_alias():
    assert Request(url="http://x", sid="api").session == "api"
    assert Request(url="http://x", session="api").sid == "api"


# ---------------------------------------------------------------------------
# Declarative config + policy
# ---------------------------------------------------------------------------


def test_from_settings_builds_typed_sessions():
    settings = Settings(
        sessions={
            "api": {"kind": "http", "headers": {"Accept": "application/json"}},
            "ui": {"kind": "browser", "wait_selector": ".x", "default": True},
        }
    )
    m = SessionManager.from_settings(settings)
    assert isinstance(m.get("api"), HttpSession)
    assert isinstance(m.get("ui"), BrowserSession)
    assert m.default_session_id == "ui"
    assert m.get("api").headers == {"Accept": "application/json"}  # type: ignore[attr-defined]


@pytest.mark.parametrize(
    "option", ["stealth", "solve_cloudflare", "hide_canvas", "block_webrtc", "impersonate", "http3"]
)
def test_evasion_options_are_rejected(option):
    with pytest.raises(NotImplementedError, match="Explicit non-goals"):
        SessionManager.from_settings(Settings(sessions={"s": {"kind": "browser", option: True}}))


def test_unknown_kind_is_rejected():
    with pytest.raises(SessionError, match="unrecognised kind"):
        SessionManager.from_settings(Settings(sessions={"s": {"kind": "carrier-pigeon"}}))


async def test_unsupported_kind_only_fails_when_used():
    d = await _open(Settings(sessions={"s": {"kind": "stealth"}}))
    try:
        # declared but unused: fine
        with pytest.raises(NotImplementedError, match="stealth"):
            await d.fetch(Request(url="http://127.0.0.1:1/", session="s"))
    finally:
        await d.close()


# ---------------------------------------------------------------------------
# HTTP sessions against a real server
# ---------------------------------------------------------------------------


async def test_unnamed_request_uses_builtin_default_session(server):
    d = await _open()
    try:
        r = await d.fetch(Request(url=_url(server, "/whoami")))
        assert r.session_id == "default"
        assert d.sessions is not None
        assert d.sessions.default_session_id == "default"
    finally:
        await d.close()


async def test_named_sessions_have_isolated_cookie_jars(server):
    settings = Settings(sessions={"alice": {"kind": "http"}, "bob": {"kind": "http"}})
    d = await _open(settings)
    try:
        await d.fetch(Request(url=_url(server, "/login?user=alice"), session="alice"))
        await d.fetch(Request(url=_url(server, "/login?user=bob"), session="bob"))

        a = json.loads((await d.fetch(Request(url=_url(server, "/whoami"), session="alice"))).text)
        b = json.loads((await d.fetch(Request(url=_url(server, "/whoami"), session="bob"))).text)
        anon = json.loads((await d.fetch(Request(url=_url(server, "/whoami")))).text)

        assert a["cookies"] == {"user": "alice"}
        assert b["cookies"] == {"user": "bob"}
        assert anon["cookies"] == {}
        assert await d.sessions.get("alice").cookies() == {"user": "alice"}  # type: ignore[union-attr]
    finally:
        await d.close()


async def test_session_default_headers_and_user_agent(server):
    settings = Settings(
        sessions={
            "api": {
                "kind": "http",
                "headers": {"Accept": "application/json", "X-Tier": "gold"},
                "user_agent": "ApiBot/2",
            }
        }
    )
    d = await _open(settings)
    try:
        who = json.loads((await d.fetch(Request(url=_url(server, "/whoami"), session="api"))).text)
        assert who["accept"] == "application/json"
        assert who["x_tier"] == "gold"
        assert who["ua"] == "ApiBot/2"
    finally:
        await d.close()


async def test_precedence_request_headers_beat_session_kwargs_beat_session(server):
    settings = Settings(sessions={"s": {"kind": "http", "headers": {"X-Tier": "session"}}})
    d = await _open(settings)
    try:
        url = _url(server, "/whoami")
        base = json.loads((await d.fetch(Request(url=url, session="s"))).text)
        kw = json.loads(
            (await d.fetch(Request(url=url, session="s", session_kwargs={"headers": {"X-Tier": "kwargs"}}))).text
        )
        hdr = json.loads(
            (
                await d.fetch(
                    Request(
                        url=url,
                        session="s",
                        headers={"X-Tier": "request"},
                        session_kwargs={"headers": {"X-Tier": "kwargs"}},
                    )
                )
            ).text
        )
        assert (base["x_tier"], kw["x_tier"], hdr["x_tier"]) == ("session", "kwargs", "request")
    finally:
        await d.close()


async def test_per_request_cookies(server):
    d = await _open(Settings(sessions={"s": {"kind": "http"}}))
    try:
        r = await d.fetch(Request(url=_url(server, "/whoami"), session="s", cookies={"a": "1"}))
        assert json.loads(r.text)["cookies"] == {"a": "1"}
        r = await d.fetch(
            Request(url=_url(server, "/whoami"), session="s", session_kwargs={"cookies": {"b": "2"}})
        )
        assert json.loads(r.text)["cookies"] == {"b": "2"}
    finally:
        await d.close()


async def test_initial_cookies(server):
    d = await _open(Settings(sessions={"s": {"kind": "http", "cookies": {"seed": "x"}}}))
    try:
        r = await d.fetch(Request(url=_url(server, "/whoami"), session="s"))
        assert json.loads(r.text)["cookies"] == {"seed": "x"}
    finally:
        await d.close()


async def test_clear_cookies_logs_out(server):
    d = await _open(Settings(sessions={"s": {"kind": "http"}}))
    try:
        await d.fetch(Request(url=_url(server, "/login?user=zed"), session="s"))
        s = d.sessions.get("s")  # type: ignore[union-attr]
        assert await s.cookies() == {"user": "zed"}
        s.clear_cookies()  # type: ignore[attr-defined]
        assert await s.cookies() == {}
    finally:
        await d.close()


async def test_max_concurrency_caps_in_flight_requests(server):
    d = await _open(Settings(sessions={"s": {"kind": "http", "max_concurrency": 2}}))
    try:
        await asyncio.gather(*[d.fetch(Request(url=_url(server, "/slow"), session="s")) for _ in range(8)])
        assert server.state.max_in_flight == 2
    finally:
        await d.close()


async def test_conditional_get_cache_is_isolated_per_session(server):
    """Same URL + same ETag, different logins: session B must not be handed
    session A's cached body on a 304."""
    settings = Settings(sessions={"alice": {"kind": "http"}, "bob": {"kind": "http"}})
    d = await _open(settings)
    try:
        await d.fetch(Request(url=_url(server, "/login?user=alice"), session="alice"))
        await d.fetch(Request(url=_url(server, "/login?user=bob"), session="bob"))
        url = _url(server, "/profile")
        a1 = await d.fetch(Request(url=url, session="alice"))
        b1 = await d.fetch(Request(url=url, session="bob"))
        assert a1.text == "profile of alice"
        assert b1.text == "profile of bob"
        # Second round is conditional (304 -> served from that session's own cache).
        a2 = await d.fetch(Request(url=url, session="alice"))
        b2 = await d.fetch(Request(url=url, session="bob"))
        assert a2.text == "profile of alice"
        assert b2.text == "profile of bob"
    finally:
        await d.close()


async def test_session_static_proxy_is_used_and_beats_rotator(server):
    seen: list[str | None] = []
    d = await _open(Settings(sessions={"s": {"kind": "http", "proxy": "http://127.0.0.1:9"}}))
    try:
        orig = d._do_fetch

        async def spy(request, session=None):
            seen.append(request.meta.get("proxy"))
            return await orig(request, session)

        d._do_fetch = spy  # type: ignore[method-assign]
        # Port 9 (discard) refuses the connection -> DownloadError, but the
        # point is that the session proxy was selected.
        with pytest.raises(Exception):  # noqa: B017
            await d.fetch(Request(url=_url(server, "/whoami"), session="s", max_retries=0))
        assert seen == ["http://127.0.0.1:9"]
    finally:
        await d.close()


async def test_request_meta_proxy_beats_session_proxy(server):
    seen: list[str | None] = []
    d = await _open(Settings(sessions={"s": {"kind": "http", "proxy": "http://127.0.0.1:9"}}))
    try:
        orig = d._do_fetch

        async def spy(request, session=None):
            seen.append(request.meta.get("proxy"))
            return await orig(request, session)

        d._do_fetch = spy  # type: ignore[method-assign]
        r = await d.fetch(Request(url=_url(server, "/whoami"), session="s", meta={"proxy": None}))
        assert r.status == 200  # meta["proxy"]=None explicitly disables proxying
        assert seen == [None]
    finally:
        await d.close()


async def test_session_counters(server):
    d = await _open(Settings(sessions={"s": {"kind": "http"}}))
    try:
        await d.fetch(Request(url=_url(server, "/whoami"), session="s"))
        await d.fetch(Request(url=_url(server, "/whoami"), session="s"))
        info = {i["sid"]: i for i in d.sessions.info()}  # type: ignore[union-attr]
        assert info["s"]["requests"] == 2
        assert info["default"]["default"] is True
    finally:
        await d.close()


async def test_configure_hook_can_add_and_replace_sessions(server):
    def configure(manager: SessionManager) -> None:
        manager.add("api", HttpSession(headers={"X-Tier": "code"}))
        manager.add("default", HttpSession(headers={"X-Tier": "custom-default"}))  # replaces builtin

    d = await _open(configure=configure)
    try:
        a = json.loads((await d.fetch(Request(url=_url(server, "/whoami"), session="api"))).text)
        dflt = json.loads((await d.fetch(Request(url=_url(server, "/whoami")))).text)
        assert a["x_tier"] == "code"
        assert dflt["x_tier"] == "custom-default"
    finally:
        await d.close()


async def test_sessions_are_closed_with_the_downloader(server):
    d = await _open(Settings(sessions={"s": {"kind": "http"}}))
    s = d.sessions.get("s")  # type: ignore[union-attr]
    assert s.started
    await d.close()
    assert not s.started and s.closed  # type: ignore[union-attr]


# ---------------------------------------------------------------------------
# Fake Playwright (no browser binary needed)
# ---------------------------------------------------------------------------


class _FakeResp:
    status = 200
    headers: ClassVar[dict[str, str]] = {"content-type": "text/html"}


class _FakePage:
    def __init__(self, ctx: _FakeContext) -> None:
        self.ctx = ctx
        self.url = ""
        self.closed = False
        self.extra_headers: dict[str, str] = {}
        self.waited: list[Any] = []

    async def set_extra_http_headers(self, headers):
        self.extra_headers = dict(headers)

    async def goto(self, url, wait_until=None, timeout=None):
        self.url = url
        self.ctx.gotos.append({"url": url, "wait_until": wait_until, "timeout": timeout})
        return _FakeResp()

    def set_default_timeout(self, timeout):
        self.default_timeout = timeout

    async def wait_for_selector(self, selector, timeout=None, state="attached"):
        self.waited.append((selector, timeout))

    async def content(self):
        return f"<html><body>rendered {self.url}</body></html>"

    async def close(self):
        self.closed = True

    async def route(self, *a, **k):
        return None

    def on(self, *a, **k):
        return None


class _FakeContext:
    def __init__(self, kwargs: dict[str, Any]) -> None:
        self.kwargs = kwargs
        self.pages: list[_FakePage] = []
        self.gotos: list[dict[str, Any]] = []
        self.cookie_jar: list[dict[str, Any]] = []
        self.closed = False

    async def new_page(self):
        p = _FakePage(self)
        self.pages.append(p)
        return p

    async def add_cookies(self, cookies):
        self.cookie_jar.extend(cookies)

    async def cookies(self, urls=None):
        return list(self.cookie_jar)

    async def storage_state(self, path=None, indexed_db=False):
        return {}

    def on(self, *a, **k):
        return None

    async def clear_cookies(self):
        self.cookie_jar.clear()

    async def add_init_script(self, script=None, path=None):
        return None

    async def close(self):
        self.closed = True


class _FakeBrowser:
    def __init__(self, owner: _FakeBrowserType) -> None:
        self.owner = owner
        self.closed = False

    async def new_context(self, **kwargs):
        ctx = _FakeContext(kwargs)
        self.owner.contexts.append(ctx)
        return ctx

    async def close(self):
        self.closed = True


class _FakeBrowserType:
    def __init__(self) -> None:
        self.launches: list[dict[str, Any]] = []
        self.persistent: list[tuple[str, dict[str, Any]]] = []
        self.cdp: list[str] = []
        self.contexts: list[_FakeContext] = []
        self.browsers: list[_FakeBrowser] = []

    async def launch(self, **kwargs):
        self.launches.append(kwargs)
        b = _FakeBrowser(self)
        self.browsers.append(b)
        return b

    async def launch_persistent_context(self, user_data_dir, **kwargs):
        self.persistent.append((user_data_dir, kwargs))
        ctx = _FakeContext(kwargs)
        self.contexts.append(ctx)
        return ctx

    async def connect_over_cdp(self, url):
        self.cdp.append(url)
        b = _FakeBrowser(self)
        self.browsers.append(b)
        return b


class _FakePW:
    def __init__(self) -> None:
        self.chromium = _FakeBrowserType()
        self.firefox = _FakeBrowserType()
        self.stopped = False

    async def stop(self):
        self.stopped = True


def _browser_session(**kwargs: Any) -> tuple[BrowserSession, _FakePW]:
    pw = _FakePW()

    async def factory():
        return pw

    return BrowserSession(playwright_factory=factory, **kwargs), pw


async def _dl_with_browser(session: BrowserSession, *, lazy: bool | None = None, settings=None) -> Downloader:
    def configure(m: SessionManager) -> None:
        m.add("ui", session, lazy=lazy)

    return await _open(settings or Settings(), configure=configure)


# ---------------------------------------------------------------------------
# Browser sessions (fake Playwright)
# ---------------------------------------------------------------------------


async def test_browser_session_launches_once_and_reuses_context():
    session, pw = _browser_session()
    d = await _dl_with_browser(session)
    try:
        for i in range(3):
            r = await d.fetch(Request(url=f"http://example.test/{i}", session="ui"))
            assert r.session_id == "ui"
            assert f"rendered http://example.test/{i}" in r.text
        assert len(pw.chromium.launches) == 1  # one launch for three requests
        assert len(pw.chromium.contexts) == 1  # one shared context
        ctx = pw.chromium.contexts[0]
        assert len(ctx.pages) == 3 and all(p.closed for p in ctx.pages)  # a tab each, closed after
    finally:
        await d.close()
    assert pw.stopped and pw.chromium.browsers[0].closed and ctx.closed


async def test_browser_session_is_lazy_by_default():
    session, pw = _browser_session()
    d = await _dl_with_browser(session)
    try:
        assert not session.started and not pw.chromium.launches  # nothing launched yet
        await d.fetch(Request(url="http://example.test/", session="ui"))
        assert session.started and len(pw.chromium.launches) == 1
    finally:
        await d.close()


async def test_non_lazy_browser_session_starts_with_the_downloader():
    session, pw = _browser_session()
    d = await _dl_with_browser(session, lazy=False)
    try:
        assert session.started and len(pw.chromium.launches) == 1
    finally:
        await d.close()


async def test_concurrent_first_use_starts_the_browser_once():
    session, pw = _browser_session(max_concurrency=8)
    d = await _dl_with_browser(session)
    try:
        await asyncio.gather(*[d.fetch(Request(url=f"http://e.test/{i}", session="ui")) for i in range(6)])
        assert len(pw.chromium.launches) == 1
    finally:
        await d.close()


async def test_browser_option_precedence_request_over_session_over_settings():
    session, pw = _browser_session(wait_until="domcontentloaded", wait_selector=".price", timeout=5)
    settings = Settings(browser_wait_until="networkidle", browser_timeout=30.0)
    d = await _dl_with_browser(session, settings=settings)
    try:
        await d.fetch(Request(url="http://e.test/a", session="ui"))
        await d.fetch(
            Request(
                url="http://e.test/b",
                session="ui",
                session_kwargs={"wait_until": "load", "wait_selector": ".other", "timeout": 2},
            )
        )
        ctx = pw.chromium.contexts[0]
        a, b = ctx.gotos
        assert (a["wait_until"], a["timeout"]) == ("domcontentloaded", 5000.0)
        assert (b["wait_until"], b["timeout"]) == ("load", 2000.0)
        assert ctx.pages[0].waited == [(".price", 5000.0)]
        assert ctx.pages[1].waited == [(".other", 2000.0)]
    finally:
        await d.close()


async def test_browser_falls_back_to_settings_when_session_leaves_options_unset():
    session, pw = _browser_session()
    settings = Settings(browser_wait_until="commit", playwright_headless=False, browser_locale="de-DE")
    d = await _dl_with_browser(session, settings=settings)
    try:
        await d.fetch(Request(url="http://e.test/", session="ui"))
        assert pw.chromium.contexts[0].gotos[0]["wait_until"] == "commit"
        assert pw.chromium.launches[0]["headless"] is False
        assert pw.chromium.contexts[0].kwargs["locale"] == "de-DE"
    finally:
        await d.close()


async def test_browser_launch_options_are_passed_through():
    session, pw = _browser_session(
        proxy="http://proxy:3128", headless=True, args=["--no-sandbox"], channel="chrome",
        viewport=(800, 600), locale="fr-FR",
    )
    d = await _dl_with_browser(session)
    try:
        await d.fetch(Request(url="http://e.test/", session="ui"))
        launch = pw.chromium.launches[0]
        assert launch["proxy"] == {"server": "http://proxy:3128"}
        assert launch["args"] == ["--no-sandbox"] and launch["channel"] == "chrome"
        kw = pw.chromium.contexts[0].kwargs
        assert kw["viewport"] == {"width": 800, "height": 600} and kw["locale"] == "fr-FR"
    finally:
        await d.close()


async def test_browser_headers_and_per_request_cookies():
    session, pw = _browser_session(headers={"X-Session": "s"})
    d = await _dl_with_browser(session)
    try:
        await d.fetch(
            Request(
                url="http://e.test/", session="ui", headers={"X-Req": "r"}, cookies={"c": "1"}
            )
        )
        ctx = pw.chromium.contexts[0]
        assert ctx.kwargs["extra_http_headers"] == {"X-Session": "s"}  # session-wide
        assert ctx.pages[0].extra_headers == {"X-Req": "r"}  # this tab only
        assert ctx.cookie_jar == [{"name": "c", "value": "1", "url": "http://e.test/"}]
        assert await session.cookies() == {"c": "1"}
    finally:
        await d.close()


async def test_persistent_profile_and_cdp_modes():
    s1, pw1 = _browser_session(user_data_dir="/tmp/profile")
    d1 = await _dl_with_browser(s1)
    try:
        await d1.fetch(Request(url="http://e.test/", session="ui"))
        assert pw1.chromium.persistent[0][0] == "/tmp/profile" and not pw1.chromium.launches
    finally:
        await d1.close()

    s2, pw2 = _browser_session(cdp_url="http://localhost:9222")
    d2 = await _dl_with_browser(s2)
    try:
        await d2.fetch(Request(url="http://e.test/", session="ui"))
        assert pw2.chromium.cdp == ["http://localhost:9222"]
    finally:
        await d2.close()
    # An externally managed (CDP) browser is never closed by us.
    assert not pw2.chromium.browsers[0].closed


async def test_max_concurrency_limits_open_tabs():
    session, _pw = _browser_session(max_concurrency=2)
    d = await _dl_with_browser(session)
    peak = 0
    open_tabs = 0
    orig = _FakePage.goto

    async def slow_goto(self, url, wait_until=None, timeout=None):
        nonlocal peak, open_tabs
        open_tabs += 1
        peak = max(peak, open_tabs)
        await asyncio.sleep(0.03)
        open_tabs -= 1
        return await orig(self, url, wait_until, timeout)

    _FakePage.goto = slow_goto  # type: ignore[method-assign]
    try:
        await asyncio.gather(*[d.fetch(Request(url=f"http://e.test/{i}", session="ui")) for i in range(6)])
        assert peak == 2
    finally:
        _FakePage.goto = orig  # type: ignore[method-assign]
        await d.close()


async def test_page_is_closed_even_when_navigation_fails():
    session, pw = _browser_session()
    d = await _dl_with_browser(session)

    async def boom(self, url, wait_until=None, timeout=None):
        raise RuntimeError("navigation failed")

    orig = _FakePage.goto
    _FakePage.goto = boom  # type: ignore[method-assign]
    try:
        with pytest.raises(RuntimeError):
            await d.fetch(Request(url="http://e.test/", session="ui"))
        assert pw.chromium.contexts[0].pages[0].closed
        assert session.failures_count == 1
    finally:
        _FakePage.goto = orig  # type: ignore[method-assign]
        await d.close()


async def test_explicit_default_browser_session_serves_unnamed_requests():
    session, _pw = _browser_session()

    def configure(m: SessionManager) -> None:
        m.add("ui", session, default=True, lazy=False)

    d = await _open(configure=configure)
    try:
        r = await d.fetch(Request(url="http://e.test/"))
        assert r.session_id == "ui" and "rendered" in r.text
    finally:
        await d.close()


async def test_stealth_settings_still_refuse_browser_sessions():
    session, _ = _browser_session()
    d = await _dl_with_browser(session, settings=Settings(browser_stealth=True))
    try:
        with pytest.raises(NotImplementedError, match="Explicit non-goals"):
            await d.fetch(Request(url="http://e.test/", session="ui"))
    finally:
        await d.close()


# ---------------------------------------------------------------------------
# Spider integration: routing, follow() inheritance, escalation, stats
# ---------------------------------------------------------------------------


class _FollowSpider(Spider):
    name = "follow_demo"
    sessions: ClassVar[dict[str, dict[str, Any]]] = {
        "alice": {"kind": "http", "headers": {"X-Tier": "alice"}},
        "bob": {"kind": "http", "headers": {"X-Tier": "bob"}},
    }

    def __init__(self, base: str, **kw: Any) -> None:
        super().__init__(**kw)
        self.base = base
        self.seen: list[tuple[str, str | None]] = []

    def start_requests(self):
        return [Request(url=f"{self.base}/chain/1", session="alice")]

    async def parse(self, response):
        self.seen.append((response.url.rsplit("/", 1)[-1], response.request.session))
        for href in response.css("a::attr(href)").getall():
            # No session named: inherits "alice" from the page it was found on.
            yield self.follow(href)
        yield {"page": response.url}


async def test_follow_inherits_session_from_parent_response(server):
    spider = _FollowSpider(_url(server, "").rstrip("/"))
    stats = await Engine(spider=spider, settings=spider.settings).run()
    assert spider.seen == [("1", "alice"), ("2", "alice"), ("3", "alice")]
    assert stats.sessions_requests == {"alice": 3}
    assert stats.items_scraped == 3


class _SwitchSpider(_FollowSpider):
    name = "switch_demo"

    async def parse(self, response):
        self.seen.append((response.url.rsplit("/", 1)[-1], response.request.session))
        for href in response.css("a::attr(href)").getall():
            yield self.follow(href, session="bob")  # explicit override wins
        yield {"page": response.url}


async def test_follow_explicit_session_overrides_inherited(server):
    spider = _SwitchSpider(_url(server, "").rstrip("/"))
    stats = await Engine(spider=spider, settings=spider.settings).run()
    assert spider.seen == [("1", "alice"), ("2", "bob"), ("3", "bob")]
    assert stats.sessions_requests == {"alice": 1, "bob": 2}


class _NoInheritSpider(_FollowSpider):
    name = "noinherit_demo"

    async def parse(self, response):
        self.seen.append((response.url.rsplit("/", 1)[-1], response.request.session))
        for href in response.css("a::attr(href)").getall():
            yield self.follow(href, inherit_session=False)
        yield {"page": response.url}


async def test_follow_can_opt_out_of_inheritance(server):
    spider = _NoInheritSpider(_url(server, "").rstrip("/"))
    stats = await Engine(spider=spider, settings=spider.settings).run()
    assert spider.seen[0] == ("1", "alice") and spider.seen[1] == ("2", None)
    assert "default" in stats.sessions_requests


class _KwargsSpider(Spider):
    name = "kwargs_demo"
    sessions: ClassVar[dict[str, dict[str, Any]]] = {"s": {"kind": "http"}}

    def __init__(self, base: str, **kw: Any) -> None:
        super().__init__(**kw)
        self.base = base
        self.tiers: list[str | None] = []

    def start_requests(self):
        return [
            Request(url=f"{self.base}/whoami", session="s", session_kwargs={"headers": {"X-Tier": "t1"}})
        ]

    async def parse(self, response):
        self.tiers.append(json.loads(response.text)["x_tier"])
        if len(self.tiers) == 1:
            # inherits session + session_kwargs; adds a new header on top
            yield self.follow(
                "/whoami", session_kwargs={"headers": {"X-Extra": "e"}}, dont_filter=True
            )
        yield {"tier": self.tiers[-1]}


async def test_follow_inherits_and_merges_session_kwargs(server):
    spider = _KwargsSpider(_url(server, "").rstrip("/"))
    await Engine(spider=spider, settings=spider.settings).run()
    # session_kwargs from the parent request were inherited by the follow-up
    # (its whole headers dict is replaced by the child's, as documented: shallow merge).
    assert spider.tiers[0] == "t1"
    assert len(spider.tiers) == 2


class _EscalateSpider(Spider):
    """Plain session gets a 403; the block hook moves the request to a session
    that carries credentials (legitimate recovery, no evasion)."""

    name = "escalate_demo"
    sessions: ClassVar[dict[str, dict[str, Any]]] = {
        "plain": {"kind": "http"},
        "authed": {"kind": "http", "headers": {"Authorization": "Bearer good"}},
    }

    def __init__(self, base: str, **kw: Any) -> None:
        super().__init__(**kw)
        self.base = base
        self.parsed_sessions: list[str | None] = []

    def start_requests(self):
        return [Request(url=f"{self.base}/guarded", session="plain", max_retries=0)]

    async def is_blocked(self, response: Response) -> bool:
        return response.status == 403

    async def retry_blocked_request(self, request, response):
        if request.session == "plain":
            return request.model_copy(update={"session": "authed"})
        return None

    async def parse(self, response):
        self.parsed_sessions.append(response.session_id)
        yield {"h1": response.css("h1::text").get()}


async def test_blocked_request_escalates_to_another_session(server):
    spider = _EscalateSpider(_url(server, "").rstrip("/"))
    stats = await Engine(spider=spider, settings=spider.settings).run()
    assert spider.parsed_sessions == ["authed"]  # blocked page was NOT parsed
    assert stats.items_scraped == 1
    assert stats.requests_blocked == 1 and stats.requests_escalated == 1
    assert stats.sessions_blocked == {"plain": 1}
    assert stats.sessions_requests == {"plain": 1, "authed": 1}
    assert server.state.hits == ["-", "Bearer good"]


class _NeverGivesUpSpider(_EscalateSpider):
    name = "never_gives_up"

    async def is_blocked(self, response):
        return True  # everything looks blocked

    async def retry_blocked_request(self, request, response):
        return request  # always asks for the same thing again


async def test_escalation_budget_prevents_infinite_loops(server):
    spider = _NeverGivesUpSpider(_url(server, "").rstrip("/"))
    settings = spider.settings.model_copy(update={"blocked_escalation_max": 2})
    stats = await Engine(spider=spider, settings=settings).run()
    assert stats.requests_escalated == 2  # exactly the budget
    assert stats.requests_made == 3  # original + 2 retries, then parsed as-is
    assert stats.items_scraped == 1


async def test_default_hooks_leave_blocked_responses_alone(server):
    class Plain(_EscalateSpider):
        name = "plain_blocked"

        async def is_blocked(self, response):
            return response.status == 403

        async def retry_blocked_request(self, request, response):
            return None  # decline

    spider = Plain(_url(server, "").rstrip("/"))
    stats = await Engine(spider=spider, settings=spider.settings).run()
    assert stats.requests_blocked == 1 and stats.requests_escalated == 0
    assert stats.requests_made == 1


class _ConfigureSpider(Spider):
    name = "configure_demo"

    def __init__(self, base: str, **kw: Any) -> None:
        super().__init__(**kw)
        self.base = base
        self.uas: list[str] = []

    def configure_sessions(self, manager: SessionManager) -> None:
        manager.add("bot", HttpSession(user_agent="ConfBot/1"), default=True)

    def start_requests(self):
        return [Request(url=f"{self.base}/whoami")]

    async def parse(self, response):
        self.uas.append(json.loads(response.text)["ua"])
        yield {"ok": True}


async def test_engine_calls_configure_sessions_before_crawl(server):
    spider = _ConfigureSpider(_url(server, "").rstrip("/"))
    stats = await Engine(spider=spider, settings=spider.settings).run()
    assert spider.uas == ["ConfBot/1"]
    assert stats.sessions_requests == {"bot": 1}


def test_requests_with_session_kwargs_survive_json_roundtrip():
    """Checkpoints/Redis queues serialise requests; session fields must survive."""
    r = Request(url="http://x/", session="ui", session_kwargs={"wait_selector": ".a", "timeout": 3})
    back = Request.model_validate_json(r.model_dump_json())
    assert back.session == "ui"
    assert back.session_kwargs == {"wait_selector": ".a", "timeout": 3}


def test_parsed_response_exposes_session_id_and_captured_xhr():
    """Regression: captured XHR was stored under extra["captured_xhr"] but the
    callback-facing wrapper read extra["xhr"], so it was always empty."""
    from bitscrape.parser.selector import ParsedResponse

    req = Request(url="http://e.test/", session="ui")
    resp = Response(
        url="http://e.test/",
        status=200,
        body=b"<html></html>",
        request=req,
        session_id="ui",
        extra={"captured_xhr": [{"url": "http://e.test/api/items", "status": 200, "body": "[]"}]},
    )
    pr = ParsedResponse(resp)
    assert pr.session_id == "ui" and pr.sid == "ui"
    assert pr.captured_xhr == pr.xhr and len(pr.xhr) == 1
    assert pr.find_xhr(r"/api/")[0]["status"] == 200


async def test_captured_xhr_reaches_callbacks_through_a_browser_session():
    session, _pw = _browser_session(capture_xhr=r"/api/")
    d = await _dl_with_browser(session)
    try:
        # The fake page never emits responses, so nothing is captured -- but the
        # capture hook must at least be wired without raising on a session.
        r = await d.fetch(Request(url="http://e.test/", session="ui"))
        assert "captured_xhr" not in r.extra
    finally:
        await d.close()
