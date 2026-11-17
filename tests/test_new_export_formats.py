"""
Tests for the 13 new export formats (Parquet, ORC, Avro, YAML, TOML, INI,
Protobuf, MessagePack, BSON, Markdown, WARC, PDF, Word) and the new
:class:`~bitscrape.itemlist.ItemList` / ``Settings.collect_items`` /
``CrawlStats.items`` machinery that makes ``result.items.to_json()`` work.

Every format is round-tripped with a real reader for that format (not just
"a file was written") wherever a reasonably lightweight one is available.
Regression tests cover two real bugs found while building this:

* ``pyarrow.Table.from_pylist`` silently uses only the *first* row's keys as
  the schema and drops any column that first appears in a later row --
  ``normalize_rows()`` (used by Parquet/ORC/Avro) exists specifically to
  prevent this.
* ``fpdf2``'s ``multi_cell`` doesn't reset the x-position to the left margin
  by default, so a second consecutive call can fail with "not enough
  horizontal space" -- exercised here by an item with several fields.
"""

from __future__ import annotations

import configparser
import json
import struct

import pytest
from pydantic import BaseModel

from bitscrape.core.models import Request, Response
from bitscrape.core.spider import Spider
from bitscrape.engine import Engine
from bitscrape.exporters import get_exporter
from bitscrape.exporters._base import normalize_rows, require
from bitscrape.exporters.warc import response_to_item
from bitscrape.itemlist import ItemList


class _Item(BaseModel):
    title: str
    price: float


HOMOGENEOUS = [{"title": "Widget", "price": 9.99}, {"title": "Gadget", "price": 19.99}]
HETEROGENEOUS = [
    {"title": "Widget", "price": 9.99},
    {"title": "Gadget", "price": 19.99, "tags": ["a", "b"], "in_stock": True},
]


# ---------------------------------------------------------------------------
# _base.normalize_rows / require
# ---------------------------------------------------------------------------


def test_normalize_rows_widens_to_the_union_of_keys():
    rows = normalize_rows(HETEROGENEOUS)
    assert set(rows[0]) == {"title", "price", "tags", "in_stock"}
    assert rows[0]["tags"] is None and rows[0]["in_stock"] is None


def test_normalize_rows_json_encodes_nested_values():
    rows = normalize_rows([{"a": 1, "tags": ["x", "y"]}])
    assert rows[0]["tags"] == '["x", "y"]'


def test_normalize_rows_preserves_first_seen_column_order():
    rows = normalize_rows([{"b": 1}, {"a": 2, "b": 3}, {"c": 4}])
    assert list(rows[0]) == ["b", "a", "c"]


def test_require_raises_actionable_import_error_for_missing_module():
    with pytest.raises(ImportError, match=r"pip install bitscrape\[nope\]"):
        require("bitscrape_definitely_not_a_real_module_xyz", "nope", "Testing")


def test_require_returns_the_module_when_present():
    mod = require("json", "n/a", "Testing")
    assert mod is json


# ---------------------------------------------------------------------------
# YAML
# ---------------------------------------------------------------------------


def test_yaml_exporter_round_trips(tmp_path):
    import yaml

    path = tmp_path / "out.yaml"
    exp = get_exporter("yaml", str(path))
    exp.open()
    for item in HETEROGENEOUS:
        exp.export_item(item)
    exp.close()

    docs = list(yaml.safe_load_all(path.read_text()))
    assert docs == HETEROGENEOUS


def test_yaml_handles_pydantic_model(tmp_path):
    import yaml

    path = tmp_path / "out.yaml"
    exp = get_exporter("yaml", str(path))
    exp.open()
    exp.export_item(_Item(title="Widget", price=9.99))
    exp.close()
    assert next(iter(yaml.safe_load_all(path.read_text()))) == {"title": "Widget", "price": 9.99}


def test_yml_is_an_alias_for_yaml(tmp_path):
    from bitscrape.exporters.structured import YAMLExporter

    assert isinstance(get_exporter("yml", str(tmp_path / "x.yml")), YAMLExporter)


