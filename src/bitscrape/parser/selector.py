"""
Bitscrape Parser
================
Wraps a Response and provides CSS/XPath selectors via selectolax (fast C-backed
parser) with parsel as a fallback for full XPath support.

Key design:
  - ParsedResponse  — wraps a full Response; call .css() / .xpath() on it
  - NodeSelector    — wraps a *single* selectolax/parsel node so you can chain
                      .css() / .xpath() calls on individual matched elements
  - SelectorList    — list of NodeSelectors (or plain strings for text/attr results)
                      supports iteration (``for item in response.css("div.quote")``)
                      as well as .get() / .getall()
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable, Iterator
from re import Pattern
from typing import TYPE_CHECKING, Any
from urllib.parse import urlparse

from bitscrape.core.models import Response

if TYPE_CHECKING:
    from bitscrape.parser.adaptive import AdaptiveStore
    from bitscrape.parser.structured import StructuredData
    from bitscrape.parser.tree import TreeNode

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# NodeSelector  —  wraps ONE node, supports chained .css() / .xpath()
# ---------------------------------------------------------------------------


class NodeSelector:
    """
    Wraps a single HTML node (selectolax Node or parsel Selector).
    Supports the same .css() / .xpath() / .get() / .getall() API as
    ParsedResponse, so spider code can iterate over matched elements and
    call selectors on each child::

        for quote in response.css("div.quote"):          # iterates NodeSelectors
            text = quote.css("span.text::text").get()    # chained query
    """

    def __init__(self, node: Any, *, backend: str = "selectolax", doc: Any = None) -> None:
        self._node = node
        self._backend = backend
        self._doc = doc  # the owning ParsedResponse, when known

    # ------------------------------------------------------------------
    # Selector methods
    # ------------------------------------------------------------------

    def css(self, query: str) -> SelectorList:
        return _css_on_node(self._node, query, self._backend, self._doc)

    def xpath(self, query: str) -> SelectorList:
        return _xpath_on_node(self._node, query, self._backend, self._doc)

    # ------------------------------------------------------------------
    # Value extraction (when this node IS the result)
    # ------------------------------------------------------------------

    def get(self, default: str | None = None) -> str | None:
        """Return the text content of this node."""
        try:
            if self._backend == "selectolax":
                return self._node.text(strip=True) or default
            else:  # parsel
                return self._node.get(default=default)
        except Exception:
            return default

    def getall(self) -> list[str]:
        return [self.get() or ""]

    # ------------------------------------------------------------------
    # Attribute access
    # ------------------------------------------------------------------

    def attrib(self, name: str, default: str = "") -> str:
        try:
            if self._backend == "selectolax":
                return self._node.attributes.get(name, default)
            else:
                vals = self._node.attrib.get(name, default)
                return vals if vals is not None else default
        except Exception:
            return default

    # ------------------------------------------------------------------
    # Introspection / navigation (added in 0.9)
    # ------------------------------------------------------------------

    def tree_node(self) -> TreeNode:
        """Backend-independent view of this element (see ``bitscrape.parser.tree``)."""
        from bitscrape.parser.tree import wrap

        return wrap(self._node, self._backend)

    def _wrap_tree(self, tn: TreeNode | None) -> NodeSelector | None:
        if tn is None:
            return None
        return NodeSelector(tn.raw, backend=self._backend, doc=self._doc)

    @property
    def tag(self) -> str:
        return self.tree_node().tag

    @property
    def attributes(self) -> dict[str, str]:
        """All attributes as a dict (use :meth:`attrib` for a single one)."""
        return self.tree_node().attrs

    @property
    def text(self) -> str:
        """Normalised text of the whole subtree."""
        return self.tree_node().text()

    def get_all_text(self, separator: str = " ", strip: bool = True) -> str:
        """Text of the whole subtree with a custom separator."""
        if self._backend == "selectolax":
            return str(self._node.text(deep=True, separator=separator, strip=strip))
        return separator.join(t.strip() if strip else t for t in self._node.root.itertext() if t)

    @property
    def html(self) -> str:
        """Outer HTML of this element."""
        if self._backend == "selectolax":
            return str(self._node.html)
        from lxml import etree  # pyright: ignore[reportAttributeAccessIssue]

        return str(etree.tostring(self._node.root, encoding="unicode", method="html"))

    @property
    def parent(self) -> NodeSelector | None:
        return self._wrap_tree(self.tree_node().parent)

    @property
    def children(self) -> SelectorList:
        return SelectorList([n for c in self.tree_node().children if (n := self._wrap_tree(c))])

    @property
    def siblings(self) -> SelectorList:
        me = self.tree_node()
        return SelectorList(
            [n for s_ in me.siblings if s_.key != me.key and (n := self._wrap_tree(s_))]
        )

    @property
    def next_sibling(self) -> NodeSelector | None:
        me = self.tree_node()
        sibs = me.siblings
        i = me.index_in_parent
        return self._wrap_tree(sibs[i + 1]) if i + 1 < len(sibs) else None

    @property
    def previous_sibling(self) -> NodeSelector | None:
        me = self.tree_node()
        i = me.index_in_parent
        return self._wrap_tree(me.siblings[i - 1]) if i > 0 else None

    def re(self, pattern: str | Pattern[str], flags: int = 0) -> list[str]:
        """Regex matches over this element's text (groups if any, else whole match)."""
        return _regex_findall(pattern, self.text, flags)

    def re_first(
        self, pattern: str | Pattern[str], default: str | None = None, flags: int = 0
    ) -> str | None:
        found = self.re(pattern, flags)
        return found[0] if found else default

    def find_similar(
        self,
        threshold: float = 0.2,
        ignore_attributes: tuple[str, ...] = ("href", "src"),
        match_text: bool = False,
    ) -> SelectorList:
        """Other elements shaped like this one (same tag and tree position,
        similar attributes) -- e.g. every product card given one card."""
        from bitscrape.parser.adaptive import find_similar

        found = find_similar(
            self.tree_node(),
            threshold=threshold,
            ignore_attributes=ignore_attributes,
            match_text=match_text,
        )
        return SelectorList([n for f in found if (n := self._wrap_tree(f))])

    def generate_css_selector(self) -> str:
        """Shortest CSS selector that matches exactly this element."""
        from bitscrape.parser.selectorgen import generate_css

        return generate_css(self.tree_node())

    def generate_full_css_selector(self) -> str:
        """Absolute positional CSS path from the root (unique but brittle)."""
        from bitscrape.parser.selectorgen import generate_css

        return generate_css(self.tree_node(), full=True)

    def generate_xpath_selector(self) -> str:
        """Short XPath for this element (anchored on the nearest unique ``id``)."""
        from bitscrape.parser.selectorgen import generate_xpath

        return generate_xpath(self.tree_node())

    def generate_full_xpath_selector(self) -> str:
        """Absolute positional XPath (``/html[1]/body[1]/div[2]/p[1]``)."""
        from bitscrape.parser.selectorgen import generate_xpath

        return generate_xpath(self.tree_node(), full=True)

    def signature(self) -> Any:
        """The :class:`~bitscrape.parser.adaptive.ElementSignature` of this element."""
        from bitscrape.parser.adaptive import signature_of

        return signature_of(self.tree_node())

    def clean_text(self, **kwargs: Any) -> str:
        """This element's text, tidied (whitespace/Unicode/entities); see
        :func:`bitscrape.textutil.clean_text` for the options."""
        from bitscrape.textutil import clean_text

        return clean_text(self.get_all_text(separator=" ", strip=True), **kwargs)

    def numbers(self) -> list[Any]:
        """Numbers in this element's text as ``Decimal``\\ s; see :func:`bitscrape.textutil.extract_numbers`."""
        from bitscrape.textutil import extract_numbers

        return extract_numbers(self.text)

    def price(self) -> tuple[Any, str | None] | None:
        """``(amount, currency)`` of the first price in this element's text, or ``None``."""
        from bitscrape.textutil import parse_price

        return parse_price(self.text)

    def to_markdown(self, **kwargs: Any) -> str:
        """Convert this element's HTML to Markdown (see ``bitscrape.markdown``)."""
        from bitscrape.markdown import html_to_markdown

        return html_to_markdown(self.html, main_content=False, **kwargs)

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, NodeSelector):
            return NotImplemented
        return self.tree_node().key == other.tree_node().key

    def __hash__(self) -> int:
        return hash(self.tree_node().key)

    def __repr__(self) -> str:
        try:
            tag = self._node.tag if self._backend == "selectolax" else self._node.root.tag
        except Exception:
            tag = "?"
        return f"<NodeSelector tag={tag!r}>"


