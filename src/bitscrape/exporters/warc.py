"""
WARC (Web ARChive) export -- the format the Internet Archive and Common
Crawl use to store raw captured web resources.

WARC's natural unit is a *fetched HTTP resource* (request/response headers +
body), not an arbitrary key-value item -- the opposite of every other
exporter in this package. Two ways to use it:

1. Have your spider yield :func:`response_to_item` (built from the
   ``ParsedResponse``/``Response`` it's already holding) alongside, or
   instead of, your extracted data -- :class:`WARCExporter` then writes a
   real ``response`` record with the actual HTTP status/headers/body::

       async def parse(self, response):
           yield response_to_item(response)          # archive the raw page
           yield {"title": response.css("h1::text").get()}   # your extracted item

2. Export plain dict items as-is (no ``response_to_item``): each becomes a
   ``resource`` record whose payload is that item's JSON, and whose
   ``WARC-Target-URI`` is the item's own ``url`` field if it has one, else a
   generated ``urn:bitscrape:item:N``. This means ``ItemList.to_warc()`` /
   ``get_exporter("warc", ...)`` never fails on ordinary items -- it degrades
   to "one resource record per item" rather than requiring your spider to
   change what it yields.

Requires ``warcio`` (``pip install bitscrape[warc]``), the library Common
Crawl's own tooling is built on -- WARC's exact framing rules (header casing,
byte-accurate Content-Length, digests) are easy to get subtly wrong by hand.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

from bitscrape.exporters._base import require
from bitscrape.exporters.feed import BaseExporter, _item_to_dict

#: Marks an item built by :func:`response_to_item`, so :class:`WARCExporter`
#: (and nothing else -- this key is otherwise ignored, e.g. if such an item
#: reaches a different exporter) can tell it apart from an ordinary item.
_WARC_RESPONSE_MARKER = "_bitscrape_warc_response"


def response_to_item(response: Any) -> dict[str, Any]:
    """Wrap a ``ParsedResponse`` or ``Response`` as an item
    :class:`WARCExporter` archives as a real WARC ``response`` record
    (actual status/headers/body), instead of the generic ``resource`` record
    an ordinary dict item gets. Yield this from a spider callback like any
    other item.
    """
    raw = getattr(response, "response", response)  # unwrap ParsedResponse if needed
    return {
        _WARC_RESPONSE_MARKER: True,
        "url": raw.url,
        "status": raw.status,
        "headers": dict(raw.headers),
        "body": raw.body,
    }


class WARCExporter(BaseExporter):
    """Writes a WARC/1.0 file: one ``warcinfo`` record, then one record per
    item -- a ``response`` record for a :func:`response_to_item` item, a
    ``resource`` record (JSON payload) for any other item. Requires
    ``warcio`` (``pip install bitscrape[warc]``).

    Written one item at a time, exactly matching how WARC is meant to be
    used: a growing, appendable sequence of records, nothing held in memory
    beyond the current one.
    """

    def open(self) -> None:
        require("warcio", "warc", "WARC export")
        from warcio.warcwriter import WARCWriter

        if self._uri and self._uri != "-":
            path = Path(self._uri)
            path.parent.mkdir(parents=True, exist_ok=True)
            self._file = path.open("wb")  # type: ignore[assignment]
        else:
            self._file = sys.stdout.buffer  # type: ignore[assignment]
        self._writer = WARCWriter(self._file, gzip=False)
        self._writer.write_record(
            self._writer.create_warcinfo_record(
                self._uri or "<stdout>", {"software": "bitscrape", "format": "WARC File Format 1.0"}
            )
        )

    def export_item(self, item: Any) -> None:
        d = _item_to_dict(item)
        if d.get(_WARC_RESPONSE_MARKER):
            self._write_response_record(d)
        else:
            self._write_resource_record(d)
        self._count += 1

    def _write_response_record(self, d: dict[str, Any]) -> None:
        import io

        from warcio.statusandheaders import StatusAndHeaders

        body = d["body"] if isinstance(d["body"], bytes) else str(d["body"]).encode("utf-8")
        header_pairs = list(d.get("headers", {}).items())
        status_line = f"{d['status']} {_status_reason(d['status'])}"
        http_headers = StatusAndHeaders(status_line, header_pairs, protocol="HTTP/1.1")
        record = self._writer.create_warc_record(
            d["url"], "response", payload=io.BytesIO(body), http_headers=http_headers
        )
        self._writer.write_record(record)

    def _write_resource_record(self, d: dict[str, Any]) -> None:
        import io
        import json

        target_uri = d.get("url") or f"urn:bitscrape:item:{self._count}"
        payload = json.dumps(d, default=str, ensure_ascii=False).encode("utf-8")
        record = self._writer.create_warc_record(
            target_uri,
            "resource",
            payload=io.BytesIO(payload),
            warc_content_type="application/json",
        )
        self._writer.write_record(record)

    def close(self) -> None:
        if self._file and self._file not in (sys.stdout, sys.stdout.buffer):
            self._file.close()
        import logging

        logging.getLogger(__name__).info("Exported %d WARC records", self._count)


_HTTP_REASONS = {
    200: "OK",
    201: "Created",
    204: "No Content",
    301: "Moved Permanently",
    302: "Found",
    304: "Not Modified",
    400: "Bad Request",
    401: "Unauthorized",
    403: "Forbidden",
    404: "Not Found",
    429: "Too Many Requests",
    500: "Internal Server Error",
    503: "Service Unavailable",
}


def _status_reason(status: int) -> str:
    return _HTTP_REASONS.get(int(status), "Unknown")
