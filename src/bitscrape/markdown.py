"""
HTML -> Markdown for LLMs and RAG
=================================

::

    md = html_to_markdown(html, base_url="https://example.com/post")

Designed for retrieval pipelines rather than round-tripping:

* **Main content only** by default -- navigation, footers, cookie banners and
  sidebars are dropped (see :mod:`bitscrape.content`).
* **Hidden text is removed** (``display:none``, ``aria-hidden``, zero-size
  fonts, off-screen positioning) so invisible instructions never reach a model.
* Relative links and images are made **absolute**.
* Structure survives: headings, nested lists, fenced code blocks (with the
  language when the markup declares it), GFM tables, blockquotes, definition
  lists and ``<details>``.

No third-party dependency beyond lxml.
"""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import urljoin

from bitscrape.content import find_main_content, parse_html, strip_noise

_BLOCK_TAGS = frozenset(
    {"p", "div", "section", "article", "main", "header", "footer", "aside", "nav", "figure",
     "figcaption", "address", "fieldset", "legend", "details", "summary", "center", "body", "html"}
)  # fmt: skip
_SKIP_SCHEMES = ("javascript:", "data:", "vbscript:")
_MD_ESCAPE = re.compile(r"([\\`*_\[\]<>])")
_LINE_START = re.compile(r"^(\s*)([#>+\-]|\d+[.)])(\s)")
_WS = re.compile(r"[ \t\r\n\f\v]+")


def _escape(text: str) -> str:
    return _MD_ESCAPE.sub(r"\\\1", text)