# ---------------------------------------------------------------------------
# SelectorList  —  list of NodeSelectors (or plain strings)
# ---------------------------------------------------------------------------


class SelectorList:
    """
    A list of matched nodes / strings.

    - Iterate to get individual NodeSelectors (for element nodes) or strings.
    - Call .get() for the first match.
    - Call .getall() for all text values.
    - Call .css() / .xpath() to apply a further query to every matched node
      and flatten the results.
    """

    def __init__(self, items: list[Any], *, is_text: bool = False) -> None:
        self._items = items  # list of NodeSelector | str
        self._is_text = is_text
        # True when the elements were relocated by the adaptive engine because
        # the literal selector no longer matched (see ParsedResponse.css).
        self.adaptive_used = False
        # Similarity score (0-1) of each relocated element, in the same order.
        self.adaptive_scores: list[float] = []

    def __repr__(self) -> str:
        return f"<SelectorList n={len(self._items)}>"

    @property
    def first(self) -> Any:
        return self._items[0] if self._items else None

    @property
    def last(self) -> Any:
        return self._items[-1] if self._items else None

    def filter(self, fn: Callable[[Any], bool]) -> SelectorList:
        return SelectorList([i for i in self._items if fn(i)], is_text=self._is_text)

    def map(self, fn: Callable[[Any], Any]) -> list[Any]:
        return [fn(i) for i in self._items]

    def re(self, pattern: str | Pattern[str], flags: int = 0) -> list[str]:
        """Regex over every item's text; flattened list of matches."""
        out: list[str] = []
        for text in self.getall():
            out.extend(_regex_findall(pattern, text, flags))
        return out

    def re_first(
        self, pattern: str | Pattern[str], default: str | None = None, flags: int = 0
    ) -> str | None:
        found = self.re(pattern, flags)
        return found[0] if found else default

    # ------------------------------------------------------------------
    # Iteration — this is what fixes ``for quote in response.css(...)``
    # ------------------------------------------------------------------

    def __iter__(self) -> Iterator[Any]:
        return iter(self._items)

    def __len__(self) -> int:
        return len(self._items)

    def __bool__(self) -> bool:
        return bool(self._items)

    def __getitem__(self, idx: int) -> Any:
        return self._items[idx]

    # ------------------------------------------------------------------
    # Value extraction
    # ------------------------------------------------------------------

    def get(self, default: str | None = None) -> str | None:
        """Return the first matched value or ``default``."""
        if not self._items:
            return default
        item = self._items[0]
        if isinstance(item, str):
            return item if item != "" else default
        if isinstance(item, NodeSelector):
            return item.get(default=default)
        return default

    def getall(self) -> list[str]:
        """Return all matched values as strings."""
        result: list[str] = []
        for item in self._items:
            if isinstance(item, str):
                if item != "":
                    result.append(item)
            elif isinstance(item, NodeSelector):
                v = item.get()
                if v is not None:
                    result.append(v)
        return result

    # ------------------------------------------------------------------
    # Chained selectors (apply query to every node in the list)
    # ------------------------------------------------------------------

    def css(self, query: str) -> SelectorList:
        results: list[Any] = []
        for item in self._items:
            if isinstance(item, NodeSelector):
                results.extend(item.css(query)._items)
        return SelectorList(results, is_text=_is_text_query(query))

    def xpath(self, query: str) -> SelectorList:
        results: list[Any] = []
        for item in self._items:
            if isinstance(item, NodeSelector):
                results.extend(item.xpath(query)._items)
        return SelectorList(results, is_text=True)


