"""
Adaptive selectors -- scrapers that survive site redesigns
==========================================================

The problem: a selector like ``div.product-price`` breaks the day the site
renames a class or wraps the price in another ``<div>``. The scraper returns
nothing until a human notices and fixes it.

The idea: the first time a selector works, remember what the matched element
*looked like* (its :class:`ElementSignature`: tag, attributes, text, where it
sat in the tree, its neighbours). When the selector later stops matching,
score every element in the new page against the stored signature and pick the
best match.

    price = response.css("div.product-price", auto_save=True, identifier="price")
    ...
    # after a redesign, the same call still finds the price:
    price = response.css("div.product-price", adaptive=True, identifier="price")

Signatures live in a small SQLite database (:class:`AdaptiveStore`), keyed by
``(site, identifier)``. Scoring is deterministic and explainable: a weighted
mix of tag, attribute, text, tree-path, parent, sibling and child similarity,
each in ``[0, 1]`` (see :data:`WEIGHTS`).
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from dataclasses import asdict, dataclass, field
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any

from bitscrape.parser.tree import NON_CONTENT_TAGS, TreeNode

# Attributes whose values are session/random noise and shouldn't drive matching.
_VOLATILE_ATTRS = frozenset({"style", "nonce", "data-reactid", "data-react-checksum"})
# Attributes that identify an element far better than the rest.
_STRONG_ATTRS = ("id", "name", "itemprop", "data-testid", "data-test", "role", "type", "for")

WEIGHTS: dict[str, float] = {
    "tag": 0.10,
    "attrs": 0.25,
    "text": 0.25,
    "path": 0.15,
    "parent": 0.10,
    "siblings": 0.05,
    "children": 0.10,
}

# Calibrated on measured scores: a correct match after an *extreme* redesign (every
# tag and class changed) scores ~0.52; a removed element on a similar-looking page
# tops out ~0.28 and an unrelated page ~0.20. 0.40 sits in the middle of that gap.
DEFAULT_THRESHOLD = 0.40
_TEXT_LIMIT = 300
_MAX_SIBLINGS = 12


@dataclass
class ElementSignature:
    """A serialisable fingerprint of one element."""

    tag: str
    attrs: dict[str, str] = field(default_factory=dict)
    text: str = ""
    path: list[str] = field(default_factory=list)  # ancestor descriptors, root first
    parent_tag: str = ""
    parent_attrs: dict[str, str] = field(default_factory=dict)
    sibling_tags: list[str] = field(default_factory=list)
    child_tags: list[str] = field(default_factory=list)
    index: int = 0  # 0-based among all element siblings

    def to_json(self) -> str:
        return json.dumps(asdict(self), ensure_ascii=False, sort_keys=True)

    @classmethod
    def from_json(cls, raw: str | dict[str, Any]) -> ElementSignature:
        data = json.loads(raw) if isinstance(raw, str) else raw
        return cls(**data)


def _clean_attrs(attrs: dict[str, str]) -> dict[str, str]:
    return {k: v for k, v in attrs.items() if k not in _VOLATILE_ATTRS}


def _descriptor(node: TreeNode) -> str:
    """``tag#id.cls1.cls2`` -- one ancestor's contribution to the path."""
    a = node.attrs
    d = node.tag
    if a.get("id"):
        d += f"#{a['id']}"
    cls = ".".join(sorted(a.get("class", "").split()))
    if cls:
        d += f".{cls}"
    return d


def signature_of(node: TreeNode) -> ElementSignature:
    """Compute the :class:`ElementSignature` of ``node``."""
    parent = node.parent
    siblings = node.siblings
    return ElementSignature(
        tag=node.tag,
        attrs=_clean_attrs(node.attrs),
        text=node.text()[:_TEXT_LIMIT],
        path=[_descriptor(a) for a in node.ancestors()],
        parent_tag=parent.tag if parent is not None else "",
        parent_attrs=_clean_attrs(parent.attrs) if parent is not None else {},
        sibling_tags=[s.tag for s in siblings if s.key != node.key][:_MAX_SIBLINGS],
        child_tags=[c.tag for c in node.children][:_MAX_SIBLINGS],
        index=node.index_in_parent,
    )


