"""
Tests for the proxy-rotation upgrade: custom strategies on
:class:`~bitscrape.downloader.proxy.ProxyRotator`, and wiring it into the two
session types that previously only took a single static proxy --
:class:`~bitscrape.sessions.BrowserSession` and
:class:`~bitscrape.fetchers.http.AsyncFetcherSession` (and its sync/one-shot
siblings, which share the same code).

Four parts:
  * the core ``ProxyRotator`` (built-ins unchanged, custom strategies, errors);
  * ``BrowserSession.proxy_rotator`` plumbing (no real browser needed: a fake
    Playwright, same as ``test_multi_session.py``) and reporting (real browser,
    marked and skipped if none is available, same as ``test_dynamic_fetcher.py``);
  * ``AsyncFetcherSession``/``FetcherSession``/one-shot ``Fetcher`` proxy_rotator,
    against a real local server;
  * both HTTP and browser proxy_rotator end-to-end through a spider.
"""

from __future__ import annotations

import time
from typing import Any

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from bitscrape.core.models import Request
from bitscrape.core.settings import Settings
from bitscrape.core.spider import Spider
from bitscrape.downloader.downloader import Downloader
from bitscrape.downloader.proxy import (
    BUILTIN_STRATEGIES,
    VALID_STRATEGIES,
    ProxyRotator,
    ProxyState,
    ProxyStrategyError,
    _ProxyState,
    list_proxy_strategies,
    register_proxy_strategy,
    unregister_proxy_strategy,
)
from bitscrape.engine import Engine
from bitscrape.fetchers import AsyncFetcherSession, Fetcher, FetcherSession
from bitscrape.sessions import BrowserSession, SessionError, SessionManager
from tests.test_dynamic_fetcher import real_browser  # noqa: F401 -- reused as a fixture
from tests.test_fetchers import _ThreadedServer
from tests.test_multi_session import _browser_session, _dl_with_browser

# ---------------------------------------------------------------------------
# Part 1: ProxyRotator core
# ---------------------------------------------------------------------------


def test_backward_compat_aliases():
    assert _ProxyState is ProxyState
    assert VALID_STRATEGIES == BUILTIN_STRATEGIES
    assert BUILTIN_STRATEGIES == ("random", "round_robin", "least_used", "sticky")


def test_built_in_round_robin_unchanged():
    r = ProxyRotator(["http://p1", "http://p2", "http://p3"], strategy="round_robin")
    assert [r.get_proxy() for _ in range(6)] == ["http://p1", "http://p2", "http://p3"] * 2


def test_built_in_least_used_unchanged():
    r = ProxyRotator(["http://p1", "http://p2"], strategy="least_used")
    r.get_proxy()  # p1 (first in insertion order on a tie)
    assert r.get_proxy() == "http://p2"
    assert r.get_proxy() in ("http://p1", "http://p2")


def test_built_in_sticky_unchanged():
    r = ProxyRotator(["http://p1", "http://p2"], strategy="sticky")
    first = r.get_proxy(domain="example.com")
    for _ in range(5):
        assert r.get_proxy(domain="example.com") == first
    assert r.get_proxy(domain="other.com") in ("http://p1", "http://p2")


def test_empty_pool_returns_none():
    assert ProxyRotator([]).get_proxy() is None


def test_ban_after_threshold_and_recovery(monkeypatch):
    clock = {"t": 0.0}
    monkeypatch.setattr(time, "monotonic", lambda: clock["t"])
    r = ProxyRotator(["http://p1"], ban_threshold=2, ban_seconds=10)
    r.report_failure("http://p1")
    assert r.get_proxy() == "http://p1"  # one failure: not banned yet
    r.report_failure("http://p1")
    assert r.get_proxy() is None  # two: banned
    clock["t"] = 11
    assert r.get_proxy() == "http://p1"  # cool-down elapsed


def test_report_success_resets_failure_streak():
    r = ProxyRotator(["http://p1"], ban_threshold=2)
    r.report_failure("http://p1")
    r.report_success("http://p1")
    r.report_failure("http://p1")
    assert r.get_proxy() == "http://p1"  # streak was reset; still only 1 since


# -- custom strategies -------------------------------------------------------


def test_custom_strategy_passed_directly():
    def shortest_queue(available: list[ProxyState], domain: str | None) -> ProxyState:
        return min(available, key=lambda p: p.uses)

    r = ProxyRotator(["http://p1", "http://p2"], strategy=shortest_queue)
    assert r.strategy == "shortest_queue"
    seen = [r.get_proxy() for _ in range(4)]
    assert seen == ["http://p1", "http://p2", "http://p1", "http://p2"]


