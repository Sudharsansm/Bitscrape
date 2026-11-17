"""
Main-content extraction
=======================

Pages are mostly chrome: navigation, cookie banners, related-article rails,
footers. For LLM/RAG use only the article matters -- feeding the rest wastes
tokens and dilutes retrieval. :func:`find_main_content` picks the subtree that
holds the primary content, and :func:`strip_noise` removes everything that
should never reach a reader (scripts, styles, *hidden* text).

Strategy, in order of trust:

1. An explicit landmark: ``<main>``, ``[role=main]``, or a single ``<article>``.
2. Otherwise a density score over block elements -- text length rewarded, link
   text penalised (navigation is mostly links), class/id hints (``sidebar``,
   ``comment``, ``footer``, ``advert`` ...) penalised, ``article``/``content``
   hints rewarded.
3. Otherwise the whole ``<body>``.
"""

from __future__ import annotations

import re
from typing import Any

_XML_DECL = re.compile(r"^\s*<\?xml[^>]*\?>", re.I)


def parse_html(html: str | bytes) -> Any:
    """Parse ``html`` into an lxml ``<html>`` element, hardened for untrusted input.

    * Always returns a full document root (``<html><body>...``): a fragment
      such as ``<a href=..>x</a>`` is wrapped rather than *becoming* the root,
      so its own tag is never skipped.
    * ``huge_tree`` lifts libxml2's 256-level nesting cap to ~2,000 levels
      (real pages never nest that deep). Markup nested deeper than that is
      discarded by the parser rather than crashing -- an adversarial-input
      safeguard, not a feature.
    * A leading ``<?xml ... encoding=...?>`` declaration (which makes lxml
      reject ``str`` input) is stripped.

    Raises ``ValueError`` when nothing parseable is found.
    """
    from lxml import etree  # pyright: ignore[reportAttributeAccessIssue]
    from lxml import html as lxml_html

    if isinstance(html, bytes):
        html = html.decode("utf-8", errors="replace")
    html = _XML_DECL.sub("", html)
    if not html.strip():
        raise ValueError("empty document")
    parser = lxml_html.HTMLParser(huge_tree=True, remove_comments=False)
    try:
        return lxml_html.document_fromstring(html, parser=parser)
    except (etree.ParserError, etree.XMLSyntaxError) as exc:
        raise ValueError(str(exc)) from exc


# Elements that never contain readable content.
_DROP_TAGS = frozenset(
    {"script", "style", "noscript", "template", "svg", "canvas", "iframe", "object", "embed",
     "link", "meta", "head", "select", "option", "datalist", "audio", "video", "source", "track",
     "map", "area", "dialog", "button", "input", "textarea", "form"}
)  # fmt: skip
# Page chrome removed when we are extracting the main content.
_CHROME_TAGS = frozenset({"nav", "header", "footer", "aside"})

_NEG_HINT = re.compile(
    r"(comment|combx|community|cookie|consent|banner|breadcrumb|disqus|extra|foot|footer|gdpr|"
    r"header|menu|nav|related|remark|rss|share|shoutbox|sidebar|skyscraper|social|sponsor|"
    r"advert|ad-|ads|adbox|promo|popup|modal|newsletter|subscribe|pagination|pager|widget|"
    r"toolbar|tabs|sr-only|visually-hidden)",
    re.I,
)
_POS_HINT = re.compile(
    r"(article|body|content|entry|main|page|post|text|blog|story|hentry|prose|markdown)", re.I
)
_HIDDEN_STYLE = re.compile(
    r"(display\s*:\s*none|visibility\s*:\s*hidden|opacity\s*:\s*0(?:\.0+)?\s*(?:;|$)|"
    r"font-size\s*:\s*0(?:px|pt|em|rem|%)?\s*(?:;|$)|"
    r"(?:height|width)\s*:\s*0(?:px)?\s*;?[^}]*overflow\s*:\s*hidden|"
    r"text-indent\s*:\s*-\d{3,}|left\s*:\s*-\d{3,}px|top\s*:\s*-\d{3,}px|"
    r"clip\s*:\s*rect\(\s*0)",
    re.I,
)