# ---------------------------------------------------------------------------
# Similarity
# ---------------------------------------------------------------------------


def _ratio(a: str, b: str) -> float:
    if not a and not b:
        return 1.0
    if not a or not b:
        return 0.0
    sm = SequenceMatcher(None, a, b, autojunk=False)
    # quick_ratio is an upper bound; skip the expensive exact ratio when even
    # the bound is poor.
    if sm.real_quick_ratio() < 0.3 or sm.quick_ratio() < 0.3:
        return 0.0
    return sm.ratio()


def _jaccard(a: list[str], b: list[str]) -> float:
    if not a and not b:
        return 1.0
    sa, sb = set(a), set(b)
    union = sa | sb
    return len(sa & sb) / len(union) if union else 1.0


def _attr_value_similarity(key: str, a: str, b: str) -> float:
    if key == "class":
        return _jaccard(a.split(), b.split())
    if a == b:
        return 1.0
    return _ratio(a, b) * 0.8  # near-miss values earn partial, never full, credit


def attrs_similarity(a: dict[str, str], b: dict[str, str]) -> float:
    """Weighted similarity of two attribute dicts in ``[0, 1]``. Strong
    identifying attributes (id, name, itemprop, ...) count double."""
    keys = set(a) | set(b)
    if not keys:
        return 1.0
    total = weight = 0.0
    for k in keys:
        w = 2.0 if k in _STRONG_ATTRS else 1.0
        weight += w
        if k in a and k in b:
            total += w * _attr_value_similarity(k, a[k], b[k])
    return total / weight if weight else 1.0


def _cheap_parts(sig: ElementSignature, cand: ElementSignature) -> dict[str, float]:
    """The inexpensive similarity components (no string-alignment)."""
    return {
        "tag": 1.0 if sig.tag == cand.tag else 0.0,
        "attrs": attrs_similarity(sig.attrs, cand.attrs),
        "parent": (0.5 if sig.parent_tag == cand.parent_tag else 0.0)
        + 0.5 * attrs_similarity(sig.parent_attrs, cand.parent_attrs),
        "siblings": _jaccard(sig.sibling_tags, cand.sibling_tags),
        "children": _jaccard(sig.child_tags, cand.child_tags),
    }


def _text_score(a: str, b: str) -> float:
    """Exact equality is strong evidence; near-misses (``$2.99`` vs ``$22.99``)
    are weak evidence, so the raw ratio is cubed to separate them."""
    if a == b:
        return 1.0
    return _ratio(a, b) ** 3


def score(sig: ElementSignature, cand: ElementSignature) -> float:
    """Similarity of ``cand`` to ``sig`` in ``[0, 1]`` (1.0 = identical)."""
    parts = _cheap_parts(sig, cand)
    parts["text"] = _text_score(sig.text, cand.text)
    parts["path"] = _ratio_seq(sig.path, cand.path)
    return sum(WEIGHTS[k] * parts[k] for k in WEIGHTS)


def _ratio_seq(a: list[str], b: list[str]) -> float:
    if not a and not b:
        return 1.0
    if not a or not b:
        return 0.0
    return SequenceMatcher(None, a, b, autojunk=False).ratio()


# ---------------------------------------------------------------------------
# Relocation
# ---------------------------------------------------------------------------


class _Candidate:
    """One element of the page being searched. The expensive ``text`` is
    computed lazily -- most candidates are pruned before it is needed."""

    __slots__ = ("node", "order", "sig", "_text_done")

    def __init__(self, node: TreeNode, order: int, sig: ElementSignature) -> None:
        self.node = node
        self.order = order
        self.sig = sig
        self._text_done = False

    def full_sig(self) -> ElementSignature:
        if not self._text_done:
            self.sig.text = self.node.text()[:_TEXT_LIMIT]
            self._text_done = True
        return self.sig


