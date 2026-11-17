"""
Tests for ``bitscrape.fetchers`` (``Fetcher``, ``AsyncFetcher``,
``FetcherSession``, ``AsyncFetcherSession``), plus the two small changes that
shipped with them: ``Settings.default_headers`` and ``Response.http_version``.

HTTP/1.1 runs against a real local aiohttp server. **HTTP/2 is verified for
real, not mocked**: a local hypercorn server terminates TLS with a throwaway
CA (``trustme``) and advertises ``h2`` via ALPN, so ``response.http_version ==
"2"`` proves the protocol was actually negotiated end to end. Both servers run
in a background thread with their own event loop, so the *synchronous*
``Fetcher`` (which can't run inside a running loop, by design) can call them.
"""

from __future__ import annotations

import asyncio
import json
import socket
import ssl
import threading
import time
from collections import defaultdict
from typing import Any

import pytest
from aiohttp import web

from bitscrape.core.models import Request
from bitscrape.core.settings import Settings
from bitscrape.downloader.downloader import Downloader
from bitscrape.fetchers import (
    AsyncFetcher,
    AsyncFetcherSession,
    Fetcher,
    FetchError,
    FetcherSession,
)
from bitscrape.parser.selector import ParsedResponse
from bitscrape.sessions import HttpSession, SessionManager

DEFAULT_ACCEPT = "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"

# ---------------------------------------------------------------------------
# A local HTTP/1.1 server, run in a background thread
# ---------------------------------------------------------------------------


class _State:
    def __init__(self) -> None:
        self.hits: dict[str, int] = defaultdict(int)
        self.flaky_left = 0
        self.rate_left = 0


def _make_app(state: _State) -> web.Application:
    async def echo(request: web.Request) -> web.Response:
        body = await request.read()
        return web.json_response(
            {
                "method": request.method,
                "path": request.path_qs,
                "query": [[k, v] for k, v in request.query.items()],
                "headers": {k.lower(): v for k, v in request.headers.items()},
                "body": body.decode("utf-8", "replace"),
                "cookies": dict(request.cookies),
            }
        )

    async def set_cookie(request: web.Request) -> web.Response:
        resp = web.Response(text="ok")
        resp.set_cookie(request.match_info["name"], request.match_info["value"])
        return resp

    async def redirect(request: web.Request) -> web.Response:
        raise web.HTTPFound("/echo?redirected=1")

    async def loop(request: web.Request) -> web.Response:
        state.hits["/loop"] += 1
        raise web.HTTPFound("/loop")

    async def status(request: web.Request) -> web.Response:
        return web.Response(status=int(request.match_info["code"]), text="status")

    async def flaky(request: web.Request) -> web.Response:
        state.hits[request.path] += 1
        if state.flaky_left > 0:
            state.flaky_left -= 1
            return web.Response(status=503, text="busy")
        return web.Response(text="ok")

    async def rate(request: web.Request) -> web.Response:
        state.hits["/rate"] += 1
        if state.rate_left > 0:
            state.rate_left -= 1
            return web.Response(status=429, headers={"Retry-After": "0.25"}, text="slow down")
        return web.Response(text="ok")

    async def slow(request: web.Request) -> web.Response:
        state.hits["/slow"] += 1
        await asyncio.sleep(2)
        return web.Response(text="late")

    async def html(request: web.Request) -> web.Response:
        return web.Response(
            text="<html><body><h1>Hello</h1><a href='/x'>x</a></body></html>", content_type="text/html"
        )

    async def gz(request: web.Request) -> web.Response:
        resp = web.Response(text="compressed " * 200)
        resp.enable_compression()
        return resp

    app = web.Application()
    app.router.add_route("*", "/echo", echo)
    app.router.add_get("/set-cookie/{name}/{value}", set_cookie)
    app.router.add_get("/redirect", redirect)
    app.router.add_get("/loop", loop)
    app.router.add_get("/status/{code}", status)
    app.router.add_route("*", "/flaky", flaky)
    app.router.add_route("*", "/flaky-post", flaky)
    app.router.add_get("/rate", rate)
    app.router.add_get("/slow", slow)
    app.router.add_get("/html", html)
    app.router.add_get("/gz", gz)
    return app


