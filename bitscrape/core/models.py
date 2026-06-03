"""
Core Pydantic models for Bitscrape.
All data contracts are strongly typed — no bare dicts in the hot path.
"""

from __future__ import annotations

import time
from enum import Enum
from typing import Any
from uuid import uuid4

from pydantic import BaseModel, Field

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

    model_config = {"arbitrary_types_allowed": True}


class Response(BaseModel):
    """An HTTP response returned by the downloader."""

    url: str
    status: int
    headers: dict[str, str] = Field(default_factory=dict)
    body: bytes = b""
    request: Request
    elapsed_ms: float = 0.0
    encoding: str = "utf-8"

    @property
    def text(self) -> str:
        return self.body.decode(self.encoding, errors="replace")

    @property
    def ok(self) -> bool:
        return 200 <= self.status < 300

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
    requests_made: int = 0
    requests_failed: int = 0
    responses_received: int = 0
    items_scraped: int = 0
    items_dropped: int = 0
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
