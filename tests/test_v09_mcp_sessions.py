"""MCP server: URL guard, bearer auth, persistent sessions, saved state, extract tools."""
import json
import shutil
import subprocess
from pathlib import Path

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from bitscrape.browser import SHADOW_DOM_JS
from bitscrape.mcp_server import BearerAuthMiddleware, BitscrapeMCPServer, check_public_url, run_http
from tests.test_multi_session import _FakePW

HTML = """<html><body><h1>Shop</h1>
<ul><li class=i><a href="/a">A</a> <span class=p>$5</span></li><li class=i><a href="/b">B</a> <span class=p>$7</span></li></ul>
<a href="https://other.test/x">out</a></body></html>"""


async def _site():
    app = web.Application()

    async def page(_):
        return web.Response(text=HTML, content_type="text/html")

    app.router.add_get("/", page)
    srv = TestServer(app)
    await srv.start_server()
    return srv


async def test_check_public_url():
    with pytest.raises(ValueError, match="scheme"):
        await check_public_url("file:///etc/passwd")
    for bad in ("http://127.0.0.1/", "http://169.254.169.254/latest", "http://10.0.0.5/", "http://[::1]/"):
        with pytest.raises(ValueError, match="non-public"):
            await check_public_url(bad)
    await check_public_url("http://127.0.0.1/", allow_private=True)
    await check_public_url("https://8.8.8.8/")


async def test_server_refuses_private_by_default():
    srv = await _site()
    try:
        s = BitscrapeMCPServer()
        with pytest.raises(ValueError, match="non-public"):
            await s.fetch(str(srv.make_url("/")))
        res = await s.bulk_fetch([str(srv.make_url("/"))])
        assert "error" in res[0]
    finally:
        await srv.close()


async def test_extract_links_and_selected_fetch():
    srv = await _site()
    try:
        s = BitscrapeMCPServer(allow_private_networks=True)
        url = str(srv.make_url("/"))
        r = await s.extract(url, ".p")
        assert r["values"] == ["$5", "$7"] and r["total_matches"] == 2
        r = await s.extract(url, ".i a", attribute="href", limit=1)
        assert r["values"] == ["/a"] and r["total_matches"] == 2
        links = await s.list_links(url, same_domain_only=True)
        assert links["total"] == 2 and all("other.test" not in u for u in links["links"])
        md = await s.fetch(url, css_selector=".i")
        assert "A" in md["markdown"] and "Shop" not in md["markdown"]
        many = await s.bulk_fetch([url, url], max_length=500)
        assert len(many) == 2 and "markdown" in many[0]
        with pytest.raises(ValueError):
            await s.bulk_fetch([url] * 26)
    finally:
        await srv.close()


async def _asgi(app, headers):
    sent = []

    async def send(m):
        sent.append(m)

    async def receive():
        return {"type": "http.request"}

    await app({"type": "http", "headers": headers}, receive, send)
    return sent[0]["status"]


async def test_bearer_middleware():
    async def inner(scope, receive, send):
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})

    app = BearerAuthMiddleware(inner, "s3cret")
    assert await _asgi(app, []) == 401
    assert await _asgi(app, [(b"authorization", b"Bearer nope")]) == 401
    assert await _asgi(app, [(b"authorization", b"Basic s3cret")]) == 401
    assert await _asgi(app, [(b"authorization", b"bearer s3cret")]) == 200
    with pytest.raises(ValueError):
        BearerAuthMiddleware(inner, "")


def test_run_http_refuses_open_bind_without_token():
    server = BitscrapeMCPServer().build_fastmcp()
    with pytest.raises(ValueError, match="without an auth token"):
        run_http(server, host="0.0.0.0")
    with pytest.raises(ValueError, match="transport"):
        run_http(server, host="127.0.0.1", transport="carrier-pigeon")


def _server(tmp_path):
    pw = _FakePW()

    async def factory():
        return pw

    return BitscrapeMCPServer(allow_private_networks=True, state_dir=tmp_path / "st", playwright_factory=factory), pw


async def test_session_lifecycle_and_limits(tmp_path):
    s, _pw = _server(tmp_path)
    s.max_sessions = 1
    r = await s.session_open("main")
    assert r["open"] and s.session_list()[0]["session_id"] == "main"
    with pytest.raises(ValueError, match="already open"):
        await s.session_open("main")
    with pytest.raises(ValueError, match="at most"):
        await s.session_open("two")
    with pytest.raises(ValueError):
        await s.session_open("../evil")
    page = await s.session_fetch("main", "http://127.0.0.1:9/x")
    assert "rendered" in page["markdown"] and page["recorded_requests"] == 0
    assert s.network_search("main") == []
    with pytest.raises(ValueError, match="no open session"):
        await s.session_fetch("ghost", "http://127.0.0.1/")
    await s.session_close("main")
    assert s.session_list() == []


async def test_state_save_load_names_and_permissions(tmp_path):
    s, _pw = _server(tmp_path)
    await s.session_open("a")
    sess = s._sessions["a"]

    async def storage_state(path=None, indexed_db=False):
        Path(path).write_text(json.dumps({"cookies": [], "origins": []}))

    sess._browser.context.storage_state = storage_state

    async def clear_cookies():
        return None

    sess._browser.context.clear_cookies = clear_cookies
    out = await s.session_save_state("a", "login-1")
    assert out["saved_as"] == "login-1"
    f = tmp_path / "st" / "login-1.json"
    assert f.is_file() and (f.stat().st_mode & 0o077) == 0
    await s.session_load_state("a", "login-1")
    await s.session_close("a")
    await s.session_open("b", load_state="login-1")
    for bad in ("../x", "a/b", "", "x" * 65, "sp ace"):
        with pytest.raises(ValueError):
            await s.session_save_state("b", bad) if bad else s._state_path(bad)
    with pytest.raises(ValueError, match="no saved state"):
        await s.session_open("c", load_state="missing")
    await s.aclose()


@pytest.mark.skipif(shutil.which("node") is None or not Path("/home/claude/jstest/node_modules/jsdom").exists(), reason="needs node+jsdom")
def test_shadow_dom_js_in_jsdom(tmp_path):
    (tmp_path / "shadow.js").write_text(SHADOW_DOM_JS)
    script = """
const {JSDOM}=require('/home/claude/jstest/node_modules/jsdom'); const fs=require('fs');
const js=fs.readFileSync(process.argv[2],'utf8');
const dom=new JSDOM('<html><body><x-c><b slot="s">v</b><i>drop</i></x-c></body></html>',{runScripts:'outside-only'});
const w=dom.window; const h=w.document.querySelector('x-c');
h.attachShadow({mode:'open'}).innerHTML='<p>t</p><slot name="s"></slot>';
console.log(w.eval('('+js+')()'));"""
    (tmp_path / "t.js").write_text(script)
    out = subprocess.run(["node", str(tmp_path / "t.js"), str(tmp_path / "shadow.js")], capture_output=True, text=True, check=True).stdout
    assert "<shadow-root><p>t</p><slot name=\"s\"><b slot=\"s\">v</b></slot></shadow-root>" in out and "drop" not in out
