"""
:class:`ItemList` -- a plain ``list`` of scraped items with one export method
per format, built on top of the same exporters
:func:`bitscrape.exporters.get_exporter` uses for file/CLI export::

    stats = await engine.run()       # Settings(collect_items=True)
    stats.items.to_json("out.json")  # write to a file
    text = stats.items.to_jsonl()    # or get the content back directly

``ItemList`` also works standalone, with items collected any other way::

    from bitscrape import ItemList
    items = ItemList([{"title": "Widget"}, {"title": "Gadget"}])
    items.to_parquet("out.parquet")

Every ``to_X`` method takes an optional ``path`` (``str`` or ``pathlib.Path``):
given one, it writes there (returning ``None``); omitted, it returns the
content directly -- ``str`` for a text format, ``bytes`` for a binary one --
without touching disk. A few formats take extra keyword arguments
(``to_parquet(compression=)``, ``to_avro(schema=)``) -- see the matching
exporter class in ``bitscrape.exporters`` for details.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path
from typing import Any, cast

from bitscrape.exporters import (
    AvroExporter,
    BaseExporter,
    BSONExporter,
    CSVExporter,
    DocxExporter,
    INIExporter,
    JSONExporter,
    JSONLExporter,
    MarkdownExporter,
    MessagePackExporter,
    ORCExporter,
    ParquetExporter,
    PDFExporter,
    ProtobufExporter,
    TOMLExporter,
    WARCExporter,
    XMLExporter,
    YAMLExporter,
)

#: Formats whose exporter writes text (everything else is binary) -- decides
#: whether the no-``path`` convenience path in :meth:`ItemList._run` reads its
#: temp file back as ``str`` or ``bytes``.
_TEXT_FORMATS = {"jsonl", "json", "csv", "xml", "yaml", "toml", "ini", "markdown"}


class ItemList(list):
    """A ``list`` of scraped items (``dict`` or pydantic model instances, the
    same two shapes every exporter already accepts) with a ``to_X()`` export
    method per supported format. See the module docstring for the optional
    ``path`` argument shared by all of them.
    """

    # -- the original four -----------------------------------------------

    def to_jsonl(self, path: str | Path | None = None) -> str | None:
        """JSON Lines: one JSON object per line."""
        return cast(str | None, self._run(JSONLExporter, "jsonl", path))

    def to_json(self, path: str | Path | None = None) -> str | None:
        """A single pretty-printed JSON array."""
        return cast(str | None, self._run(JSONExporter, "json", path))

    def to_csv(self, path: str | Path | None = None) -> str | None:
        """CSV, columns taken from the first item's keys."""
        return cast(str | None, self._run(CSVExporter, "csv", path))

    def to_xml(self, path: str | Path | None = None) -> str | None:
        """XML: one ``<item>`` element per item, one child element per field."""
        return cast(str | None, self._run(XMLExporter, "xml", path))

    # -- structured text ---------------------------------------------------

    def to_yaml(self, path: str | Path | None = None) -> str | None:
        """YAML, as a stream of ``---``-separated documents (one per item)."""
        return cast(str | None, self._run(YAMLExporter, "yaml", path))

    def to_toml(self, path: str | Path | None = None) -> str | None:
        """TOML, as a top-level array of tables: ``items = [ {...}, ... ]``."""
        return cast(str | None, self._run(TOMLExporter, "toml", path))

    def to_ini(self, path: str | Path | None = None) -> str | None:
        """INI, one section per item (``[item_0]``, ``[item_1]``, ...)."""
        return cast(str | None, self._run(INIExporter, "ini", path))

    def to_markdown(self, path: str | Path | None = None) -> str | None:
        """Markdown: one ``## Item N`` section with a bullet per field."""
        return cast(str | None, self._run(MarkdownExporter, "markdown", path))

    # -- binary, streamed ---------------------------------------------------

    def to_msgpack(self, path: str | Path | None = None) -> bytes | None:
        """MessagePack: a compact concatenated stream, one item after another."""
        return cast(bytes | None, self._run(MessagePackExporter, "msgpack", path))

    def to_bson(self, path: str | Path | None = None) -> bytes | None:
        """BSON: MongoDB's binary format, one encoded document after another."""
        return cast(bytes | None, self._run(BSONExporter, "bson", path))

    def to_protobuf(self, path: str | Path | None = None) -> bytes | None:
        """Protocol Buffers: one length-delimited ``Struct`` message per item."""
        return cast(bytes | None, self._run(ProtobufExporter, "protobuf", path))

    def to_warc(self, path: str | Path | None = None) -> bytes | None:
        """WARC: a ``warcinfo`` record, then one record per item. See
        ``bitscrape.exporters.warc.response_to_item`` for archiving real
        fetched responses instead of plain items."""
        return cast(bytes | None, self._run(WARCExporter, "warc", path))

    # -- columnar, buffered ---------------------------------------------------

    def to_parquet(
        self, path: str | Path | None = None, *, compression: str = "snappy"
    ) -> bytes | None:
        """Apache Parquet."""
        return cast(
            bytes | None, self._run(ParquetExporter, "parquet", path, compression=compression)
        )

    def to_orc(self, path: str | Path | None = None) -> bytes | None:
        """Apache ORC."""
        return cast(bytes | None, self._run(ORCExporter, "orc", path))

    def to_avro(
        self, path: str | Path | None = None, *, schema: dict[str, Any] | None = None
    ) -> bytes | None:
        """Apache Avro. Without ``schema=``, every field is written as a
        nullable string -- see ``AvroExporter`` for why, and how to pass a
        real typed schema."""
        return cast(bytes | None, self._run(AvroExporter, "avro", path, schema=schema))

    # -- documents, buffered ---------------------------------------------------

    def to_pdf(self, path: str | Path | None = None) -> bytes | None:
        """PDF: each item as its own page."""
        return cast(bytes | None, self._run(PDFExporter, "pdf", path))

    def to_docx(self, path: str | Path | None = None) -> bytes | None:
        """Word (.docx): each item as a heading plus one paragraph per field."""
        return cast(bytes | None, self._run(DocxExporter, "docx", path))

    # -- machinery ---------------------------------------------------

    def _run(
        self, exporter_cls: type[BaseExporter], fmt: str, path: str | Path | None, **kwargs: Any
    ) -> str | bytes | None:
        if path is not None:
            self._export_to(exporter_cls, str(path), **kwargs)
            return None
        # No path: run the real exporter against a private temp file (so
        # every format's actual, tested file-writing code runs unchanged --
        # no per-format special-casing of in-memory buffers, no touching
        # sys.stdout) and hand back its content instead of leaving it on disk.
        fd, tmp_path = tempfile.mkstemp(suffix=f".{fmt}")
        os.close(fd)
        try:
            self._export_to(exporter_cls, tmp_path, **kwargs)
            mode = "r" if fmt in _TEXT_FORMATS else "rb"
            kwargs_read = {"encoding": "utf-8"} if fmt in _TEXT_FORMATS else {}
            with open(tmp_path, mode, **kwargs_read) as f:  # type: ignore[call-overload]
                return f.read()
        finally:
            os.unlink(tmp_path)

    def _export_to(self, exporter_cls: type[BaseExporter], uri: str, **kwargs: Any) -> None:
        exporter = exporter_cls(uri, **kwargs)
        exporter.open()
        try:
            for item in self:
                exporter.export_item(item)
        finally:
            exporter.close()


__all__ = ["ItemList"]
