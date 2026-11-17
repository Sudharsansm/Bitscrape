"""
Text cleaning & extraction helpers
===================================

Small, dependency-free functions for the string work every scraper ends up
doing: normalising whitespace and Unicode, stripping tags, pulling numbers,
prices, e-mails and URLs out of messy text. All are also reachable from
selectors: ``el.clean_text()``, ``el.numbers()``, ``el.price()``,
``page.clean_text()``, ``page.emails()``, ...
"""

from __future__ import annotations

import html as _html
import re
import unicodedata
from typing import Literal
from decimal import Decimal, InvalidOperation

_ZERO_WIDTH = dict.fromkeys(map(ord, "\u200b\u200c\u200d\u2060\ufeff\u00ad"), None)
_WS = re.compile(r"[ \t\r\f\v\u00a0\u2000-\u200a\u202f\u205f\u3000]+")
_MULTI_NL = re.compile(r"\n{3,}")
_TAG = re.compile(
    r"<(script|style)\b[^>]*>.*?</\1\s*>|<!--.*?-->|<[^>]+>", re.IGNORECASE | re.DOTALL
)
_EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}")
_URL = re.compile(r"https?://[^\s<>\"'\)\]]+", re.IGNORECASE)
_NUMBER = re.compile(r"[-+]?\d[\d,.\u00a0 ]*\d|[-+]?\d")
_CURRENCY = {
    "$": "USD", "US$": "USD", "€": "EUR", "£": "GBP", "¥": "JPY", "₹": "INR", "₩": "KRW",
    "₽": "RUB", "₺": "TRY", "₫": "VND", "₪": "ILS", "R$": "BRL", "A$": "AUD", "C$": "CAD",
    "CHF": "CHF", "USD": "USD", "EUR": "EUR", "GBP": "GBP", "INR": "INR", "JPY": "JPY",
}  # fmt: skip
_CURRENCY_RE = re.compile(
    "|".join(re.escape(c) for c in sorted(_CURRENCY, key=len, reverse=True)), re.IGNORECASE
)


def normalize_whitespace(text: str, *, keep_newlines: bool = False) -> str:
    """Collapse runs of whitespace (including non-breaking and zero-width spaces) to a
    single space and trim. ``keep_newlines=True`` preserves paragraph breaks (max one blank line)."""
    text = text.translate(_ZERO_WIDTH)
    if not keep_newlines:
        return " ".join(text.split())
    lines = [_WS.sub(" ", ln).strip() for ln in text.replace("\r\n", "\n").split("\n")]
    return _MULTI_NL.sub("\n\n", "\n".join(lines)).strip()


def strip_tags(html: str) -> str:
    """Remove tags (and the *contents* of ``<script>``/``<style>`` and comments), then
    unescape entities. Not a sanitiser -- use it to read text, not to make HTML safe."""
    return _html.unescape(_TAG.sub(" ", html))


def strip_accents(text: str) -> str:
    """``"Café Münster"`` → ``"Cafe Munster"``."""
    return "".join(c for c in unicodedata.normalize("NFKD", text) if not unicodedata.combining(c))


def clean_text(
    text: str,
    *,
    strip_html: bool = False,
    unescape: bool = True,
    unicode_form: Literal["NFC", "NFD", "NFKC", "NFKD"] | None = "NFKC",
    keep_newlines: bool = False,
    accents: bool = True,
) -> str:
    """One-call tidy-up: optional tag stripping, entity unescaping, Unicode
    normalisation (NFKC by default: ``"ﬁ"`` → ``"fi"``, full-width digits → ASCII),
    whitespace collapsing, and (with ``accents=False``) accent removal."""
    if strip_html:
        text = strip_tags(text)
    elif unescape:
        text = _html.unescape(text)
    if unicode_form:
        text = unicodedata.normalize(unicode_form, text)
    if not accents:
        text = strip_accents(text)
    return normalize_whitespace(text, keep_newlines=keep_newlines)