class _ThreadedServer:
    """Runs an aiohttp app in its own thread + event loop and exposes its base URL."""

    def __init__(self, app: web.Application) -> None:
        self._app = app
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._loop.run_forever, daemon=True)
        self._runner: web.AppRunner | None = None
        self.base = ""

    def start(self) -> None:
        self._thread.start()

        async def _up() -> str:
            self._runner = web.AppRunner(self._app)
            await self._runner.setup()
            site = web.TCPSite(self._runner, "127.0.0.1", 0)
            await site.start()
            port = site._server.sockets[0].getsockname()[1]  # type: ignore[union-attr]
            return f"http://127.0.0.1:{port}"

        self.base = asyncio.run_coroutine_threadsafe(_up(), self._loop).result(10)

    def stop(self) -> None:
        if self._runner is not None:
            asyncio.run_coroutine_threadsafe(self._runner.cleanup(), self._loop).result(10)
        self._loop.call_soon_threadsafe(self._loop.stop)
        self._thread.join(5)


@pytest.fixture
def server():
    state = _State()
    srv = _ThreadedServer(_make_app(state))
    srv.start()
    srv.state = state  # type: ignore[attr-defined]
    try:
        yield srv
    finally:
        srv.stop()


def _fast(**kw: Any) -> dict[str, Any]:
    """Retry options that keep tests quick."""
    return {"retry_backoff": 0.01, **kw}


# ---------------------------------------------------------------------------
# Basics: methods, response object
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("method", ["get", "post", "put", "patch", "delete", "options"])
def test_fetcher_http_methods(server, method):
    r = getattr(Fetcher, method)(server.base + "/echo")
    assert r.status == 200
    assert r.json()["method"] == method.upper()


def test_fetcher_head_has_no_body(server):
    r = Fetcher.head(server.base + "/echo")
    assert r.status == 200 and r.body == b""


def test_fetcher_request_generic(server):
    assert Fetcher.request("post", server.base + "/echo").json()["method"] == "POST"


def test_response_is_a_parsed_response_with_protocol_and_timing(server):
    r = Fetcher.get(server.base + "/html")
    assert isinstance(r, ParsedResponse)
    assert r.css("h1::text").get() == "Hello"
    assert r.http_version == "1.1"
    assert r.ok and r.status == 200
    assert r.response.elapsed_ms > 0
    assert r.url == server.base + "/html"


def test_gzip_responses_are_decoded(server):
    r = Fetcher.get(server.base + "/gz")
    assert r.text.startswith("compressed compressed")


def test_request_model_records_what_was_sent(server):
    r = Fetcher.post(server.base + "/echo", json={"a": 1}, params={"p": "1"})
    assert r.request.method == "POST"
    assert r.request.url.endswith("/echo?p=1")
    assert json.loads(r.request.body or b"") == {"a": 1}


# ---------------------------------------------------------------------------
# Params, bodies, auth
# ---------------------------------------------------------------------------


def test_params_are_appended_to_the_existing_query(server):
    r = Fetcher.get(server.base + "/echo?a=1", params={"b": [2, 3], "c": "x y"})
    assert r.json()["query"] == [["a", "1"], ["b", "2"], ["b", "3"], ["c", "x y"]]


def test_json_body_sets_content_type(server):
    body = Fetcher.post(server.base + "/echo", json={"q": "widgets", "n": 3}).json()
    assert body["headers"]["content-type"] == "application/json"
    assert json.loads(body["body"]) == {"q": "widgets", "n": 3}


def test_json_body_keeps_a_content_type_you_set(server):
    body = Fetcher.post(
        server.base + "/echo", json={"a": 1}, headers={"content-type": "application/vnd.api+json"}
    ).json()
    assert body["headers"]["content-type"] == "application/vnd.api+json"


def test_form_dict_is_urlencoded(server):
    body = Fetcher.post(server.base + "/echo", data={"user": "me", "tag": ["a", "b"]}).json()
    assert body["headers"]["content-type"] == "application/x-www-form-urlencoded"
    assert body["body"] == "user=me&tag=a&tag=b"


def test_str_and_bytes_bodies_pass_through(server):
    assert Fetcher.post(server.base + "/echo", data="héllo").json()["body"] == "héllo"
    assert Fetcher.post(server.base + "/echo", data=b"raw-bytes").json()["body"] == "raw-bytes"


