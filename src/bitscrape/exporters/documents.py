"""
Human-readable document export formats: Markdown, PDF, Word (.docx).

Markdown is written one item at a time (a plain text format with nothing to
gain from buffering). PDF and DOCX are paginated documents built as a whole,
so both subclass :class:`~bitscrape.exporters._base.BufferedExporter`.

None of these are meant for round-tripping structured data back out --
they're for handing a scrape's results to a person to read.
"""

from __future__ import annotations

import json
from typing import Any

from bitscrape.exporters._base import BufferedExporter, require
from bitscrape.exporters.feed import BaseExporter, _item_to_dict

# ---------------------------------------------------------------------------
# Shared: turn one item into readable "key: value" lines
# ---------------------------------------------------------------------------


def _format_value(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, dict | list):
        return json.dumps(value, default=str, ensure_ascii=False)
    return str(value)


# ---------------------------------------------------------------------------
# Markdown
# ---------------------------------------------------------------------------


class MarkdownExporter(BaseExporter):
    """Markdown: each item as a ``## Item N`` section with a
    ``key: value`` bullet per field. No dependency -- plain text.
    """

    def open(self) -> None:
        super().open()
        assert self._file is not None
        self._file.write("# Scraped items\n\n")

    def export_item(self, item: Any) -> None:
        assert self._file is not None
        d = _item_to_dict(item)
        self._file.write(f"## Item {self._count + 1}\n\n")
        for key, value in d.items():
            self._file.write(f"- **{key}**: {_format_value(value)}\n")
        self._file.write("\n")
        self._count += 1


# ---------------------------------------------------------------------------
# PDF
# ---------------------------------------------------------------------------


class PDFExporter(BufferedExporter):
    """PDF: each item rendered as its own page (a title plus a
    ``key: value`` line per field). Requires ``fpdf2``
    (``pip install bitscrape[pdf]``). This is a plain, readable dump for
    handing results to a person -- not a customisable report layout; build
    your own with ``fpdf2``/another PDF library directly for that.
    """

    def _write_all(self, items: list[dict[str, Any]]) -> bytes:
        fpdf_module = require("fpdf", "pdf", "PDF export")
        pdf = fpdf_module.FPDF()
        pdf.set_auto_page_break(auto=True, margin=15)
        for i, item in enumerate(items, start=1):
            pdf.add_page()
            pdf.set_font("Helvetica", "B", size=16)
            pdf.cell(0, 10, f"Item {i}", new_x="LMARGIN", new_y="NEXT")
            pdf.set_font("Helvetica", size=11)
            pdf.ln(4)
            for key, value in item.items():
                pdf.set_font("Helvetica", "B", size=11)
                pdf.multi_cell(0, 7, f"{key}:", new_x="LMARGIN", new_y="NEXT")
                pdf.set_font("Helvetica", size=11)
                pdf.multi_cell(
                    0, 7, _format_value(value) or "(empty)", new_x="LMARGIN", new_y="NEXT"
                )
                pdf.ln(2)
        return bytes(pdf.output())


# ---------------------------------------------------------------------------
# Word (.docx)
# ---------------------------------------------------------------------------


class DocxExporter(BufferedExporter):
    """Word document (.docx / OOXML): each item as a heading plus a
    ``key: value`` paragraph per field. Requires ``python-docx``
    (``pip install bitscrape[docx]``).

    ``.doc`` (the legacy pre-2007 binary format) is accepted as a filename
    extension but is **not** actually produced -- no maintained pure-Python
    library writes real binary .doc, so this always writes .docx (OOXML)
    content regardless of which of the two extensions you use. Word and
    LibreOffice both open it correctly either way (they detect the real
    format from the file's content, not its extension), but a tool that
    strictly validates a ``.doc`` file's format before reading it will reject
    it -- name the output ``.docx`` to avoid that ambiguity.
    """

    def _write_all(self, items: list[dict[str, Any]]) -> bytes:
        docx_module = require("docx", "docx", "Word (.docx) export")
        import io

        document = docx_module.Document()
        document.add_heading("Scraped items", level=1)
        for i, item in enumerate(items, start=1):
            document.add_heading(f"Item {i}", level=2)
            for key, value in item.items():
                p = document.add_paragraph()
                p.add_run(f"{key}: ").bold = True
                p.add_run(_format_value(value) or "(empty)")
        buf = io.BytesIO()
        document.save(buf)
        return buf.getvalue()
