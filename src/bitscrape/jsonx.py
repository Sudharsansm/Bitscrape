"""
Fast JSON
==========

One place for JSON encode/decode, using ``orjson`` (a core dependency; several
times faster than the standard library, and it handles ``datetime``, ``UUID``,
dataclasses and numpy natively) and falling back to the standard library when a
value orjson refuses -- integers beyond 64 bits, non-string dict keys of odd
types, ... -- or when orjson isn't importable (a vendored/stripped install).

Behaviour is identical either way: compact output, UTF-8 (no ``\\uXXXX`` escaping),
unknown objects stringified (``default=str``) rather than raising.
"""

from __future__ import annotations

import json as _json
from typing import Any

orjson: Any = None
try:  # pragma: no cover - exercised implicitly
    import orjson as _orjson_module

    orjson = _orjson_module
    HAS_ORJSON = True
except ImportError:  # pragma: no cover
    HAS_ORJSON = False


def _default(obj: Any) -> Any:
    if isinstance(obj, (set, frozenset)):
        return sorted(obj, key=repr)
    if isinstance(obj, (bytes, bytearray)):
        return bytes(obj).decode("utf-8", errors="replace")
    return str(obj)


def dumps_bytes(obj: Any, *, indent: bool = False, sort_keys: bool = False) -> bytes:
    """Serialise ``obj`` to UTF-8 JSON bytes."""
    if HAS_ORJSON:
        opts = orjson.OPT_NON_STR_KEYS
        if indent:
            opts |= orjson.OPT_INDENT_2
        if sort_keys:
            opts |= orjson.OPT_SORT_KEYS
        try:
            return orjson.dumps(obj, default=_default, option=opts)
        except (TypeError, orjson.JSONEncodeError):
            pass  # e.g. an int wider than 64 bits -> stdlib handles it
    return _json.dumps(
        obj,
        default=_default,
        ensure_ascii=False,
        indent=2 if indent else None,
        separators=None if indent else (",", ":"),
        sort_keys=sort_keys,
    ).encode("utf-8")


def dumps(obj: Any, *, indent: bool = False, sort_keys: bool = False) -> str:
    """Serialise ``obj`` to a JSON ``str``."""
    return dumps_bytes(obj, indent=indent, sort_keys=sort_keys).decode("utf-8")


def loads(data: str | bytes | bytearray | memoryview) -> Any:
    """Parse JSON. Raises ``ValueError`` (a ``json.JSONDecodeError`` subclass in both
    backends' fallback path) on invalid input."""
    if HAS_ORJSON:
        try:
            return orjson.loads(data)
        except orjson.JSONDecodeError:
            pass  # re-parse with stdlib for its richer error (and NaN/Infinity leniency)
    if isinstance(data, (bytes, bytearray, memoryview)):
        data = bytes(data).decode("utf-8", errors="replace")
    return _json.loads(data)


__all__ = ["HAS_ORJSON", "dumps", "dumps_bytes", "loads"]