# ---------------------------------------------------------------------------
# ParsedResponse  —  top-level entry point, wraps a full Response
# ---------------------------------------------------------------------------


class ParsedResponse:
    """
    Wraps a Response and exposes ``.css()`` and ``.xpath()`` selectors.

    Usage::

        pr = ParsedResponse(response)
        title = pr.css("h1::text").get()

        # Iterate over matched elements and apply sub-selectors:
        for quote in pr.css("div.quote"):
            text   = quote.css("span.text::text").get(default="")
            author = quote.css("small.author::text").get(default="")

        # Attributes:
        links = pr.css("a::attr(href)").getall()
    """

    def __init__(self, response: Response, *, adaptive_store: AdaptiveStore | None = None) -> None:
        self._response = response
        self._tree: Any = None
        self._backend: str = "none"
        self._adaptive_store = adaptive_store
        self._structured: StructuredData | None = None

    # ------------------------------------------------------------------
    # Proxy Response attributes
    # ------------------------------------------------------------------

    @property
    def url(self) -> str:
        return self._response.url

    @property
    def status(self) -> int:
        return self._response.status

    @property
    def request(self) -> Any:
        return self._response.request

    @property
    def text(self) -> str:
        return self._response.text

    @property
    def body(self) -> bytes:
        return self._response.body

    @property
    def headers(self) -> dict[str, str]:
        return self._response.headers

    @property
    def encoding(self) -> str:
        """The response's detected/declared text encoding (used to decode
        ``.body`` into ``.text``)."""
        return self._response.encoding

    @property
    def meta(self) -> dict[str, Any]:
        return self._response.request.meta

    @property
    def ok(self) -> bool:
        return self._response.ok

    @property
    def session_id(self) -> str | None:
        """Id of the session that fetched this page (``None`` for a per-request browser)."""
        return self._response.session_id

    @property
    def sid(self) -> str | None:
        """Alias for :attr:`session_id`."""
        return self._response.session_id

    @property
    def captured_xhr(self) -> list[dict[str, Any]]:
        """Alias for :attr:`xhr` -- background XHR/fetch calls captured by a browser fetch."""
        return self.xhr

    @property
    def response(self) -> Response:
        """The underlying :class:`~bitscrape.core.models.Response`."""
        return self._response

    @property
    def http_version(self) -> str | None:
        return self._response.http_version

    @property
    def extra(self) -> dict[str, Any]:
        """Fetcher extras (captured XHR, screenshots, cookies, ...)."""
        return self._response.extra

    @property
    def xhr(self) -> list[dict[str, Any]]:
        """XHR/fetch calls captured by the browser fetcher (empty otherwise)."""
        extra = self._response.extra
        # The downloader stores captures under "captured_xhr"; "xhr" is the
        # legacy key (it used to be the only one read here, so captured calls
        # never reached callbacks).
        return list(extra.get("captured_xhr") or extra.get("xhr") or [])

    def find_xhr(self, pattern: str | Pattern[str]) -> list[dict[str, Any]]:
        """Captured XHR/fetch calls whose URL matches the regex ``pattern``."""
        rx = re.compile(pattern) if isinstance(pattern, str) else pattern
        return [x for x in self.xhr if rx.search(x.get("url", ""))]

    def json(self) -> Any:
        return self._response.json()

    def urljoin(self, url: str) -> str:
        return self._response.urljoin(url)

    # ------------------------------------------------------------------
    # Lazy tree initialisation
    # ------------------------------------------------------------------

    def _get_tree(self) -> Any:
        if self._tree is None:
            try:
                from selectolax.parser import HTMLParser

                self._tree = HTMLParser(self._response.body)
                self._backend = "selectolax"
            except ImportError:
                try:
                    from parsel import Selector

                    self._tree = Selector(text=self._response.text)
                    self._backend = "parsel"
                except ImportError as err:
                    raise ImportError(
                        "Install selectolax or parsel: pip install selectolax"
                    ) from err
        return self._tree

    # ------------------------------------------------------------------
    # Public selectors
    # ------------------------------------------------------------------

    def css(
        self,
        query: str,
        *,
        auto_save: bool = False,
        adaptive: bool = False,
        identifier: str | None = None,
        threshold: float | None = None,
    ) -> SelectorList:
        """
        CSS selector with ``::text`` and ``::attr(name)`` pseudo-element support.
        Returns a SelectorList that is iterable and supports chained ``.css()``.

        Adaptive mode (survive site redesigns):

        * ``auto_save=True`` -- when the selector matches, remember what the
          matched element(s) look like under ``identifier`` (default: the
          query string).
        * ``adaptive=True`` -- when the selector matches nothing, relocate the
          element(s) saved under ``identifier`` by structural similarity
          (``threshold`` in [0, 1]; default 0.40). ``result.adaptive_used`` is
          then ``True``.
        """
        tree = self._get_tree()
        result = _css_on_node(tree, query, self._backend, self)
        if not (auto_save or adaptive):
            return result
        return self._css_adaptive(query, result, auto_save, adaptive, identifier, threshold)

    def xpath(self, query: str) -> SelectorList:
        """XPath selector — requires parsel (``pip install parsel``)."""
        tree = self._get_tree()
        return _xpath_on_node(tree, query, self._backend, self)

    # ------------------------------------------------------------------
    # Adaptive helpers
    # ------------------------------------------------------------------

    def _site_key(self) -> str:
        return (urlparse(self.url).hostname or "local").lower()

    def _store(self) -> AdaptiveStore:
        if self._adaptive_store is None:
            from bitscrape.parser.adaptive import default_store

            self._adaptive_store = default_store()
        return self._adaptive_store

    def _root_tree_node(self) -> TreeNode:
        from bitscrape.parser.tree import wrap

        tree = self._get_tree()
        raw = tree.root if self._backend == "selectolax" else tree
        return wrap(raw, self._backend)

    def _css_adaptive(
        self,
        query: str,
        result: SelectorList,
        auto_save: bool,
        adaptive: bool,
        identifier: str | None,
        threshold: float | None,
    ) -> SelectorList:
        from bitscrape.parser.adaptive import DEFAULT_THRESHOLD, relocate, signature_of

        base, kind, arg = _split_pseudo(query)
        ident = identifier or query
        site = self._site_key()
        store = self._store()
        tree = self._get_tree()

        # Element matches (independent of any ::text / ::attr pseudo-element).
        if kind is None:
            elems = list(result)
        else:
            elems = list(_css_on_node(tree, base, self._backend, self)) if base else []

        if elems:
            if auto_save:
                store.save(site, ident, [signature_of(e.tree_node()) for e in elems[:50]])
            return result
        if not adaptive:
            return result

        sigs = store.load(site, ident)
        if not sigs:
            logger.debug("adaptive: nothing saved for %s / %s", site, ident)
            return result
        matches = relocate(
            self._root_tree_node(),
            sigs,
            DEFAULT_THRESHOLD if threshold is None else threshold,
        )
        if not matches:
            return result

        nodes = [NodeSelector(tn.raw, backend=self._backend, doc=self) for tn, _ in matches]
        scores = [sc for _, sc in matches]
        if auto_save:  # keep the stored fingerprint current after a redesign
            store.save(site, ident, [signature_of(n.tree_node()) for n in nodes[:50]])
        if kind == "text":
            out = SelectorList([n.get("") or "" for n in nodes], is_text=True)
        elif kind == "attr" and arg:
            out = SelectorList([n.attrib(arg) for n in nodes], is_text=True)
        else:
            out = SelectorList(nodes)
        out.adaptive_used = True
        out.adaptive_scores = scores
        logger.info("adaptive: relocated %d element(s) for %r on %s", len(nodes), ident, site)
        return out

    # ------------------------------------------------------------------
    # Text / regex search, similarity, structured data
    # ------------------------------------------------------------------

    def find_by_text(
        self,
        text: str,
        *,
        partial: bool = True,
        case_sensitive: bool = False,
        tag: str | None = None,
    ) -> SelectorList:
        """Elements whose *own* text contains (or equals) ``text``. Matching
        the element's own text (not descendants') returns the innermost
        element rather than every ancestor."""
        needle = text if case_sensitive else text.lower()
        out: list[Any] = []
        for tn in [self._root_tree_node(), *self._root_tree_node().descendants()]:
            if tag and tn.tag != tag:
                continue
            own = tn.own_text()
            hay = own if case_sensitive else own.lower()
            if (needle in hay) if partial else (needle == hay):
                out.append(NodeSelector(tn.raw, backend=self._backend, doc=self))
        return SelectorList(out)

    def find_by_regex(self, pattern: str | Pattern[str], *, tag: str | None = None) -> SelectorList:
        """Elements whose own text matches ``pattern`` (``re.search``)."""
        rx = re.compile(pattern) if isinstance(pattern, str) else pattern
        out: list[Any] = []
        for tn in [self._root_tree_node(), *self._root_tree_node().descendants()]:
            if tag and tn.tag != tag:
                continue
            if rx.search(tn.own_text()):
                out.append(NodeSelector(tn.raw, backend=self._backend, doc=self))
        return SelectorList(out)

    def find_similar(self, node: NodeSelector, **kwargs: Any) -> SelectorList:
        """Alias of ``node.find_similar(**kwargs)``."""
        return node.find_similar(**kwargs)

    @property
    def structured(self) -> StructuredData:
        """JSON-LD, Microdata, RDFa, OpenGraph, Twitter cards, meta and link
        relations (parsed once, cached). See ``bitscrape.parser.structured``."""
        if self._structured is None:
            from bitscrape.parser.structured import extract_structured

            self._structured = extract_structured(self.text, base_url=self.url)
        return self._structured

    @property
    def json_ld(self) -> list[dict[str, Any]]:
        return self.structured.json_ld

    @property
    def opengraph(self) -> dict[str, Any]:
        return self.structured.opengraph

    def markdown(self, **kwargs: Any) -> str:
        """The page as clean Markdown (main content by default) -- ready for
        LLM/RAG use. See :func:`bitscrape.markdown.html_to_markdown`."""
        from bitscrape.markdown import html_to_markdown

        return html_to_markdown(self.text, base_url=self.url, **kwargs)

    def clean_text(self, **kwargs: Any) -> str:
        """The page's visible text, tidied; see :func:`bitscrape.textutil.clean_text`.
        ``<script>``, ``<style>`` and comments are dropped."""
        from bitscrape.textutil import clean_text

        return clean_text(self.text, strip_html=True, **kwargs)

    def emails(self) -> list[str]:
        """Distinct e-mail addresses in the page text."""
        from bitscrape.textutil import extract_emails, strip_tags

        return extract_emails(strip_tags(self.text))

    def re(self, pattern: Any, flags: int = 0) -> list[str]:
        """Regex matches over the page's raw text (groups if any, else whole match)."""
        return _regex_findall(pattern, self.text, flags)

    def re_first(self, pattern: Any, default: str | None = None, flags: int = 0) -> str | None:
        found = self.re(pattern, flags)
        return found[0] if found else default

    def links(self, **kwargs: Any) -> list[str]:
        """Absolute links on the page (see :class:`bitscrape.crawl.LinkExtractor`)."""
        from bitscrape.crawl.linkextract import LinkExtractor

        return [link.url for link in LinkExtractor(**kwargs).extract_links(self)]

    def __repr__(self) -> str:
        return f"<ParsedResponse url={self.url!r} status={self.status}>"


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _is_text_query(query: str) -> bool:
    return query.endswith("::text") or "::attr(" in query


