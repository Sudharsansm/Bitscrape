"""
Background API capture: a bounded, searchable network history
==============================================================

Turn on ``record_requests=True`` on a browser session and every *completed*
request the browser makes -- document loads, XHR/fetch calls, scripts,
redirects, HTTP 4xx/5xx -- is saved to :attr:`BrowserSession.network`, across
page loads, tabs and (for the session's single shared context) actions. Search
it afterwards instead of re-requesting anything::

    async with AsyncDynamicSession(record_requests=True) as s:
        await s.fetch("https://example.com/app", network_idle=True)
        for r in s.network.search(url_pattern=r"/api/", resource_type="fetch", limit=None):
            print(r.status, r.url, r.json())

Only text-like bodies are saved (``text/*``, JSON, XML, JavaScript, GraphQL,
URL-encoded forms, ``+json``/``+xml``); anything else keeps its metadata but an
empty body and a ``body_note`` saying why. The history is **bounded**: at most
``max_entries`` requests (default 1,000), ``max_body_bytes`` per body (1 MiB)
and ``max_total_bytes`` of body bytes overall (20 MiB); the oldest entries are
evicted first. IDs only ever grow -- they are never reused after eviction or
:meth:`NetworkRecorder.clear`.

``search`` / ``get`` / ``clear`` are plain local calls (no ``await``), also on
async sessions. Beyond what a plain recorder offers, :meth:`NetworkRecorder.to_har`
exports the history as a HAR 1.2 document and :meth:`NetworkRecorder.find_json`
digs a key out of every JSON API response.

Failed and unfinished requests are not recorded. Captures still reading a body
when the page closes may be omitted; call :meth:`NetworkRecorder.drain` (the
fetchers do) to let in-flight ones finish.
"""

from __future__ import annotations

import asyncio
import json as _json
import logging
import re
import time
from collections import OrderedDict
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from typing import Any

from bitscrape import jsonx

logger = logging.getLogger("bitscrape.network")

DEFAULT_MAX_ENTRIES = 1_000
DEFAULT_MAX_BODY_BYTES = 1 * 1024 * 1024
DEFAULT_MAX_TOTAL_BYTES = 20 * 1024 * 1024
DRAIN_TIMEOUT = 5.0

_TEXT_TYPES = (
    "text/",
    "application/json",
    "application/xml",
    "application/javascript",
    "application/x-javascript",
    "application/ecmascript",
    "application/graphql",
    "application/x-www-form-urlencoded",
    "application/ld+json",
)
_NO_BODY_STATUS = frozenset({204, 205, 304})
_STATIC_TYPES = frozenset(
    {"document", "stylesheet", "image", "media", "font", "script", "manifest", "texttrack", "other"}
)


def is_text_content_type(content_type: str | None) -> bool:
    """Whether a ``Content-Type`` is one whose body the recorder saves."""
    if not content_type:
        return False
    ctype = content_type.split(";", 1)[0].strip().lower()
    if ctype.startswith(_TEXT_TYPES):
        return True
    return ctype.endswith(("+json", "+xml", "/json", "/xml"))


def _charset(content_type: str) -> str | None:
    m = re.search(r"charset=([\w.-]+)", content_type or "", re.IGNORECASE)
    return m.group(1) if m else None


@dataclass
class RecordedResponse:
    """One saved request/response pair. Reads are local -- nothing here awaits."""

    network_id: int
    url: str
    method: str
    status: int
    headers: dict[str, str] = field(default_factory=dict)
    request_headers: dict[str, str] = field(default_factory=dict)
    resource_type: str = "other"
    request_body: bytes | None = None
    body: bytes = b""
    body_note: str | None = None
    started_at: float = field(default_factory=time.time)
    page_url: str | None = None

    # -- Response-like conveniences ------------------------------------------

    @property
    def ok(self) -> bool:
        return 200 <= self.status < 300

    @property
    def content_type(self) -> str:
        for k, v in self.headers.items():
            if k.lower() == "content-type":
                return v
        return ""

    @property
    def encoding(self) -> str:
        return _charset(self.content_type) or "utf-8"

    @property
    def text(self) -> str:
        return self.body.decode(self.encoding, errors="replace")

    @property
    def meta(self) -> dict[str, Any]:
        """``network_id``, ``resource_type``, ``request_body`` and (when the body
        wasn't saved) ``body_note`` -- mirrors what a fetched response carries."""
        m: dict[str, Any] = {
            "network_id": self.network_id,
            "resource_type": self.resource_type,
            "request_body": self.request_body,
        }
        if self.body_note:
            m["body_note"] = self.body_note
        return m

    def json(self) -> Any:
        """Parse the saved body as JSON (``ValueError`` if it isn't)."""
        return (
            jsonx.loads(self.body)
            if self.encoding.lower().replace("-", "") == "utf8"
            else _json.loads(self.text)
        )

    def parsed(self) -> Any:
        """A :class:`~bitscrape.parser.selector.ParsedResponse` over this response,
        for CSS/XPath. An unparseable or empty body gives an empty document."""
        from bitscrape.core.models import Request, Response
        from bitscrape.parser.selector import ParsedResponse

        return ParsedResponse(
            Response(
                url=self.url,
                status=self.status,
                headers=dict(self.headers),
                body=self.body,
                request=Request(
                    url=self.url, method=self.method, headers=dict(self.request_headers)
                ),
                encoding=self.encoding,
            )
        )

    def css(self, query: str) -> Any:
        return self.parsed().css(query)

    def xpath(self, query: str) -> Any:
        return self.parsed().xpath(query)

    def to_dict(self) -> dict[str, Any]:
        return {
            "network_id": self.network_id,
            "url": self.url,
            "method": self.method,
            "status": self.status,
            "resource_type": self.resource_type,
            "content_type": self.content_type,
            "body_bytes": len(self.body),
            "body_note": self.body_note,
        }