def test_custom_strategy_receives_live_state():
    seen_uses: list[int] = []

    def spy(available: list[ProxyState], domain: str | None) -> ProxyState:
        chosen = available[0]
        seen_uses.append(chosen.uses)
        return chosen

    r = ProxyRotator(["http://p1"], strategy=spy)
    for _ in range(3):
        r.get_proxy()
    assert seen_uses == [0, 1, 2]  # .uses reflects the rotator's own bookkeeping


def test_custom_strategy_receives_the_domain():
    seen_domains: list[str | None] = []

    def spy(available: list[ProxyState], domain: str | None) -> ProxyState:
        seen_domains.append(domain)
        return available[0]

    r = ProxyRotator(["http://p1"], strategy=spy)
    r.get_proxy()
    r.get_proxy(domain="example.com")
    assert seen_domains == [None, "example.com"]


def test_custom_strategy_only_sees_non_banned_proxies():
    seen: list[list[str]] = []

    def spy(available: list[ProxyState], domain: str | None) -> ProxyState:
        seen.append([p.url for p in available])
        return available[0]

    r = ProxyRotator(["http://p1", "http://p2"], strategy=spy, ban_threshold=1)
    r.report_failure("http://p2")
    r.get_proxy()
    assert seen == [["http://p1"]]


def test_register_proxy_strategy_then_select_by_name():
    def my_strategy(available: list[ProxyState], domain: str | None) -> ProxyState:
        return available[-1]

    register_proxy_strategy("last_one", my_strategy)
    try:
        assert "last_one" in list_proxy_strategies()
        r = ProxyRotator(["http://p1", "http://p2"], strategy="last_one")
        assert r.strategy == "last_one"
        assert r.get_proxy() == "http://p2"
    finally:
        unregister_proxy_strategy("last_one")
    assert "last_one" not in list_proxy_strategies()


def test_register_proxy_strategy_rejects_builtin_names():
    def fn(available: list[ProxyState], domain: str | None) -> ProxyState:
        return available[0]

    for name in BUILTIN_STRATEGIES:
        with pytest.raises(ValueError, match="built-in"):
            register_proxy_strategy(name, fn)


def test_register_proxy_strategy_rejects_non_callables():
    with pytest.raises(TypeError, match="callable"):
        register_proxy_strategy("whatever", "not a function")  # type: ignore[arg-type]


def test_register_proxy_strategy_replaces_on_reregistration():
    register_proxy_strategy("swap", lambda available, domain: available[0])
    try:
        register_proxy_strategy("swap", lambda available, domain: available[-1])
        r = ProxyRotator(["http://p1", "http://p2"], strategy="swap")
        assert r.get_proxy() == "http://p2"
    finally:
        unregister_proxy_strategy("swap")


def test_unregister_unknown_name_is_a_no_op():
    unregister_proxy_strategy("totally-not-registered")  # must not raise


def test_unknown_strategy_name_lists_known_ones():
    with pytest.raises(ValueError, match=r"random.*round_robin.*least_used.*sticky"):
        ProxyRotator(["http://p1"], strategy="nonexistent")


def test_custom_strategy_returning_a_foreign_object_raises():
    def bad(available: list[ProxyState], domain: str | None) -> Any:
        return "not-a-proxy-state"

    r = ProxyRotator(["http://p1"], strategy=bad)
    with pytest.raises(ProxyStrategyError, match="must return one of"):
        r.get_proxy()


def test_custom_strategy_returning_a_proxystate_from_elsewhere_raises():
    """A strategy can't hand back a ProxyState from a different rotator / a
    fresh default-valued lookalike -- only one of the objects it was actually
    called with (checked by identity, not just value equality)."""
    foreign = ProxyState(url="http://p1")  # field-equal to the real one, but not it

    def bad(available: list[ProxyState], domain: str | None) -> ProxyState:
        return foreign

    r = ProxyRotator(["http://p1"], strategy=bad)
    with pytest.raises(ProxyStrategyError):
        r.get_proxy()


def test_custom_strategy_that_raises_is_wrapped():
    def boom(available: list[ProxyState], domain: str | None) -> ProxyState:
        raise KeyError("oops")

    r = ProxyRotator(["http://p1"], strategy=boom)
    with pytest.raises(ProxyStrategyError, match="KeyError") as exc:
        r.get_proxy()
    assert isinstance(exc.value.__cause__, KeyError)


def test_proxy_strategy_error_is_a_value_error():
    assert issubclass(ProxyStrategyError, ValueError)


