"""Exceptions shared across bitscrape's direct-fetch APIs.

Kept in a leaf module (importing nothing else from bitscrape) so that both
the HTTP client (:mod:`bitscrape.fetchers.http`) and the browser core
(:mod:`bitscrape.browser`) can raise them without importing each other.
Everything is re-exported from :mod:`bitscrape` and :mod:`bitscrape.fetchers`.
"""

from __future__ import annotations


class FetchError(Exception):
    """A request failed at the network level (connection refused, timeout, TLS
    error, redirect loop...) and any retries were used up. ``__cause__`` holds
    the underlying library exception; ``retryable`` says whether retrying could
    ever have helped (a redirect loop or malformed URL never does)."""

    def __init__(self, message: str, *, url: str = "", retryable: bool = True) -> None:
        super().__init__(message)
        self.url = url
        self.retryable = retryable


class DynamicFetchError(FetchError):
    """A browser fetch failed: navigation error or timeout, a ``page_action`` /
    ``page_setup`` hook that raised, a URL that triggers a file download, ...
    A :class:`FetchError` subclass, so ``except FetchError`` catches both HTTP
    and browser failures. ``__cause__`` holds the original exception (a
    Playwright error, or whatever your hook raised)."""


class BrowserLaunchError(DynamicFetchError):
    """The browser itself couldn't be started: Playwright's browsers aren't
    installed, Google Chrome isn't installed, ``executable_path`` is wrong, or
    the ``cdp_url`` isn't reachable. A configuration problem, never retryable;
    the message says how to fix it."""

    def __init__(self, message: str, *, url: str = "") -> None:
        super().__init__(message, url=url, retryable=False)


__all__ = ["BrowserLaunchError", "DynamicFetchError", "FetchError"]
