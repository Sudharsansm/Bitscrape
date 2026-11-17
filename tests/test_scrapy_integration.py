"""
Tests for bitscrape.integrations.scrapy -- a decorator that lets an
existing Scrapy callback also get a bitscrape ParsedResponse over the same
bytes, without needing bitscrape to fetch anything or depend on `scrapy`
being installed. Uses a minimal duck-typed fake response instead of a real
scrapy.Response, exactly matching the module's own claim that it works
with "anything exposing .url, .body ... and optionally .status/.headers".
"""

from __future__ import annotations

from bitscrape.integrations.scrapy import bitscrape_response, to_parsed_response
from bitscrape.parser.selector import ParsedResponse


class _FakeHeaders:
    """Mimics Scrapy's Headers: bytes keys/values, list-wrapped values."""

    def __init__(self, data: dict[bytes, list[bytes]]) -> None:
        self._data = data

    def items(self):
        return self._data.items()


class _FakeScrapyResponse:
    def __init__(
        self,
        url: str,
        body: bytes,
        status: int = 200,
        headers: _FakeHeaders | None = None,
        encoding: str = "utf-8",
    ) -> None:
        self.url = url
        self.body = body
        self.status = status
        self.headers = headers or _FakeHeaders({})
        self.encoding = encoding


def test_to_parsed_response_wraps_body_and_url():
    fake = _FakeScrapyResponse(
        url="https://example.com/page",
        body=b"<html><body><h1>Hello</h1></body></html>",
    )
    parsed = to_parsed_response(fake)
    assert isinstance(parsed, ParsedResponse)
    assert parsed.url == "https://example.com/page"
    assert parsed.css("h1::text").get() == "Hello"


def test_to_parsed_response_decodes_scrapy_style_headers():
    fake = _FakeScrapyResponse(
        url="https://example.com/",
        body=b"ok",
        headers=_FakeHeaders({b"Content-Type": [b"text/html; charset=utf-8"]}),
    )
    parsed = to_parsed_response(fake)
    assert parsed.headers["Content-Type"] == "text/html; charset=utf-8"


def test_to_parsed_response_accepts_plain_dict_headers():
    fake = _FakeScrapyResponse(url="https://example.com/", body=b"ok")
    fake.headers = {"X-Test": "1"}  # not Scrapy's Headers class, just a dict
    parsed = to_parsed_response(fake)
    assert parsed.headers["X-Test"] == "1"


def test_bitscrape_response_decorator_injects_bs_kwarg_without_touching_response():
    captured = {}

    class FakeSpider:
        @bitscrape_response
        def parse(self, response, **kwargs):
            captured["response"] = response
            captured["bs"] = kwargs["bs"]
            return None

    fake = _FakeScrapyResponse(
        url="https://example.com/product",
        body=b"<html><body><div class='price'>$9.99</div></body></html>",
    )
    spider = FakeSpider()
    spider.parse(fake)

    # The original response object is passed through completely unchanged.
    assert captured["response"] is fake
    # `bs` is a real ParsedResponse over the same bytes.
    assert isinstance(captured["bs"], ParsedResponse)
    assert captured["bs"].css("div.price::text").get() == "$9.99"


def test_bitscrape_response_decorator_preserves_function_metadata():
    @bitscrape_response
    def parse(self, response, **kwargs):
        """My docstring."""

    assert parse.__name__ == "parse"
    assert parse.__doc__ == "My docstring."