# ---------------------------------------------------------------------------
# TOML
# ---------------------------------------------------------------------------


def test_toml_exporter_round_trips(tmp_path):
    import tomllib

    path = tmp_path / "out.toml"
    exp = get_exporter("toml", str(path))
    exp.open()
    for item in HETEROGENEOUS:
        exp.export_item(item)
    exp.close()

    with path.open("rb") as f:
        data = tomllib.load(f)
    assert data == {"items": HETEROGENEOUS}


def test_toml_no_items_produces_empty_items_array(tmp_path):
    import tomllib

    path = tmp_path / "out.toml"
    exp = get_exporter("toml", str(path))
    exp.open()
    exp.close()
    with path.open("rb") as f:
        assert tomllib.load(f) == {"items": []}


# ---------------------------------------------------------------------------
# INI
# ---------------------------------------------------------------------------


def test_ini_exporter_one_section_per_item(tmp_path):
    path = tmp_path / "out.ini"
    exp = get_exporter("ini", str(path))
    exp.open()
    exp.export_item({"title": "Widget", "price": 9.99})
    exp.export_item({"title": "Gadget", "price": 19.99})
    exp.close()

    cp = configparser.ConfigParser()
    cp.read(path)
    assert cp.sections() == ["item_0", "item_1"]
    assert cp["item_0"]["title"] == "Widget"
    assert cp["item_1"]["price"] == "19.99"


def test_ini_nested_values_become_json_text(tmp_path):
    path = tmp_path / "out.ini"
    exp = get_exporter("ini", str(path))
    exp.open()
    exp.export_item({"title": "Gadget", "tags": ["a", "b"]})
    exp.close()
    cp = configparser.ConfigParser()
    cp.read(path)
    assert json.loads(cp["item_0"]["tags"]) == ["a", "b"]


def test_ini_omits_none_values(tmp_path):
    path = tmp_path / "out.ini"
    exp = get_exporter("ini", str(path))
    exp.open()
    exp.export_item({"title": "Widget", "note": None})
    exp.close()
    cp = configparser.ConfigParser()
    cp.read(path)
    assert "note" not in cp["item_0"]


# ---------------------------------------------------------------------------
# Markdown
# ---------------------------------------------------------------------------


def test_markdown_exporter_one_section_per_item(tmp_path):
    path = tmp_path / "out.md"
    exp = get_exporter("md", str(path))
    exp.open()
    exp.export_item({"title": "Widget", "price": 9.99})
    exp.export_item({"title": "Gadget", "price": 19.99})
    exp.close()

    text = path.read_text()
    assert "## Item 1" in text and "## Item 2" in text
    assert "**title**: Widget" in text
    assert "**price**: 19.99" in text


def test_markdown_is_an_alias_for_md(tmp_path):
    from bitscrape.exporters.documents import MarkdownExporter

    assert isinstance(get_exporter("markdown", str(tmp_path / "x.md")), MarkdownExporter)


# ---------------------------------------------------------------------------
# MessagePack
# ---------------------------------------------------------------------------


def test_msgpack_exporter_round_trips(tmp_path):
    import msgpack

    path = tmp_path / "out.msgpack"
    exp = get_exporter("msgpack", str(path))
    exp.open()
    for item in HETEROGENEOUS:
        exp.export_item(item)
    exp.close()

    with path.open("rb") as f:
        docs = list(msgpack.Unpacker(f, raw=False))
    assert docs == HETEROGENEOUS


# ---------------------------------------------------------------------------
# BSON
# ---------------------------------------------------------------------------


def test_bson_exporter_round_trips(tmp_path):
    bson = require("bson", "bson", "test")

    path = tmp_path / "out.bson"
    exp = get_exporter("bson", str(path))
    exp.open()
    for item in HETEROGENEOUS:
        exp.export_item(item)
    exp.close()

    data = path.read_bytes()
    docs, offset = [], 0
    while offset < len(data):
        n = struct.unpack_from("<i", data, offset)[0]
        docs.append(bson.decode(data[offset : offset + n]))
        offset += n
    assert docs == HETEROGENEOUS


