# Feed Exporters

Feed exporters write scraped items to files. Bitscrape supports four formats
out of the box: JSONL, JSON, CSV, and XML.

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

```json
[
  {"title": "Widget A", "price": 9.99},
  {"title": "Widget B", "price": 14.99}
]
```

### CSV

Comma-separated values. Opens directly in Excel / Google Sheets.

```bash
bitscrape crawl myspider.py -o data.csv --fmt csv
```

```
title,price,in_stock
Widget A,9.99,True
Widget B,14.99,False
```

### XML

```bash
bitscrape crawl myspider.py -o data.xml --fmt xml
```

```xml
<?xml version="1.0" encoding="UTF-8"?>
<items>
  <item>
    <title>Widget A</title>
    <price>9.99</price>
  </item>
</items>
```

---

## Using Exporters in Code

```python
import bitscrape
from bitscrape import get_exporter

# Create an exporter
exporter = get_exporter("jsonl", "output/data.jsonl")

# Or use specific classes directly
from bitscrape import JSONLExporter, CSVExporter

exporter = JSONLExporter("output/data.jsonl")
exporter = CSVExporter("output/data.csv")
```

Pass to `bitscrape.run()`:

```python
bitscrape.run(MySpider, output="data.jsonl", fmt="jsonl")
```

Or to `Engine`:

```python
from bitscrape import Engine
from bitscrape.exporters.feed import get_exporter

engine = Engine(
    spider=MySpider(),
    exporter=get_exporter("csv", "output/products.csv"),
)
```

---

## Output to stdout

Omit the filename to write to stdout:

```python
exporter = get_exporter("jsonl")   # writes to stdout
```

---

## Processing Output Files

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

### CSV with pandas

```python
import pandas as pd

df = pd.read_csv("data.csv")
print(df.head())
```
