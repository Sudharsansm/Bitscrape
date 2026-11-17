"""
Registers every exporter added beyond the original JSON/JSONL/CSV/XML set
into :func:`bitscrape.exporters.feed.get_exporter`'s dispatch table, so
``get_exporter("parquet", path)`` (and therefore the CLI's ``--format``, and
``bitscrape.run(fmt=...)``) picks up all seventeen formats through the one
existing entry point, with no change to :mod:`bitscrape.exporters.feed`
itself. Importing this module (which :mod:`bitscrape.exporters` does)
mutates ``feed._EXPORTERS`` in place -- the same dict object
``feed.get_exporter`` already reads from -- rather than replacing it, so
every existing caller (the CLI imports ``get_exporter`` straight from
``feed``) benefits automatically.

An optional format's class is only usable once its dependency is installed
(``get_exporter("parquet", ...)`` raises a clear ``ImportError`` naming the
right ``pip install bitscrape[extra]`` otherwise -- see each module) -- but
it's always *listed*, so ``ValueError: Unknown feed format`` never happens
just because an extra isn't installed; the error names the missing package
instead.
"""

from __future__ import annotations

from bitscrape.exporters.binary import BSONExporter, MessagePackExporter, ProtobufExporter
from bitscrape.exporters.columnar import AvroExporter, ORCExporter, ParquetExporter
from bitscrape.exporters.documents import DocxExporter, MarkdownExporter, PDFExporter
from bitscrape.exporters.feed import _EXPORTERS
from bitscrape.exporters.structured import INIExporter, TOMLExporter, YAMLExporter
from bitscrape.exporters.warc import WARCExporter

_EXPORTERS.update(
    {
        "parquet": ParquetExporter,
        "orc": ORCExporter,
        "avro": AvroExporter,
        "yaml": YAMLExporter,
        "yml": YAMLExporter,
        "toml": TOMLExporter,
        "ini": INIExporter,
        "proto": ProtobufExporter,
        "protobuf": ProtobufExporter,
        "msgpack": MessagePackExporter,
        "bson": BSONExporter,
        "md": MarkdownExporter,
        "markdown": MarkdownExporter,
        "warc": WARCExporter,
        "pdf": PDFExporter,
        "docx": DocxExporter,
        "doc": DocxExporter,  # see DocxExporter's docstring: writes .docx content
    }
)

__all__: list[str] = []  # side-effect module; import it, don't import names from it
