"""
Tests for bitscrape.exporters.feed -- JSONL, JSON, CSV, and XML exporters.

Closes the coverage gap flagged in BITSCRAPE_QA_REPORT.md ("exporters/
feed.py -- 61% coverage; only JSONL export looks covered"), and includes a
regression test for a real bug found while writing these: XMLExporter
didn't escape special characters (&, <, >, ") in values, producing
genuinely invalid, unparseable XML for any scraped text containing them
(e.g. "Smith & Sons", "5 < 10 items"), and didn't sanitize dict keys into
valid XML element names either.
"""

from __future__ import annotations

import csv
import json
import xml.etree.ElementTree as ET

import pytest
from pydantic import BaseModel

from bitscrape.exporters.feed import (
    CSVExporter,
    JSONExporter,
    JSONLExporter,
    XMLExporter,
    get_exporter,
)


class _Item(BaseModel):
    title: str
    price: float


# ---------------------------------------------------------------------------
# JSONLExporter
# ---------------------------------------------------------------------------


def test_jsonl_exports_one_object_per_line(tmp_path):
    path = tmp_path / "out.jsonl"
    exp = JSONLExporter(str(path))
    exp.open()
    exp.export_item({"title": "A"})
    exp.export_item({"title": "B"})
    exp.close()

    lines = path.read_text().strip().splitlines()
    assert len(lines) == 2
    assert json.loads(lines[0]) == {"title": "A"}
    assert json.loads(lines[1]) == {"title": "B"}


def test_jsonl_handles_pydantic_model(tmp_path):
    path = tmp_path / "out.jsonl"
    exp = JSONLExporter(str(path))
    exp.open()
    exp.export_item(_Item(title="Widget", price=9.99))
    exp.close()
    assert json.loads(path.read_text().strip()) == {"title": "Widget", "price": 9.99}


def test_jsonl_empty_export_produces_empty_file(tmp_path):
    path = tmp_path / "out.jsonl"
    exp = JSONLExporter(str(path))
    exp.open()
    exp.close()
    assert path.read_text() == ""


# ---------------------------------------------------------------------------
# JSONExporter
# ---------------------------------------------------------------------------


def test_json_exports_a_single_array(tmp_path):
    path = tmp_path / "out.json"
    exp = JSONExporter(str(path))
    exp.open()
    exp.export_item({"title": "A"})
    exp.export_item({"title": "B"})
    exp.close()

    data = json.loads(path.read_text())
    assert data == [{"title": "A"}, {"title": "B"}]


def test_json_empty_export_produces_empty_array(tmp_path):
    path = tmp_path / "out.json"
    exp = JSONExporter(str(path))
    exp.open()
    exp.close()
    assert json.loads(path.read_text()) == []


def test_json_handles_unicode_content(tmp_path):
    path = tmp_path / "out.json"
    exp = JSONExporter(str(path))
    exp.open()
    exp.export_item({"title": "Café Münchën 日本語"})
    exp.close()
    data = json.loads(path.read_text())
    assert data[0]["title"] == "Café Münchën 日本語"


# ---------------------------------------------------------------------------
# CSVExporter
# ---------------------------------------------------------------------------


def test_csv_infers_header_from_first_item(tmp_path):
    path = tmp_path / "out.csv"
    exp = CSVExporter(str(path))
    exp.open()
    exp.export_item({"title": "A", "price": 1})
    exp.export_item({"title": "B", "price": 2})
    exp.close()

    with open(path, newline="") as f:
        rows = list(csv.DictReader(f))
    assert rows == [{"title": "A", "price": "1"}, {"title": "B", "price": "2"}]


def test_csv_handles_pydantic_model(tmp_path):
    path = tmp_path / "out.csv"
    exp = CSVExporter(str(path))
    exp.open()
    exp.export_item(_Item(title="Widget", price=9.99))
    exp.close()
    with open(path, newline="") as f:
        rows = list(csv.DictReader(f))
    assert rows[0]["title"] == "Widget"
    assert rows[0]["price"] == "9.99"


def test_csv_handles_commas_and_quotes_in_values(tmp_path):
    """CSV values containing commas/quotes must be properly quoted/escaped
    by the underlying csv module, not just naively joined."""
    path = tmp_path / "out.csv"
    exp = CSVExporter(str(path))
    exp.open()
    exp.export_item({"title": 'Widget, "Deluxe" Edition', "price": 1})
    exp.close()

    with open(path, newline="") as f:
        rows = list(csv.DictReader(f))
    assert rows[0]["title"] == 'Widget, "Deluxe" Edition'


def test_csv_no_items_produces_empty_file(tmp_path):
    path = tmp_path / "out.csv"
    exp = CSVExporter(str(path))
    exp.open()
    exp.close()
    assert path.read_text() == ""


