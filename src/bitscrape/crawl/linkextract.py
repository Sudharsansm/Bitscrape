"""
Link extraction
===============

:class:`LinkExtractor` pulls the links worth following out of a page, with the
filtering a crawler needs: URL regexes, domain allow/deny lists, file
extensions to skip, page regions to restrict to, and URL canonicalisation so
``/a?utm_source=x#top`` and ``/a`` count as one link.

    le = LinkExtractor(allow=r"/product/\\d+", restrict_css="main", deny_extensions=None)
    for link in le.extract_links(response):
        print(link.url, link.text)

It accepts a :class:`~bitscrape.parser.selector.ParsedResponse`, a raw
:class:`~bitscrape.core.models.Response`, or ``(html, base_url)`` via
:meth:`extract_from_html`.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from typing import Any
from urllib.parse import urljoin, urlparse

from bitscrape.canonicalize import canonicalize_url
from bitscrape.content import parse_html

# Extensions that are never worth crawling as pages (same spirit as Scrapy's list).
IGNORED_EXTENSIONS: tuple[str, ...] = (
    # images
    "mng", "pct", "bmp", "gif", "jpg", "jpeg", "png", "pst", "psp", "tif", "tiff", "ai", "drw",
    "dxf", "eps", "ps", "svg", "cdr", "ico", "webp", "avif",
    # audio
    "mp3", "wma", "ogg", "wav", "ra", "aac", "mid", "au", "aiff", "flac", "m4a",
    # video
    "3gp", "asf", "asx", "avi", "mov", "mp4", "mpg", "qt", "rm", "swf", "wmv", "m4a", "m4v",
    "flv", "webm", "mkv",
    # office / documents
    "xls", "xlsx", "ppt", "pptx", "pps", "doc", "docx", "odt", "ods", "odg", "odp", "pdf",
    # other
    "css", "exe", "bin", "rss", "zip", "rar", "gz", "tgz", "7z", "bz2", "xz", "dmg", "iso",
    "apk", "msi", "jar", "woff", "woff2", "ttf", "eot", "otf",
)  # fmt: skip

_ALLOWED_SCHEMES = frozenset({"http", "https"})


@dataclass(frozen=True)
class Link:
    """One extracted link."""

    url: str
    text: str = ""
    nofollow: bool = False
    tag: str = "a"
    attr: str = "href"

    def __str__(self) -> str:
        return self.url


def _as_regexes(
    value: str | re.Pattern[str] | Iterable[str | re.Pattern[str]] | None,
) -> list[re.Pattern[str]]:
    if value is None:
        return []
    if isinstance(value, (str, re.Pattern)):
        value = [value]
    return [re.compile(v) if isinstance(v, str) else v for v in value]


def _as_list(value: str | Iterable[str] | None) -> list[str]:
    if value is None:
        return []
    return [value] if isinstance(value, str) else list(value)


def _host_matches(host: str, domains: Sequence[str]) -> bool:
    host = host.lower().rstrip(".")
    return any(
        host == d or host.endswith("." + d) for d in (x.lower().lstrip(".") for x in domains)
    )


class LinkExtractor:
    """Configurable link extractor.

    Args:
        allow: regex(es); a URL must match at least one (default: all).
        deny: regex(es); a URL matching any is dropped.
        allow_domains / deny_domains: host suffix filters (``example.com``
            also matches ``www.example.com``).
        deny_extensions: file extensions to skip; ``None`` uses
            :data:`IGNORED_EXTENSIONS`, ``()`` skips nothing.
        restrict_css / restrict_xpaths: only look inside these regions.
        tags / attrs: which elements/attributes carry links
            (default ``("a", "area")`` / ``("href",)``).
        canonicalize: normalise URLs (sorted query, tracking params removed,
            fragment dropped, default ports dropped).
        unique: return each URL once (first occurrence wins).
        honor_nofollow: skip ``rel="nofollow"`` links.
        process_value: hook to rewrite/skip a raw attribute value
            (return ``None`` to skip) -- e.g. to unwrap ``javascript:go('/x')``.
        strip_query_params: extra query parameters to strip when canonicalising.
    """

    def __init__(
        self,
        allow: str | re.Pattern[str] | Iterable[str | re.Pattern[str]] | None = (),
        deny: str | re.Pattern[str] | Iterable[str | re.Pattern[str]] | None = (),
        allow_domains: str | Iterable[str] | None = (),
        deny_domains: str | Iterable[str] | None = (),
        deny_extensions: Iterable[str] | None = None,
        restrict_css: str | Iterable[str] | None = (),
        restrict_xpaths: str | Iterable[str] | None = (),
        tags: str | Iterable[str] = ("a", "area"),
        attrs: str | Iterable[str] = ("href",),
        canonicalize: bool = True,
        unique: bool = True,
        honor_nofollow: bool = False,
        process_value: Callable[[str], str | None] | None = None,
        strip_query_params: Iterable[str] | None = None,
    ) -> None:
        self.allow = _as_regexes(allow)
        self.deny = _as_regexes(deny)
        self.allow_domains = _as_list(allow_domains)
        self.deny_domains = _as_list(deny_domains)
        exts = IGNORED_EXTENSIONS if deny_extensions is None else tuple(deny_extensions)
        self.deny_extensions = frozenset(e.lower().lstrip(".") for e in exts)
        self.restrict_css = _as_list(restrict_css)
        self.restrict_xpaths = _as_list(restrict_xpaths)
        self.tags = tuple(_as_list(tags))
        self.attrs = tuple(_as_list(attrs))
        self.canonicalize = canonicalize
        self.unique = unique
        self.honor_nofollow = honor_nofollow
        self.process_value = process_value
        self.strip_query_params = frozenset(strip_query_params) if strip_query_params else None

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def extract_links(self, response: Any) -> list[Link]:
        """Extract links from a ``ParsedResponse`` / ``Response``."""
        text = response.text
        url = response.url
        return self.extract_from_html(text, url)

    def matches(self, url: str) -> bool:
        """Whether ``url`` itself (already absolute) passes this extractor's
        ``allow``/``deny``/``allow_domains``/``deny_domains``/``deny_extensions``
        rules -- the same test :meth:`extract_from_html` applies to each link
        it finds, exposed for URLs that don't come from an ``<a href>`` (e.g.
        a sitemap's ``<loc>`` entries in :class:`~bitscrape.spiders.templates.SitemapSpider`).
        ``restrict_css``/``restrict_xpaths``/``tags``/``attrs`` don't apply --
        those only make sense while extracting from a document -- but
        ``canonicalize`` does, matching how the URL would be compared if it
        had been extracted from a page.
        """
        try:
            parsed = urlparse(canonicalize_url(url) if self.canonicalize else url)
        except ValueError:
            return False
        if parsed.scheme.lower() not in _ALLOWED_SCHEMES or not parsed.netloc:
            return False
        return self._allowed(parsed.geturl(), parsed.hostname or "", parsed.path)

    def extract_from_html(self, html: str | bytes, base_url: str) -> list[Link]:
        if isinstance(html, bytes):
            html = html.decode("utf-8", errors="replace")
        if not html.strip():
            return []
        try:
            tree = parse_html(html)
        except ValueError:
            return []
        base = tree.xpath("string(//base/@href)")
        if base:
            base_url = urljoin(base_url, base)

        regions = self._regions(tree)
        out: list[Link] = []
        seen: set[str] = set()
        for region in regions:
            for el in self._link_elements(region):
                for attr in self.attrs:
                    raw = el.get(attr)
                    if raw is None:
                        continue
                    link = self._make_link(el, attr, raw, base_url)
                    if link is None:
                        continue
                    if self.unique:
                        if link.url in seen:
                            continue
                        seen.add(link.url)
                    out.append(link)
        return out

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _regions(self, tree: Any) -> list[Any]:
        if not self.restrict_css and not self.restrict_xpaths:
            return [tree]
        regions: list[Any] = []
        for sel in self.restrict_css:
            regions.extend(tree.cssselect(sel))
        for xp in self.restrict_xpaths:
            regions.extend(x for x in tree.xpath(xp) if hasattr(x, "iter"))
        # a region nested in another would yield duplicate links; keep outermost only
        return regions

    def _link_elements(self, region: Any) -> list[Any]:
        found = []
        for el in region.iter():
            if isinstance(el.tag, str) and el.tag.lower() in self.tags:
                found.append(el)
        return found

    def _make_link(self, el: Any, attr: str, raw: str, base_url: str) -> Link | None:
        value = raw.strip()
        if self.process_value is not None:
            processed = self.process_value(value)
            if processed is None:
                return None
            value = processed
        if not value or value.startswith("#"):
            return None
        try:
            absolute = urljoin(base_url, value)
            parsed = urlparse(absolute)
        except ValueError:
            return None
        if parsed.scheme.lower() not in _ALLOWED_SCHEMES or not parsed.netloc:
            return None
        rel = (el.get("rel") or "").lower().split()
        nofollow = "nofollow" in rel
        if nofollow and self.honor_nofollow:
            return None
        if self.canonicalize:
            kwargs: dict[str, Any] = {}
            if self.strip_query_params:
                kwargs["extra_tracking_params"] = self.strip_query_params
            absolute = canonicalize_url(absolute, **kwargs)
            parsed = urlparse(absolute)
        if not self._allowed(absolute, parsed.hostname or "", parsed.path):
            return None
        text = re.sub(r"\s+", " ", el.text_content()).strip()
        return Link(url=absolute, text=text, nofollow=nofollow, tag=str(el.tag).lower(), attr=attr)

    def _allowed(self, url: str, host: str, path: str) -> bool:
        if self.allow and not any(rx.search(url) for rx in self.allow):
            return False
        if any(rx.search(url) for rx in self.deny):
            return False
        if self.allow_domains and not _host_matches(host, self.allow_domains):
            return False
        if self.deny_domains and _host_matches(host, self.deny_domains):
            return False
        ext = path.rsplit(".", 1)[-1].lower() if "." in path.rsplit("/", 1)[-1] else ""
        return not (ext and ext in self.deny_extensions)