def test_data_and_json_together_is_an_error(server):
    with pytest.raises(ValueError, match="either"):
        Fetcher.post(server.base + "/echo", data={"a": 1}, json={"b": 2})


def test_basic_auth_header(server):
    body = Fetcher.get(server.base + "/echo", auth=("user", "pa:ss")).json()
    assert body["headers"]["authorization"] == "Basic dXNlcjpwYTpzcw=="


def test_explicit_authorization_header_beats_auth(server):
    body = Fetcher.get(
        server.base + "/echo", auth=("u", "p"), headers={"Authorization": "Bearer t"}
    ).json()
    assert body["headers"]["authorization"] == "Bearer t"


# ---------------------------------------------------------------------------
# Headers
# ---------------------------------------------------------------------------


def test_default_headers_and_user_agent_are_sent(server):
    h = Fetcher.get(server.base + "/echo").json()["headers"]
    assert h["accept"] == DEFAULT_ACCEPT
    assert h["accept-language"] == "en-US,en;q=0.9"
    assert h["user-agent"].startswith("BitscrapeBot/")


def test_settings_default_headers_can_be_replaced_or_cleared(server):
    custom = Settings(default_headers={"X-Team": "data", "Accept-Language": "de"})
    h = Fetcher.get(server.base + "/echo", settings=custom).json()["headers"]
    assert h["x-team"] == "data" and h["accept-language"] == "de"
    cleared = Settings(default_headers={})
    h = Fetcher.get(server.base + "/echo", settings=cleared).json()["headers"]
    assert "accept-language" not in h and "x-team" not in h


def test_user_agent_override(server):
    h = Fetcher.get(server.base + "/echo", user_agent="MyTool/2").json()["headers"]
    assert h["user-agent"] == "MyTool/2"


def test_per_request_headers_override_session_and_defaults(server):
    with FetcherSession(headers={"X-Tier": "session", "Accept": "application/json"}) as s:
        base = s.get(server.base + "/echo").json()["headers"]
        over = s.get(server.base + "/echo", headers={"X-Tier": "request"}).json()["headers"]
    assert base["x-tier"] == "session" and base["accept"] == "application/json"
    assert over["x-tier"] == "request"
    assert over["accept"] == "application/json"  # the per-request header didn't drop the session's


# ---------------------------------------------------------------------------
# Cookies and sessions
# ---------------------------------------------------------------------------


def test_session_persists_cookies_across_requests(server):
    with FetcherSession() as s:
        s.get(server.base + "/set-cookie/sid/abc")
        assert s.get(server.base + "/echo").json()["cookies"] == {"sid": "abc"}


def test_one_off_fetcher_does_not_share_cookies_between_calls(server):
    Fetcher.get(server.base + "/set-cookie/sid/abc")
    assert Fetcher.get(server.base + "/echo").json()["cookies"] == {}


def test_per_request_cookies_are_added_to_the_session_cookies(server):
    with FetcherSession() as s:
        s.get(server.base + "/set-cookie/sid/abc")
        merged = s.get(server.base + "/echo", cookies={"x": "1"}).json()["cookies"]
        after = s.get(server.base + "/echo").json()["cookies"]
    assert merged == {"sid": "abc", "x": "1"}
    assert after == {"sid": "abc"}  # the per-request cookie didn't stick


def test_cookies_accessor_returns_everything_in_the_jar(server):
    with FetcherSession() as s:
        s.get(server.base + "/set-cookie/sid/abc")
        assert s.cookies() == {"sid": "abc"}
        assert s.cookies(server.base) == {"sid": "abc"}  # URL-scoped filter, same jar


def test_seeding_cookies_at_construction(server):
    with FetcherSession(cookies={"seed": "x"}) as s:
        assert s.get(server.base + "/echo").json()["cookies"] == {"seed": "x"}
        assert s.cookies() == {"seed": "x"}


def test_set_cookies_adds_without_a_set_cookie_header(server):
    """E.g. a login API that returns a token in its JSON body, not a Set-Cookie header."""
    with FetcherSession() as s:
        s.set_cookies({"token": "abc123"})
        assert s.get(server.base + "/echo").json()["cookies"] == {"token": "abc123"}