def is_hidden(el: Any) -> bool:
    """True if ``el`` itself is hidden from human readers by an attribute or
    inline style (``hidden``, ``aria-hidden=true``, ``display:none``,
    ``visibility:hidden``, zero opacity/font-size, off-screen positioning).

    This matters for security as much as for tidiness: invisible text is the
    classic carrier for prompt-injection payloads aimed at AI agents."""
    if el.get("hidden") is not None:
        return True
    if (el.get("aria-hidden") or "").strip().lower() == "true":
        return True
    if el.tag == "input" and (el.get("type") or "").lower() == "hidden":
        return True
    style = el.get("style")
    return bool(style and _HIDDEN_STYLE.search(style))


def strip_noise(root: Any, *, drop_hidden: bool = True, drop_chrome: bool = False) -> Any:
    """Remove scripts/styles/forms, comments, hidden elements (and optionally
    page chrome: nav/header/footer/aside) from ``root`` **in place**."""
    from lxml import etree  # pyright: ignore[reportAttributeAccessIssue]

    for el in list(root.iter()):
        if el.getparent() is None and el is not root:
            continue  # already detached with its parent
        tag = el.tag
        if not isinstance(tag, str):  # comment / processing instruction
            if el.getparent() is not None:
                _remove_keep_tail(el)
            continue
        if tag in _DROP_TAGS and el is not root:
            _remove_keep_tail(el)
        elif drop_hidden and el is not root and is_hidden(el):
            _remove_keep_tail(el)
        elif drop_chrome and tag in _CHROME_TAGS and el is not root:
            # Keep a header/footer that lives *inside* an article (bylines etc.).
            if not _inside(el, ("article", "main")):
                _remove_keep_tail(el)
    etree.strip_tags(root, etree.Comment)
    return root


def _remove_keep_tail(el: Any) -> None:
    parent = el.getparent()
    if parent is None:
        return
    tail = el.tail
    prev = el.getprevious()
    if tail:
        if prev is not None:
            prev.tail = (prev.tail or "") + tail
        else:
            parent.text = (parent.text or "") + tail
    parent.remove(el)


def _inside(el: Any, tags: tuple[str, ...]) -> bool:
    p = el.getparent()
    while p is not None:
        if p.tag in tags:
            return True
        p = p.getparent()
    return False


def _text_len(el: Any) -> int:
    return len(re.sub(r"\s+", " ", el.text_content()).strip())


def _link_text_len(el: Any) -> int:
    return sum(_text_len(a) for a in el.iter("a"))


def _hint_score(el: Any) -> float:
    ident = f"{el.get('class', '')} {el.get('id', '')}"
    score = 0.0
    if ident.strip():
        if _NEG_HINT.search(ident):
            score -= 25
        if _POS_HINT.search(ident):
            score += 25
    return score


def score_block(el: Any) -> float:
    """Density score of one block element (higher = more likely main content)."""
    total = _text_len(el)
    if total < 25:
        return 0.0
    links = _link_text_len(el)
    paragraphs = len(el.xpath(".//p")) + len(el.xpath(".//pre")) + len(el.xpath(".//li"))
    commas = el.text_content().count(",") + el.text_content().count("。")
    body = total - 1.5 * links
    return body * 0.5 + paragraphs * 12 + commas * 2 + _hint_score(el)


def find_main_content(root: Any) -> Any:
    """Return the element holding the page's primary content."""
    body = root.find(".//body")
    scope = body if body is not None else root

    landmarks = scope.xpath(".//main | .//*[@role='main']")
    landmarks = [m for m in landmarks if _text_len(m) > 0]
    if landmarks:
        return max(landmarks, key=_text_len)

    articles = scope.xpath(".//article")
    articles = [a for a in articles if _text_len(a) > 80]
    if len(articles) == 1:
        return articles[0]
    if len(articles) > 1:
        # A listing of many articles: the page *is* the content; keep the container.
        return scope

    best: Any = None
    best_score = 0.0
    for el in scope.iter("div", "section", "td", "article"):
        s = score_block(el)
        if s > best_score:
            best, best_score = el, s
    if best is not None and _text_len(best) >= 0.35 * max(_text_len(scope), 1):
        return best
    return scope