# ---------------------------------------------------------------------------
# Protobuf
# ---------------------------------------------------------------------------


def test_protobuf_exporter_round_trips_length_delimited_stream(tmp_path):
    from google.protobuf import struct_pb2

    path = tmp_path / "out.proto"
    exp = get_exporter("proto", str(path))
    exp.open()
    exp.export_item({"title": "Widget", "price": 9.99})
    exp.close()

    data = path.read_bytes()
    (n,) = struct.unpack_from(">I", data, 0)
    message = struct_pb2.Struct()
    message.ParseFromString(data[4 : 4 + n])
    assert dict(message) == {"title": "Widget", "price": 9.99}
    assert len(data) == 4 + n  # exactly one record


def test_protobuf_alias(tmp_path):
    from bitscrape.exporters.binary import ProtobufExporter

    assert isinstance(get_exporter("protobuf", str(tmp_path / "x.proto")), ProtobufExporter)


# ---------------------------------------------------------------------------
# Parquet / ORC / Avro
# ---------------------------------------------------------------------------


def test_parquet_exporter_round_trips_and_keeps_every_column(tmp_path):
    """Regression: naively calling pyarrow.Table.from_pylist(items) on
    heterogeneous items silently drops any column absent from the first row."""
    pq = require("pyarrow.parquet", "parquet", "test")

    path = tmp_path / "out.parquet"
    exp = get_exporter("parquet", str(path))
    exp.open()
    for item in HETEROGENEOUS:
        exp.export_item(item)
    exp.close()

    table = pq.read_table(str(path))
    assert set(table.column_names) == {"title", "price", "tags", "in_stock"}
    rows = table.to_pylist()
    assert rows[0]["tags"] is None  # widened, not dropped
    assert rows[1]["tags"] == '["a", "b"]'  # JSON-encoded


def test_parquet_compression_option(tmp_path):
    pq = require("pyarrow.parquet", "parquet", "test")

    path = tmp_path / "out.parquet"
    exp = get_exporter("parquet", str(path))
    exp.compression = "gzip"
    exp.open()
    exp.export_item({"a": 1})
    exp.close()
    assert pq.read_table(str(path)).to_pylist() == [{"a": 1}]


def test_orc_exporter_round_trips(tmp_path):
    orc = require("pyarrow.orc", "orc", "test")

    path = tmp_path / "out.orc"
    exp = get_exporter("orc", str(path))
    exp.open()
    for item in HOMOGENEOUS:
        exp.export_item(item)
    exp.close()

    assert orc.ORCFile(str(path)).read().to_pylist() == HOMOGENEOUS


def test_avro_exporter_default_schema_round_trips_as_strings(tmp_path):
    fastavro = require("fastavro", "avro", "test")

    path = tmp_path / "out.avro"
    exp = get_exporter("avro", str(path))
    exp.open()
    exp.export_item({"a": 1, "b": "x"})
    exp.export_item({"a": 2, "b": "y"})
    exp.close()

    with path.open("rb") as f:
        records = list(fastavro.reader(f))
    assert records == [{"a": "1", "b": "x"}, {"a": "2", "b": "y"}]


def test_avro_exporter_explicit_schema_preserves_types(tmp_path):
    fastavro = require("fastavro", "avro", "test")
    from bitscrape.exporters.columnar import AvroExporter

    schema = {
        "type": "record",
        "name": "Item",
        "fields": [{"name": "a", "type": "int"}, {"name": "b", "type": "string"}],
    }
    path = tmp_path / "out.avro"
    exp = AvroExporter(str(path), schema=schema)
    exp.open()
    exp.export_item({"a": 1, "b": "x"})
    exp.close()

    with path.open("rb") as f:
        records = list(fastavro.reader(f))
    assert records == [{"a": 1, "b": "x"}]  # a stayed an int, not "1"


# ---------------------------------------------------------------------------
# WARC
# ---------------------------------------------------------------------------