def test_clear_cookies_logs_out(server):
    with FetcherSession() as s:
        s.get(server.base + "/set-cookie/sid/abc")
        assert s.cookies() == {"sid": "abc"}
        s.clear_cookies()
        assert s.cookies() == {}
        assert s.get(server.base + "/echo").json()["cookies"] == {}


def test_save_and_load_cookies_round_trip_across_sessions(server, tmp_path):
    path = tmp_path / "cookies.json"
    with FetcherSession() as s:
        s.get(server.base + "/set-cookie/sid/abc")
        s.set_cookies({"extra": "y"})
        s.save_cookies(path)
    assert json.loads(path.read_text()) == {"sid": "abc", "extra": "y"}

    with FetcherSession() as s2:  # a fresh session, later: this is the point of saving them
        assert s2.cookies() == {}
        s2.load_cookies(path)
        assert s2.get(server.base + "/echo").json()["cookies"] == {"sid": "abc", "extra": "y"}


def test_load_cookies_adds_to_what_the_session_already_has(server, tmp_path):
    path = tmp_path / "cookies.json"
    path.write_text(json.dumps({"from_file": "1"}))
    with FetcherSession(cookies={"already_here": "2"}) as s:
        s.load_cookies(path)
        assert s.cookies() == {"already_here": "2", "from_file": "1"}


def test_accepts_str_and_pathlib_path(server, tmp_path):
    path = tmp_path / "cookies.json"
    with FetcherSession() as s:
        s.set_cookies({"a": "1"})
        s.save_cookies(str(path))  # str, not Path
    with FetcherSession() as s2:
        s2.load_cookies(path)  # Path, not str
        assert s2.cookies() == {"a": "1"}


async def test_async_session_cookie_methods(server):
    async with AsyncFetcherSession(cookies={"seed": "x"}) as s:
        await s.get(server.base + "/set-cookie/sid/abc")
        assert await s.cookies() == {"seed": "x", "sid": "abc"}
        await s.set_cookies({"extra": "y"})
        await s.clear_cookies()
        assert await s.cookies() == {}


async def test_http2_session_cookies_and_persistence(server, tmp_path):
    pytest.importorskip("httpx")
    pytest.importorskip("h2")
    path = tmp_path / "c.json"
    async with AsyncFetcherSession(protocol="http2", cookies={"seed": "x"}) as s:
        await s.get(server.base + "/set-cookie/sid/abc")
        assert await s.cookies() == {"seed": "x", "sid": "abc"}
        await s.save_cookies(path)
    async with AsyncFetcherSession(protocol="http2") as s2:
        await s2.load_cookies(path)
        assert await s2.cookies() == {"seed": "x", "sid": "abc"}


def test_one_shot_fetcher_has_no_cookie_jar_to_persist():
    """Fetcher/AsyncFetcher are one-shot by design (see module docstring): cookie
    management lives on the session classes, which is the point of having both."""
    assert not hasattr(Fetcher, "cookies")
    assert not hasattr(Fetcher, "save_cookies")


def test_session_close_is_idempotent_and_final(server):
    s = FetcherSession()
    assert s.get(server.base + "/echo").status == 200
    s.close()
    s.close()
    with pytest.raises(RuntimeError, match="closed"):
        s.get(server.base + "/echo")


def test_session_context_exit_closes_its_loop(server):
    with FetcherSession() as s:
        s.get(server.base + "/echo")
    assert s._loop is None


# ---------------------------------------------------------------------------
# Redirects
# ---------------------------------------------------------------------------


def test_redirects_are_followed_by_default(server):
    r = Fetcher.get(server.base + "/redirect")
    assert r.url.endswith("/echo?redirected=1")
    assert r.json()["query"] == [["redirected", "1"]]


def test_follow_redirects_false_returns_the_redirect_itself(server):
    r = Fetcher.get(server.base + "/redirect", follow_redirects=False)
    assert r.status == 302
    assert r.headers["Location"] == "/echo?redirected=1"


def test_redirect_loop_raises_without_retrying(server):
    with pytest.raises(FetchError, match="redirects") as exc:
        Fetcher.get(server.base + "/loop", max_redirects=3, **_fast())
    assert exc.value.retryable is False
    assert server.state.hits["/loop"] <= 5  # one attempt (~max_redirects+1 hops), not 3x that