def slugify(text: str, *, sep: str = "-", max_length: int | None = None) -> str:
    """``"Hello, Wörld!"`` → ``"hello-world"``."""
    s = strip_accents(clean_text(text)).lower()
    s = re.sub(r"[^a-z0-9]+", sep, s).strip(sep)
    return s[:max_length].strip(sep) if max_length else s


def truncate(text: str, length: int, *, suffix: str = "…") -> str:
    """Cut to ``length`` characters on a word boundary where possible, appending ``suffix``."""
    if length < 0:
        raise ValueError("length must be >= 0")
    if len(text) <= length:
        return text
    cut = text[: max(length - len(suffix), 0)]
    if " " in cut and not text[len(cut)].isspace():
        cut = cut.rsplit(" ", 1)[0]
    return cut.rstrip() + suffix


def _to_decimal(raw: str) -> Decimal | None:
    s = raw.strip().replace("\u00a0", "").replace(" ", "")
    if not s:
        return None
    sign = -1 if s.startswith("-") else 1
    s = s.lstrip("+-")
    last_dot, last_comma = s.rfind("."), s.rfind(",")
    if last_dot != -1 and last_comma != -1:  # both present: the later one is the decimal mark
        dec, grp = (".", ",") if last_dot > last_comma else (",", ".")
        s = s.replace(grp, "").replace(dec, ".")
    elif last_comma != -1 or last_dot != -1:
        mark = "," if last_comma != -1 else "."
        parts = s.split(mark)
        if len(parts) > 2:  # 1,234,567 / 1.234.567 -> grouping
            s = "".join(parts)
        elif len(parts[1]) == 3 and len(parts[0]) <= 3 and parts[0] not in ("", "0"):
            s = "".join(parts)  # 1,234 / 1.234 -> grouping
        else:
            s = parts[0] + "." + parts[1]
    try:
        return sign * Decimal(s)
    except InvalidOperation:
        return None


def extract_numbers(text: str) -> list[Decimal]:
    """Every number in ``text`` as a :class:`~decimal.Decimal`, understanding thousands
    separators and both decimal marks: ``"1,234.50"``, ``"1.234,50"`` and ``"1 234,5"`` →
    ``1234.50``/``1234.50``/``1234.5``. Signs are kept. (``"1,234"`` is read as one
    thousand two hundred thirty-four.)"""
    out: list[Decimal] = []
    for m in _NUMBER.finditer(text):
        val = _to_decimal(m.group(0))
        if val is not None:
            out.append(val)
    return out


def parse_price(text: str) -> tuple[Decimal, str | None] | None:
    """First price in ``text`` as ``(amount, currency_code)``: ``"Now only $1,299.00!"`` →
    ``(Decimal('1299.00'), 'USD')``. ``currency`` is ``None`` when no symbol/code is
    present; the whole result is ``None`` when there's no number at all."""
    cur_m = _CURRENCY_RE.search(text)
    nums = extract_numbers(text)
    if not nums:
        return None
    code = (
        _CURRENCY[next(k for k in _CURRENCY if k.lower() == cur_m.group(0).lower())]
        if cur_m
        else None
    )
    return nums[0], code


def extract_emails(text: str) -> list[str]:
    """Distinct e-mail addresses in order of appearance (lower-cased)."""
    return list(dict.fromkeys(m.group(0).lower() for m in _EMAIL.finditer(text)))


def extract_urls(text: str) -> list[str]:
    """Distinct ``http(s)`` URLs in order of appearance (trailing punctuation trimmed)."""
    return list(dict.fromkeys(m.group(0).rstrip(".,;:!?") for m in _URL.finditer(text)))


__all__ = [
    "clean_text",
    "extract_emails",
    "extract_numbers",
    "extract_urls",
    "normalize_whitespace",
    "parse_price",
    "slugify",
    "strip_accents",
    "strip_tags",
    "truncate",
]
