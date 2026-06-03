# Pipelines

Item pipelines process every item your spider yields. They run in sequence —
each pipeline receives the item from the previous one and returns it (possibly
modified), or raises `DropItem` to discard it.

```
Spider yields item
      │
      ▼
 [Pipeline 1]   ← ValidationPipeline
      │
      ▼
 [Pipeline 2]   ← DedupPipeline
      │
      ▼
 [Pipeline 3]   ← PostgresPipeline
      │
      ▼
  FeedExporter  → writes to file
```

---

## Built-in Pipelines

### LoggingPipeline

Logs every item at DEBUG level. Useful during development.

```python
from bitscrape import LoggingPipeline
```

### ValidationPipeline

Validates Pydantic items and drops items that fail validation.
Can also coerce plain dicts into a Pydantic model.

```python
from bitscrape import ValidationPipeline

# Validate any Pydantic Item (default)
pipeline = ValidationPipeline()

# Coerce dicts to a specific model and validate
pipeline = ValidationPipeline(model=ProductItem)
```

When using `model=ProductItem`, plain dicts yielded by your spider will be
automatically converted:

```python
# Spider yields a dict
yield {"name": "Widget", "price": "9.99"}

# ValidationPipeline(model=ProductItem) converts it to:
# ProductItem(name="Widget", price=9.99)  ← price is now a float
```

### DedupPipeline

Drops duplicate items. Uses a SHA-256 fingerprint of the item's full content
by default.

```python
from bitscrape import DedupPipeline

# Default: fingerprint entire item
pipeline = DedupPipeline()

# Custom key function
pipeline = DedupPipeline(key_fn=lambda item: item.get("url", ""))
```

### PostgresPipeline

Inserts or upserts items into a PostgreSQL table.

```python
from bitscrape import PostgresPipeline

pipeline = PostgresPipeline(
    table="products",
    conflict_cols=["url"],    # ON CONFLICT (url) DO UPDATE SET ...
)
```

Requires `BITSCRAPE_DATABASE_URL` to be set:

```env
BITSCRAPE_DATABASE_URL=postgresql://user:pass@localhost/mydb
```

Install: `pip install "bitscrape[postgres]"`

---

## Using Pipelines

Pass pipeline instances to `bitscrape.run()`:

```python
import bitscrape

stats = bitscrape.run(
    MySpider,
    output="data.jsonl",
    pipelines=[
        bitscrape.ValidationPipeline(),
        bitscrape.DedupPipeline(),
        bitscrape.LoggingPipeline(),
    ],
)
```

Or with `Engine` directly:

```python
from bitscrape import Engine, Settings
from bitscrape import ValidationPipeline, DedupPipeline, PostgresPipeline

engine = Engine(
    spider=MySpider(),
    settings=Settings(),
    pipelines=[
        ValidationPipeline(),
        DedupPipeline(),
        PostgresPipeline(table="products", conflict_cols=["url"]),
    ],
)
stats = await engine.run()
```

---

## Dropping Items

Raise `bitscrape.DropItem` inside a pipeline to silently discard an item:

```python
from bitscrape import DropItem

class PriceFilterPipeline(bitscrape.BasePipeline):
    async def process_item(self, item, spider):
        if item.get("price", 0) <= 0:
            raise DropItem(f"Invalid price: {item}")
        return item
```

Dropped items are counted in `CrawlStats.items_dropped`.

---

## Writing Custom Pipelines

Subclass `bitscrape.BasePipeline`:

```python
import bitscrape

class MyPipeline(bitscrape.BasePipeline):

    async def open_spider(self, spider):
        """Called once before the spider starts."""
        self.items_seen = 0

    async def process_item(self, item, spider):
        """
        Called for every item.
        Return the item to continue, raise DropItem to discard.
        """
        self.items_seen += 1
        # Modify the item
        if isinstance(item, dict):
            item["processed"] = True
        return item

    async def close_spider(self, spider):
        """Called once after the spider finishes."""
        print(f"Processed {self.items_seen} items")
```

---

## Custom Pipeline Examples

### Save to a custom file format

```python
import json

class NDJSONPipeline(bitscrape.BasePipeline):
    def __init__(self, filepath: str):
        self._filepath = filepath
        self._file = None

    async def open_spider(self, spider):
        self._file = open(self._filepath, "w", encoding="utf-8")

    async def process_item(self, item, spider):
        from pydantic import BaseModel
        data = item.model_dump() if isinstance(item, BaseModel) else item
        self._file.write(json.dumps(data) + "\n")
        return item

    async def close_spider(self, spider):
        if self._file:
            self._file.close()
```

### Send items to a webhook

```python
import aiohttp

class WebhookPipeline(bitscrape.BasePipeline):
    def __init__(self, url: str):
        self._url = url
        self._session = None

    async def open_spider(self, spider):
        self._session = aiohttp.ClientSession()

    async def process_item(self, item, spider):
        from pydantic import BaseModel
        data = item.model_dump() if isinstance(item, BaseModel) else item
        async with self._session.post(self._url, json=data) as resp:
            if resp.status != 200:
                spider.logger.warning("Webhook failed: %d", resp.status)
        return item

    async def close_spider(self, spider):
        if self._session:
            await self._session.close()
```

### Filter items by field value

```python
class InStockPipeline(bitscrape.BasePipeline):
    async def process_item(self, item, spider):
        in_stock = (
            item.in_stock
            if hasattr(item, "in_stock")
            else item.get("in_stock", True)
        )
        if not in_stock:
            raise bitscrape.DropItem("Out of stock — skipping")
        return item
```

### Enrich items with extra data

```python
import aiohttp

class GeoEnrichPipeline(bitscrape.BasePipeline):
    """Add country name from IP address."""

    async def open_spider(self, spider):
        self._session = aiohttp.ClientSession()

    async def process_item(self, item, spider):
        ip = item.get("ip") if isinstance(item, dict) else getattr(item, "ip", None)
        if ip:
            async with self._session.get(f"https://ipapi.co/{ip}/country_name/") as r:
                country = await r.text()
                if isinstance(item, dict):
                    item["country"] = country.strip()
        return item

    async def close_spider(self, spider):
        await self._session.close()
```

---

## Pipeline Order Matters

Pipelines run in the order you provide them. Put validation/filtering early
to avoid processing items that will be dropped later:

```python
pipelines = [
    ValidationPipeline(),    # 1. validate — drop invalid items early
    DedupPipeline(),         # 2. dedup — drop duplicates
    InStockPipeline(),       # 3. filter — drop out-of-stock
    GeoEnrichPipeline(),     # 4. enrich — only runs on valid, unique, in-stock items
    PostgresPipeline(table="products"),  # 5. store — last step
]
```