def _css_on_node(node: Any, query: str, backend: str, doc: Any = None) -> SelectorList:
    """Apply a CSS query to a selectolax node or parsel Selector."""
    text_pseudo = query.endswith("::text")
    attr_pseudo = "::attr(" in query

    if text_pseudo:
        css_query = query[: -len("::text")]
        attr_name = None
    elif attr_pseudo:
        css_query, attr_part = query.rsplit("::attr(", 1)
        attr_name = attr_part.rstrip(")")
    else:
        css_query = query.strip()
        attr_name = None

    if backend == "selectolax":
        matched = node.css(css_query) if css_query else [node]
        if text_pseudo:
            texts = [n.text(strip=True) or "" for n in matched]
            return SelectorList(texts, is_text=True)
        if attr_pseudo and attr_name:
            attrs = [n.attributes.get(attr_name, "") for n in matched]
            return SelectorList(attrs, is_text=True)
        # Return NodeSelectors for element results so they can be iterated
        return SelectorList(
            [NodeSelector(n, backend="selectolax", doc=doc) for n in matched],
            is_text=False,
        )

    else:  # parsel
        if text_pseudo or attr_pseudo:
            # parsel handles pseudo-elements natively
            return SelectorList(node.css(query).getall(), is_text=True)
        matched = node.css(css_query)
        return SelectorList(
            [NodeSelector(n, backend="parsel", doc=doc) for n in matched],
            is_text=False,
        )