def test_warc_exporter_plain_item_becomes_resource_record(tmp_path):
    require("warcio", "warc", "test")
    from warcio.archiveiterator import ArchiveIterator

    path = tmp_path / "out.warc"
    exp = get_exporter("warc", str(path))
    exp.open()
    exp.export_item({"title": "Widget", "price": 9.99})
    exp.close()

    with path.open("rb") as f:
        records = []
        for r in ArchiveIterator(f):
            records.append((r.rec_type, r.rec_headers.get_header("WARC-Target-URI"), r.content_stream().read()))
    types = [r[0] for r in records]
    assert types == ["warcinfo", "resource"]
    assert json.loads(records[1][2]) == {"title": "Widget", "price": 9.99}
    assert records[1][1] == "urn:bitscrape:item:0"


def test_warc_exporter_response_to_item_becomes_response_record(tmp_path):
    from warcio.archiveiterator import ArchiveIterator

    request = Request(url="https://example.com/page")
    response = Response(
        url="https://example.com/page",
        status=200,
        headers={"Content-Type": "text/html"},
        body=b"<html><body>hi</body></html>",
        request=request,
    )

    path = tmp_path / "out.warc"
    exp = get_exporter("warc", str(path))
    exp.open()
    exp.export_item(response_to_item(response))
    exp.close()

    with path.open("rb") as f:
        found = None
        for r in ArchiveIterator(f):
            if r.rec_type == "response":
                found = (
                    r.rec_headers.get_header("WARC-Target-URI"),
                    r.content_stream().read(),
                    r.http_headers.get_statuscode(),
                )
    assert found is not None
    target_uri, body, status_code = found
    assert target_uri == "https://example.com/page"
    assert body == b"<html><body>hi</body></html>"
    assert status_code == "200"


def test_response_to_item_unwraps_parsed_response():
    from bitscrape.parser.selector import ParsedResponse

    request = Request(url="https://example.com/x")
    response = Response(url="https://example.com/x", status=200, body=b"<html></html>", request=request)
    parsed = ParsedResponse(response)
    item = response_to_item(parsed)
    assert item["url"] == "https://example.com/x" and item["status"] == 200


# ---------------------------------------------------------------------------
# PDF
# ---------------------------------------------------------------------------


def test_pdf_exporter_writes_a_valid_pdf(tmp_path):
    path = tmp_path / "out.pdf"
    exp = get_exporter("pdf", str(path))
    exp.open()
    exp.export_item({"title": "Widget", "price": 9.99})
    exp.close()
    data = path.read_bytes()
    assert data.startswith(b"%PDF-")
    assert data.rstrip().endswith(b"%%EOF")


def test_pdf_exporter_handles_multiple_fields_without_layout_error(tmp_path):
    """Regression: fpdf2's multi_cell doesn't reset x to the left margin by
    default, so a second consecutive call for the next field can raise
    'Not enough horizontal space to render a single character'."""
    path = tmp_path / "out.pdf"
    exp = get_exporter("pdf", str(path))
    exp.open()
    exp.export_item(
        {"title": "Widget", "price": 9.99, "description": "A fine widget", "tags": ["a", "b", "c"]}
    )
    exp.close()
    assert path.read_bytes().startswith(b"%PDF-")


def test_pdf_exporter_multiple_items_multiple_pages(tmp_path):
    import re

    path = tmp_path / "out.pdf"
    exp = get_exporter("pdf", str(path))
    exp.open()
    exp.export_item({"title": "Widget"})
    exp.export_item({"title": "Gadget"})
    exp.close()
    data = path.read_bytes()
    # Crude but dependency-free page count: "/Type /Page" without the trailing
    # "s" that would make it match the single parent "/Type /Pages" node.
    assert len(re.findall(rb"/Type\s*/Page[^s]", data)) == 2


# ---------------------------------------------------------------------------
# DOCX
# ---------------------------------------------------------------------------