# ---------------------------------------------------------------------------
# Failures and retries
# ---------------------------------------------------------------------------


def test_connection_refused_raises_fetch_error():
    with pytest.raises(FetchError, match="127.0.0.1:1") as exc:
        Fetcher.get("http://127.0.0.1:1/", max_retries=0)
    assert exc.value.url == "http://127.0.0.1:1/"
    assert isinstance(exc.value.__cause__, Exception)


def test_invalid_url_is_not_retried():
    with pytest.raises(FetchError) as exc:
        Fetcher.get("not a url", max_retries=3, **_fast())
    assert exc.value.retryable is False


def test_timeout_raises_and_is_retried_for_idempotent_methods(server):
    with pytest.raises(FetchError, match="imeout"):
        Fetcher.get(server.base + "/slow", timeout=0.1, max_retries=2, **_fast())
    assert server.state.hits["/slow"] == 3  # the original attempt + 2 retries


def test_503_is_retried_until_it_succeeds(server):
    server.state.flaky_left = 2
    r = Fetcher.get(server.base + "/flaky", max_retries=2, **_fast())
    assert r.status == 200
    assert server.state.hits["/flaky"] == 3


def test_when_retries_run_out_on_a_status_the_last_response_is_returned(server):
    server.state.flaky_left = 99
    r = Fetcher.get(server.base + "/flaky", max_retries=1, **_fast())
    assert r.status == 503 and not r.ok
    assert server.state.hits["/flaky"] == 2


def test_max_retries_zero_means_one_attempt(server):
    server.state.flaky_left = 99
    assert Fetcher.get(server.base + "/flaky", max_retries=0).status == 503
    assert server.state.hits["/flaky"] == 1


def test_post_is_not_retried_by_default(server):
    """A retried POST can repeat its side effects, so only idempotent methods retry."""
    server.state.flaky_left = 99
    r = Fetcher.post(server.base + "/flaky-post", max_retries=3, **_fast())
    assert r.status == 503
    assert server.state.hits["/flaky-post"] == 1


def test_retry_methods_can_opt_a_post_in(server):
    server.state.flaky_left = 1
    r = Fetcher.post(server.base + "/flaky-post", max_retries=2, retry_methods={"POST"}, **_fast())
    assert r.status == 200
    assert server.state.hits["/flaky-post"] == 2


def test_only_listed_statuses_are_retried(server):
    r = Fetcher.get(server.base + "/status/404", max_retries=3, **_fast())
    assert r.status == 404  # returned straight away, no retries


def test_retry_statuses_can_be_customised(server):
    r = Fetcher.get(server.base + "/status/418", max_retries=1, retry_statuses={418}, **_fast())
    assert r.status == 418  # retried once (still 418), then returned


def test_retry_after_is_honoured(server):
    server.state.rate_left = 1
    t0 = time.monotonic()
    r = Fetcher.get(server.base + "/rate", max_retries=1, retry_backoff=0.001)
    assert r.status == 200
    assert time.monotonic() - t0 >= 0.2  # waited what the server asked (0.25s), not the 1ms backoff


def test_retry_after_is_ignored_when_respect_retry_after_is_off(server):
    server.state.rate_left = 1
    t0 = time.monotonic()
    r = Fetcher.get(
        server.base + "/rate",
        max_retries=1,
        retry_backoff=0.001,
        settings=Settings(respect_retry_after=False),
    )
    assert r.status == 200
    assert time.monotonic() - t0 < 0.2


# ---------------------------------------------------------------------------
# Option validation, and the deliberate non-goals
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("option", ["impersonate", "stealthy_headers", "http3", "stealth", "fingerprint"])
def test_evasion_options_are_refused_with_an_explanation(server, option):
    with pytest.raises(NotImplementedError, match="Explicit non-goals"):
        Fetcher.get(server.base + "/echo", **{option: "chrome"})
    with pytest.raises(NotImplementedError, match="Explicit non-goals"):
        AsyncFetcherSession(**{option: "chrome"})


def test_evasion_options_are_refused_on_a_session_request_too(server):
    with FetcherSession() as s, pytest.raises(NotImplementedError, match="Explicit non-goals"):
        s.get(server.base + "/echo", impersonate="chrome")


