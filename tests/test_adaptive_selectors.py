"""Adaptive selectors, find-similar and automatic selector generation."""

from __future__ import annotations

import lxml.html
import pytest
from selectolax.parser import HTMLParser

from bitscrape.core.models import Request, Response
from bitscrape.parser.adaptive import (
    AdaptiveStore,
    ElementSignature,
    attrs_similarity,
    relocate,
    score,
    signature_of,
)
from bitscrape.parser.selector import ParsedResponse
from bitscrape.parser.selectorgen import generate_css, generate_xpath
from bitscrape.parser.tree import LxmlNode, SelectolaxNode


def make_response(html: str, url: str = "https://shop.example/p/1") -> ParsedResponse:
    resp = Response(url=url, status=200, headers={}, body=html.encode(), request=Request(url=url))
    return resp_to_parsed(resp)


_STORE = AdaptiveStore(":memory:")


def resp_to_parsed(resp: Response, store: AdaptiveStore | None = None) -> ParsedResponse:
    return ParsedResponse(resp, adaptive_store=store or _STORE)


ORIGINAL = """
<html><body>
  <header><nav><a href="/">Home</a><a href="/cart">Cart</a></nav></header>
  <main>
    <div class="product-card" id="p1">
      <h1 class="product-title">Trail Runner Pro</h1>
      <div class="price-box"><span class="product-price" itemprop="price">$129.99</span>
        <span class="old-price">$159.99</span></div>
      <p class="description">Lightweight trail running shoe.</p>
    </div>
  </main>
  <footer><span class="copyright">(c) Shop</span></footer>
</body></html>
"""

# Redesign: every class renamed, price nested one level deeper in a new
# wrapper, tags changed (span -> strong), decorative siblings added.
REDESIGNED = """
<html><body>
  <header><nav><a href="/">Home</a><a href="/cart">Cart</a></nav></header>
  <main>
    <section class="pdp" id="p1">
      <h2 class="pdp__name">Trail Runner Pro</h2>
      <div class="pdp__buy">
        <div class="pdp__pricing">
          <strong class="pdp__amount" itemprop="price">$129.99</strong>
          <s class="pdp__was">$159.99</s>
        </div>
        <button>Add to cart</button>
      </div>
      <p class="pdp__blurb">Lightweight trail running shoe.</p>
    </section>
  </main>
  <footer><span class="copyright">(c) Shop</span></footer>
</body></html>
"""