def test_csv_items_with_different_keys_uses_first_items_header(tmp_path):
    """Documents actual behavior: the header is fixed from the FIRST item;
    a later item with an extra field will raise (DictWriter's
    extrasaction defaults to "raise")."""
    path = tmp_path / "out.csv"
    exp = CSVExporter(str(path))
    exp.open()
    exp.export_item({"title": "A"})
    with pytest.raises(ValueError):
        exp.export_item({"title": "B", "extra_field": "surprise"})
    exp.close()


# ---------------------------------------------------------------------------
# XMLExporter
# ---------------------------------------------------------------------------


def test_xml_produces_well_formed_output_with_plain_values(tmp_path):
    path = tmp_path / "out.xml"
    exp = XMLExporter(str(path))
    exp.open()
    exp.export_item({"title": "Widget", "price": 9.99})
    exp.close()

    root = ET.parse(path).getroot()
    items = root.findall("item")
    assert len(items) == 1
    assert items[0].find("title").text == "Widget"
    assert items[0].find("price").text == "9.99"


def test_xml_escapes_ampersand_in_values(tmp_path):
    """Regression test: unescaped '&' previously produced invalid,
    unparseable XML."""
    path = tmp_path / "out.xml"
    exp = XMLExporter(str(path))
    exp.open()
    exp.export_item({"title": "Smith & Sons"})
    exp.close()

    root = ET.parse(path).getroot()  # raises ET.ParseError if malformed
    assert root.find("item/title").text == "Smith & Sons"


def test_xml_escapes_angle_brackets_in_values(tmp_path):
    """Regression test: unescaped '<'/'>' previously produced invalid,
    unparseable XML (the content was interpreted as nested tags)."""
    path = tmp_path / "out.xml"
    exp = XMLExporter(str(path))
    exp.open()
    exp.export_item({"description": "Rated 5 < 10, use with <caution>"})
    exp.close()

    root = ET.parse(path).getroot()
    assert root.find("item/description").text == "Rated 5 < 10, use with <caution>"


def test_xml_escapes_quotes_in_values(tmp_path):
    path = tmp_path / "out.xml"
    exp = XMLExporter(str(path))
    exp.open()
    exp.export_item({"quote": 'She said "hello" & waved'})
    exp.close()

    root = ET.parse(path).getroot()
    assert root.find("item/quote").text == 'She said "hello" & waved'


def test_xml_sanitizes_keys_with_spaces_into_valid_tag_names(tmp_path):
    """Regression test: a dict key with spaces/special chars (e.g. from a
    scraped column header) previously produced invalid XML tags."""
    path = tmp_path / "out.xml"
    exp = XMLExporter(str(path))
    exp.open()
    exp.export_item({"2024 price ($)": 42, "product name": "Widget"})
    exp.close()

    root = ET.parse(path).getroot()  # raises if tags are invalid XML names
    item = root.find("item")
    # Exact sanitized tag names aren't the contract -- parseability and
    # the values being preserved are what matters.
    texts = {child.tag: child.text for child in item}
    assert "42" in texts.values()
    assert "Widget" in texts.values()


def test_xml_handles_none_value_as_empty_element(tmp_path):
    path = tmp_path / "out.xml"
    exp = XMLExporter(str(path))
    exp.open()
    exp.export_item({"optional_field": None})
    exp.close()

    root = ET.parse(path).getroot()
    assert root.find("item/optional_field").text is None


def test_xml_multiple_items_all_well_formed(tmp_path):
    path = tmp_path / "out.xml"
    exp = XMLExporter(str(path))
    exp.open()
    for i in range(5):
        exp.export_item({"index": i, "note": f"item & thing #{i} <special>"})
    exp.close()

    root = ET.parse(path).getroot()
    assert len(root.findall("item")) == 5


def test_xml_handles_pydantic_model(tmp_path):
    path = tmp_path / "out.xml"
    exp = XMLExporter(str(path))
    exp.open()
    exp.export_item(_Item(title="Widget & Co", price=9.99))
    exp.close()

    root = ET.parse(path).getroot()
    assert root.find("item/title").text == "Widget & Co"


# ---------------------------------------------------------------------------
# get_exporter factory
# ---------------------------------------------------------------------------


def test_get_exporter_returns_correct_class_for_each_format(tmp_path):
    assert isinstance(get_exporter("jsonl", str(tmp_path / "a.jsonl")), JSONLExporter)
    assert isinstance(get_exporter("json", str(tmp_path / "a.json")), JSONExporter)
    assert isinstance(get_exporter("csv", str(tmp_path / "a.csv")), CSVExporter)
    assert isinstance(get_exporter("xml", str(tmp_path / "a.xml")), XMLExporter)


def test_get_exporter_unknown_format_raises():
    with pytest.raises((ValueError, KeyError)):
        get_exporter("yaml", "out.yaml")
