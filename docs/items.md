# Items

Items are the structured data objects your spider yields. Bitscrape uses
**Pydantic v2** models for all items, giving you automatic type validation,
conversion, and serialization.

---

## Defining an Item

Subclass `bitscrape.Item` (which is `BaseItem`, a Pydantic `BaseModel`):

```python
import bitscrape

class ProductItem(bitscrape.Item):
    name: str
    price: float
    in_stock: bool = True
    url: str = ""
    tags: list[str] = []
```

---

## Built-in Fields

Every `bitscrape.Item` automatically includes:

| Field | Type | Description |
|-------|------|-------------|
| `source_url` | `str` | URL the item was scraped from |
| `scraped_at` | `float` | Unix timestamp of when item was scraped |

---

## Using Pydantic Field

Use `bitscrape.Field` (which is Pydantic's `Field`) for advanced validation:

```python
import bitscrape

class ProductItem(bitscrape.Item):
    name: str = bitscrape.Field(..., min_length=1, max_length=500)
    price: float = bitscrape.Field(..., ge=0)           # must be >= 0
    rating: float = bitscrape.Field(default=0.0, ge=0, le=5.0)
    url: str = bitscrape.Field(..., pattern=r"^https?://")
    description: str | None = None
```

---

## Type Coercion

Pydantic automatically converts compatible types, so you don't need to
manually cast values:

```python
class ProductItem(bitscrape.Item):
    price: float    # "9.99" → 9.99 automatically
    count: int      # "42" → 42 automatically
    active: bool    # "true" → True automatically
```

```python
# Spider code — no manual float() needed
yield ProductItem(
    price=response.css(".price::text").get(default="0"),  # "£9.99" → needs manual strip
    count=response.css(".count::text").get(default="0"),  # "42" → auto-converted
)
```

---

## Optional Fields

Use Python's `Optional` or `| None` syntax:

```python
from typing import Optional
import bitscrape

class ArticleItem(bitscrape.Item):
    title: str
    author: str | None = None        # may not be present
    published_date: str | None = None
    image_url: Optional[str] = None
```

---

## Nested Items

```python
import bitscrape
from pydantic import BaseModel

class AddressModel(BaseModel):
    street: str
    city: str
    country: str = "US"

class CompanyItem(bitscrape.Item):
    name: str
    address: AddressModel
    employee_count: int = 0
```

---

## Yielding Items in a Spider

```python
import bitscrape

class BookItem(bitscrape.Item):
    title: str
    price: float
    in_stock: bool = True

class BooksSpider(bitscrape.Spider):
    name = "books"
    start_urls = ["https://books.toscrape.com/"]

    async def parse(self, response):
        for book in response.css("article.product_pod"):
            raw_price = book.css("p.price_color::text").get(default="0")
            try:
                price = float(raw_price.replace("£", "").replace("Â", "").strip())
            except ValueError:
                price = 0.0

            yield BookItem(
                title=book.css("h3 a::attr(title)").get(default=""),
                price=price,
                in_stock="In stock" in (book.css("p.availability::text").get() or ""),
            )
```

---

## Plain Dicts vs Item Models

You can yield plain dicts — they work fine for simple scripts:

```python
yield {"title": "foo", "price": 9.99}
```

But `bitscrape.Item` models are recommended because:

- **Type safety** — wrong types raise errors immediately at scrape time
- **Default values** — missing fields get sensible defaults
- **Validation** — invalid data is caught before reaching your database
- **Serialization** — clean `.model_dump()` / `.model_dump_json()` output
- **IDE support** — autocomplete and type hints everywhere

---

## Serializing Items

```python
item = BookItem(title="Dune", price=12.99)

# To dict
item.model_dump()
# → {"title": "Dune", "price": 12.99, "in_stock": True, "source_url": "", ...}

# To JSON string
item.model_dump_json()
# → '{"title":"Dune","price":12.99,"in_stock":true,...}'

# To dict, only set fields
item.model_dump(exclude_defaults=True)
```

---

## Validation Errors

If you pass invalid data, Pydantic raises a `ValidationError` immediately:

```python
# This raises ValidationError — price must be a float
BookItem(title="Dune", price="not-a-number")
```

Use the `ValidationPipeline` to automatically drop items that fail validation
instead of crashing the spider. See [pipelines.md](pipelines.md).
