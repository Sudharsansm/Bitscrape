"""
Structured text export formats: YAML, TOML, INI.

YAML and INI are written one item at a time (safe for a large crawl: nothing
beyond the current item is held in memory). TOML is buffered -- see
:class:`TOMLExporter` for why.
"""

from __future__ import annotations

import configparser
import json
from typing import Any

from bitscrape.exporters._base import BufferedExporter, require
from bitscrape.exporters.feed import BaseExporter, _item_to_dict

# ---------------------------------------------------------------------------
# YAML
# ---------------------------------------------------------------------------


class YAMLExporter(BaseExporter):
    """YAML. Requires ``pyyaml`` (``pip install bitscrape[yaml]``).

    Written as a stream of ``---``-separated documents, one per item -- valid
    multi-document YAML (``yaml.safe_load_all`` reads it back as a list) that
    never needs the whole item set in memory at once.
    """

    def open(self) -> None:
        require("yaml", "yaml", "YAML export")
        super().open()

    def export_item(self, item: Any) -> None:
        import yaml

        assert self._file is not None
        d = _item_to_dict(item)
        self._file.write("---\n")
        self._file.write(yaml.safe_dump(d, sort_keys=False, allow_unicode=True))
        self._count += 1


# ---------------------------------------------------------------------------
# TOML
# ---------------------------------------------------------------------------


class TOMLExporter(BufferedExporter):
    """TOML, as a top-level array of tables: ``items = [ {...}, {...} ]``.
    Requires ``tomli-w`` (``pip install bitscrape[toml]``; the standard
    library's ``tomllib``, Python 3.11+, only *reads* TOML).

    Buffered rather than streamed: correctly escaping arbitrary strings,
    telling a number from a string, and nesting are all things worth using a
    real TOML writer for rather than hand-formatting text per item, and doing
    that needs the whole document at once.
    """

    binary = False

    def _write_all(self, items: list[dict[str, Any]]) -> str:
        tomli_w = require("tomli_w", "toml", "TOML export")
        # TOML has no bare top-level array; every item must be JSON-safe
        # (nested dict/list survive natively in TOML, unlike the columnar
        # formats, so no normalize_rows() JSON-flattening is needed here).
        return tomli_w.dumps({"items": items})


# ---------------------------------------------------------------------------
# INI
# ---------------------------------------------------------------------------


class INIExporter(BaseExporter):
    """INI, one section per item (``[item_0]``, ``[item_1]``, ...). INI has no
    native list/nesting support, so a ``dict``/``list`` value is written as
    its JSON text and every value is written as a plain string -- readers get
    it back as text, not a typed value, same as ``configparser`` itself
    always does. Best suited to small, flat, mostly-uniform result sets;
    prefer CSV, JSON or YAML for anything nested or at any real scale.
    """

    def __init__(self, uri: str | None = None) -> None:
        super().__init__(uri)
        self._index = 0

    def export_item(self, item: Any) -> None:
        assert self._file is not None
        d = _item_to_dict(item)
        section = configparser.ConfigParser()
        section_name = f"item_{self._index}"
        section[section_name] = {
            str(k): (
                json.dumps(v, default=str, ensure_ascii=False)
                if isinstance(v, dict | list)
                else str(v)
            )
            for k, v in d.items()
            if v is not None
        }
        section.write(self._file)
        self._file.write("\n")
        self._index += 1
        self._count += 1