async def test_async_fetcher_refuses_evasion_options_too():
    with pytest.raises(NotImplementedError, match="Explicit non-goals"):
        await AsyncFetcher.get("http://127.0.0.1:1/", impersonate="chrome")


def test_unknown_keyword_is_a_type_error(server):
    with pytest.raises(TypeError, match="bogus"):
        Fetcher.get(server.base + "/echo", bogus=1)


def test_invalid_protocol_is_a_value_error():
    with pytest.raises(ValueError, match="'http1' or 'http2'"):
        AsyncFetcherSession(protocol="spdy")  # type: ignore[arg-type]


def test_http3_is_explicitly_not_supported():
    with pytest.raises(ValueError, match="HTTP/3"):
        AsyncFetcherSession(protocol="http3")  # type: ignore[arg-type]


def test_negative_max_retries_is_rejected():
    with pytest.raises(ValueError, match="max_retries"):
        AsyncFetcherSession(max_retries=-1)


# ---------------------------------------------------------------------------
# Async API, and the "sync inside a running loop" guard
# ---------------------------------------------------------------------------


async def test_async_fetcher_and_session(server):
    r = await AsyncFetcher.get(server.base + "/echo", params={"a": 1})
    assert r.json()["query"] == [["a", "1"]]
    async with AsyncFetcherSession() as s:
        await s.get(server.base + "/set-cookie/sid/xyz")
        assert (await s.get(server.base + "/echo")).json()["cookies"] == {"sid": "xyz"}
        results = await asyncio.gather(*[s.get(server.base + "/echo") for _ in range(8)])
    assert [x.status for x in results] == [200] * 8


async def test_async_session_close_is_final(server):
    s = AsyncFetcherSession()
    await s.get(server.base + "/echo")
    await s.close()
    await s.close()
    with pytest.raises(RuntimeError, match="closed"):
        await s.get(server.base + "/echo")


async def test_sync_fetcher_inside_a_running_loop_explains_itself(server):
    with pytest.raises(RuntimeError, match="AsyncFetcher"):
        Fetcher.get(server.base + "/echo")
    with pytest.raises(RuntimeError, match="AsyncFetcher"):
        FetcherSession()


# ---------------------------------------------------------------------------
# default_headers / http_version through the spider Downloader and sessions
# ---------------------------------------------------------------------------


async def _downloader(settings: Settings | None = None, configure: Any = None) -> Downloader:
    d = Downloader(settings or Settings())
    await d.open(configure=configure)
    return d


async def test_downloader_sends_default_headers_and_reports_http_version(server):
    d = await _downloader()
    try:
        r = await d.fetch(Request(url=server.base + "/echo"))
    finally:
        await d.close()
    h = json.loads(r.text)["headers"]
    assert h["accept"] == DEFAULT_ACCEPT and h["accept-language"] == "en-US,en;q=0.9"
    assert r.http_version == "1.1"


async def test_downloader_default_headers_can_be_cleared(server):
    d = await _downloader(Settings(default_headers={}))
    try:
        r = await d.fetch(Request(url=server.base + "/echo"))
    finally:
        await d.close()
    assert "accept-language" not in json.loads(r.text)["headers"]


async def test_http_session_headers_override_default_headers(server):
    def configure(m: SessionManager) -> None:
        m.add("api", HttpSession(headers={"Accept": "application/json"}))

    d = await _downloader(configure=configure)
    try:
        r = await d.fetch(Request(url=server.base + "/echo", session="api"))
    finally:
        await d.close()
    h = json.loads(r.text)["headers"]
    assert h["accept"] == "application/json"  # the session's header beat the default
    assert h["accept-language"] == "en-US,en;q=0.9"  # untouched defaults still apply


# ---------------------------------------------------------------------------
# HTTP/2, verified against a real TLS + ALPN server
# ---------------------------------------------------------------------------


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