class TestAdaptiveSelectors:
    def test_literal_selector_still_works_and_saves(self):
        store = AdaptiveStore(":memory:")
        page = resp_to_parsed(make_response(ORIGINAL).response, store)
        got = page.css("span.product-price::text", auto_save=True, identifier="price").get()
        assert got == "$129.99"
        assert store.identifiers("shop.example") == [("shop.example", "price")]

    def test_survives_redesign(self):
        store = AdaptiveStore(":memory:")
        old = resp_to_parsed(make_response(ORIGINAL).response, store)
        old.css("span.product-price", auto_save=True, identifier="price")

        new = resp_to_parsed(make_response(REDESIGNED).response, store)
        # The literal selector is now dead...
        assert not new.css("span.product-price")
        # ...but the adaptive lookup finds the price, not the struck-through old price.
        found = new.css("span.product-price", adaptive=True, identifier="price")
        assert found.adaptive_used is True
        assert len(found) == 1
        assert found[0].text == "$129.99"
        assert found[0].tag == "strong"

    def test_adaptive_text_and_attr_pseudo_elements(self):
        store = AdaptiveStore(":memory:")
        old = resp_to_parsed(make_response(ORIGINAL).response, store)
        old.css("span.product-price", auto_save=True, identifier="price")
        new = resp_to_parsed(make_response(REDESIGNED).response, store)
        assert (
            new.css("span.product-price::text", adaptive=True, identifier="price").get()
            == "$129.99"
        )
        assert (
            new.css("span.product-price::attr(itemprop)", adaptive=True, identifier="price").get()
            == "price"
        )

    def test_does_not_hallucinate_when_element_is_gone(self):
        store = AdaptiveStore(":memory:")
        old = resp_to_parsed(make_response(ORIGINAL).response, store)
        old.css("span.product-price", auto_save=True, identifier="price")
        unrelated = "<html><body><ul><li>alpha</li><li>beta</li></ul></body></html>"
        new = resp_to_parsed(make_response(unrelated).response, store)
        found = new.css("span.product-price", adaptive=True, identifier="price")
        assert len(found) == 0
        assert found.adaptive_used is False

    @staticmethod
    def _listing(n: int, redesigned: bool, *, prices: bool = True) -> str:
        rows = []
        for i in range(n):
            if redesigned:
                price = f'<b class="amt">${i}.99</b>' if prices else ""
                rows.append(
                    f'<article class="c{i % 7}"><div class="w"><h3 class="ttl">Product {i}</h3>'
                    f'<p class="d">desc {i} lorem ipsum dolor</p>{price}</div></article>'
                )
            else:
                rows.append(
                    f'<div class="item"><h2 class="title">Product {i}</h2>'
                    f'<p class="desc">desc {i} lorem ipsum dolor</p><span class="price">${i}.99</span></div>'
                )
        return "<html><body><main id='m'>" + "".join(rows) + "</main></body></html>"

    def test_total_redesign_of_a_long_list_finds_the_right_item(self):
        """Every tag and class changed; only text + position survive. The correct
        element must still win over 49 structurally-identical decoys."""
        store = AdaptiveStore(":memory:")
        q = "div.item:nth-of-type(3) span.price"
        resp_to_parsed(make_response(self._listing(50, False)).response, store).css(
            q, auto_save=True, identifier="p"
        )
        found = resp_to_parsed(make_response(self._listing(50, True)).response, store).css(
            q, adaptive=True, identifier="p"
        )
        assert [n.text for n in found] == ["$2.99"]
        assert found.adaptive_scores and found.adaptive_scores[0] >= 0.40

    def test_removed_element_on_similar_page_is_not_hallucinated(self):
        store = AdaptiveStore(":memory:")
        q = "div.item:nth-of-type(3) span.price"
        resp_to_parsed(make_response(self._listing(50, False)).response, store).css(
            q, auto_save=True, identifier="p"
        )
        gone = resp_to_parsed(make_response(self._listing(50, True, prices=False)).response, store)
        assert len(gone.css(q, adaptive=True, identifier="p")) == 0

    def test_relocation_is_fast_on_large_pages(self):
        import time

        store = AdaptiveStore(":memory:")
        q = "div.item:nth-of-type(3) span.price"
        resp_to_parsed(make_response(self._listing(1000, False)).response, store).css(
            q, auto_save=True, identifier="p"
        )
        big = resp_to_parsed(
            make_response(self._listing(1000, True)).response, store
        )  # ~5000 elements
        t0 = time.perf_counter()
        found = big.css(q, adaptive=True, identifier="p")
        elapsed = time.perf_counter() - t0
        assert found[0].text == "$2.99"
        assert elapsed < 3.0, (
            f"relocation took {elapsed:.2f}s (was quadratic before the index rewrite)"
        )

    def test_adaptive_without_saved_signature_is_empty(self):
        store = AdaptiveStore(":memory:")
        page = resp_to_parsed(make_response(REDESIGNED).response, store)
        assert len(page.css("span.nope", adaptive=True, identifier="never-saved")) == 0

    def test_auto_save_and_adaptive_together_refresh_fingerprint(self):
        store = AdaptiveStore(":memory:")
        resp_to_parsed(make_response(ORIGINAL).response, store).css(
            "span.product-price", auto_save=True, adaptive=True, identifier="price"
        )
        new = resp_to_parsed(make_response(REDESIGNED).response, store)
        got = new.css("span.product-price", auto_save=True, adaptive=True, identifier="price")
        assert got.adaptive_used and got[0].text == "$129.99"
        # The stored fingerprint now describes the *redesigned* element.
        saved = store.load("shop.example", "price")[0]
        assert saved.tag == "strong"

    def test_multiple_elements_are_relocated(self):
        store = AdaptiveStore(":memory:")
        old_html = (
            "<div id=l>"
            + "".join(
                f'<div class="row item-{i}"><a class="t" href="/{i}">Item {i}</a></div>'
                for i in range(4)
            )
            + "</div>"
        )
        new_html = (
            "<section id=l>"
            + "".join(
                f'<article class="card"><h3><a class="title" href="/{i}">Item {i}</a></h3></article>'
                for i in range(4)
            )
            + "</section>"
        )
        resp_to_parsed(make_response(old_html).response, store).css(
            "div.row a.t", auto_save=True, identifier="items"
        )
        found = resp_to_parsed(make_response(new_html).response, store).css(
            "div.row a.t", adaptive=True, identifier="items"
        )
        assert [n.text for n in found] == [f"Item {i}" for i in range(4)]

    def test_store_persists_to_disk(self, tmp_path):
        path = tmp_path / "adaptive.sqlite"
        s1 = AdaptiveStore(path)
        resp_to_parsed(make_response(ORIGINAL).response, s1).css(
            "span.product-price", auto_save=True, identifier="price"
        )
        s1.close()
        s2 = AdaptiveStore(path)
        assert s2.load("shop.example", "price")[0].attrs["itemprop"] == "price"
        s2.delete("shop.example", "price")
        assert s2.load("shop.example", "price") == []

    def test_signature_json_roundtrip(self):
        page = make_response(ORIGINAL)
        sig = signature_of(page.css("span.product-price")[0].tree_node())
        assert ElementSignature.from_json(sig.to_json()) == sig

    def test_score_is_one_for_identical_and_lower_for_decoy(self):
        page = make_response(ORIGINAL)
        price = signature_of(page.css("span.product-price")[0].tree_node())
        decoy = signature_of(page.css("span.old-price")[0].tree_node())
        assert score(price, price) == pytest.approx(1.0)
        assert score(price, decoy) < score(price, price)

    def test_attrs_similarity_bounds(self):
        assert attrs_similarity({}, {}) == 1.0
        assert attrs_similarity({"id": "a"}, {"id": "b"}) < 1.0
        assert attrs_similarity({"class": "a b"}, {"class": "a b"}) == 1.0

    def test_relocate_respects_threshold(self):
        page = make_response(REDESIGNED)
        sig = ElementSignature(tag="marquee", attrs={"class": "x"}, text="zzz")
        assert relocate(page._root_tree_node(), [sig], threshold=0.9) == []

    def test_works_with_parsel_backend_tree(self):
        doc = lxml.html.fromstring(ORIGINAL)
        el = doc.xpath("//span[@itemprop='price']")[0]
        sig = signature_of(LxmlNode(el))
        assert sig.tag == "span" and sig.text == "$129.99"


