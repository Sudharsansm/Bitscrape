"""
Blocked-request detection
==========================

A site can "block" a crawler in ways that don't show up as a clean
transport error: a ``200 OK`` interstitial page ("Access Denied", "Rate
limit exceeded", a maintenance notice), or a status code your general
``retry_http_codes`` list doesn't already cover for this particular site.
:class:`BlockedRequestDetector` centralises "does this response look like
a block?" so the downloader can retry with backoff (and, if a
:class:`~bitscrape.downloader.proxy.ProxyRotator` is configured, a
different proxy) instead of quietly treating the interstitial as real
content.

This is pattern *matching* against markers you configure, not fingerprint
spoofing or challenge-solving: it doesn't try to get past a block, it just
notices one happened and asks the caller to back off and retry -- the same
behaviour a polite human crawler operator would take on seeing "you've
been rate-limited", not an attempt to defeat the site's protection.
"""

from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass
class BlockedRequestDetector:
    """
    Args:
        blocked_http_codes: status codes that, on their own, count as a block
            for this detector's purposes (independent of the downloader's
            general ``retry_http_codes``).
        extra_markers: substrings or regex patterns to look for in the response
            body (case-insensitive) that indicate a block page, e.g.
            ``["access denied", r"rate limit(ed)?", "unusual traffic"]``.
        max_retries: how many times a blocked response is retried before giving up.
        backoff_base: base seconds for backoff, doubled per attempt (capped at
            ``backoff_max``).
        backoff_max: cap on the backoff delay, however many attempts have
            happened.
    """

    blocked_http_codes: frozenset[int]
    extra_markers: tuple[str, ...] = ()
    max_retries: int = 3
    backoff_base: float = 2.0
    backoff_max: float = 30.0

    def __post_init__(self) -> None:
        self.blocked_http_codes = frozenset(self.blocked_http_codes)
        self._compiled = [re.compile(m, re.IGNORECASE) for m in self.extra_markers]

    def is_blocked(self, status: int, body: bytes | None = None) -> bool:
        """True if this response looks like a block: a configured status
        code, or a configured marker pattern found in the body."""
        if status in self.blocked_http_codes:
            return True
        if body and self._compiled:
            try:
                text = body.decode("utf-8", errors="replace")
            except Exception:  # noqa: BLE001 -- decoding must never break detection
                return False
            return any(pattern.search(text) for pattern in self._compiled)
        return False

    def backoff_seconds(self, attempt: int) -> float:
        """Exponential backoff for the given (1-indexed) retry attempt."""
        return min(self.backoff_base * (2 ** max(0, attempt - 1)), self.backoff_max)

    @classmethod
    def from_settings(cls, settings: "object") -> "BlockedRequestDetector":
        """Build a detector from a ``bitscrape.core.settings.Settings`` instance."""
        return cls(
            blocked_http_codes=frozenset(getattr(settings, "blocked_http_codes", ())),
            extra_markers=tuple(getattr(settings, "blocked_extra_markers", ())),
            max_retries=getattr(settings, "blocked_max_retries", 3),
            backoff_base=getattr(settings, "blocked_retry_backoff", 2.0),
            backoff_max=getattr(settings, "blocked_max_backoff", 30.0),
        )
