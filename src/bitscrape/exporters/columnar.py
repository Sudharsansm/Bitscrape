"""
Columnar export formats: Parquet, ORC, Avro.

All three need the full row set up front (a column's type and even its
existence come from looking at every row), so all three subclass
:class:`~bitscrape.exporters._base.BufferedExporter` and go through
:func:`~bitscrape.exporters._base.normalize_rows` first -- see that
function's docstring for the concrete data-loss bug (pyarrow silently
dropping any column not present in the *first* row) this avoids.
"""

from __future__ import annotations

from typing import Any

from bitscrape.exporters._base import BufferedExporter, normalize_rows, require

# ---------------------------------------------------------------------------
# Parquet
# ---------------------------------------------------------------------------


class ParquetExporter(BufferedExporter):
    """Apache Parquet (columnar, compressed). Requires ``pyarrow``
    (``pip install bitscrape[parquet]``). Pass ``compression=`` (default
    ``"snappy"``; pyarrow also accepts ``"gzip"``, ``"zstd"``, ``"none"``, ...)
    to the constructor to change it."""

    def __init__(self, uri: str | None = None, *, compression: str = "snappy") -> None:
        super().__init__(uri)
        self.compression = compression

    def _write_all(self, items: list[dict[str, Any]]) -> bytes:
        pa = require("pyarrow", "parquet", "Parquet export")
        pq = require("pyarrow.parquet", "parquet", "Parquet export")
        import io

        table = pa.Table.from_pylist(normalize_rows(items))
        buf = io.BytesIO()
        pq.write_table(table, buf, compression=self.compression)
        return buf.getvalue()


# ---------------------------------------------------------------------------
# ORC
# ---------------------------------------------------------------------------


class ORCExporter(BufferedExporter):
    """Apache ORC (columnar, compressed; common in the Hadoop/Hive ecosystem).
    Requires ``pyarrow`` built with ORC support (``pip install
    bitscrape[orc]``) -- the official PyPI wheels include it on Linux/macOS;
    if yours doesn't, this raises the same actionable ``ImportError`` as a
    missing package."""

    def _write_all(self, items: list[dict[str, Any]]) -> bytes:
        pa = require("pyarrow", "orc", "ORC export")
        try:
            orc = require("pyarrow.orc", "orc", "ORC export")
        except ImportError as exc:
            raise ImportError(
                "ORC export requires pyarrow to be built with ORC support. "
                "The official PyPI wheels (pip install bitscrape[orc]) include it; "
                "a pyarrow built without it will raise this even once installed."
            ) from exc
        import io

        table = pa.Table.from_pylist(normalize_rows(items))
        buf = io.BytesIO()
        orc.write_table(table, buf)
        return buf.getvalue()


# ---------------------------------------------------------------------------
# Avro
# ---------------------------------------------------------------------------


def _infer_avro_schema(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """A permissive schema: every field is nullable Avro ``string`` (values
    are stringified -- ``bool``/``int``/``float`` via ``str()``, already-JSON
    nested structures from :func:`normalize_rows` pass through as-is). This
    guarantees any item shape round-trips without a type-conflict write error,
    at the cost of losing native numeric/boolean typing in the Avro file.
    Pass your own ``schema=`` (a fastavro-compatible dict) to
    :class:`AvroExporter` for real typing."""
    fields = [
        {"name": key, "type": ["null", "string"], "default": None}
        for key in (rows[0] if rows else {})
    ]
    return {"type": "record", "name": "Item", "fields": fields}


class AvroExporter(BufferedExporter):
    """Apache Avro (compact binary, schema-carrying). Requires ``fastavro``
    (``pip install bitscrape[avro]``).

    Without an explicit schema, every field is written as a nullable string
    (see :func:`_infer_avro_schema`) so heterogeneous items never fail to
    write; pass ``schema=`` (a fastavro/Avro JSON schema dict) for real field
    typing, in which case every item must actually match it.
    """

    def __init__(self, uri: str | None = None, *, schema: dict[str, Any] | None = None) -> None:
        super().__init__(uri)
        self.schema = schema

    def _write_all(self, items: list[dict[str, Any]]) -> bytes:
        fastavro = require("fastavro", "avro", "Avro export")
        import io

        rows = normalize_rows(items)
        schema = self.schema
        if schema is None:
            schema = _infer_avro_schema(rows)
            rows = [{k: (None if v is None else str(v)) for k, v in row.items()} for row in rows]
        buf = io.BytesIO()
        fastavro.writer(buf, schema, rows)
        return buf.getvalue()
