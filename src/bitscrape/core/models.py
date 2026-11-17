"""
Core Pydantic models for Bitscrape.
All data contracts are strongly typed — no bare dicts in the hot path.
"""

from __future__ import annotations

import json as _json
import time
from enum import Enum
from typing import Any
from urllib.parse import urljoin as _urljoin
from uuid import uuid4

from pydantic import AliasChoices, BaseModel, ConfigDict, Field

from bitscrape import jsonx
from bitscrape.itemlist import ItemList

# ---------------------------------------------------------------------------
# Request / Response
# ---------------------------------------------------------------------------


class RequestPriority(int, Enum):
    HIGH = 0
    NORMAL = 5
    LOW = 10


class Request(BaseModel):
    """A crawl request enqueued by the scheduler."""

    url: str
    method: str = "GET"
    headers: dict[str, str] = Field(default_factory=dict)
    body: bytes | None = None
    meta: dict[str, Any] = Field(default_factory=dict)
    retries: int = 0
    max_retries: int = 3
    priority: RequestPriority = RequestPriority.NORMAL
    use_playwright: bool = False
    callback: str = "parse"
    errback: str | None = None
    fingerprint: str | None = None
    depth: int = 0
    request_id: str = Field(default_factory=lambda: str(uuid4()))
    # --- Added in 0.9 (all optional; defaults preserve prior behaviour) -----
    # Bypass the scheduler's duplicate filter (retries, re-tries after a
    # blocked response, deliberate re-fetches).
    dont_filter: bool = False
    # Id of a registered fetch session (any id declared in ``Settings.sessions``,
    # ``Spider.sessions`` or added in ``Spider.configure_sessions()``). ``None``
    # keeps the default routing (the default session, or a per-request browser
    # when ``use_playwright``). ``sid`` is accepted as an alias.
    session: str | None = Field(default=None, validation_alias=AliasChoices("session", "sid"))
    # Per-request options forwarded to the session, overriding its defaults.
    # HTTP sessions: ``headers``, ``cookies``, ``proxy``, ``timeout``,
    # ``follow_redirects``. Browser sessions: ``wait_until``, ``wait_selector``,
    # ``timeout``, ``scroll``, ``screenshot``, ``capture_xhr``. Must be
    # JSON-serialisable so requests survive checkpoints and Redis queues.
    session_kwargs: dict[str, Any] = Field(default_factory=dict)
    # Extra keyword arguments passed to the callback: ``callback(response, **cb_kwargs)``.
    cb_kwargs: dict[str, Any] = Field(default_factory=dict)
    # Per-request cookies, merged into the outgoing ``Cookie`` header.
    cookies: dict[str, str] = Field(default_factory=dict)

    model_config = ConfigDict(arbitrary_types_allowed=True, populate_by_name=True)

    @property
    def sid(self) -> str | None:
        """Alias for :attr:`session` (the session id this request is routed to)."""
        return self.session


class Response(BaseModel):
    """An HTTP response returned by the downloader."""

    url: str
    status: int
    headers: dict[str, str] = Field(default_factory=dict)
    body: bytes = b""
    request: Request
    elapsed_ms: float = 0.0
    encoding: str = "utf-8"
    # Negotiated protocol, e.g. "1.1", "2", "3" (None when unknown).
    http_version: str | None = None
    # Fetcher-specific extras: captured XHR/fetch calls, screenshots, cookies,
    # console messages, ... (see ``bitscrape.fetchers.browser``).
    extra: dict[str, Any] = Field(default_factory=dict)
    # Id of the session that fetched this response (``None`` for a legacy
    # per-request browser fetch).
    session_id: str | None = None

    @property
    def text(self) -> str:
        return self.body.decode(self.encoding, errors="replace")

    @property
    def ok(self) -> bool:
        return 200 <= self.status < 300

    @property
    def meta(self) -> dict[str, Any]:
        """Shortcut for ``response.request.meta``."""
        return self.request.meta

    # Deliberately shadows pydantic's deprecated ``BaseModel.json()`` (v1 API, superseded by
    # ``model_dump_json``): for an HTTP response ``.json()`` means "decode the body".
    def json(self) -> Any:  # type: ignore[override]
        """Parse the body as JSON (raises ``ValueError`` if it isn't)."""
        return (
            jsonx.loads(self.body)
            if self.encoding.lower().replace("-", "") == "utf8"
            else _json.loads(self.body.decode(self.encoding, errors="replace"))
        )

    def urljoin(self, url: str) -> str:
        """Resolve ``url`` (possibly relative) against this response's URL."""
        return _urljoin(self.url, url)

    model_config = {"arbitrary_types_allowed": True}


# ---------------------------------------------------------------------------
# Items – scraped data entities
# ---------------------------------------------------------------------------


class BaseItem(BaseModel):
    """All scraped item models should inherit from this."""

    source_url: str = ""
    scraped_at: float = Field(default_factory=time.time)

    model_config = {"arbitrary_types_allowed": True, "extra": "allow"}


# ---------------------------------------------------------------------------
# Stats
# ---------------------------------------------------------------------------


class CrawlStats(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)

    requests_made: int = 0
    requests_failed: int = 0
    responses_received: int = 0
    items_scraped: int = 0
    items_dropped: int = 0
    items_noindexed: int = 0  # skipped due to meta-robots noindex
    requests_blocked: int = 0  # responses classified as anti-bot blocks
    requests_retried_blocked: int = 0  # re-queued because of a block
    cache_hits: int = 0  # served from the dev-mode response cache
    # Per-session breakdowns (keyed by session id).
    sessions_requests: dict[str, int] = Field(default_factory=dict)
    sessions_failed: dict[str, int] = Field(default_factory=dict)
    sessions_blocked: dict[str, int] = Field(default_factory=dict)
    requests_escalated: int = 0  # blocked requests re-queued via retry_blocked_request()
    # Requests deliberately dropped by a process_request middleware
    # (robots.txt Disallow, allowed_domains/offsite) before ever being
    # fetched -- NOT counted in requests_failed, since this is policy, not
    # an error.
    requests_dropped: int = 0
    requests_offsite_dropped: int = 0  # subset of requests_dropped: allowed_domains
    # Every successfully-piped item, in memory -- only populated when
    # Settings.collect_items=True (off by default: a large crawl shouldn't
    # silently buffer millions of items). Exposes .to_json()/.to_csv()/
    # .to_parquet()/... for all 17 built-in export formats -- see
    # bitscrape.ItemList and docs/exporters.md.
    items: ItemList = Field(default_factory=ItemList)
    # AutoThrottle (only populated when autothrottle_enabled)
    throttle_backoffs: int = 0  # block / rate-limit signals that slowed a domain down
    throttle_cooldowns: int = 0  # Retry-After cool-downs applied
    throttle_wait_seconds: float = 0.0  # total time spent waiting on per-domain pacing
    throttle_delays: dict[str, float] = Field(default_factory=dict)  # learned delay per domain
    finish_reason: str = "finished"  # finished | paused | cancelled | closespider
    links_nofollow_skipped: int = 0  # links not enqueued due to meta-robots nofollow
    bytes_downloaded: int = 0
    start_time: float = Field(default_factory=time.time)
    finish_time: float | None = None

    @property
    def elapsed(self) -> float:
        end = self.finish_time or time.time()
        return end - self.start_time

    @property
    def rps(self) -> float:
        e = self.elapsed
        return self.requests_made / e if e > 0 else 0.0
