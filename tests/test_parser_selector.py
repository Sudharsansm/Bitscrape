"""
Tests for bitscrape.parser.selector -- CSS/XPath selection over Response
bodies.

Closes the coverage gap flagged in BITSCRAPE_QA_REPORT.md ("parser/
selector.py -- 46% coverage; the parsel-based fallback selector path looks
untested"). While writing these, found a real packaging bug: `parsel` was
never declared as a project dependency anywhere in pyproject.toml, despite
`.xpath()` requiring it, being documented throughout docs/, and being used
in the bundled examples/polite_sitemap_spider.py -- meaning a fresh
`pip install -e ".[all,cli]"` left .xpath() broken with
`ImportError: XPath requires parsel: pip install parsel` on any project
where selectolax (which has no native XPath support) was the active CSS
backend. Fixed by adding `parsel>=1.9` to pyproject.toml's base
dependencies. Every test below runs against the real, now-correctly-
installed parsel and selectolax.
"""

from __future__ import annotations

import builtins

import pytest

from bitscrape.core.models import Request, Response
from bitscrape.parser.selector import ParsedResponse

HTML = """
<html>
<head><title>Test Page</title></head>
<body>
  <h1>Main Heading</h1>
  <div class="quote">
    <span class="text">First quote text</span>
    <small class="author">Author One</small>
    <a href="/author/1">more</a>
  </div>
  <div class="quote">
    <span class="text">Second quote text</span>
    <small class="author">Author Two</small>
    <a href="/author/2">more</a>
  </div>
  <ul id="tags">
    <li>python</li>
    <li>scraping</li>
  </ul>
</body>
</html>
"""


def _make_response(html: str = HTML) -> Response:
    req = Request(url="https://example.com/page")
    return Response(
        url="https://example.com/page",
        status=200,
        headers={},
        body=html.encode("utf-8"),
        request=req,
        elapsed_ms=1.0,
    )


# ---------------------------------------------------------------------------
# CSS selection (default selectolax backend)
# ---------------------------------------------------------------------------


def test_css_text_extraction():
    parsed = ParsedResponse(_make_response())
    assert parsed.css("h1::text").get() == "Main Heading"


def test_css_attr_extraction():
    parsed = ParsedResponse(_make_response())
    assert parsed.css("a::attr(href)").get() == "/author/1"


def test_css_getall_returns_every_match():
    parsed = ParsedResponse(_make_response())
    hrefs = parsed.css("a::attr(href)").getall()
    assert hrefs == ["/author/1", "/author/2"]


def test_css_get_with_no_match_returns_default():
    parsed = ParsedResponse(_make_response())
    assert parsed.css("div.nonexistent::text").get() is None
    assert parsed.css("div.nonexistent::text").get("fallback") == "fallback"


def test_css_getall_with_no_match_returns_empty_list():
    parsed = ParsedResponse(_make_response())
    assert parsed.css("div.nonexistent").getall() == []


def test_css_chained_selection_on_matched_elements():
    parsed = ParsedResponse(_make_response())
    quotes = parsed.css("div.quote")
    texts = [q.css("span.text::text").get() for q in quotes]
    authors = [q.css("small.author::text").get() for q in quotes]
    assert texts == ["First quote text", "Second quote text"]
    assert authors == ["Author One", "Author Two"]


def test_css_iteration_over_selector_list():
    parsed = ParsedResponse(_make_response())
    count = sum(1 for _ in parsed.css("li"))
    assert count == 2


def test_css_id_selector():
    parsed = ParsedResponse(_make_response())
    assert parsed.css("#tags li::text").getall() == ["python", "scraping"]


# ---------------------------------------------------------------------------
# XPath selection -- previously broken (missing parsel dependency)
# ---------------------------------------------------------------------------


def test_xpath_text_extraction():
    parsed = ParsedResponse(_make_response())
    assert parsed.xpath("//h1/text()").get() == "Main Heading"


def test_xpath_attribute_extraction():
    parsed = ParsedResponse(_make_response())
    assert parsed.xpath("//a/@href").get() == "/author/1"


def test_xpath_getall():
    parsed = ParsedResponse(_make_response())
    hrefs = parsed.xpath("//a/@href").getall()
    assert hrefs == ["/author/1", "/author/2"]


def test_xpath_predicate_expression():
    """The kind of thing CSS can't express directly -- selecting by text
    content via an XPath predicate."""
    parsed = ParsedResponse(_make_response())
    result = parsed.xpath("//small[text()='Author One']/../span/text()").get()
    assert result == "First quote text"


def test_xpath_no_match_returns_none():
    parsed = ParsedResponse(_make_response())
    assert parsed.xpath("//div[@class='nope']/text()").get() is None


def test_xpath_local_name_matches_bundled_sitemap_example_usage():
    """Mirrors the exact query used in examples/polite_sitemap_spider.py,
    which was silently broken before the parsel dependency fix."""
    sitemap_xml = """
    <urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
      <url><loc>https://example.com/a</loc></url>
      <url><loc>https://example.com/b</loc></url>
    </urlset>
    """
    parsed = ParsedResponse(_make_response(sitemap_xml))
    locs = parsed.xpath("//*[local-name()='loc']/text()").getall()
    assert locs == ["https://example.com/a", "https://example.com/b"]


# ---------------------------------------------------------------------------
# The parsel-only fallback backend (selectolax unavailable)
# ---------------------------------------------------------------------------


def test_parsel_fallback_backend_when_selectolax_unavailable(monkeypatch):
    """
    Forces the fallback path by making `import selectolax.parser` fail,
    simulating an environment where only parsel is installed. Confirms
    ParsedResponse transparently falls back to the parsel backend and CSS
    selection still works correctly through it.
    """
    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name == "selectolax.parser" or name.startswith("selectolax"):
            raise ImportError("simulated: selectolax not installed")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)

    parsed = ParsedResponse(_make_response())
    assert parsed.css("h1::text").get() == "Main Heading"
    assert parsed._backend == "parsel"


def test_parsel_fallback_backend_xpath_still_works(monkeypatch):
    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name == "selectolax.parser" or name.startswith("selectolax"):
            raise ImportError("simulated: selectolax not installed")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)

    parsed = ParsedResponse(_make_response())
    assert parsed.xpath("//h1/text()").get() == "Main Heading"


def test_parsel_fallback_chained_css_on_matched_elements(monkeypatch):
    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name == "selectolax.parser" or name.startswith("selectolax"):
            raise ImportError("simulated: selectolax not installed")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)

    parsed = ParsedResponse(_make_response())
    quotes = parsed.css("div.quote")
    texts = [q.css("span.text::text").get() for q in quotes]
    assert texts == ["First quote text", "Second quote text"]


def test_neither_backend_available_raises_clear_error(monkeypatch):
    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name.startswith("selectolax") or name == "parsel":
            raise ImportError(f"simulated: {name} not installed")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)

    parsed = ParsedResponse(_make_response())
    with pytest.raises(ImportError):
        parsed.css("h1::text").get()


# ---------------------------------------------------------------------------
# Response.css()/.xpath() delegation
# ---------------------------------------------------------------------------


def test_response_text_and_body_accessible_via_parsed_response():
    response = _make_response()
    parsed = ParsedResponse(response)
    assert parsed.text == response.text
    assert parsed.body == response.body