def test_docx_exporter_round_trips(tmp_path):
    import docx

    path = tmp_path / "out.docx"
    exp = get_exporter("docx", str(path))
    exp.open()
    exp.export_item({"title": "Widget", "price": 9.99})
    exp.close()

    document = docx.Document(str(path))
    text = "\n".join(p.text for p in document.paragraphs)
    assert "Widget" in text and "9.99" in text


def test_doc_alias_produces_valid_docx_content(tmp_path):
    """`.doc` is accepted as documented (docstring), but must always contain
    real, openable .docx (OOXML) content -- never a bare/broken file."""
    import docx

    path = tmp_path / "out.doc"
    exp = get_exporter("doc", str(path))
    exp.open()
    exp.export_item({"title": "Widget"})
    exp.close()
    document = docx.Document(str(path))  # raises if it isn't valid OOXML
    assert "Widget" in document.paragraphs[-1].text


# ---------------------------------------------------------------------------
# ItemList
# ---------------------------------------------------------------------------


def test_item_list_is_a_plain_list():
    items = ItemList(HOMOGENEOUS)
    assert isinstance(items, list)
    assert len(items) == 2
    items.append({"title": "Extra", "price": 1})
    assert len(items) == 3


@pytest.mark.parametrize(
    "method,expects_bytes",
    [
        ("to_jsonl", False),
        ("to_json", False),
        ("to_csv", False),
        ("to_xml", False),
        ("to_yaml", False),
        ("to_toml", False),
        ("to_ini", False),
        ("to_markdown", False),
        ("to_msgpack", True),
        ("to_bson", True),
        ("to_protobuf", True),
        ("to_warc", True),
        ("to_parquet", True),
        ("to_orc", True),
        ("to_avro", True),
        ("to_pdf", True),
        ("to_docx", True),
    ],
)
def test_item_list_no_path_returns_content_for_every_format(method, expects_bytes):
    items = ItemList(HOMOGENEOUS)
    result = getattr(items, method)()
    assert isinstance(result, bytes if expects_bytes else str)
    assert len(result) > 0


@pytest.mark.parametrize(
    "method,suffix",
    [
        ("to_jsonl", ".jsonl"),
        ("to_json", ".json"),
        ("to_csv", ".csv"),
        ("to_xml", ".xml"),
        ("to_yaml", ".yaml"),
        ("to_toml", ".toml"),
        ("to_ini", ".ini"),
        ("to_markdown", ".md"),
        ("to_msgpack", ".msgpack"),
        ("to_bson", ".bson"),
        ("to_protobuf", ".proto"),
        ("to_warc", ".warc"),
        ("to_parquet", ".parquet"),
        ("to_orc", ".orc"),
        ("to_avro", ".avro"),
        ("to_pdf", ".pdf"),
        ("to_docx", ".docx"),
    ],
)
def test_item_list_with_path_writes_a_file_and_returns_none(tmp_path, method, suffix):
    items = ItemList(HOMOGENEOUS)
    path = tmp_path / f"out{suffix}"
    result = getattr(items, method)(path)
    assert result is None
    assert path.exists() and path.stat().st_size > 0


def test_item_list_to_json_matches_jsonl_content_when_read_back():
    items = ItemList(HOMOGENEOUS)
    text = items.to_json()
    assert json.loads(text) == HOMOGENEOUS


def test_item_list_accepts_pathlib_path_and_str(tmp_path):
    items = ItemList(HOMOGENEOUS)
    items.to_json(tmp_path / "a.json")
    items.to_json(str(tmp_path / "b.json"))
    assert (tmp_path / "a.json").read_text() == (tmp_path / "b.json").read_text()


def test_item_list_parquet_compression_kwarg(tmp_path):
    pq = require("pyarrow.parquet", "parquet", "test")

    items = ItemList(HOMOGENEOUS)
    path = tmp_path / "out.parquet"
    items.to_parquet(path, compression="gzip")
    assert pq.read_table(str(path)).to_pylist() == HOMOGENEOUS


