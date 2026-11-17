"""
Tree adapters
=============

Bitscrape's selector features (adaptive relocation, find-similar, selector
generation) need to walk a DOM: parent, siblings, children, text, attributes.
The DOM can come from two backends -- *selectolax* (fast CSS, the default) or
*lxml* (behind parsel, used for XPath). Rather than write every algorithm
twice, they are written once against the tiny :class:`TreeNode` interface
below, with one implementation per backend.

Only *element* nodes are exposed: text nodes, comments and doctype nodes are
filtered out, so "the next sibling" always means the next element.
"""

from __future__ import annotations

import re
from collections.abc import Hashable, Iterator
from typing import Any

_WS = re.compile(r"\s+")

# Elements that never carry user-visible content and are never useful targets.
NON_CONTENT_TAGS = frozenset({"script", "style", "noscript", "head", "meta", "link", "title"})


def normalize_text(text: str | None) -> str:
    """Collapse runs of whitespace and strip -- the canonical form used when
    comparing element text."""
    if not text:
        return ""
    return _WS.sub(" ", text).strip()


class TreeNode:
    """Backend-independent view of one *element* node."""

    __slots__ = ()

    # -- identity --------------------------------------------------------
    @property
    def key(self) -> Hashable:  # pragma: no cover - interface
        raise NotImplementedError

    @property
    def raw(self) -> Any:  # pragma: no cover - interface
        raise NotImplementedError

    # -- structure -------------------------------------------------------
    @property
    def tag(self) -> str:  # pragma: no cover - interface
        raise NotImplementedError

    @property
    def attrs(self) -> dict[str, str]:  # pragma: no cover - interface
        raise NotImplementedError

    @property
    def parent(self) -> TreeNode | None:  # pragma: no cover - interface
        raise NotImplementedError

    @property
    def children(self) -> list[TreeNode]:  # pragma: no cover - interface
        raise NotImplementedError

    def descendants(self) -> Iterator[TreeNode]:  # pragma: no cover - interface
        raise NotImplementedError

    # -- text ------------------------------------------------------------
    def text(self) -> str:  # pragma: no cover - interface
        """Normalised text of the whole subtree."""
        raise NotImplementedError

    def own_text(self) -> str:  # pragma: no cover - interface
        """Normalised text directly inside this element (not descendants)."""
        raise NotImplementedError

    # -- derived helpers (backend independent) ---------------------------
    @property
    def siblings(self) -> list[TreeNode]:
        p = self.parent
        return p.children if p is not None else [self]

    @property
    def index_in_parent(self) -> int:
        """0-based position among *all* element siblings."""
        for i, sib in enumerate(self.siblings):
            if sib.key == self.key:
                return i
        return 0

    @property
    def index_among_same_tag(self) -> int:
        """1-based position among siblings sharing this tag (XPath/CSS
        ``nth-of-type`` semantics)."""
        n = 0
        for sib in self.siblings:
            if sib.tag == self.tag:
                n += 1
            if sib.key == self.key:
                return n
        return max(n, 1)

    def ancestors(self) -> list[TreeNode]:
        """Ancestors from the root down to (excluding) this node."""
        chain: list[TreeNode] = []
        cur = self.parent
        while cur is not None:
            chain.append(cur)
            cur = cur.parent
        chain.reverse()
        return chain

    @property
    def root(self) -> TreeNode:
        cur: TreeNode = self
        while cur.parent is not None:
            cur = cur.parent
        return cur

    @property
    def depth(self) -> int:
        return len(self.ancestors())

    def __eq__(self, other: object) -> bool:
        return isinstance(other, TreeNode) and self.key == other.key

    def __hash__(self) -> int:
        return hash(self.key)

    def __repr__(self) -> str:
        return f"<{self.__class__.__name__} {self.tag} {self.attrs!r}>"


# ---------------------------------------------------------------------------
# selectolax
# ---------------------------------------------------------------------------


def _sl_is_element(node: Any) -> bool:
    tag = node.tag
    return bool(tag) and not tag.startswith(("-", "_", "!"))


class SelectolaxNode(TreeNode):
    __slots__ = ("_n",)

    def __init__(self, node: Any) -> None:
        self._n = node

    @property
    def key(self) -> Hashable:
        return self._n.mem_id

    @property
    def raw(self) -> Any:
        return self._n

    @property
    def tag(self) -> str:
        return str(self._n.tag)

    @property
    def attrs(self) -> dict[str, str]:
        return {k: (v if v is not None else "") for k, v in self._n.attributes.items()}

    @property
    def parent(self) -> TreeNode | None:
        p = self._n.parent
        # selectolax exposes the synthetic "#document" node above <html>.
        while p is not None and not _sl_is_element(p):
            p = p.parent
        return SelectolaxNode(p) if p is not None else None

    @property
    def children(self) -> list[TreeNode]:
        return [SelectolaxNode(c) for c in self._n.iter() if _sl_is_element(c)]

    def descendants(self) -> Iterator[TreeNode]:
        it = self._n.traverse()
        first = True
        for n in it:
            if first:  # traverse() yields the node itself first
                first = False
                continue
            if _sl_is_element(n):
                yield SelectolaxNode(n)

    def text(self) -> str:
        try:
            return normalize_text(self._n.text(deep=True, separator=" ", strip=True))
        except TypeError:  # very old selectolax
            return normalize_text(self._n.text())

    def own_text(self) -> str:
        parts: list[str] = []
        for c in self._n.iter(include_text=True):
            if c.tag == "-text":
                parts.append(c.text_content or "")
        return normalize_text(" ".join(parts))


# ---------------------------------------------------------------------------
# lxml
# ---------------------------------------------------------------------------


def _lx_is_element(el: Any) -> bool:
    return isinstance(el.tag, str)


class LxmlNode(TreeNode):
    __slots__ = ("_e",)

    def __init__(self, element: Any) -> None:
        self._e = element

    @property
    def key(self) -> Hashable:
        return self._e

    @property
    def raw(self) -> Any:
        return self._e

    @property
    def tag(self) -> str:
        return str(self._e.tag).lower()

    @property
    def attrs(self) -> dict[str, str]:
        return {str(k): str(v) for k, v in self._e.attrib.items()}

    @property
    def parent(self) -> TreeNode | None:
        p = self._e.getparent()
        return LxmlNode(p) if p is not None else None

    @property
    def children(self) -> list[TreeNode]:
        return [LxmlNode(c) for c in self._e if _lx_is_element(c)]

    def descendants(self) -> Iterator[TreeNode]:
        for el in self._e.iterdescendants():
            if _lx_is_element(el):
                yield LxmlNode(el)

    def text(self) -> str:
        return normalize_text(" ".join(self._e.itertext()))

    def own_text(self) -> str:
        parts = [self._e.text or ""]
        parts.extend(c.tail or "" for c in self._e if _lx_is_element(c))
        return normalize_text(" ".join(parts))


def wrap(node: Any, backend: str) -> TreeNode:
    """Wrap a raw selectolax Node / parsel Selector / lxml element."""
    if backend == "selectolax":
        return SelectolaxNode(node)
    root = getattr(node, "root", node)  # parsel Selector -> lxml element
    return LxmlNode(root)
