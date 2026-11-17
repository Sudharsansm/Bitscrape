"""
Structured data extraction
==========================

Most commercial and editorial pages describe themselves in machine-readable
form. Reading that is far more stable than scraping the visible markup, so
Bitscrape extracts every common flavour in one call::

    data = extract_structured(html, base_url="https://shop.example/p/1")
    data.json_ld          # [{"@type": "Product", "name": ..., "offers": {...}}]
    data.microdata        # [{"type": [...], "properties": {...}}]
    data.rdfa             # [{"type": [...], "properties": {...}}]
    data.opengraph        # {"og:title": ..., "og:image": ...}
    data.twitter          # {"twitter:card": ...}
    data.meta             # {"description": ..., "keywords": ..., "title": ...}
    data.links            # canonical / alternate / hreflang / feeds / icons
    data.find_type("Product")   # every item of a schema.org type, any syntax

Implementation notes: no third-party dependency (lxml only). JSON-LD parsing
is *tolerant* -- real pages ship trailing commas, ``<!-- -->`` wrappers, CDATA
and raw newlines inside strings, all of which are repaired before giving up.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urljoin

from bitscrape.content import parse_html

_URL_ATTR_BY_TAG = {
    "a": "href",
    "area": "href",
    "link": "href",
    "audio": "src",
    "embed": "src",
    "iframe": "src",
    "img": "src",
    "source": "src",
    "track": "src",
    "video": "src",
    "object": "data",
}


@dataclass
class StructuredData:
    """Everything machine-readable found on one page."""

    json_ld: list[dict[str, Any]] = field(default_factory=list)
    microdata: list[dict[str, Any]] = field(default_factory=list)
    rdfa: list[dict[str, Any]] = field(default_factory=list)
    opengraph: dict[str, Any] = field(default_factory=dict)
    twitter: dict[str, str] = field(default_factory=dict)
    meta: dict[str, str] = field(default_factory=dict)
    links: dict[str, list[dict[str, str]]] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "json_ld": self.json_ld,
            "microdata": self.microdata,
            "rdfa": self.rdfa,
            "opengraph": self.opengraph,
            "twitter": self.twitter,
            "meta": self.meta,
            "links": self.links,
        }

    def __bool__(self) -> bool:
        return any(
            (self.json_ld, self.microdata, self.rdfa, self.opengraph, self.twitter, self.meta)
        )

    def find_type(self, type_name: str) -> list[dict[str, Any]]:
        """All items whose type is (or ends with) ``type_name`` (e.g.
        ``"Product"`` matches ``"https://schema.org/Product"``), from JSON-LD
        (including nested nodes), Microdata and RDFa. Microdata/RDFa items are
        normalised to a flat ``{"@type": ..., **properties}`` shape."""
        wanted = type_name.lower()
        found: list[dict[str, Any]] = []

        def matches(t: Any) -> bool:
            types = t if isinstance(t, list) else [t]
            return any(isinstance(x, str) and x.rstrip("/").lower().endswith(wanted) for x in types)

        def walk_ld(node: Any) -> None:
            if isinstance(node, dict):
                if matches(node.get("@type")):
                    found.append(node)
                for v in node.values():
                    walk_ld(v)
            elif isinstance(node, list):
                for v in node:
                    walk_ld(v)

        walk_ld(self.json_ld)

        def walk_items(items: list[dict[str, Any]]) -> None:
            for it in items:
                if matches(it.get("type")):
                    flat: dict[str, Any] = {"@type": it.get("type")}
                    for k, v in it.get("properties", {}).items():
                        flat[k] = v[0] if isinstance(v, list) and len(v) == 1 else v
                    found.append(flat)
                for vals in it.get("properties", {}).values():
                    for v in vals if isinstance(vals, list) else [vals]:
                        if isinstance(v, dict) and "properties" in v:
                            walk_items([v])

        walk_items(self.microdata)
        walk_items(self.rdfa)
        return found


# ---------------------------------------------------------------------------
# JSON-LD
# ---------------------------------------------------------------------------

_TRAILING_COMMA = re.compile(r",\s*([}\]])")
_CDATA = re.compile(r"^\s*(?:<!--)?\s*(?://)?\s*<!\[CDATA\[(.*?)\]\]>\s*(?:-->)?\s*$", re.S)


def _loads_tolerant(raw: str) -> Any:
    text = raw.strip()
    m = _CDATA.match(text)
    if m:
        # "//<![CDATA[ ... //]]>" leaves a dangling line-comment marker behind.
        text = re.sub(r"\s*//\s*$", "", m.group(1).strip()).strip()
    text = re.sub(r"^\s*<!--", "", text)
    text = re.sub(r"-->\s*$", "", text).strip()
    try:
        return json.loads(text)
    except ValueError:
        pass
    repaired = _TRAILING_COMMA.sub(r"\1", text)
    try:
        return json.loads(repaired)
    except ValueError:
        pass
    try:  # raw control characters (newlines/tabs) inside strings
        return json.loads(repaired, strict=False)
    except ValueError:
        return None


def _flatten_ld(data: Any) -> list[dict[str, Any]]:
    """Top-level JSON-LD nodes; ``@graph`` containers are expanded."""
    out: list[dict[str, Any]] = []
    if isinstance(data, list):
        for d in data:
            out.extend(_flatten_ld(d))
    elif isinstance(data, dict):
        graph = data.get("@graph")
        if isinstance(graph, list):
            ctx = data.get("@context")
            for node in graph:
                if isinstance(node, dict) and ctx is not None and "@context" not in node:
                    node = {"@context": ctx, **node}
                out.extend(_flatten_ld(node))
            rest = {k: v for k, v in data.items() if k != "@graph"}
            if set(rest) - {"@context"}:
                out.append(rest)
        else:
            out.append(data)
    return out


def extract_json_ld(tree: Any) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for script in tree.xpath(
        "//script[translate(@type,'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz')"
        "='application/ld+json']"
    ):
        parsed = _loads_tolerant(script.text_content() or "")
        if parsed is not None:
            items.extend(_flatten_ld(parsed))
    return items


# ---------------------------------------------------------------------------
# Microdata
# ---------------------------------------------------------------------------


def _text(el: Any) -> str:
    return re.sub(r"\s+", " ", el.text_content()).strip()


def _md_value(el: Any, base_url: str) -> Any:
    if el.get("itemscope") is not None:
        return _md_item(el, base_url, set())
    tag = el.tag
    if tag == "meta":
        return el.get("content", "")
    if tag in _URL_ATTR_BY_TAG:
        raw = el.get(_URL_ATTR_BY_TAG[tag], "")
        return urljoin(base_url, raw) if base_url and raw else raw
    if tag in ("data", "meter"):
        return el.get("value", "")
    if tag == "time":
        return el.get("datetime") or _text(el)
    return _text(el)


def _md_item(scope: Any, base_url: str, seen: set[Any]) -> dict[str, Any]:
    item: dict[str, Any] = {"properties": {}}
    itemtype = scope.get("itemtype")
    if itemtype:
        types = itemtype.split()
        item["type"] = types
    if scope.get("itemid"):
        item["id"] = urljoin(base_url, scope.get("itemid")) if base_url else scope.get("itemid")

    roots = [scope]
    refs = (scope.get("itemref") or "").split()
    doc = scope.getroottree()
    for ref in refs:
        found = doc.xpath(f"//*[@id={_xp_lit(ref)}]")
        roots.extend(found)

    def visit(node: Any, top: bool) -> None:
        for child in node:
            if not isinstance(child.tag, str):
                continue
            has_prop = child.get("itemprop") is not None
            if has_prop:
                value = _md_value(child, base_url)
                for name in child.get("itemprop", "").split():
                    item["properties"].setdefault(name, []).append(value)
            if child.get("itemscope") is not None:
                continue  # nested item: its own properties belong to it
            visit(child, False)

    for r in roots:
        if r is not scope and r.get("itemprop") is not None:
            value = _md_value(r, base_url)
            for name in r.get("itemprop", "").split():
                item["properties"].setdefault(name, []).append(value)
        if r is scope or r.get("itemscope") is None:
            visit(r, True)
    return item


def _xp_lit(v: str) -> str:
    return f'"{v}"' if '"' not in v else f"'{v}'"


def extract_microdata(tree: Any, base_url: str = "") -> list[dict[str, Any]]:
    out = []
    for scope in tree.xpath("//*[@itemscope and not(@itemprop)]"):
        out.append(_md_item(scope, base_url, set()))
    return out


# ---------------------------------------------------------------------------
# RDFa (Lite subset: vocab / typeof / property / resource / content / href / src)
# ---------------------------------------------------------------------------


def _rdfa_value(el: Any, base_url: str) -> Any:
    if el.get("typeof") is not None:
        return _rdfa_item(el, base_url, el.get("vocab", ""))
    if el.get("content") is not None:
        return el.get("content")
    for attr in ("resource", "href", "src"):
        v = el.get(attr)
        if v:
            return urljoin(base_url, v) if base_url else v
    if el.tag == "time" and el.get("datetime"):
        return el.get("datetime")
    return _text(el)


def _rdfa_item(scope: Any, base_url: str, vocab: str) -> dict[str, Any]:
    vocab = scope.get("vocab") or vocab
    types = [
        t if ":" in t or not vocab else vocab.rstrip("/#") + "/" + t
        for t in (scope.get("typeof") or "").split()
    ]
    item: dict[str, Any] = {"type": types, "properties": {}}
    if scope.get("resource") or scope.get("about"):
        item["id"] = scope.get("resource") or scope.get("about")

    def visit(node: Any) -> None:
        for child in node:
            if not isinstance(child.tag, str):
                continue
            if child.get("property") is not None:
                value = _rdfa_value(child, base_url)
                for name in child.get("property", "").split():
                    item["properties"].setdefault(name, []).append(value)
                if child.get("typeof") is not None:
                    continue
            elif child.get("typeof") is not None:
                continue
            visit(child)

    visit(scope)
    return item


def extract_rdfa(tree: Any, base_url: str = "") -> list[dict[str, Any]]:
    out = []
    for scope in tree.xpath("//*[@typeof]"):
        # only top-level typeof (not nested inside another typeof's property)
        anc = scope.getparent()
        nested = False
        while anc is not None:
            if isinstance(anc.tag, str) and anc.get("typeof") is not None:
                nested = True
                break
            anc = anc.getparent()
        if nested:
            continue
        out.append(_rdfa_item(scope, base_url, ""))
    return out


# ---------------------------------------------------------------------------
# OpenGraph / Twitter / meta / links
# ---------------------------------------------------------------------------

_OG_PREFIXES = ("og:", "article:", "book:", "profile:", "music:", "video:", "product:", "fb:")


def extract_meta(
    tree: Any, base_url: str = ""
) -> tuple[dict[str, Any], dict[str, str], dict[str, str]]:
    og: dict[str, Any] = {}
    tw: dict[str, str] = {}
    meta: dict[str, str] = {}
    title = tree.xpath("string(//head/title)") or tree.xpath("string(//title)")
    if title and title.strip():
        meta["title"] = re.sub(r"\s+", " ", title).strip()
    for m in tree.xpath("//meta"):
        content = m.get("content")
        if content is None:
            continue
        key = (m.get("property") or m.get("name") or m.get("itemprop") or "").strip()
        if not key:
            continue
        lk = key.lower()
        if lk.startswith(_OG_PREFIXES):
            if lk in og:  # repeated tags (e.g. og:image) become lists
                cur = og[lk]
                og[lk] = [*cur, content] if isinstance(cur, list) else [cur, content]
            else:
                og[lk] = content
        elif lk.startswith("twitter:"):
            tw[lk] = content
        else:
            meta.setdefault(lk, content)
    return og, tw, meta


def extract_links(tree: Any, base_url: str = "") -> dict[str, list[dict[str, str]]]:
    out: dict[str, list[dict[str, str]]] = {}
    for ln in tree.xpath("//link[@rel and @href]"):
        for rel in ln.get("rel", "").lower().split():
            entry = {
                "href": urljoin(base_url, ln.get("href", "")) if base_url else ln.get("href", "")
            }
            for attr in ("hreflang", "type", "title", "media", "sizes"):
                if ln.get(attr):
                    entry[attr] = ln.get(attr)
            out.setdefault(rel, []).append(entry)
    return out


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def extract_structured(html: str | bytes, base_url: str = "") -> StructuredData:
    """Extract all structured data from an HTML document."""
    if isinstance(html, bytes):
        html = html.decode("utf-8", errors="replace")
    if not html.strip():
        return StructuredData()
    try:
        tree = parse_html(html)
    except ValueError:
        return StructuredData()
    # <base href> overrides the document URL for relative-link resolution
    base = tree.xpath("string(//base/@href)")
    if base:
        base_url = urljoin(base_url, base) if base_url else base
    og, tw, meta = extract_meta(tree, base_url)
    return StructuredData(
        json_ld=extract_json_ld(tree),
        microdata=extract_microdata(tree, base_url),
        rdfa=extract_rdfa(tree, base_url),
        opengraph=og,
        twitter=tw,
        meta=meta,
        links=extract_links(tree, base_url),
    )
