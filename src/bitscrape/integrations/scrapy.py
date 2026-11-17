"""
Scrapy integration
===================

If you already have a Scrapy project and just want bitscrape's parser
(adaptive selectors that survive redesigns, auto CSS/XPath generation,
structured-data extraction, ``find_similar``, markdown export) on the
responses you're *already fetching with Scrapy* -- without rewriting the
spider to use bitscrape's own downloader -- decorate the callback with
:func:`bitscrape_response`. It doesn't touch how the response was fetched;
it just hands the callback a second argument wrapping the same bytes in
bitscrape's parser.

Usage (inside a Scrapy spider)::

    from bitscrape.integrations.scrapy import bitscrape_response

    class MySpider(scrapy.Spider):
        name = "my_spider"
        start_urls = ["https://example.com"]

        @bitscrape_response
        def parse(self, response, bs, **kwargs):
            # `response` is Scrapy's own response, unchanged -- everything
            # you already had keeps working.
            title = response.css("title::text").get()

            # `bs` is a bitscrape ParsedResponse over the same bytes --
            # use it for the extras Scrapy's own parser doesn't have.
            similar = bs.find_similar(bs.css("div.product").first())
            yield {"title": title, "similar_products": len(similar)}

This module deliberately doesn't import ``scrapy`` itself (so bitscrape
doesn't need it as a dependency just to expose this decorator) -- it
duck-types against any response object that has ``.url`` and ``.body``,
which covers Scrapy's ``Response``/``TextResponse`` and anything with the
same shape.
"""

from __future__ import annotations

import functools
from typing import Any, Callable, TypeVar

from bitscrape.core.models import Request, Response
from bitscrape.parser.selector import ParsedResponse

F = TypeVar("F", bound=Callable[..., Any])


def to_parsed_response(scrapy_response: Any) -> ParsedResponse:
    """Wrap a Scrapy (or Scrapy-shaped) response in a bitscrape
    :class:`~bitscrape.parser.selector.ParsedResponse`, sharing the same
    body bytes -- no re-fetch.

    Works with anything exposing ``.url``, ``.body`` (bytes), and
    optionally ``.status`` / ``.headers`` / ``.encoding`` -- so it isn't
    limited to Scrapy specifically.
    """
    status = getattr(scrapy_response, "status", 200)
    headers = {}
    raw_headers = getattr(scrapy_response, "headers", None)
    if raw_headers is not None:
        try:
            # Scrapy's Headers object: iterate key/value pairs, decoding
            # the bytes keys/values it stores internally.
            for key, values in raw_headers.items():
                value = values[0] if isinstance(values, (list, tuple)) else values
                headers[_decode(key)] = _decode(value)
        except AttributeError:
            headers = dict(raw_headers)  # already a plain mapping

    encoding = getattr(scrapy_response, "encoding", None) or "utf-8"
    request = Request(url=scrapy_response.url)
    response = Response(
        url=scrapy_response.url,
        status=status,
        headers=headers,
        body=bytes(scrapy_response.body),
        request=request,
        encoding=encoding,
    )
    return ParsedResponse(response)


def _decode(value: Any) -> str:
    return value.decode("utf-8", errors="replace") if isinstance(value, bytes) else str(value)


def bitscrape_response(callback: F) -> F:
    """Decorate a Scrapy callback to receive a bitscrape ``ParsedResponse``
    (built from the same response bytes Scrapy already fetched) as a `bs`
    keyword argument, alongside the original Scrapy response unchanged.
    """

    @functools.wraps(callback)
    def wrapper(self: Any, response: Any, *args: Any, **kwargs: Any) -> Any:
        kwargs["bs"] = to_parsed_response(response)
        return callback(self, response, *args, **kwargs)

    return wrapper  # type: ignore[return-value]