def test_from_file_accepts_a_custom_strategy(tmp_path):
    path = tmp_path / "proxies.txt"
    path.write_text("http://p1\nhttp://p2\n")
    r = ProxyRotator.from_file(path, strategy=lambda available, domain: available[0])
    assert r.get_proxy() == "http://p1"


# ---------------------------------------------------------------------------
# Part 2: BrowserSession.proxy_rotator
# ---------------------------------------------------------------------------


def test_browser_session_rejects_proxy_and_rotator_together():
    rotator = ProxyRotator(["http://p1"])
    with pytest.raises(SessionError, match="proxy_rotator"):
        BrowserSession(proxy="http://static:1", proxy_rotator=rotator)


async def test_browser_session_draws_once_and_memoizes():
    rotator = ProxyRotator(["http://p1:1", "http://p2:1"], strategy="round_robin")
    session, _pw = _browser_session(proxy_rotator=rotator)
    assert session.resolved_proxy is None
    settings = Settings()
    cfg1 = session.launch_config(settings)
    assert session.resolved_proxy == "http://p1:1"
    assert cfg1.proxy == "http://p1:1"
    cfg2 = session.launch_config(settings)  # called again: must not redraw
    assert cfg2.proxy == "http://p1:1"


async def test_separate_browser_sessions_each_get_a_turn():
    rotator = ProxyRotator(["http://p1:1", "http://p2:1"], strategy="round_robin")
    s1, _ = _browser_session(proxy_rotator=rotator)
    s2, _ = _browser_session(proxy_rotator=rotator)
    settings = Settings()
    assert s1.launch_config(settings).proxy == "http://p1:1"
    assert s2.launch_config(settings).proxy == "http://p2:1"


async def test_browser_session_proxy_rotator_actually_launches_with_it():
    rotator = ProxyRotator(["http://p1:1"])
    session, pw = _browser_session(proxy_rotator=rotator)
    d = await _dl_with_browser(session, lazy=False)
    try:
        assert pw.chromium.launches[0]["proxy"] == {"server": "http://p1:1"}
    finally:
        await d.close()


# -- against a real browser --------------------------------------------------


class _Site:
    def __init__(self) -> None:
        self.hits = 0


def _static_app(st: _Site) -> web.Application:
    async def page(request: web.Request) -> web.Response:
        st.hits += 1
        return web.Response(text="<html><body><h1>ok</h1></body></html>", content_type="text/html")

    app = web.Application()
    app.router.add_get("/", page)
    return app


async def test_browser_session_reports_failure_to_the_rotator(real_browser):  # noqa: F811
    """No real proxy needed: a proxy pointing at a port nothing listens on
    makes every navigation fail, which must be reported to the rotator."""
    rotator = ProxyRotator(["http://127.0.0.1:1"], ban_threshold=2, ban_seconds=30)

    def configure(m: SessionManager) -> None:
        m.add("b", BrowserSession(proxy_rotator=rotator, timeout=5))

    d = Downloader(Settings())
    await d.open(configure=configure)
    try:
        for _ in range(2):
            with pytest.raises(Exception):  # noqa: B017, PT011 -- DownloadError, wrapping the nav failure
                await d.fetch(Request(url="http://example.test/", session="b", dont_filter=True))
    finally:
        await d.close()
    assert rotator.get_proxy() is None  # banned after 2 consecutive failures


async def test_browser_session_reports_success_to_the_rotator(real_browser):  # noqa: F811
    """The browser itself launches with no proxy at all (a fake proxy URL would
    just make Chromium fail to connect, which isn't what this test is about);
    the session's proxy_rotator/resolved_proxy are set only *after* it has
    already started, so the running browser is unaffected and this isolates
    exactly what's being verified: that a successful fetch through the session
    reports success back to the rotator, using its resolved_proxy."""
    st = _Site()
    srv = _ThreadedServer(_static_app(st))
    srv.start()
    try:
        session = BrowserSession()

        def configure(m: SessionManager) -> None:
            m.add("b", session)

        d = Downloader(Settings())
        await d.open(configure=configure)
        try:
            assert d.sessions is not None
            await d.sessions.start_session("b")  # launches now, with no proxy

            rotator = ProxyRotator(["http://p1:1"])
            calls: list[tuple[str, str]] = []
            rotator.report_success = lambda p: calls.append(("success", p))  # type: ignore[method-assign]
            rotator.report_failure = lambda p: calls.append(("failure", p))  # type: ignore[method-assign]
            session.proxy_rotator = rotator
            session.resolved_proxy = "http://p1:1"

            r = await d.fetch(Request(url=srv.base, session="b"))
            assert r.status == 200
        finally:
            await d.close()
        assert calls == [("success", "http://p1:1")]
    finally:
        srv.stop()


