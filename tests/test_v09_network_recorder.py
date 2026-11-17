"""NetworkRecorder + BrowserSession.network/save_state/load_state with a fake Playwright."""
import asyncio
import json
from typing import Any

import pytest

from bitscrape.network import NetworkRecorder, is_text_content_type
from bitscrape.sessions import BrowserSession, SessionError
from tests.test_multi_session import _FakePW


class FakeRequest:
    def __init__(self, url, method="GET", rtype="fetch", post=None):
        self.url, self.method, self.resource_type = url, method, rtype
        self.headers = {"accept": "*/*"}
        self.post_data_buffer = post
        self.frame = type("F", (), {"url": "https://site.test/app"})()

    async def all_headers(self):
        return {**self.headers, "cookie": "a=1"}


class FakeResponse:
    def __init__(self, url, status=200, ctype="application/json", body=b"{}", **kw):
        self.url, self.status = url, status
        self.headers = {"content-type": ctype}
        self._body = body
        self.request = FakeRequest(url, **kw)

    async def body(self):
        return self._body


async def feed(rec, *responses):
    for r in responses:
        rec._on_response(r)
    await rec.drain()


async def test_saves_text_bodies_and_notes_binary():
    rec = NetworkRecorder()
    await feed(
        rec,
        FakeResponse("https://a.test/api/items", body=b'{"items": [{"price": 5}]}'),
        FakeResponse("https://a.test/logo.png", ctype="image/png", body=b"\x89PNG", rtype="image"),
    )
    api, img = rec.search(limit=None)
    assert api.json()["items"][0]["price"] == 5 and api.request_headers["cookie"] == "a=1"
    assert img.body == b"" and "Non-text" in img.body_note
    assert [r.network_id for r in rec.search(limit=None)] == [1, 2]


async def test_search_filters_and_api_only():
    rec = NetworkRecorder()
    await feed(
        rec,
        FakeResponse("https://a.test/api/x", body=b"{}"),
        FakeResponse("https://a.test/app.js", ctype="text/javascript", body=b"1", rtype="script"),
        FakeResponse("https://a.test/missing", status=404, ctype="text/html", body=b"nf", rtype="document"),
        FakeResponse("https://a.test/api/post", method="POST", body=b'{"ok":1}', post=b"q=1"),
    )
    assert len(rec.search(url_pattern=r"/api/", limit=None)) == 2
    assert len(rec.search(method="post")) == 1
    assert rec.search(method="post")[0].request_body == b"q=1"
    assert [r.status for r in rec.search(include_static=False, limit=None)] == [200, 404, 200]
    assert len(rec.search(status={404})) == 1
    assert len(rec.search(status=lambda s: s >= 400)) == 1
    assert rec.search(after_id=3)[0].network_id == 4
    assert rec.find_json("ok") == [1]


async def test_bounds_evict_oldest_and_ids_never_reused():
    rec = NetworkRecorder(max_entries=2)
    await feed(rec, *(FakeResponse(f"https://a.test/{i}", body=b"{}") for i in range(4)))
    assert [r.network_id for r in rec.search(limit=None)] == [3, 4] and rec.evicted == 2
    assert rec.get(1) is None and rec.get(4) is not None
    rec.clear()
    await feed(rec, FakeResponse("https://a.test/n", body=b"{}"))
    assert rec.search()[0].network_id == 5


async def test_large_body_not_saved_and_total_cap():
    rec = NetworkRecorder(max_body_bytes=10, max_total_bytes=15)
    await feed(rec, FakeResponse("https://a.test/big", body=b"x" * 11))
    assert rec.get(1).body == b"" and "too large" in rec.get(1).body_note
    await feed(rec, FakeResponse("https://a.test/a", body=b"12345678"), FakeResponse("https://a.test/b", body=b"12345678"))
    assert rec.total_body_bytes <= 15 and rec.get(2) is None


async def test_unreadable_body_and_bad_regex():
    rec = NetworkRecorder()

    class Broken(FakeResponse):
        async def body(self):
            raise RuntimeError("Target closed")

    await feed(rec, Broken("https://a.test/x"))
    assert "unreadable" in rec.get(1).body_note
    import re

    with pytest.raises(re.error):
        rec.search(url_pattern="(")


async def test_har_export_and_content_types():
    rec = NetworkRecorder()
    await feed(rec, FakeResponse("https://a.test/api", body=b'{"a":1}'))
    har = rec.to_har()
    assert har["log"]["entries"][0]["response"]["content"]["text"] == '{"a":1}'
    json.dumps(har)
    assert is_text_content_type("application/vnd.api+json; charset=utf-8")
    assert not is_text_content_type("image/png") and not is_text_content_type(None)


async def test_recorded_response_parsed_css():
    rec = NetworkRecorder()
    await feed(rec, FakeResponse("https://a.test/p", ctype="text/html", body=b"<h1>Hi</h1>"))
    assert rec.get(1).css("h1::text").get() == "Hi"


# -- BrowserSession wiring ------------------------------------------------------


class RecCtx:
    """Extends the fake context with .on() and state methods."""


def _session(**kw: Any):
    pw = _FakePW()

    async def factory():
        return pw

    return BrowserSession(playwright_factory=factory, **kw), pw


async def test_session_attaches_recorder_to_context():
    from bitscrape.core.settings import Settings

    s, pw = _session(record_requests=True, max_recorded_requests=5)
    with pytest.raises(SessionError):
        _session()[0].network  # noqa: B018
    attached = []
    orig = pw.chromium.launch

    async def launch(**kw):
        b = await orig(**kw)
        nc = b.new_context

        async def new_context(**k):
            ctx = await nc(**k)
            ctx.on = lambda ev, cb: attached.append((ev, cb))
            return ctx

        b.new_context = new_context
        return b

    pw.chromium.launch = launch
    await s.start(Settings())
    assert attached and attached[0][0] == "response"
    assert s.network.max_entries == 5
    await s.close()


async def test_save_and_load_state(tmp_path):
    from bitscrape.core.settings import Settings

    s, pw = _session()
    await s.start(Settings())
    ctx = s.context
    saved = {}

    async def storage_state(path=None, indexed_db=False):
        saved["indexed_db"] = indexed_db
        (tmp_path / "st.json").write_text(json.dumps({"cookies": [{"name": "a", "value": "1", "domain": "x.test", "path": "/"}], "origins": [{"origin": "https://x.test", "localStorage": [{"name": "k", "value": "v"}]}]}))

    scripts, cleared = [], []

    async def add_init_script(script=None, path=None):
        scripts.append(script)

    async def clear_cookies():
        cleared.append(1)

    ctx.storage_state, ctx.add_init_script, ctx.clear_cookies = storage_state, add_init_script, clear_cookies
    await s.save_state(tmp_path / "st.json")
    assert saved["indexed_db"] is True
    await s.load_state(tmp_path / "st.json")
    assert cleared and ctx.cookie_jar[0]["name"] == "a"
    assert "https://x.test" in scripts[0] and '"k"' in scripts[0]
    (tmp_path / "bad.json").write_text("[]")
    with pytest.raises(ValueError):
        await s.load_state(tmp_path / "bad.json")
    await s.close()


def test_browser_fetch_kwargs_accepted():
    from bitscrape.fetchers.dynamic import AsyncDynamicSession

    d = AsyncDynamicSession(record_requests=True, pierce_shadow=True, allow_domains=["a.com"], block_ads=True)
    assert d.network is not None
    assert d._page_request("https://x.test", {}).pierce_shadow is True
    assert d._page_request("https://x.test", {"pierce_shadow": False}).pierce_shadow is False
