"""
Binary export formats written one item at a time: MessagePack, BSON,
Protocol Buffers.

All three formats are self-delimiting per record (each encoded message
carries its own length), so concatenating them into one file -- exactly what
writing one item at a time, on arrival, produces -- is a valid, standard way
to store a sequence of them and needs nothing held in memory beyond the
current item. All three subclass :class:`~bitscrape.exporters.feed.BaseExporter`
directly, opened in binary mode.
"""

from __future__ import annotations

import struct
import sys
from pathlib import Path
from typing import Any

from bitscrape.exporters._base import require
from bitscrape.exporters.feed import BaseExporter, _item_to_dict

# ---------------------------------------------------------------------------
# Shared: binary-mode open (the base class's ``open()`` always opens text)
# ---------------------------------------------------------------------------


class _BinaryStreamExporter(BaseExporter):
    # BaseExporter types `_file` as `TextIO | None`; every subclass here
    # writes bytes to it, so re-declare it as `Any` for them specifically
    # rather than sprinkling `# type: ignore[arg-type]` on every `.write()`.
    _file: Any

    def open(self) -> None:
        if self._uri and self._uri != "-":
            path = Path(self._uri)
            path.parent.mkdir(parents=True, exist_ok=True)
            self._file = path.open("wb")
        else:
            self._file = sys.stdout.buffer


# ---------------------------------------------------------------------------
# MessagePack
# ---------------------------------------------------------------------------


class MessagePackExporter(_BinaryStreamExporter):
    """MessagePack: a compact binary stream, one packed item after another.
    Requires ``msgpack`` (``pip install bitscrape[msgpack]``). Read it back
    with ``msgpack.Unpacker`` (yields items one at a time, so reading is as
    memory-light as writing was) rather than ``msgpack.unpackb`` (which
    expects exactly one document, not a concatenated stream).
    """

    def open(self) -> None:
        require("msgpack", "msgpack", "MessagePack export")
        super().open()

    def export_item(self, item: Any) -> None:
        import msgpack

        assert self._file is not None
        self._file.write(msgpack.packb(_item_to_dict(item), use_bin_type=True))
        self._count += 1


# ---------------------------------------------------------------------------
# BSON
# ---------------------------------------------------------------------------


class BSONExporter(_BinaryStreamExporter):
    """BSON: MongoDB's binary document format, one encoded document after
    another -- the same concatenated-documents shape a MongoDB data file
    itself uses internally, so any BSON-aware tool can stream it back.
    Requires ``pymongo`` (``pip install bitscrape[bson]`` -- specifically for
    its bundled ``bson`` module; the standalone PyPI ``bson`` package is a
    different, incompatible library of the same name, so this extra pins
    pymongo rather than leaving that to chance).
    """

    def open(self) -> None:
        require("bson", "bson", "BSON export")
        super().open()

    def export_item(self, item: Any) -> None:
        import bson

        assert self._file is not None
        self._file.write(bson.encode(_item_to_dict(item)))
        self._count += 1


# ---------------------------------------------------------------------------
# Protocol Buffers
# ---------------------------------------------------------------------------


class ProtobufExporter(_BinaryStreamExporter):
    """Protocol Buffers, one length-delimited message per item (a 4-byte
    big-endian length prefix followed by the serialized message -- the usual
    way to store a *sequence* of protobuf messages in one file, since a bare
    ``.SerializeToString()`` has no built-in framing of its own). Requires the
    ``protobuf`` package (``pip install bitscrape[protobuf]``).

    Each item is encoded as a ``google.protobuf.Struct`` -- protobuf's own
    built-in message type for arbitrary JSON-like data -- rather than a
    hand-defined ``.proto`` schema message, since scraped items don't have a
    single fixed shape known ahead of time. If your project defines its own
    ``.proto`` message for these items, build it directly instead of using
    this exporter (subclassing it and overriding :meth:`_encode` to return
    ``your_message.SerializeToString()`` is the natural way to do that while
    keeping the same length-delimited framing).

    Note: ``Struct``'s number type is a double, so integers round-trip as
    floats (``1`` becomes ``1.0``) -- a limitation of protobuf's built-in
    generic type, not of this exporter.
    """

    def open(self) -> None:
        require("google.protobuf", "protobuf", "Protobuf export")
        super().open()

    def _encode(self, item: dict[str, Any]) -> bytes:
        from google.protobuf import json_format, struct_pb2

        message = struct_pb2.Struct()
        # Struct.update() rejects some values Struct can represent once
        # wrapped as protobuf Values (e.g. a raw non-finite float); go through
        # google.protobuf's own JSON-to-Struct conversion, which already
        # handles that, instead of reimplementing it.
        json_format.ParseDict(item, message)
        return message.SerializeToString()

    def export_item(self, item: Any) -> None:
        assert self._file is not None
        payload = self._encode(_item_to_dict(item))
        self._file.write(struct.pack(">I", len(payload)))
        self._file.write(payload)
        self._count += 1