async def _asgi_app(scope: dict[str, Any], receive: Any, send: Any) -> None:
    """A tiny ASGI app: /echo, /set/<n>/<v>, /redirect, /flaky (503 once), /status/<n>."""
    if scope["type"] != "http":
        return
    body = b""
    while True:
        message = await receive()
        body += message.get("body", b"")
        if not message.get("more_body"):
            break
    path = scope["path"]
    headers: list[tuple[bytes, bytes]] = [(b"content-type", b"application/json")]
    status = 200
    hdrs = {k.decode(): v.decode() for k, v in scope["headers"]}
    payload: Any = {
        "method": scope["method"],
        "query": scope["query_string"].decode(),
        "headers": hdrs,
        "body": body.decode("utf-8", "replace"),
        "http_version": scope["http_version"],
    }
    if path.startswith("/set/"):
        _, _, name, value = path.split("/", 3)
        headers.append((b"set-cookie", f"{name}={value}; Path=/".encode()))
    elif path == "/redirect":
        status = 302
        headers.append((b"location", b"/echo"))
    elif path == "/flaky":
        _asgi_app.hits = getattr(_asgi_app, "hits", 0) + 1  # type: ignore[attr-defined]
        if _asgi_app.hits == 1:  # type: ignore[attr-defined]
            status = 503
    elif path.startswith("/status/"):
        status = int(path.rsplit("/", 1)[1])
    await send({"type": "http.response.start", "status": status, "headers": headers})
    await send({"type": "http.response.body", "body": json.dumps(payload).encode()})


class _H2Server:
    """hypercorn terminating TLS (throwaway CA) with configurable ALPN, in a thread."""

    def __init__(self, tmp_path: Any, alpn: list[str]) -> None:
        import trustme

        ca = trustme.CA()
        cert = ca.issue_cert("127.0.0.1")
        self.cert_path = str(tmp_path / "server.pem")
        cert.private_key_and_cert_chain_pem.write_to_path(self.cert_path)
        self.ca_path = str(tmp_path / "ca.pem")
        ca.cert_pem.write_to_path(self.ca_path)
        self.ssl_context = ssl.create_default_context(cafile=self.ca_path)
        self.port = _free_port()
        self.base = f"https://127.0.0.1:{self.port}"
        self._alpn = alpn
        self._stop: asyncio.Event | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread = threading.Thread(target=self._run, daemon=True)

    def _run(self) -> None:
        from hypercorn.asyncio import serve
        from hypercorn.config import Config

        config = Config()
        config.bind = [f"127.0.0.1:{self.port}"]
        config.certfile = self.cert_path
        config.keyfile = self.cert_path
        config.alpn_protocols = self._alpn
        config.accesslog = None

        async def main() -> None:
            self._loop = asyncio.get_running_loop()
            self._stop = asyncio.Event()
            await serve(_asgi_app, config, shutdown_trigger=self._stop.wait)  # type: ignore[arg-type]

        asyncio.run(main())

    def start(self) -> None:
        _asgi_app.hits = 0  # type: ignore[attr-defined]
        self._thread.start()
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            try:
                socket.create_connection(("127.0.0.1", self.port), timeout=0.2).close()
                return
            except OSError:
                time.sleep(0.05)
        raise RuntimeError("HTTP/2 test server did not start")

    def stop(self) -> None:
        if self._loop is not None and self._stop is not None:
            self._loop.call_soon_threadsafe(self._stop.set)
        self._thread.join(5)


@pytest.fixture
def h2_server(tmp_path):
    pytest.importorskip("httpx")
    pytest.importorskip("h2")
    pytest.importorskip("hypercorn")
    pytest.importorskip("trustme")
    srv = _H2Server(tmp_path, ["h2", "http/1.1"])
    srv.start()
    try:
        yield srv
    finally:
        srv.stop()


@pytest.fixture
def h1_only_server(tmp_path):
    pytest.importorskip("httpx")
    pytest.importorskip("h2")
    pytest.importorskip("hypercorn")
    pytest.importorskip("trustme")
    srv = _H2Server(tmp_path, ["http/1.1"])
    srv.start()
    try:
        yield srv
    finally:
        srv.stop()


def test_http2_is_really_negotiated(h2_server):
    r = Fetcher.get(h2_server.base + "/echo", protocol="http2", verify_ssl=h2_server.ssl_context)
    assert r.status == 200
    assert r.http_version == "2"  # proof: ALPN negotiated h2 end to end
    assert r.json()["http_version"] == "2"  # ...and the server saw an HTTP/2 request