class _Converter:
    def __init__(
        self,
        base_url: str,
        include_links: bool,
        include_images: bool,
        include_tables: bool,
    ) -> None:
        self.base_url = base_url
        self.include_links = include_links
        self.include_images = include_images
        self.include_tables = include_tables

    # -- helpers -------------------------------------------------------------

    def _abs(self, url: str) -> str:
        url = url.strip()
        if url.lower().startswith(_SKIP_SCHEMES):
            return ""
        return urljoin(self.base_url, url) if self.base_url else url

    @staticmethod
    def _norm(text: str | None) -> str:
        return _WS.sub(" ", text) if text else ""

    # -- inline --------------------------------------------------------------

    def inline(self, el: Any) -> str:
        """Render the *children and text* of ``el`` as one inline run."""
        parts: list[str] = []
        if el.text:
            parts.append(_escape(self._norm(el.text)))
        for child in el:
            if isinstance(child.tag, str):
                parts.append(self.inline_el(child))
            if child.tail:
                parts.append(_escape(self._norm(child.tail)))
        return "".join(parts)

    def inline_el(self, el: Any) -> str:
        tag = el.tag
        if tag in ("strong", "b"):
            inner = self.inline(el).strip()
            return f"**{inner}**" if inner else ""
        if tag in ("em", "i", "cite", "dfn"):
            inner = self.inline(el).strip()
            return f"*{inner}*" if inner else ""
        if tag in ("del", "s", "strike"):
            inner = self.inline(el).strip()
            return f"~~{inner}~~" if inner else ""
        if tag in ("code", "kbd", "samp", "tt"):
            raw = el.text_content()
            fence = "``" if "`" in raw else "`"
            return f"{fence}{self._norm(raw).strip()}{fence}" if raw.strip() else ""
        if tag == "br":
            return "  \n"
        if tag == "a":
            return self._link(el)
        if tag == "img":
            return self._image(el)
        if tag in (
            "sup",
            "sub",
            "span",
            "small",
            "mark",
            "u",
            "abbr",
            "time",
            "label",
            "bdi",
            "bdo",
            "q",
            "ins",
            "font",
            "big",
        ):
            inner = self.inline(el)
            return f'"{inner.strip()}"' if tag == "q" and inner.strip() else inner
        if tag in _BLOCK_TAGS or tag in (
            "ul",
            "ol",
            "table",
            "pre",
            "blockquote",
            "h1",
            "h2",
            "h3",
            "h4",
            "h5",
            "h6",
            "hr",
            "dl",
            "li",
        ):
            # block element in an inline run: flatten to text
            return " " + self._norm(el.text_content()).strip() + " "
        return self.inline(el)

    def _link(self, el: Any) -> str:
        text = self.inline(el).strip()
        href = el.get("href")
        if not self.include_links or not href:
            return text
        url = self._abs(href)
        if not url or url.startswith("#"):
            return text
        if not text:
            imgs = el.xpath(".//img")
            text = self._norm(imgs[0].get("alt") or "").strip() if imgs else ""
        if not text:
            text = url
        url = url.replace(")", "%29").replace("(", "%28").replace(" ", "%20")
        title = el.get("title")
        t = f' "{title.strip()}"' if title and title.strip() and '"' not in title else ""
        return f"[{text}]({url}{t})"

    def _image(self, el: Any) -> str:
        if not self.include_images:
            return ""
        src = el.get("src") or el.get("data-src") or ""
        url = self._abs(src) if src else ""
        if not url:
            return ""
        alt = self._norm(el.get("alt") or "").strip()
        return f"![{_escape(alt)}]({url.replace(' ', '%20')})"

    # -- blocks --------------------------------------------------------------

    def blocks(self, el: Any) -> list[str]:
        """Render ``el``'s content as a list of Markdown blocks."""
        out: list[str] = []
        run: list[str] = []

        def flush() -> None:
            text = "".join(run).strip()
            run.clear()
            if text:
                out.append(self._fix_line_starts(text))

        if el.text and el.text.strip():
            run.append(_escape(self._norm(el.text)))
        for child in el:
            tag = child.tag
            if isinstance(tag, str):
                if tag in _BLOCK_TAGS:
                    flush()
                    out.extend(self.blocks(child))
                elif tag in ("h1", "h2", "h3", "h4", "h5", "h6"):
                    flush()
                    txt = self.inline(child).strip()
                    if txt:
                        out.append(f"{'#' * int(tag[1])} {txt}")
                elif tag in ("ul", "ol"):
                    flush()
                    lst = self._list(child, 0)
                    if lst:
                        out.append(lst)
                elif tag == "pre":
                    flush()
                    out.append(self._pre(child))
                elif tag == "blockquote":
                    flush()
                    inner = "\n\n".join(self.blocks(child))
                    if inner.strip():
                        out.append("\n".join("> " + ln if ln else ">" for ln in inner.splitlines()))
                elif tag == "hr":
                    flush()
                    out.append("---")
                elif tag == "table":
                    flush()
                    t = self._table(child)
                    if t:
                        out.append(t)
                elif tag == "dl":
                    flush()
                    out.extend(self._dl(child))
                elif tag in ("thead", "tbody", "tfoot", "tr", "td", "th", "caption"):
                    flush()
                    out.extend(self.blocks(child))
                elif tag == "li":
                    flush()
                    out.append("- " + self.inline(child).strip())
                else:
                    run.append(self.inline_el(child))
            if child.tail and child.tail.strip():
                run.append(_escape(self._norm(child.tail)))
        flush()
        return out

    @staticmethod
    def _fix_line_starts(text: str) -> str:
        """Escape a leading ``#``, ``>``, ``-`` or ``1.`` that would otherwise be
        read as structure."""
        return "\n".join(_LINE_START.sub(r"\1\\\2\3", ln) for ln in text.split("\n"))

    def _pre(self, el: Any) -> str:
        code = el.find(".//code")
        lang = ""
        for node in (code, el):
            if node is None:
                continue
            m = re.search(
                r"(?:language|lang|highlight-source|brush:)[-_ :]?([A-Za-z0-9+#_.-]+)",
                node.get("class", ""),
            )
            if m:
                lang = m.group(1)
                break
        text = "".join(el.itertext()).strip("\n")
        fence = "```"
        while fence in text:
            fence += "`"
        return f"{fence}{lang}\n{text}\n{fence}"

    def _list(self, el: Any, depth: int) -> str:
        ordered = el.tag == "ol"
        try:
            n = int(el.get("start", "1"))
        except ValueError:
            n = 1
        lines: list[str] = []
        indent = "    " * depth
        for li in el:
            if li.tag != "li":
                continue
            marker = f"{n}. " if ordered else "- "
            n += 1
            head_parts: list[str] = []
            nested: list[str] = []
            if li.text and li.text.strip():
                head_parts.append(_escape(self._norm(li.text)))
            for child in li:
                if not isinstance(child.tag, str):
                    if child.tail and child.tail.strip():
                        head_parts.append(_escape(self._norm(child.tail)))
                    continue
                if child.tag in ("ul", "ol"):
                    sub = self._list(child, depth + 1)
                    if sub:
                        nested.append(sub)
                elif child.tag in ("p", "div"):
                    head_parts.append(" " + self.inline(child).strip())
                elif child.tag == "pre":
                    nested.append(self._indent_block(self._pre(child), indent + "    "))
                else:
                    head_parts.append(self.inline_el(child))
                if child.tail and child.tail.strip():
                    head_parts.append(_escape(self._norm(child.tail)))
            head = re.sub(r"\s+", " ", "".join(head_parts)).strip()
            lines.append(f"{indent}{marker}{head}".rstrip())
            lines.extend(nested)
        return "\n".join(lines)

    @staticmethod
    def _indent_block(text: str, prefix: str) -> str:
        return "\n".join(prefix + ln if ln else ln for ln in text.splitlines())

    def _dl(self, el: Any) -> list[str]:
        out: list[str] = []
        for child in el:
            if child.tag == "dt":
                t = self.inline(child).strip()
                if t:
                    out.append(f"**{t}**")
            elif child.tag == "dd":
                t = self.inline(child).strip()
                if t:
                    out.append(f": {t}")
        return out

    def _table(self, el: Any) -> str:
        if not self.include_tables:
            return ""
        if el.xpath(".//table//table"):  # nested layout tables: flatten instead
            return "\n\n".join(self.blocks_of_cells(el))
        rows: list[list[str]] = []
        for tr in el.iter("tr"):
            cells = []
            for cell in tr:
                if cell.tag not in ("td", "th"):
                    continue
                txt = self.inline(cell).replace("\n", " ").replace("|", "\\|")
                txt = re.sub(r"\s+", " ", txt).strip()
                try:
                    span = max(1, int(cell.get("colspan", "1")))
                except ValueError:
                    span = 1
                cells.append(txt)
                cells.extend([""] * (span - 1))
            if cells:
                rows.append(cells)
        if not rows:
            return ""
        width = max(len(r) for r in rows)
        rows = [r + [""] * (width - len(r)) for r in rows]
        # GFM tables need a header row: use the first row (a <thead>/<th> row
        # when present, otherwise simply the first data row).
        header, body = rows[0], rows[1:]
        lines = ["| " + " | ".join(header) + " |", "| " + " | ".join("---" for _ in header) + " |"]
        lines.extend("| " + " | ".join(r) + " |" for r in body)
        cap = el.find("caption")
        prefix = (
            f"**{self.inline(cap).strip()}**\n\n"
            if cap is not None and self.inline(cap).strip()
            else ""
        )
        return prefix + "\n".join(lines)

    def blocks_of_cells(self, el: Any) -> list[str]:
        out: list[str] = []
        for cell in el.iter("td", "th"):
            if cell.xpath(".//table"):
                continue
            out.extend(self.blocks(cell))
        return out