# ---------------------------------------------------------------------------
# Part 3: AsyncFetcherSession / FetcherSession / Fetcher proxy_rotator
# ---------------------------------------------------------------------------


@pytest.fixture
def intercept_proxy():
    """Records the ``proxy=`` aiohttp was actually asked to use for each
    request, then drops it before the real call -- so a test can assert which
    (fake, non-resolving) proxy a rotator chose while the request still
    actually reaches the local test server. Works for *every* bitscrape code
    path that ends up at plain aiohttp (``HttpSession``/``Downloader`` and
    ``AsyncFetcherSession``'s ``_AiohttpBackend`` alike), since both ultimately
    call ``aiohttp.ClientSession.request``.
    """
    import aiohttp

    calls: list[str | None] = []
    original = aiohttp.ClientSession.request

    def patched(self: aiohttp.ClientSession, method: str, url: Any, **kwargs: Any) -> Any:
        calls.append(kwargs.pop("proxy", None))
        return original(self, method, url, **kwargs)

    aiohttp.ClientSession.request = patched  # type: ignore[method-assign]
    try:
        yield calls
    finally:
        aiohttp.ClientSession.request = original  # type: ignore[method-assign]


@pytest.fixture
async def proxy_echo_server(intercept_proxy):
    async def echo(request: web.Request) -> web.Response:
        return web.json_response({"ok": True})

    app = web.Application()
    app.router.add_get("/echo", echo)
    server = TestServer(app)
    await server.start_server()
    server.calls = intercept_proxy  # type: ignore[attr-defined]
    server.base = str(server.make_url("/")).rstrip("/")  # type: ignore[attr-defined]
    try:
        yield server
    finally:
        await server.close()


def test_async_fetcher_session_rejects_proxy_and_rotator_together():
    rotator = ProxyRotator(["http://p1"])
    with pytest.raises(ValueError, match="proxy_rotator"):
        AsyncFetcherSession(proxy="http://static:1", proxy_rotator=rotator)


async def test_http1_draws_a_fresh_proxy_per_request(proxy_echo_server):
    rotator = ProxyRotator(["http://p1:1", "http://p2:1"], strategy="round_robin")
    async with AsyncFetcherSession(proxy_rotator=rotator) as s:
        for _ in range(4):
            await s.get(proxy_echo_server.base + "/echo")
    assert proxy_echo_server.calls == ["http://p1:1", "http://p2:1", "http://p1:1", "http://p2:1"]


async def test_no_rotator_means_no_proxy_override_per_call(proxy_echo_server):
    async with AsyncFetcherSession() as s:
        await s.get(proxy_echo_server.base + "/echo")
    assert proxy_echo_server.calls == [None]


async def test_http2_resolves_one_proxy_at_construction():
    pytest.importorskip("httpx")
    pytest.importorskip("h2")
    rotator = ProxyRotator(["http://p1:1", "http://p2:1"], strategy="round_robin")
    s = AsyncFetcherSession(protocol="http2", proxy_rotator=rotator)
    try:
        assert s.resolved_proxy == "http://p1:1"
        s2 = AsyncFetcherSession(protocol="http2", proxy_rotator=rotator)
        try:
            assert s2.resolved_proxy == "http://p2:1"  # a second session gets the next turn
        finally:
            await s2.close()
    finally:
        await s.close()


def test_fetcher_session_kwarg_passthrough_reaches_the_inner_session():
    """FetcherSession(**kwargs) is a plain passthrough to AsyncFetcherSession;
    the actual per-request draw/rotate/report behaviour is covered end-to-end
    (via the async class) in test_http1_draws_a_fresh_proxy_per_request above."""
    rotator = ProxyRotator(["http://p1:1"])
    with FetcherSession(proxy_rotator=rotator) as s:
        assert s._inner.proxy_rotator is rotator


# -- reporting success/failure and bans --------------------------------------


@pytest.fixture
async def flaky_server(intercept_proxy):
    st = {"fail_left": 0, "hits": 0}

    async def flaky(request: web.Request) -> web.Response:
        st["hits"] += 1
        if st["fail_left"] > 0:
            st["fail_left"] -= 1
            return web.Response(status=503)
        return web.Response(text="ok")

    app = web.Application()
    app.router.add_get("/flaky", flaky)
    app.router.add_post("/flaky", flaky)
    server = TestServer(app)
    await server.start_server()
    server.st = st  # type: ignore[attr-defined]
    server.base = str(server.make_url("/")).rstrip("/")  # type: ignore[attr-defined]
    try:
        yield server
    finally:
        await server.close()


