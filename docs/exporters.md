# Exporters

Exporters write scraped items to a file (or stdout). Bitscrape supports
**seventeen formats** out of the box: JSON Lines, JSON, CSV, XML, YAML, TOML,
INI, Markdown, MessagePack, BSON, Protocol Buffers, Parquet, ORC, Avro, WARC,
PDF, and Word (.docx).

Two ways to use them:

- **Files, via `get_exporter()` / the CLI / `bitscrape.run()`** — what this
  page mostly covers.
- **In memory, via `ItemList`** — `stats.items.to_json()`,
  `.to_parquet()`, etc. — see [In-memory export: `ItemList`](#in-memory-export-itemlist)
  below.

| Format | Key(s) | Extra to install | Streamed? |
|---|---|---|---|
| JSON Lines | `jsonl` | *(built in)* | yes |
| JSON | `json` | *(built in)* | no (array) |
| CSV | `csv` | *(built in)* | yes |
| XML | `xml` | *(built in)* | yes |
| YAML | `yaml`, `yml` | `bitscrape[yaml]` | yes |
| TOML | `toml` | `bitscrape[toml]` | no |
| INI | `ini` | *(stdlib)* | yes |
| Markdown | `md`, `markdown` | *(stdlib)* | yes |
| MessagePack | `msgpack` | `bitscrape[msgpack]` | yes |
| BSON | `bson` | `bitscrape[bson]` | yes |
| Protocol Buffers | `proto`, `protobuf` | `bitscrape[protobuf]` | yes |
| Parquet | `parquet` | `bitscrape[parquet]` | no (columnar) |
| ORC | `orc` | `bitscrape[orc]` | no (columnar) |
| Avro | `avro` | `bitscrape[avro]` | no |
| WARC | `warc` | `bitscrape[warc]` | yes |
| PDF | `pdf` | `bitscrape[pdf]` | no (paginated) |
| Word (.docx) | `docx`, `doc` | `bitscrape[docx]` | no (paginated) |

Install everything at once with `pip install bitscrape[export-formats]`, or
`bitscrape[all]` for every optional feature. "Streamed?": whether items are
written to disk as they arrive (safe for a huge crawl — nothing beyond the
current item is held in memory) or the whole result set is buffered until
`.close()` — unavoidable for a columnar or page-based format, which needs to
see everything before it can lay out columns or pages.

---

## Formats

### JSONL (JSON Lines) — Recommended

One JSON object per line. Best for large datasets — streaming-friendly,
easy to process line by line.

```bash
bitscrape crawl myspider.py -o data.jsonl
```

```json
{"title": "Widget A", "price": 9.99, "in_stock": true}
{"title": "Widget B", "price": 14.99, "in_stock": false}
```

### JSON

A single JSON array. Entire file must be loaded to parse — use JSONL for
large datasets.

```bash
bitscrape crawl myspider.py -o data.json --fmt json
```

### CSV

Comma-separated values. Opens directly in Excel / Google Sheets. Columns
come from the **first** item's keys; an item with an extra key not in the
first item is written without it (this is existing, intentional,
regression-tested behaviour — see `AvroExporter`/`ParquetExporter` below, or
JSON/YAML, for heterogeneous items where every field must survive).

```bash
bitscrape crawl myspider.py -o data.csv --fmt csv
```

### XML

```bash
bitscrape crawl myspider.py -o data.xml --fmt xml
```

### YAML

A stream of `---`-separated documents, one per item — valid multi-document
YAML, read back as a list with `yaml.safe_load_all()`.

```bash
bitscrape crawl myspider.py -o data.yaml --fmt yaml
```

```yaml
---
title: Widget A
price: 9.99
---
title: Widget B
price: 14.99
```

### TOML

A top-level array of tables: `items = [ {...}, {...} ]`. Buffered — TOML
needs a real writer (correct string escaping, number-vs-string typing), not
line-by-line text, so the whole item set is held until `.close()`.

```bash
bitscrape crawl myspider.py -o data.toml --fmt toml
```

### INI

One section per item (`[item_0]`, `[item_1]`, ...). INI has no native list or
nesting support: a `dict`/`list` value is written as its JSON text, and every
value comes back as a plain string on read, same as `configparser` always
does. Best for small, flat, mostly-uniform result sets.

```bash
bitscrape crawl myspider.py -o data.ini --fmt ini
```

```ini
[item_0]
title = Widget A
price = 9.99

[item_1]
title = Widget B
tags = ["sale", "new"]
```

### Markdown

Each item as a `## Item N` section with a `key: value` bullet per field — a
plain, readable dump, not meant to round-trip back to structured data.

```bash
bitscrape crawl myspider.py -o data.md --fmt md
```

### MessagePack

A compact binary stream: each item packed and concatenated, one after
another. Read it back with `msgpack.Unpacker` (yields items one at a time),
not `msgpack.unpackb` (which expects exactly one document).

```python
import msgpack
with open("data.msgpack", "rb") as f:
    items = list(msgpack.Unpacker(f, raw=False))
```

### BSON

MongoDB's binary document format — the same concatenated-documents shape a
MongoDB data file itself uses, so any BSON-aware tool can stream it back.
Uses `pymongo`'s bundled `bson` module specifically (`bitscrape[bson]` pins
`pymongo`) rather than the standalone PyPI `bson` package, which is a
different, incompatible library that happens to share the name.

