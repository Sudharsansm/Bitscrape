"""Direct fetching without a spider: plain HTTP (:mod:`bitscrape.fetchers.http`,
``docs/fetchers.md``) and real-browser fetching of JavaScript-driven pages
(:mod:`bitscrape.fetchers.dynamic`, ``docs/dynamic-fetcher.md``)."""

from bitscrape.errors import BrowserLaunchError, DynamicFetchError
from bitscrape.fetchers.dynamic import (
    AsyncDynamicFetcher,
    AsyncDynamicSession,
    DynamicFetcher,
    DynamicSession,
)
from bitscrape.fetchers.http import (
    IDEMPOTENT_METHODS,
    RETRY_STATUSES,
    AsyncFetcher,
    AsyncFetcherSession,
    Fetcher,
    FetchError,
    FetcherSession,
)

__all__ = [
    "IDEMPOTENT_METHODS",
    "RETRY_STATUSES",
    "AsyncDynamicFetcher",
    "AsyncDynamicSession",
    "AsyncFetcher",
    "AsyncFetcherSession",
    "BrowserLaunchError",
    "DynamicFetchError",
    "DynamicFetcher",
    "DynamicSession",
    "FetchError",
    "Fetcher",
    "FetcherSession",
]