async def test_repeated_bad_status_bans_the_proxy(flaky_server):
    flaky_server.st["fail_left"] = 99
    rotator = ProxyRotator(["http://p1:1"], ban_threshold=2, ban_seconds=30)
    async with AsyncFetcherSession(proxy_rotator=rotator, max_retries=3, retry_backoff=0.01) as s:
        r = await s.get(flaky_server.base + "/flaky")
    assert r.status == 503
    assert rotator.get_proxy() is None  # banned


async def test_recovering_proxy_is_not_banned(flaky_server):
    flaky_server.st["fail_left"] = 1  # one bad response, then it recovers
    rotator = ProxyRotator(["http://p1:1"], ban_threshold=2, ban_seconds=30)
    async with AsyncFetcherSession(proxy_rotator=rotator, max_retries=3, retry_backoff=0.01) as s:
        r = await s.get(flaky_server.base + "/flaky")
    assert r.status == 200
    assert rotator.get_proxy() == "http://p1:1"  # still available: streak reset on success


async def test_connection_failure_is_reported_too():
    rotator = ProxyRotator(["http://p1:1"], ban_threshold=1)
    async with AsyncFetcherSession(proxy_rotator=rotator, max_retries=0) as s:
        with pytest.raises(Exception):  # noqa: B017, PT011 -- FetchError
            await s.get("http://127.0.0.1:1/")
    assert rotator.get_proxy() is None  # one failure was enough (ban_threshold=1)


async def test_only_retry_eligible_methods_still_report_once(flaky_server):
    """A POST isn't retried by default, but the single attempt it does make
    must still report its outcome to the rotator."""
    flaky_server.st["fail_left"] = 99
    rotator = ProxyRotator(["http://p1:1"], ban_threshold=1)
    async with AsyncFetcherSession(proxy_rotator=rotator) as s:
        r = await s.post(flaky_server.base + "/flaky")
    assert r.status == 503
    assert rotator.get_proxy() is None


def test_one_shot_fetcher_routes_proxy_rotator_to_the_session_constructor():
    """`proxy_rotator` is a client-level option (like `proxy`), so the one-shot
    `Fetcher.get()` facade must route it to the AsyncFetcherSession it builds
    per call, not treat it as a per-request kwarg (which used to raise
    TypeError: unexpected keyword argument -- see _CLIENT_KEYS)."""
    rotator = ProxyRotator(["http://p1:1"])
    seen: dict[str, Any] = {}
    original_init = AsyncFetcherSession.__init__

    def spy_init(self: AsyncFetcherSession, *a: Any, **kw: Any) -> None:
        seen.update(kw)
        original_init(self, *a, **kw)

    AsyncFetcherSession.__init__ = spy_init  # type: ignore[method-assign]
    try:
        with pytest.raises(Exception):  # noqa: B017, PT011 -- it will try (and fail) to actually connect
            Fetcher.get("http://127.0.0.1:1/", proxy_rotator=rotator, max_retries=0)
    finally:
        AsyncFetcherSession.__init__ = original_init  # type: ignore[method-assign]
    assert seen.get("proxy_rotator") is rotator  # routed to the session, not request()


# ---------------------------------------------------------------------------
# Part 4: end-to-end through a spider (HTTP session)
# ---------------------------------------------------------------------------


class _RotatorSpider(Spider):
    name = "rotator_demo"

    def __init__(self, base: str, rotator: ProxyRotator, **kw: Any) -> None:
        super().__init__(**kw)
        self.base = base
        self.rotator = rotator

    def configure_sessions(self, manager: SessionManager) -> None:
        from bitscrape.sessions import HttpSession

        manager.add("api", HttpSession(proxy_rotator=self.rotator), default=True)

    def start_requests(self):
        return [Request(url=f"{self.base}/echo", dont_filter=True) for _ in range(4)]

    async def parse(self, response):
        yield {"ok": True}


async def test_spider_http_session_rotates_proxies_across_requests(proxy_echo_server):
    rotator = ProxyRotator(["http://p1:1", "http://p2:1"], strategy="round_robin")
    spider = _RotatorSpider(proxy_echo_server.base, rotator)
    stats = await Engine(spider=spider, settings=spider.settings).run()
    assert stats.items_scraped == 4
    assert sorted(proxy_echo_server.calls) == ["http://p1:1", "http://p1:1", "http://p2:1", "http://p2:1"]