### Protocol Buffers

Each item as a length-delimited `google.protobuf.Struct` message (a 4-byte
big-endian length prefix, then the serialized message) — `Struct` is
protobuf's own built-in type for arbitrary JSON-like data, used here because
scraped items don't have one fixed shape to define a custom `.proto` message
for ahead of time. If your project has its own message type for these items,
subclass `ProtobufExporter` and override `_encode()` to return
`your_message.SerializeToString()`, keeping the same framing.

```python
import struct
from google.protobuf import struct_pb2

with open("data.proto", "rb") as f:
    data = f.read()
offset = 0
while offset < len(data):
    (n,) = struct.unpack_from(">I", data, offset); offset += 4
    msg = struct_pb2.Struct(); msg.ParseFromString(data[offset:offset + n]); offset += n
    print(dict(msg))
```

Note: `Struct`'s number type is a double, so integers round-trip as floats
(`1` becomes `1.0`) — a limitation of protobuf's generic type, not of this
exporter.

### Parquet / ORC (columnar)

Compressed, columnar formats — read efficiently by pandas, DuckDB, Spark,
and most data-warehouse tooling. Buffered: a column's type (and even its
existence) comes from looking at every item first.

```bash
bitscrape crawl myspider.py -o data.parquet --fmt parquet
```

```python
import pandas as pd
df = pd.read_parquet("data.parquet")
```

`ParquetExporter(uri, compression="snappy")` — pass `"gzip"`, `"zstd"`,
`"none"`, etc. `ORCExporter` needs a pyarrow build with ORC support; the
official PyPI wheels include it.

**Heterogeneous items are handled explicitly**: passing items straight to
`pyarrow.Table.from_pylist()` silently uses only the *first* item's keys as
the schema and drops any key that first appears later — a real bug found
while building this. Both exporters compute the union of keys across every
item first (missing = `null`), so nothing is silently dropped; a nested
`dict`/`list` value is JSON-encoded, since Parquet/ORC columns are typed.

### Avro

Apache's compact, schema-carrying binary format. Buffered, same reasoning as
Parquet/ORC. Without an explicit schema, every field is written as a
nullable string (so heterogeneous items always succeed, at the cost of
native numeric/boolean typing); pass your own for real typing:

```python
from bitscrape import AvroExporter

schema = {"type": "record", "name": "Item",
          "fields": [{"name": "title", "type": "string"}, {"name": "price", "type": "double"}]}
exporter = AvroExporter("data.avro", schema=schema)
```

### WARC (Web ARChive)

The format the Internet Archive and Common Crawl use. WARC's natural unit is
a *fetched HTTP resource*, not an arbitrary key-value item — the opposite of
every other format here. Two ways to use it:

```python
from bitscrape import response_to_item

async def parse(self, response):
    yield response_to_item(response)                         # archive the raw page
    yield {"title": response.css("h1::text").get()}           # your extracted item
```

A `response_to_item()` item becomes a real WARC `response` record (actual
HTTP status/headers/body). Any other, plain-dict item becomes a `resource`
record whose payload is that item's JSON — so exporting ordinary items as
WARC never fails just because your spider doesn't call `response_to_item()`.

### PDF

Each item as its own page: a title, then a `key: value` line per field. A
plain, readable dump for handing results to a person — build your own layout
with `fpdf2` directly for anything more customised.

### Word (.docx)

Each item as a heading plus a paragraph per field.

`.doc` (the legacy pre-2007 binary format) is accepted as a filename
extension but **not actually produced** — no maintained pure-Python library
writes real binary `.doc`. Both extensions always write `.docx` (OOXML)
content; Word and LibreOffice open it correctly either way (they sniff the
real format from content, not the extension), but name the output `.docx` if
a downstream tool validates the format strictly.

---

## Using exporters in code

```python
import bitscrape
from bitscrape import get_exporter

# Create an exporter
exporter = get_exporter("parquet", "output/data.parquet")

# Or use specific classes directly
from bitscrape import JSONLExporter, CSVExporter, YAMLExporter, ParquetExporter

exporter = JSONLExporter("output/data.jsonl")
exporter = ParquetExporter("output/data.parquet", compression="zstd")
```

Pass to `bitscrape.run()`:

```python
bitscrape.run(MySpider, output="data.parquet", fmt="parquet")
```

Or to `Engine`:

```python
from bitscrape import Engine
from bitscrape.exporters import get_exporter

engine = Engine(
    spider=MySpider(),
    exporter=get_exporter("yaml", "output/products.yaml"),
)
```

### Output to stdout

Omit the filename to write to stdout:

```python
exporter = get_exporter("jsonl")   # writes to stdout
```

### A missing optional dependency raises a clear error

```python
>>> get_exporter("parquet", "out.parquet").open()
ImportError: Parquet export requires the 'pyarrow' package. Install with: pip install bitscrape[parquet]
```

---

## In-memory export: `ItemList`

`ItemList` (a plain `list` subclass) gives every format above as a `.to_X()`
method, without writing to a file exporter/pipeline yourself. Turn it on with
`Settings.collect_items=True` (off by default — a large crawl shouldn't
silently buffer every item in memory):

```python
settings = Settings(collect_items=True)
stats = await engine.run()

stats.items.to_json("out.json")          # write to a file
text = stats.items.to_jsonl()            # or get the content back directly (no path given)
stats.items.to_parquet("out.parquet", compression="zstd")
```

`stats.items` holds exactly what was actually exported — items *after* every
pipeline ran, in the order they were scraped, not the raw values a callback
yielded.

It also works standalone, with items collected any other way:

```python
from bitscrape import ItemList

items = ItemList([{"title": "Widget"}, {"title": "Gadget"}])
items.to_avro("out.avro", schema=my_schema)
```

Every `to_X` method takes an optional `path` (`str` or `pathlib.Path`):
given one, it writes there and returns `None`; omitted, it returns the
content directly — `str` for a text format, `bytes` for a binary one —
without touching disk (internally, via a private temp file so every
format's real, tested writer runs unchanged; the temp file is always cleaned
up, even on error).

`to_parquet(compression=)` and `to_avro(schema=)` accept the same extra
keyword arguments their exporter classes do.

---

## Processing output files

### JSONL with Python

```python
import json

with open("data.jsonl") as f:
    for line in f:
        item = json.loads(line)
        print(item["title"])
```

### JSONL with pandas

```python
import pandas as pd

df = pd.read_json("data.jsonl", lines=True)
print(df.describe())
```

### CSV / Parquet with pandas

```python
import pandas as pd

df = pd.read_csv("data.csv")
df = pd.read_parquet("data.parquet")
```