def _index_tree(root: TreeNode) -> list[_Candidate]:
    """Build lightweight signatures for every content element in a single
    pre-order pass. Children lists are materialised once per parent, keeping
    the whole thing O(n) even for pages with thousands of siblings."""
    out: list[_Candidate] = []
    # (node, ancestor descriptors, parent tag, parent attrs, parent's child tags, index)
    stack: list[tuple[TreeNode, list[str], str, dict[str, str], list[str], int]] = [
        (root, [], "", {}, [], 0)
    ]
    order = 0
    while stack:
        node, path, ptag, pattrs, sib_tags, idx = stack.pop()
        kids = node.children
        kid_tags = [k.tag for k in kids]
        attrs = node.attrs
        if node.tag not in NON_CONTENT_TAGS:
            siblings = [t for j, t in enumerate(sib_tags) if j != idx][:_MAX_SIBLINGS]
            sig = ElementSignature(
                tag=node.tag,
                attrs=_clean_attrs(attrs),
                text="",
                path=path,
                parent_tag=ptag,
                parent_attrs=pattrs,
                sibling_tags=siblings,
                child_tags=kid_tags[:_MAX_SIBLINGS],
                index=idx,
            )
            out.append(_Candidate(node, order, sig))
            order += 1
        child_path = [*path, _descriptor_from(node.tag, attrs)]
        clean = _clean_attrs(attrs)
        for i in range(len(kids) - 1, -1, -1):  # reversed push => document order pops
            stack.append((kids[i], child_path, node.tag, clean, kid_tags, i))
    return out


def _descriptor_from(tag: str, a: dict[str, str]) -> str:
    d = tag
    if a.get("id"):
        d += f"#{a['id']}"
    cls = ".".join(sorted(a.get("class", "").split()))
    if cls:
        d += f".{cls}"
    return d


def relocate_scored(
    root: TreeNode,
    signatures: list[ElementSignature],
    threshold: float = DEFAULT_THRESHOLD,
) -> list[tuple[TreeNode, float]]:
    """Like :func:`relocate` but exposes scores; see there for semantics."""
    cands = _index_tree(root)
    if not cands or not signatures:
        return []

    taken: dict[int, tuple[_Candidate, float]] = {}  # keyed by document order
    w_expensive = WEIGHTS["text"] + WEIGHTS["path"]

    for sig in signatures:
        # Optimistic bound = exact cheap parts + perfect text/path.
        bounded: list[tuple[float, _Candidate]] = []
        for c in cands:
            parts = _cheap_parts(sig, c.sig)
            cheap = sum(WEIGHTS[k] * v for k, v in parts.items())
            bounded.append((cheap + w_expensive, c))
        bounded.sort(key=lambda t: -t[0])

        best_c: _Candidate | None = None
        best = 0.0
        floor = threshold
        for ub, c in bounded:
            if ub < max(floor, best):
                break  # sorted: nothing further can win
            sc = score(sig, c.full_sig())
            prev = taken.get(c.order)
            if sc > best and (prev is None or prev[1] < sc):
                best_c, best = c, sc
        if best_c is not None and best >= threshold:
            taken[best_c.order] = (best_c, best)

    picked = sorted(taken.values(), key=lambda t: t[0].order)
    return [(c.node, sc) for c, sc in picked]


def relocate(
    root: TreeNode,
    signatures: list[ElementSignature],
    threshold: float = DEFAULT_THRESHOLD,
) -> list[tuple[TreeNode, float]]:
    """Find, for each stored signature, the best-matching element under
    ``root`` (score >= ``threshold``). Each element is claimed by at most one
    signature; results come back in document order with their scores."""
    return relocate_scored(root, signatures, threshold)


def _candidates(root: TreeNode) -> list[TreeNode]:
    out = [root] if root.tag not in NON_CONTENT_TAGS else []
    out.extend(n for n in root.descendants() if n.tag not in NON_CONTENT_TAGS)
    return out