def test_http1_protocol_against_an_h2_capable_server_stays_on_http11(h2_server):
    r = Fetcher.get(h2_server.base + "/echo", verify_ssl=h2_server.ssl_context)
    assert r.http_version == "1.1"
    assert r.json()["http_version"] == "1.1"


def test_http2_falls_back_to_http11_when_the_server_does_not_offer_h2(h1_only_server):
    r = Fetcher.get(h1_only_server.base + "/echo", protocol="http2", verify_ssl=h1_only_server.ssl_context)
    assert r.status == 200
    assert r.http_version == "1.1"  # the fallback is visible, not silent


def test_http2_sends_the_same_default_headers(h2_server):
    h = Fetcher.get(
        h2_server.base + "/echo", protocol="http2", verify_ssl=h2_server.ssl_context
    ).json()["headers"]
    assert h["accept"] == DEFAULT_ACCEPT
    assert h["accept-language"] == "en-US,en;q=0.9"
    assert h["user-agent"].startswith("BitscrapeBot/")


def test_http2_json_params_auth_and_methods(h2_server):
    r = Fetcher.post(
        h2_server.base + "/echo",
        protocol="http2",
        verify_ssl=h2_server.ssl_context,
        params={"a": [1, 2]},
        json={"k": "v"},
        auth=("u", "p"),
    )
    body = r.json()
    assert body["method"] == "POST"
    assert body["query"] == "a=1&a=2"
    assert json.loads(body["body"]) == {"k": "v"}
    assert body["headers"]["content-type"] == "application/json"
    assert body["headers"]["authorization"].startswith("Basic ")


def test_http2_session_cookies_persist_and_per_request_cookies_merge(h2_server):
    with FetcherSession(protocol="http2", verify_ssl=h2_server.ssl_context) as s:
        s.get(h2_server.base + "/set/sid/abc")
        merged = s.get(h2_server.base + "/echo", cookies={"x": "1"}).json()["headers"]["cookie"]
        after = s.get(h2_server.base + "/echo").json()["headers"]["cookie"]
    assert set(merged.split("; ")) == {"sid=abc", "x=1"}
    assert after == "sid=abc"


def test_http2_redirects(h2_server):
    followed = Fetcher.get(h2_server.base + "/redirect", protocol="http2", verify_ssl=h2_server.ssl_context)
    assert followed.url.endswith("/echo") and followed.status == 200
    held = Fetcher.get(
        h2_server.base + "/redirect",
        protocol="http2",
        verify_ssl=h2_server.ssl_context,
        follow_redirects=False,
    )
    assert held.status == 302


def test_http2_retries_a_503(h2_server):
    r = Fetcher.get(
        h2_server.base + "/flaky",
        protocol="http2",
        verify_ssl=h2_server.ssl_context,
        max_retries=2,
        **_fast(),
    )
    assert r.status == 200 and r.http_version == "2"


def test_http2_verifies_certificates_by_default(h2_server):
    """The server's cert is signed by a throwaway CA the client doesn't trust:
    verification must refuse it, and only an explicit opt-out may proceed."""
    with pytest.raises(FetchError):
        Fetcher.get(h2_server.base + "/echo", protocol="http2", max_retries=0)
    r = Fetcher.get(h2_server.base + "/echo", protocol="http2", verify_ssl=False)
    assert r.status == 200 and r.http_version == "2"


def test_http2_connection_refused_raises_fetch_error():
    pytest.importorskip("httpx")
    pytest.importorskip("h2")
    with pytest.raises(FetchError):
        Fetcher.get("https://127.0.0.1:1/", protocol="http2", max_retries=0)


async def test_async_http2_session_concurrent_requests(h2_server):
    async with AsyncFetcherSession(protocol="http2", verify_ssl=h2_server.ssl_context) as s:
        results = await asyncio.gather(*[s.get(h2_server.base + "/echo") for _ in range(10)])
    assert [r.status for r in results] == [200] * 10
    assert {r.http_version for r in results} == {"2"}


def test_http2_without_the_extra_installed_explains_how_to_get_it(monkeypatch):
    import sys

    monkeypatch.setitem(sys.modules, "httpx", None)  # makes `import httpx` raise ImportError
    with pytest.raises(ImportError, match=r"bitscrape\[http2\]"):
        AsyncFetcherSession(protocol="http2")