class TestFindSimilar:
    HTML = """
    <ul id="grid">
      <li class="card featured"><a href="/a">A</a></li>
      <li class="card"><a href="/b">B</a></li>
      <li class="card"><a href="/c">C</a></li>
      <li class="ad"><a href="/ad">Ad</a></li>
    </ul>
    <div class="card"><a href="/d">elsewhere</a></div>
    """

    def test_finds_siblings_of_same_shape(self):
        page = make_response(self.HTML)
        first = page.css("li.card")[1]
        similar = first.find_similar()
        texts = sorted(n.text for n in similar)
        assert "C" in texts and "A" in texts
        assert "elsewhere" not in texts  # different tag and path
        assert first.text not in texts  # never returns itself

    def test_threshold_excludes_dissimilar(self):
        page = make_response(self.HTML)
        first = page.css("li.card")[1]
        strict = [n.text for n in first.find_similar(threshold=0.9)]
        assert "Ad" not in strict
        assert "C" in strict

    def test_match_text(self):
        page = make_response(self.HTML)
        b = page.css("li.card")[1]
        loose = {n.text for n in b.find_similar(match_text=False)}
        tight = {n.text for n in b.find_similar(match_text=True, threshold=0.9)}
        assert tight <= loose


class TestSelectorGeneration:
    HTML = """<html><head><title>t</title></head><body>
    <div id="main" class="wrap a"><ul class="list"><li class="item">A</li><li class="item">B</li><li>C</li></ul>
    <p>one</p><p class="x">two</p><p class="x">three</p><span>s</span></div>
    <div id="main"><i>dup id</i></div>
    <div class="weird:cls 9bad ok"><a href="/a">1</a><a href="/b">2</a></div>
    <table><tr><td>1</td><td>2</td></tr><tr><td>3</td><td>4</td></tr></table>
    <section><div><div><div><b>deep</b><b>deep2</b></div></div></div></section>
    </body></html>"""

    def test_every_element_round_trips_via_selectolax(self):
        tree = HTMLParser(self.HTML)
        doc = lxml.html.fromstring(tree.root.html)  # bitscrape re-parses the serialised tree
        checked = 0
        for el in tree.root.traverse():
            if not el.tag or el.tag.startswith(("-", "_", "!")):
                continue
            checked += 1
            node = SelectolaxNode(el)
            for css in (generate_css(node), generate_css(node, full=True)):
                found = tree.css(css)
                assert len(found) == 1 and found[0] == el, (el.tag, css)
            for xp in (generate_xpath(node), generate_xpath(node, full=True)):
                found_x = doc.xpath(xp)
                assert len(found_x) == 1 and found_x[0].tag == el.tag, (el.tag, xp)
        assert checked > 20

    def test_every_element_round_trips_via_lxml(self):
        doc = lxml.html.fromstring(self.HTML)
        for el in doc.iter():
            if not isinstance(el.tag, str):
                continue
            node = LxmlNode(el)
            xp = generate_xpath(node)
            found = doc.xpath(xp)
            assert len(found) == 1 and found[0] is el, (el.tag, xp)
            cssfound = doc.cssselect(generate_css(node))
            assert len(cssfound) == 1 and cssfound[0] is el, (el.tag, generate_css(node))

    def test_prefers_short_id_selector(self):
        page = make_response("<div><p id='only'>x</p><p>y</p></div>")
        node = page.css("p")[0]
        assert node.generate_css_selector() == "#only"
        assert node.generate_xpath_selector() == '//*[@id="only"]'

    def test_duplicate_ids_are_not_used_as_anchor(self):
        page = make_response("<div id='d'>a</div><div id='d'>b</div>")
        second = page.css("div")[1]
        css = second.generate_css_selector()
        assert css != "#d"
        assert [n.text for n in page.css(css)] == ["b"]

    def test_invalid_class_names_are_skipped(self):
        page = make_response(self.HTML)
        node = page.css("a")[1]
        css = node.generate_css_selector()
        assert ":cls" not in css.replace(":nth", "")
        assert [n.text for n in page.css(css)] == ["2"]

    def test_full_selectors_are_positional(self):
        page = make_response(self.HTML)
        node = page.css("td")[3]
        assert node.generate_full_css_selector().startswith("html")
        assert node.generate_full_xpath_selector().startswith("/html[1]/body[1]")

    def test_selector_helpers_navigate(self):
        page = make_response(self.HTML)
        li = page.css("li.item")[0]
        assert li.parent.tag == "ul"
        assert li.next_sibling.text == "B"
        assert li.previous_sibling is None
        assert [c.text for c in li.parent.children] == ["A", "B", "C"]
        assert [n.text for n in li.siblings] == ["B", "C"]
        assert li.re(r"[A-Z]") == ["A"]
        assert li.re_first(r"(\w)") == "A"

    def test_find_by_text_and_regex(self):
        page = make_response(self.HTML)
        assert [n.tag for n in page.find_by_text("deep2")] == ["b"]
        assert len(page.find_by_text("deep", partial=False)) == 1
        assert [n.text for n in page.find_by_regex(r"^t\w+$", tag="p")] == ["two", "three"]
        assert page.find_by_text("DEEP", case_sensitive=True).first is None