def test_item_list_avro_schema_kwarg(tmp_path):
    fastavro = require("fastavro", "avro", "test")

    schema = {
        "type": "record",
        "name": "Item",
        "fields": [{"name": "title", "type": "string"}, {"name": "price", "type": "double"}],
    }
    items = ItemList(HOMOGENEOUS)
    path = tmp_path / "out.avro"
    items.to_avro(path, schema=schema)
    with path.open("rb") as f:
        assert list(fastavro.reader(f)) == HOMOGENEOUS


def test_item_list_no_path_mode_does_not_leak_temp_files(tmp_path, monkeypatch):
    import tempfile

    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(tmp_path))
    items = ItemList(HOMOGENEOUS)
    items.to_json()
    items.to_parquet()
    assert list(tmp_path.iterdir()) == []  # cleaned up, nothing left behind


def test_item_list_handles_pydantic_items():
    items = ItemList([_Item(title="Widget", price=9.99)])
    assert json.loads(items.to_json()) == [{"title": "Widget", "price": 9.99}]


def test_item_list_empty_list_all_formats_dont_crash():
    items = ItemList([])
    for method in (
        "to_jsonl",
        "to_json",
        "to_csv",
        "to_xml",
        "to_yaml",
        "to_toml",
        "to_ini",
        "to_markdown",
        "to_msgpack",
        "to_bson",
        "to_protobuf",
        "to_warc",
        "to_parquet",
        "to_orc",
        "to_pdf",
        "to_docx",
    ):
        getattr(items, method)()  # must not raise


# ---------------------------------------------------------------------------
# Settings.collect_items / CrawlStats.items end-to-end
# ---------------------------------------------------------------------------


class _ItemSpider(Spider):
    name = "collect_demo"

    def __init__(self, base: str, **kw: object) -> None:
        super().__init__(**kw)
        self.base = base

    def start_requests(self):
        return [Request(url=self.base)]

    async def parse(self, response):
        yield {"url": response.url, "title": "Home"}


async def _server():
    from aiohttp import web
    from aiohttp.test_utils import TestServer

    async def index(request):
        return web.Response(text="<html><body>hi</body></html>")

    app = web.Application()
    app.router.add_get("/", index)
    server = TestServer(app)
    await server.start_server()
    return server


async def test_collect_items_off_by_default_leaves_stats_items_empty():
    server = await _server()
    try:
        spider = _ItemSpider(str(server.make_url("/")))
        stats = await Engine(spider=spider, settings=spider.settings).run()
    finally:
        await server.close()
    assert stats.items_scraped == 1
    assert list(stats.items) == []  # opt-in, not populated


async def test_collect_items_true_populates_stats_items_as_an_item_list():
    server = await _server()
    try:
        spider = _ItemSpider(str(server.make_url("/")))
        settings = spider.settings.model_copy(update={"collect_items": True})
        stats = await Engine(spider=spider, settings=settings).run()
    finally:
        await server.close()
    assert isinstance(stats.items, ItemList)
    assert len(stats.items) == 1
    assert stats.items[0]["title"] == "Home"
    # ...and it's the literal API from the feature request:
    text = stats.items.to_json()
    assert json.loads(text)[0]["title"] == "Home"


async def test_collect_items_reflects_pipeline_transformed_items():
    """Items in stats.items must be post-pipeline (what was actually
    exported), not the raw yielded value."""
    from bitscrape.pipeline.pipelines import BasePipeline

    class UpperTitle(BasePipeline):
        async def process_item(self, item, spider):
            item["title"] = item["title"].upper()
            return item

    server = await _server()
    try:
        spider = _ItemSpider(str(server.make_url("/")))
        settings = spider.settings.model_copy(update={"collect_items": True})
        stats = await Engine(spider=spider, settings=settings, pipelines=[UpperTitle()]).run()
    finally:
        await server.close()
    assert stats.items[0]["title"] == "HOME"


def test_crawl_stats_items_field_defaults_to_empty_item_list():
    from bitscrape.core.models import CrawlStats

    stats = CrawlStats()
    assert isinstance(stats.items, ItemList)
    assert list(stats.items) == []
