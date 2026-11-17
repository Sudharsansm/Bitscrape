"""Bundled ad list, suffix matching, allow-list, hosts parsing, update_blocklist."""
import asyncio

import pytest
from aiohttp import web

from bitscrape.downloader.blocking import (
    DomainBlocker,
    load_ad_domains,
    parse_blocklist,
    update_blocklist,
)


def test_bundled_list_has_thousands_of_domains():
    assert len(load_ad_domains()) >= 3500


def test_block_ads_matches_subdomains_not_lookalikes():
    b = DomainBlocker(block_ads=True)
    assert b.is_blocked_url("https://x.y.doubleclick.net/pixel")
    assert not b.is_blocked_url("https://notdoubleclick.net/")
    assert not b.is_blocked_url("https://example.org/")


def test_allow_domains_override_list():
    b = DomainBlocker(extra_domains=["cdn.example.com"], allow_domains=["static.cdn.example.com"])
    assert b.is_blocked_url("https://a.cdn.example.com/x")
    assert not b.is_blocked_url("https://static.cdn.example.com/x")


def test_parse_blocklist_handles_hosts_and_plain_formats():
    text = "# c\n0.0.0.0 ads.a.com\n127.0.0.1 localhost\nb.org\nbad/path.com\n1.2.3.4\n"
    assert parse_blocklist(text) == {"ads.a.com", "b.org"}


async def test_route_handler_counts_blocked():
    class Req:
        def __init__(self, url, rt="script"):
            self.url, self.resource_type = url, rt

    class Route:
        def __init__(self, url):
            self.request = Req(url)
            self.action = None

        async def abort(self):
            self.action = "abort"

        async def continue_(self):
            self.action = "continue"

    b = DomainBlocker(extra_domains=["t.example.com"])
    r1, r2 = Route("https://t.example.com/a.js"), Route("https://ok.example.com/a.js")
    await b.route_handler(r1)
    await b.route_handler(r2)
    assert (r1.action, r2.action, b.blocked_count) == ("abort", "continue", 1)


async def test_update_blocklist_merges_and_load_picks_it_up(tmp_path, monkeypatch):
    app = web.Application()
    app.router.add_get("/a", lambda r: web.Response(text="0.0.0.0 one.test\n0.0.0.0 both.test\n"))
    app.router.add_get("/b", lambda r: web.Response(text="both.test\ntwo.test\n"))
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    dest = tmp_path / "ads.txt"
    srcs = [f"http://127.0.0.1:{port}/a", f"http://127.0.0.1:{port}/b"]
    try:
        n = await asyncio.to_thread(update_blocklist, srcs, dest=dest, mode="union")
        assert n == 3
        n = await asyncio.to_thread(update_blocklist, srcs, dest=dest, mode="intersection")
        assert n == 1
    finally:
        await runner.cleanup()
    monkeypatch.setenv("BITSCRAPE_BLOCKLIST", str(dest))
    assert "both.test" in load_ad_domains()
    with pytest.raises(ValueError):
        update_blocklist(["file:///etc/passwd"], dest=dest)