def _walk_json(obj: Any, key: str) -> Iterable[Any]:
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k == key:
                yield v
            yield from _walk_json(v, key)
    elif isinstance(obj, list):
        for item in obj:
            yield from _walk_json(item, key)


class NetworkRecorder:
    """Bounded history of completed browser requests -- see the module docstring."""

    def __init__(
        self,
        max_entries: int = DEFAULT_MAX_ENTRIES,
        max_body_bytes: int = DEFAULT_MAX_BODY_BYTES,
        max_total_bytes: int = DEFAULT_MAX_TOTAL_BYTES,
    ) -> None:
        if max_entries < 1:
            raise ValueError("max_entries must be >= 1")
        if max_body_bytes < 0 or max_total_bytes < 0:
            raise ValueError("byte limits must be >= 0")
        self.max_entries = max_entries
        self.max_body_bytes = max_body_bytes
        self.max_total_bytes = max_total_bytes
        self._entries: OrderedDict[int, RecordedResponse] = OrderedDict()
        self._next_id = 1
        self._total_bytes = 0
        self._tasks: set[asyncio.Task[None]] = set()
        self.evicted = 0

    # -- reading -------------------------------------------------------------

    def __len__(self) -> int:
        return len(self._entries)

    @property
    def total_body_bytes(self) -> int:
        return self._total_bytes

    def get(self, request_id: int) -> RecordedResponse | None:
        """The saved response with this ``network_id``, or ``None`` if it was never
        saved or has been evicted."""
        return self._entries.get(request_id)

    def search(
        self,
        url_pattern: str | None = None,
        method: str | None = None,
        resource_type: str | None = None,
        status: int | Iterable[int] | Callable[[int], bool] | None = None,
        after_id: int = 0,
        limit: int | None = 100,
        include_static: bool = True,
    ) -> list[RecordedResponse]:
        """Saved responses in ``network_id`` order.

        Args:
            url_pattern: regular expression searched in the URL (an invalid
                pattern raises ``re.error`` here, at search time).
            method / resource_type: exact match, case-insensitive.
            status: an int, an iterable of ints, or a predicate.
            after_id: only entries with a larger ``network_id``.
            limit: max results (default 100); ``None`` = every retained match.
            include_static: ``False`` keeps only XHR/fetch calls and HTTP errors
                (4xx/5xx) -- the usual "API traffic" view.
        """
        pattern = re.compile(url_pattern) if url_pattern else None
        wanted_method = method.upper() if method else None
        wanted_type = resource_type.lower() if resource_type else None
        if status is None or callable(status):
            status_ok = status
        elif isinstance(status, int):
            status_ok = lambda s, _w=status: s == _w  # noqa: E731
        else:
            allowed = frozenset(status)
            status_ok = lambda s, _a=allowed: s in _a  # noqa: E731
        out: list[RecordedResponse] = []
        for rid in sorted(self._entries):
            if rid <= after_id:
                continue
            r = self._entries[rid]
            if pattern is not None and not pattern.search(r.url):
                continue
            if wanted_method and r.method.upper() != wanted_method:
                continue
            if wanted_type and r.resource_type.lower() != wanted_type:
                continue
            if status_ok is not None and not status_ok(r.status):
                continue
            if not include_static and r.resource_type not in ("xhr", "fetch") and r.status < 400:
                continue
            out.append(r)
            if limit is not None and len(out) >= limit:
                break
        return out

    def find_json(self, key: str, **search_kwargs: Any) -> list[Any]:
        """Every value stored under ``key`` (at any depth) in the JSON bodies of the
        responses :meth:`search` selects -- e.g. ``find_json("price", url_pattern="/api/")``."""
        search_kwargs.setdefault("limit", None)
        found: list[Any] = []
        for r in self.search(**search_kwargs):
            if not r.body:
                continue
            try:
                data = r.json()
            except ValueError:
                continue
            found.extend(_walk_json(data, key))
        return found

    def clear(self) -> None:
        """Drop the whole history. Request IDs keep increasing -- never reused."""
        self._entries.clear()
        self._total_bytes = 0

    def to_har(self) -> dict[str, Any]:
        """The history as a HAR 1.2 dict (``json.dump`` it to open in browser devtools)."""
        entries = []
        for r in self.search(limit=None):
            text = r.text if r.body else ""
            entries.append(
                {
                    "startedDateTime": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(r.started_at))
                    + f".{int((r.started_at % 1) * 1000):03d}Z",
                    "time": 0,
                    "request": {
                        "method": r.method,
                        "url": r.url,
                        "httpVersion": "HTTP/1.1",
                        "headers": [{"name": k, "value": v} for k, v in r.request_headers.items()],
                        "queryString": [],
                        "cookies": [],
                        "headersSize": -1,
                        "bodySize": len(r.request_body or b""),
                    },
                    "response": {
                        "status": r.status,
                        "statusText": "",
                        "httpVersion": "HTTP/1.1",
                        "headers": [{"name": k, "value": v} for k, v in r.headers.items()],
                        "cookies": [],
                        "content": {
                            "size": len(r.body),
                            "mimeType": r.content_type.split(";", 1)[0],
                            "text": text,
                        },
                        "redirectURL": r.headers.get("location", ""),
                        "headersSize": -1,
                        "bodySize": len(r.body),
                    },
                    "cache": {},
                    "timings": {"send": 0, "wait": 0, "receive": 0},
                    "_resourceType": r.resource_type,
                    "_networkId": r.network_id,
                }
            )
        from bitscrape import __version__

        return {
            "log": {
                "version": "1.2",
                "creator": {"name": "bitscrape", "version": __version__},
                "entries": entries,
            }
        }

    # -- recording -----------------------------------------------------------

    def attach(self, target: Any) -> None:
        """Listen to ``response`` events on a Playwright context (or page)."""
        target.on("response", self._on_response)

    def _on_response(self, response: Any) -> None:
        try:
            request = response.request
            rid = self._next_id
            self._next_id += 1
            entry = RecordedResponse(
                network_id=rid,
                url=response.url,
                method=request.method,
                status=response.status,
                headers=dict(response.headers),
                request_headers=dict(getattr(request, "headers", {}) or {}),
                resource_type=getattr(request, "resource_type", "other"),
            )
            try:
                post = request.post_data_buffer
                entry.request_body = bytes(post) if post is not None else None
            except Exception:  # noqa: BLE001 -- not every request has a body buffer
                entry.request_body = None
            try:
                entry.page_url = request.frame.url
            except Exception:  # noqa: BLE001
                entry.page_url = None
        except Exception:
            logger.debug("Could not record a response", exc_info=True)
            return
        task = asyncio.ensure_future(self._finish(entry, response))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _finish(self, entry: RecordedResponse, response: Any) -> None:
        # Fuller request headers (cookies, ...) are only available asynchronously.
        try:
            entry.request_headers = dict(await response.request.all_headers())
        except Exception:  # noqa: BLE001 -- keep the partial headers
            pass
        if entry.status in _NO_BODY_STATUS or entry.method.upper() == "HEAD":
            pass
        elif not is_text_content_type(entry.content_type):
            entry.body_note = "Non-text body; not saved."
        else:
            try:
                raw = await response.body()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 -- page closed, redirect body, ...
                first = str(exc).splitlines()[0][:200] if str(exc) else type(exc).__name__
                entry.body_note = f"Body unreadable: {first}"
            else:
                if len(raw) > self.max_body_bytes:
                    entry.body_note = (
                        f"Body too large ({len(raw)} bytes > {self.max_body_bytes}); not saved."
                    )
                else:
                    entry.body = raw
        self._store(entry)

    def _store(self, entry: RecordedResponse) -> None:
        self._entries[entry.network_id] = entry
        self._total_bytes += len(entry.body)
        while self._entries and (
            len(self._entries) > self.max_entries or self._total_bytes > self.max_total_bytes
        ):
            _, old = self._entries.popitem(last=False)
            self._total_bytes -= len(old.body)
            self.evicted += 1

    async def drain(self, timeout: float = DRAIN_TIMEOUT) -> None:
        """Wait up to ``timeout`` seconds for in-flight body reads (cancelling the rest)."""
        pending = list(self._tasks)
        if not pending:
            return
        _done, not_done = await asyncio.wait(pending, timeout=timeout)
        for task in not_done:
            task.cancel()
        if not_done:
            await asyncio.gather(*not_done, return_exceptions=True)


__all__ = [
    "DEFAULT_MAX_BODY_BYTES",
    "DEFAULT_MAX_ENTRIES",
    "DEFAULT_MAX_TOTAL_BYTES",
    "NetworkRecorder",
    "RecordedResponse",
    "is_text_content_type",
]
