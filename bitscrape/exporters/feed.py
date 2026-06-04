"""
Bitscrape Feed Exporters
========================
Write scraped items to files or stdout.

Supported formats: jsonl, json, csv, xml
"""

from __future__ import annotations

import csv
import json
import logging
import sys
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any, TextIO

from pydantic import BaseModel

logger = logging.getLogger(__name__)


def _item_to_dict(item: Any) -> dict[str, Any]:
    if isinstance(item, BaseModel):
        return item.model_dump(mode="json")
    if isinstance(item, dict):
        return item
    raise TypeError(f"Cannot export item of type {type(item)}")


# ---------------------------------------------------------------------------
# Base
# ---------------------------------------------------------------------------


class BaseExporter(ABC):
    def __init__(self, uri: str | None = None) -> None:
        self._uri = uri
        self._file: TextIO | None = None
        self._count = 0

    def open(self) -> None:
        if self._uri and self._uri != "-":
            p = Path(self._uri)
            p.parent.mkdir(parents=True, exist_ok=True)
            self._file = p.open("w", encoding="utf-8")
            logger.info("Exporting to %s", p)
        else:
            self._file = sys.stdout

    @abstractmethod
    def export_item(self, item: Any) -> None: ...

    def close(self) -> None:
        if self._file and self._file is not sys.stdout:
            self._file.close()
        logger.info("Exported %d items", self._count)


# ---------------------------------------------------------------------------
# JSONL (one JSON object per line)  ← fastest / recommended for large datasets
# ---------------------------------------------------------------------------


class JSONLExporter(BaseExporter):
    def export_item(self, item: Any) -> None:
        assert self._file is not None
        d = _item_to_dict(item)
        self._file.write(json.dumps(d, ensure_ascii=False) + "\n")
        self._count += 1


# ---------------------------------------------------------------------------
# JSON array
# ---------------------------------------------------------------------------


class JSONExporter(BaseExporter):
    def __init__(self, uri: str | None = None) -> None:
        super().__init__(uri)
        self._items: list[dict] = []

    def export_item(self, item: Any) -> None:
        self._items.append(_item_to_dict(item))
        self._count += 1

    def close(self) -> None:
        assert self._file is not None
        json.dump(self._items, self._file, ensure_ascii=False, indent=2)
        super().close()


# ---------------------------------------------------------------------------
# CSV
# ---------------------------------------------------------------------------


class CSVExporter(BaseExporter):
    def __init__(self, uri: str | None = None) -> None:
        super().__init__(uri)
        self._writer: csv.DictWriter | None = None

    def export_item(self, item: Any) -> None:
        assert self._file is not None
        d = _item_to_dict(item)
        if self._writer is None:
            self._writer = csv.DictWriter(self._file, fieldnames=list(d.keys()))
            self._writer.writeheader()
        self._writer.writerow(d)
        self._count += 1


# ---------------------------------------------------------------------------
# XML
# ---------------------------------------------------------------------------


class XMLExporter(BaseExporter):
    def open(self) -> None:
        super().open()
        assert self._file is not None
        self._file.write('<?xml version="1.0" encoding="UTF-8"?>\n<items>\n')

    def export_item(self, item: Any) -> None:
        assert self._file is not None
        d = _item_to_dict(item)
        self._file.write("  <item>\n")
        for k, v in d.items():
            self._file.write(f"    <{k}>{v}</{k}>\n")
        self._file.write("  </item>\n")
        self._count += 1

    def close(self) -> None:
        if self._file:
            self._file.write("</items>\n")
        super().close()


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------

_EXPORTERS: dict[str, type[BaseExporter]] = {
    "jsonl": JSONLExporter,
    "json": JSONExporter,
    "csv": CSVExporter,
    "xml": XMLExporter,
}


def get_exporter(fmt: str, uri: str | None = None) -> BaseExporter:
    cls = _EXPORTERS.get(fmt.lower())
    if cls is None:
        raise ValueError(f"Unknown feed format {fmt!r}. Choose from: {list(_EXPORTERS)}")
    return cls(uri)
