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
from typing import Any, Iterator

from bitscrape.core.models import Response

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

    def __init__(self, node: Any, *, backend: str = "selectolax") -> None:
        self._node = node
        self._backend = backend

    # ------------------------------------------------------------------
    # Selector methods
    # ------------------------------------------------------------------

    def css(self, query: str) -> "SelectorList":
        return _css_on_node(self._node, query, self._backend)

    def xpath(self, query: str) -> "SelectorList":
        return _xpath_on_node(self._node, query, self._backend)

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

    def __repr__(self) -> str:
        try:
            tag = (
                self._node.tag if self._backend == "selectolax" else self._node.root.tag
            )
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

    def css(self, query: str) -> "SelectorList":
        results: list[Any] = []
        for item in self._items:
            if isinstance(item, NodeSelector):
                results.extend(item.css(query)._items)
        return SelectorList(results, is_text=_is_text_query(query))

    def xpath(self, query: str) -> "SelectorList":
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

    def __init__(self, response: Response) -> None:
        self._response = response
        self._tree: Any = None
        self._backend: str = "none"

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
                except ImportError:
                    raise ImportError(
                        "Install selectolax or parsel: pip install selectolax"
                    )
        return self._tree

    # ------------------------------------------------------------------
    # Public selectors
    # ------------------------------------------------------------------

    def css(self, query: str) -> SelectorList:
        """
        CSS selector with ``::text`` and ``::attr(name)`` pseudo-element support.
        Returns a SelectorList that is iterable and supports chained ``.css()``.
        """
        tree = self._get_tree()
        return _css_on_node(tree, query, self._backend)

    def xpath(self, query: str) -> SelectorList:
        """XPath selector — requires parsel (``pip install parsel``)."""
        tree = self._get_tree()
        return _xpath_on_node(tree, query, self._backend)

    def __repr__(self) -> str:
        return f"<ParsedResponse url={self.url!r} status={self.status}>"


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _is_text_query(query: str) -> bool:
    return query.endswith("::text") or "::attr(" in query


def _css_on_node(node: Any, query: str, backend: str) -> SelectorList:
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
            [NodeSelector(n, backend="selectolax") for n in matched],
            is_text=False,
        )

    else:  # parsel
        if text_pseudo or attr_pseudo:
            # parsel handles pseudo-elements natively
            return SelectorList(node.css(query).getall(), is_text=True)
        matched = node.css(css_query)
        return SelectorList(
            [NodeSelector(n, backend="parsel") for n in matched],
            is_text=False,
        )


def _xpath_on_node(node: Any, query: str, backend: str) -> SelectorList:
    """Apply an XPath query — always uses parsel/lxml."""
    if backend == "parsel":
        matched = node.xpath(query)
        # If results are strings (e.g. text()) return them directly
        items: list[Any] = []
        for m in matched:
            if isinstance(m, str):
                items.append(m)
            else:
                items.append(NodeSelector(m, backend="parsel"))
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
                items.append(NodeSelector(m, backend="parsel"))
        return SelectorList(items, is_text=False)
    except ImportError:
        raise ImportError("XPath requires parsel: pip install parsel")