def html_to_markdown(
    html: str | bytes,
    base_url: str = "",
    *,
    main_content: bool = True,
    include_links: bool = True,
    include_images: bool = True,
    include_tables: bool = True,
    strip_hidden: bool = True,
    max_length: int | None = None,
) -> str:
    """Convert ``html`` to Markdown.

    Args:
        base_url: used to make relative links/images absolute (``<base href>``
            in the document takes precedence).
        main_content: keep only the primary content (drops nav/header/footer/
            aside and boilerplate). Set ``False`` to convert the whole body --
            appropriate for a fragment such as one element's HTML.
        include_links / include_images / include_tables: toggle each construct.
        strip_hidden: drop elements hidden by attribute or inline style.
        max_length: truncate the result at a paragraph boundary near this many
            characters.
    """
    try:
        root = parse_html(html)
    except ValueError:
        return ""

    base = root.xpath("string(//base/@href)")
    if base:
        base_url = urljoin(base_url, base) if base_url else base

    strip_noise(root, drop_hidden=strip_hidden, drop_chrome=main_content)
    body = root.find(".//body")
    scope = find_main_content(root) if main_content else (body if body is not None else root)

    conv = _Converter(base_url, include_links, include_images, include_tables)
    try:
        blocks = [b for b in conv.blocks(scope) if b.strip()]
        text = "\n\n".join(blocks)
    except RecursionError:
        # Pathologically deep markup: degrade to plain paragraphs of text.
        text = "\n\n".join(re.sub(r"\s+", " ", t).strip() for t in scope.itertext() if t.strip())
    text = re.sub(r"[ \t]+\n", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    if max_length is not None and len(text) > max_length:
        cut = text.rfind("\n\n", 0, max_length)
        text = text[: cut if cut > max_length // 2 else max_length].rstrip() + "\n\n…"
    return text
