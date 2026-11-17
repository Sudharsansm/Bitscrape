"""
Tests for bitscrape.fetch_url() -- the synchronous single-request helper
added alongside bitscrape.run() (which is sync-for-a-whole-spider;
fetch_url is sync-for-a-single-request). Uses the same loop-independent
threaded server as test_package_api.py, for the same reason: it's a
blocking API managing its own internal event loop via asyncio.run().
"""

from __future__ import annotations

import asyncio
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

import bitscrape
from bitscrape.core.settings import Settings


class _ThreadedTestServer:
    def __init__(self, body: bytes = b"<html><body>hello</body></html>", status: int = 200):
        self.body = body
        self.status = status
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802
                self.send_response(outer.status)
                self.send_header("Content-Type", "text/html")
                self.end_headers()
                self.wfile.write(outer.body)

            def log_message(self, format: str, *args: object) -> None:  # noqa: A002
                pass

        self._httpd = HTTPServer(("127.0.0.1", 0), Handler)
        self._thread = threading.Thread(target=self._httpd.serve_forever, daemon=True)
        self._thread.start()

    @property
    def url(self) -> str:
        host, port = self._httpd.server_address
        return f"http://{host}:{port}/page"

    def close(self) -> None:
        self._httpd.shutdown()
        self._httpd.server_close()
        self._thread.join(timeout=2)


def test_fetch_url_returns_a_real_response_no_async_needed():
    server = _ThreadedTestServer(body=b"<html><body><h1>Sync works</h1></body></html>")
    try:
        response = bitscrape.fetch_url(server.url)
        assert response.status == 200
        assert b"Sync works" in response.body
    finally:
        server.close()


def test_fetch_url_accepts_custom_settings():
    server = _ThreadedTestServer()
    try:
        response = bitscrape.fetch_url(
            server.url, settings=Settings(user_agent="MyCustomBot/1.0")
        )
        assert response.status == 200
    finally:
        server.close()


def test_fetch_url_raises_helpful_error_inside_a_running_event_loop():
    server = _ThreadedTestServer()
    try:

        async def _inside_loop():
            bitscrape.fetch_url(server.url)

        with pytest.raises(RuntimeError, match="synchronous helper"):
            asyncio.run(_inside_loop())
    finally:
        server.close()
