"""
Shared machinery for the exporters in ``bitscrape.exporters.*`` beyond the
original JSON/JSONL/CSV/XML set in :mod:`bitscrape.exporters.feed`.

Two things every new format needs, factored out here instead of repeated in
each module:

* :func:`normalize_rows` -- item dicts in a crawl are rarely uniform (an
  optional field only some items have, a field added mid-crawl after a site
  layout change). Handing such a list straight to a columnar library is
  dangerous: **pyarrow's ``Table.from_pylist`` silently uses only the first
  row's keys as the schema and drops any key that first appears later** --
  verified while building this module, not a hypothetical. This function
  computes the union of keys up front (in first-seen order) so every row gets
  every column (missing = ``None``), and JSON-encodes any value a target
  format can't represent natively (nested dict/list) rather than silently
  dropping or mangling it.
* :class:`BufferedExporter` -- a :class:`~bitscrape.exporters.feed.BaseExporter`
  for formats that must see every item before writing anything at all
  (columnar formats need the full column set; PDF/DOCX are paginated
  documents built as a whole) -- Parquet, ORC, Avro, TOML, PDF, DOCX. Handles
  binary-vs-text file opening and defers to :meth:`_write_all`.

Formats that *can* be written one item at a time as they arrive (safe for a
large crawl, since nothing is held in memory beyond the current item) --
YAML, INI, MessagePack, BSON, Protobuf, WARC, Markdown -- extend
``BaseExporter`` directly, the same way the original JSONL/CSV/XML exporters
do, and aren't affected by anything in this module.
"""

from __future__ import annotations

import json
import sys
from abc import abstractmethod
from pathlib import Path
from typing import Any, ClassVar

from bitscrape.exporters.feed import BaseExporter, _item_to_dict

__all__ = ["BufferedExporter", "normalize_rows", "require"]


def normalize_rows(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Every item widened to the union of all keys across ``items`` (missing
    = ``None``), with any nested ``dict``/``list``/``tuple`` value replaced by
    its JSON text -- so passing this to a columnar/typed format never
    silently drops a field or a nested structure. Column order matches each
    key's first appearance across the input.
    """
    columns: dict[str, None] = {}
    for item in items:
        for key in item:
            columns[key] = None
    out: list[dict[str, Any]] = []
    for item in items:
        row: dict[str, Any] = {}
        for col in columns:
            value = item.get(col)
            if isinstance(value, dict | list | tuple):
                value = json.dumps(value, default=str, ensure_ascii=False)
            row[col] = value
        out.append(row)
    return out


def require(module_name: str, extra: str, purpose: str) -> Any:
    """Import ``module_name`` or raise a clear, actionable ``ImportError``
    naming the ``pip install bitscrape[extra]`` that provides it, instead of
    a bare ``ModuleNotFoundError`` pointing at an internal import line."""
    import importlib

    try:
        return importlib.import_module(module_name)
    except ImportError as exc:
        raise ImportError(
            f"{purpose} requires the {module_name!r} package. "
            f"Install with: pip install bitscrape[{extra}]"
        ) from exc


class BufferedExporter(BaseExporter):
    """Base for a format that needs every item before writing anything.
    Subclasses implement :meth:`_write_all`, returning the complete file
    content as ``bytes`` (``binary = True``, the default) or ``str``.
    """

    binary: ClassVar[bool] = True

    def __init__(self, uri: str | None = None) -> None:
        super().__init__(uri)
        self._items: list[dict[str, Any]] = []
        self._out: Any = None

    def open(self) -> None:
        if self._uri and self._uri != "-":
            path = Path(self._uri)
            path.parent.mkdir(parents=True, exist_ok=True)
            self._out = path.open(
                "wb" if self.binary else "w", encoding=None if self.binary else "utf-8"
            )
        else:
            self._out = sys.stdout.buffer if self.binary else sys.stdout

    def export_item(self, item: Any) -> None:
        self._items.append(_item_to_dict(item))
        self._count += 1

    @abstractmethod
    def _write_all(self, items: list[dict[str, Any]]) -> bytes | str: ...

    def close(self) -> None:
        if self._out is None:
            return
        data = self._write_all(self._items)
        self._out.write(data)
        if self._out not in (sys.stdout, sys.stdout.buffer):
            self._out.close()
        import logging

        logging.getLogger(__name__).info("Exported %d items", self._count)
