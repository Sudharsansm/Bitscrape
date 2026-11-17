"""
Automatic selector generation
=============================

Given an element, produce a CSS or XPath selector that resolves back to
exactly that element. Useful for teaching a scraper "the thing I clicked"
and for storing a stable handle to an element.

CSS generation is *verified*: every candidate is run against the real
document and accepted only if it matches this element and nothing else. It
tries, in order of brevity and stability::

    #id                      (when the id is a plain identifier and unique)
    tag.class1.class2        (when unique)
    ancestor > tag.cls:nth-of-type(n)   (climbing until unique)

XPath generation is positional by construction (``/html/body/div[2]/p[1]``)
so it is unique without a runtime check; the short form anchors on the
nearest ancestor with an ``id``.
"""

from __future__ import annotations

import re
from typing import Any

from bitscrape.parser.tree import LxmlNode, SelectolaxNode, TreeNode

_IDENT = re.compile(r"^-?[A-Za-z_][A-Za-z0-9_-]*$")


def _is_ident(value: str) -> bool:
    return bool(_IDENT.match(value))


def _css_matches(node: TreeNode, selector: str) -> list[TreeNode] | None:
    """Run ``selector`` against ``node``'s whole document; ``None`` if the
    backend rejects the selector."""
    root = node.root
    try:
        if isinstance(node, SelectolaxNode):
            found = root.raw.css(selector)
            return [SelectolaxNode(n) for n in found]
        if isinstance(node, LxmlNode):
            from lxml.cssselect import CSSSelector  # pragma: no cover - needs cssselect

            return [LxmlNode(e) for e in CSSSelector(selector)(root.raw)]
    except Exception:
        return None
    return None  # pragma: no cover


def _unique(node: TreeNode, selector: str) -> bool:
    found = _css_matches(node, selector)
    return found is not None and len(found) == 1 and found[0].key == node.key


def _classes(node: TreeNode) -> list[str]:
    return [c for c in node.attrs.get("class", "").split() if _is_ident(c)]


def _segment(node: TreeNode, *, with_classes: bool, nth: bool) -> str:
    seg = node.tag
    if with_classes:
        for c in _classes(node):
            seg += f".{c}"
    if nth:
        seg += f":nth-of-type({node.index_among_same_tag})"
    return seg


def generate_css(node: TreeNode, *, full: bool = False) -> str:
    """Return a CSS selector for ``node``.

    ``full=True`` returns the complete positional path from the root
    (``html > body > div:nth-of-type(2) > p:nth-of-type(1)``), which is
    unique but brittle; the default returns the *shortest* verified-unique
    selector.
    """
    chain = [*node.ancestors(), node]

    if full:
        return " > ".join(_segment(n, with_classes=False, nth=True) for n in chain)

    # 1. #id
    ident = node.attrs.get("id", "")
    if ident and _is_ident(ident):
        cand = f"#{ident}"
        if _unique(node, cand):
            return cand

    # 2. tag.classes
    cand = _segment(node, with_classes=True, nth=False)
    if _unique(node, cand):
        return cand

    # 3. climb until unique. Each level contributes tag + classes (+ nth-of-type
    #    only when needed to disambiguate among same-tag siblings).
    parts: list[str] = []
    for n in reversed(chain):
        needs_nth = any(s.tag == n.tag and s.key != n.key for s in n.siblings)
        anchor_id = n.attrs.get("id", "")
        if n is not node and anchor_id and _is_ident(anchor_id):
            parts.insert(0, f"#{anchor_id}")
            sel = " > ".join(parts)
            if _unique(node, sel):
                return sel
            continue
        parts.insert(0, _segment(n, with_classes=True, nth=needs_nth))
        sel = " > ".join(parts)
        if _unique(node, sel):
            return sel
    return generate_css(node, full=True)  # always unique


def _xpath_literal(value: str) -> str:
    if '"' not in value:
        return f'"{value}"'
    if "'" not in value:
        return f"'{value}'"
    parts = value.split('"')
    return "concat(" + ", '\"', ".join(f'"{p}"' for p in parts) + ")"


def _xpath_step(node: TreeNode) -> str:
    same = [s for s in node.siblings if s.tag == node.tag]
    if len(same) > 1:
        return f"{node.tag}[{node.index_among_same_tag}]"
    return node.tag


def generate_xpath(node: TreeNode, *, full: bool = False) -> str:
    """Return an XPath for ``node``.

    ``full=True`` gives the absolute path from the root with an index on
    every step that has same-tag siblings *or* always for exactness
    (``/html/body/div[2]/p[1]``). The short form anchors on the nearest
    ancestor (or the node itself) that has an ``id``.
    """
    chain = [*node.ancestors(), node]
    if full:
        return "/" + "/".join(f"{n.tag}[{n.index_among_same_tag}]" for n in chain)

    # ids are only trustworthy anchors when they are unique in the document
    # (real-world HTML frequently repeats them).
    id_counts: dict[str, int] = {}
    for el in [chain[0].root, *chain[0].root.descendants()]:
        i_ = el.attrs.get("id", "")
        if i_:
            id_counts[i_] = id_counts.get(i_, 0) + 1

    for i in range(len(chain) - 1, -1, -1):
        ident = chain[i].attrs.get("id", "")
        if ident and id_counts.get(ident, 0) == 1:
            head = f"//*[@id={_xpath_literal(ident)}]"
            tail = "/".join(_xpath_step(n) for n in chain[i + 1 :])
            return head + (f"/{tail}" if tail else "")
    return "/" + "/".join(_xpath_step(n) for n in chain)


def generate_selector(node: Any, backend: str = "selectolax", *, xpath: bool = False) -> str:
    """Convenience wrapper accepting a raw backend node."""
    from bitscrape.parser.tree import wrap

    tn = wrap(node, backend)
    return generate_xpath(tn) if xpath else generate_css(tn)
