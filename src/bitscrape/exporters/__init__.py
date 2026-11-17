"""
Bitscrape's exporters: write scraped items to a file (or stdout) in any of
seventeen formats, through one factory function, :func:`get_exporter`.

Format         | Key(s)              | Extra              | Streamed?
---------------|---------------------|--------------------|----------
JSON Lines     | ``jsonl``           | (built in)         | yes
JSON           | ``json``            | (built in)         | no (array)
CSV            | ``csv``             | (built in)         | yes
XML            | ``xml``             | (built in)         | yes
YAML           | ``yaml``, ``yml``   | ``yaml``           | yes
TOML           | ``toml``            | ``toml``           | no
INI            | ``ini``             | (stdlib)           | yes
Markdown       | ``md``, ``markdown``| (stdlib)           | yes
MessagePack    | ``msgpack``         | ``msgpack``        | yes
BSON           | ``bson``            | ``bson``           | yes
Protobuf       | ``proto``, ``protobuf`` | ``protobuf``   | yes
Parquet        | ``parquet``         | ``parquet``        | no (columnar)
ORC            | ``orc``             | ``orc``            | no (columnar)
Avro           | ``avro``            | ``avro``           | no
WARC           | ``warc``            | ``warc``           | yes
PDF            | ``pdf``             | ``pdf``            | no (paginated)
Word (.docx)   | ``docx``, ``doc``   | ``docx``           | no (paginated)

("(built in)" / "(stdlib)": no extra install needed; every other Key needs
``pip install bitscrape[extra]`` -- e.g. ``bitscrape[parquet]`` -- or
``bitscrape[all]``. "Streamed?": whether items are written as they arrive
(safe for a huge crawl) or the whole result set is held in memory until
:meth:`~bitscrape.exporters.feed.BaseExporter.close` -- unavoidable for a
columnar or page-based format.)

Two ways to use these:

* **The pipeline/CLI path** -- what ``bitscrape crawl --format parquet -o out.parquet``
  and ``bitscrape.run(MySpider, output="out.parquet")`` use under the hood:
  ``get_exporter(fmt, uri)`` returns a ``BaseExporter``, which the ``Engine``
  drives (``open()`` → ``export_item()`` per item → ``close()``).
* **The in-memory path** -- :class:`~bitscrape.itemlist.ItemList` (returned by
  a crawl's ``stats.items`` when ``Settings.collect_items=True``, and usable
  standalone) wraps these same exporter classes as ``.to_json(path)``,
  ``.to_parquet(path)``, etc.

See ``docs/exporters.md``.
"""

# Side-effecting import: registers every class above into feed._EXPORTERS,
# so get_exporter() (imported straight from feed.py by the CLI, or via this
# package, or via top-level `bitscrape`) dispatches all 17 formats no matter
# which of those three import paths a caller used.
from bitscrape.exporters import registry as _registry  # noqa: F401
from bitscrape.exporters.binary import BSONExporter, MessagePackExporter, ProtobufExporter
from bitscrape.exporters.columnar import AvroExporter, ORCExporter, ParquetExporter
from bitscrape.exporters.documents import DocxExporter, MarkdownExporter, PDFExporter
from bitscrape.exporters.feed import (
    BaseExporter,
    CSVExporter,
    JSONExporter,
    JSONLExporter,
    XMLExporter,
    get_exporter,
)
from bitscrape.exporters.structured import INIExporter, TOMLExporter, YAMLExporter
from bitscrape.exporters.warc import WARCExporter, response_to_item

__all__ = [
    "AvroExporter",
    "BSONExporter",
    "BaseExporter",
    "CSVExporter",
    "DocxExporter",
    "INIExporter",
    "JSONExporter",
    "JSONLExporter",
    "MarkdownExporter",
    "MessagePackExporter",
    "ORCExporter",
    "PDFExporter",
    "ParquetExporter",
    "ProtobufExporter",
    "TOMLExporter",
    "WARCExporter",
    "XMLExporter",
    "YAMLExporter",
    "get_exporter",
    "response_to_item",
]
