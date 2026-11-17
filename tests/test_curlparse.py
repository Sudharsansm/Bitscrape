"""Tests for bitscrape.curlparse.curl_to_request."""

from __future__ import annotations

import pytest

from bitscrape.curlparse import curl_to_request


def test_simple_get():
    req = curl_to_request("curl 'https://example.com/page'")
    assert req.url == "https://example.com/page"
    assert req.method == "GET"
    assert req.headers == {}
    assert req.body is None


def test_without_leading_curl_token():
    req = curl_to_request("'https://example.com/page'")
    assert req.url == "https://example.com/page"


def test_headers_parsed():
    req = curl_to_request(
        "curl 'https://example.com' -H 'Accept: text/html' -H 'X-Custom: 1'"
    )
    assert req.headers == {"Accept": "text/html", "X-Custom": "1"}


def test_data_raw_implies_post_and_sets_body():
    req = curl_to_request("""curl 'https://api.example.com' --data-raw '{"q":"widgets"}'""")
    assert req.method == "POST"
    assert req.body == b'{"q":"widgets"}'


def test_explicit_method_overrides_default():
    req = curl_to_request("curl -X PUT 'https://example.com/resource/1' --data-raw '{}'")
    assert req.method == "PUT"


def test_cookie_flag_becomes_cookie_header():
    req = curl_to_request("curl 'https://example.com' -b 'session=abc123; theme=dark'")
    assert req.headers["Cookie"] == "session=abc123; theme=dark"


def test_user_agent_flag():
    req = curl_to_request("curl 'https://example.com' -A 'MyBot/1.0'")
    assert req.headers["User-Agent"] == "MyBot/1.0"


def test_ignored_boolean_flags_dont_break_parsing():
    req = curl_to_request("curl 'https://example.com' --compressed -k -L -s")
    assert req.url == "https://example.com"
    assert req.method == "GET"


def test_extra_request_kwargs_passed_through():
    req = curl_to_request("curl 'https://example.com'", callback="parse_item")
    assert req.callback == "parse_item"


def test_realistic_browser_copy_as_curl():
    # A trimmed but realistic "Copy as cURL" output.
    cmd = """curl 'https://example.com/api/items?page=2' \\
  -H 'accept: application/json, text/plain, */*' \\
  -H 'accept-language: en-US,en;q=0.9' \\
  -H 'referer: https://example.com/items' \\
  -H 'user-agent: Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)' \\
  --compressed"""
    req = curl_to_request(cmd)
    assert req.url == "https://example.com/api/items?page=2"
    assert req.headers["accept"] == "application/json, text/plain, */*"
    assert req.headers["referer"] == "https://example.com/items"


def test_missing_url_raises():
    with pytest.raises(ValueError, match="Could not find a URL"):
        curl_to_request("curl -H 'Accept: text/html'")