def _xpath_on_node(node: Any, query: str, backend: str, doc: Any = None) -> SelectorList:
    """Apply an XPath query — always uses parsel/lxml."""
    if backend == "parsel":
        matched = node.xpath(query)
        # parsel always wraps results in a Selector, even for text()/@attr
        # queries -- the underlying string value lives at `.root`, not the
        # Selector object itself, so `isinstance(m, str)` here was always
        # False (a real bug: text()/@attr XPath queries were silently
        # broken whenever parsel was the *native* backend, i.e. selectolax
        # unavailable -- fixed by checking `m.root` instead of `m`).
        items: list[Any] = []
        for m in matched:
            if isinstance(m.root, str):
                items.append(m.root)
            else:
                items.append(NodeSelector(m, backend="parsel", doc=doc))
        return SelectorList(items, is_text=False)

    # selectolax has no xpath — convert node HTML to parsel
    try:
        from parsel import Selector

        html = node.html if hasattr(node, "html") else str(node)
        sel = Selector(text=html)
        matched = sel.xpath(query)
        items = []
        for m in matched:
            if isinstance(m.get(), str):
                items.append(m.get())
            else:
                items.append(NodeSelector(m, backend="parsel", doc=doc))
        return SelectorList(items, is_text=False)
    except ImportError as err:
        raise ImportError("XPath requires parsel: pip install parsel") from err


def _regex_findall(pattern: str | Pattern[str], text: str, flags: int = 0) -> list[str]:
    """``re.findall`` flattened to strings: the first group if the pattern has
    groups (Scrapy/parsel semantics), else the whole match."""
    rx = re.compile(pattern, flags) if isinstance(pattern, str) else pattern
    out: list[str] = []
    for m in rx.finditer(text):
        if rx.groups:
            groups = [g for g in m.groups() if g is not None]
            out.append(groups[0] if groups else m.group(0))
        else:
            out.append(m.group(0))
    return out


def _split_pseudo(query: str) -> tuple[str, str | None, str | None]:
    """Split ``sel::text`` / ``sel::attr(x)`` into ``(sel, kind, arg)``."""
    if query.endswith("::text"):
        return query[: -len("::text")], "text", None
    if "::attr(" in query:
        base, part = query.rsplit("::attr(", 1)
        return base, "attr", part.rstrip(")")
    return query.strip(), None, None