def find_similar(
    node: TreeNode,
    *,
    threshold: float = 0.2,
    ignore_attributes: tuple[str, ...] = ("href", "src"),
    match_text: bool = False,
) -> list[TreeNode]:
    """Elements structurally similar to ``node`` (e.g. the other product cards
    on a listing page).

    Candidates must share ``node``'s tag and ancestor tag-path; they are then
    ranked by attribute similarity (``ignore_attributes`` -- typically
    per-item values like ``href`` -- are skipped) and, if ``match_text``,
    text similarity. ``node`` itself is excluded. Results are in document
    order.
    """
    my_path = [a.tag for a in node.ancestors()]
    my_attrs = {k: v for k, v in _clean_attrs(node.attrs).items() if k not in ignore_attributes}
    my_text = node.text()
    root = node.root

    out: list[TreeNode] = []
    for cand in _candidates(root):
        if cand.key == node.key or cand.tag != node.tag:
            continue
        if [a.tag for a in cand.ancestors()] != my_path:
            continue
        c_attrs = {k: v for k, v in _clean_attrs(cand.attrs).items() if k not in ignore_attributes}
        s = attrs_similarity(my_attrs, c_attrs)
        if match_text:
            s = (s + _ratio(my_text, cand.text())) / 2
        if s >= threshold:
            out.append(cand)
    return out


# ---------------------------------------------------------------------------
# Persistent store
# ---------------------------------------------------------------------------


class AdaptiveStore:
    """SQLite-backed store of element signatures, keyed by
    ``(site, identifier)``. Thread-safe. Use ``":memory:"`` for a throwaway
    store (tests)."""

    def __init__(self, path: str | Path = ".bitscrape/adaptive.sqlite") -> None:
        self._path = str(path)
        self._lock = threading.Lock()
        self._conn_: sqlite3.Connection | None = None  # opened on first use

    @property
    def _conn(self) -> sqlite3.Connection:
        """Connect lazily: creating a store touches nothing on disk, so a
        crawl that never uses adaptive selectors never creates the database."""
        if self._conn_ is None:
            if self._path != ":memory:":
                Path(self._path).expanduser().parent.mkdir(parents=True, exist_ok=True)
            conn = sqlite3.connect(self._path, check_same_thread=False)
            conn.execute(
                "CREATE TABLE IF NOT EXISTS signatures ("
                " site TEXT NOT NULL, identifier TEXT NOT NULL, data TEXT NOT NULL,"
                " updated_at REAL NOT NULL, PRIMARY KEY (site, identifier))"
            )
            conn.commit()
            self._conn_ = conn
        return self._conn_

    def save(self, site: str, identifier: str, sigs: list[ElementSignature]) -> None:
        payload = json.dumps([asdict(s) for s in sigs], ensure_ascii=False)
        with self._lock:
            self._conn.execute(
                "INSERT INTO signatures (site, identifier, data, updated_at) VALUES (?,?,?,?)"
                " ON CONFLICT(site, identifier) DO UPDATE SET data=excluded.data,"
                " updated_at=excluded.updated_at",
                (site, identifier, payload, time.time()),
            )
            self._conn.commit()

    def load(self, site: str, identifier: str) -> list[ElementSignature]:
        with self._lock:
            row = self._conn.execute(
                "SELECT data FROM signatures WHERE site=? AND identifier=?", (site, identifier)
            ).fetchone()
        if not row:
            return []
        return [ElementSignature.from_json(d) for d in json.loads(row[0])]

    def delete(self, site: str, identifier: str) -> None:
        with self._lock:
            self._conn.execute(
                "DELETE FROM signatures WHERE site=? AND identifier=?", (site, identifier)
            )
            self._conn.commit()

    def identifiers(self, site: str | None = None) -> list[tuple[str, str]]:
        with self._lock:
            if site is None:
                rows = self._conn.execute("SELECT site, identifier FROM signatures").fetchall()
            else:
                rows = self._conn.execute(
                    "SELECT site, identifier FROM signatures WHERE site=?", (site,)
                ).fetchall()
        return [(r[0], r[1]) for r in rows]

    def close(self) -> None:
        with self._lock:
            if self._conn_ is not None:
                self._conn_.close()
                self._conn_ = None


_default_store: AdaptiveStore | None = None


def default_store(path: str | Path | None = None) -> AdaptiveStore:
    """Process-wide default store, created lazily (so nothing touches the
    filesystem unless adaptive features are actually used)."""
    global _default_store
    if _default_store is None:
        if path is None:
            from bitscrape.core.settings import Settings

            path = Settings().adaptive_db_path
        _default_store = AdaptiveStore(path)
    return _default_store


def reset_default_store() -> None:
    global _default_store
    if _default_store is not None:
        _default_store.close()
    _default_store = None
