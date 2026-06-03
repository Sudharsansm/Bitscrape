# Spiders

A spider is a Python class that defines:
- **Where** to start crawling (`start_urls`)
- **How** to parse pages (`parse` method)
- **What** to yield (items or new requests)

---

## Minimal Spider

```python
import bitscrape

class MySpider(bitscrape.Spider):
    name = "myspider"
    start_urls = ["https://example.com"]

    async def parse(self, response):
        yield {"title": response.css("h1::text").get()}
```

---

## Spider Class Attributes

| Attribute | Type | Required | Description |
|-----------|------|----------|-------------|
| `name` | `str` | ✅ Yes | Unique identifier for the spider |
| `start_urls` | `list[str]` | ✅ Yes | Seed URLs to begin crawling |
| `custom_settings` | `dict` | No | Override global settings for this spider only |

### `name`

Must be unique across all spiders in a project. Used by the CLI to identify
which spider to run.

```python
name = "products"
```

### `start_urls`

One or more seed URLs. The engine enqueues all of them before starting.

```python
start_urls = [
    "https://example.com/category/phones",
    "https://example.com/category/laptops",
    "https://example.com/category/tablets",
]
```

### `custom_settings`

Override specific settings just for this spider without changing the global
`Settings` object:

```python
custom_settings = {
    "concurrent_requests": 4,
    "download_delay": 1.0,
    "robotstxt_obey": False,
}
```

---

## The `parse` Method

The `parse` method is an **async generator** — it receives a `ParsedResponse`
and yields items (dicts or `bitscrape.Item` subclasses) and/or new `Request`
objects.

```python
async def parse(self, response):
    # Yield an item
    yield {"title": response.css("h1::text").get()}

    # Yield a new request to follow
    yield self.follow("/next-page")
```

### Receiving a ParsedResponse

The `response` argument is a `ParsedResponse` object with these properties:

| Property | Type | Description |
|----------|------|-------------|
| `response.url` | `str` | Final URL after redirects |
| `response.status` | `int` | HTTP status code |
| `response.text` | `str` | Response body as string |
| `response.body` | `bytes` | Raw response bytes |
| `response.request` | `Request` | The original request |

---

## Multiple Callbacks

Use different parse methods for different page types:

```python
import bitscrape

class EcommerceSpider(bitscrape.Spider):
    name = "shop"
    start_urls = ["https://shop.example.com/"]

    async def parse(self, response):
        """Parse category page — find product links."""
        for link in response.css("a.product-link::attr(href)").getall():
            yield self.follow(link, callback="parse_product")

        # Pagination
        nxt = response.css("a.next::attr(href)").get()
        if nxt:
            yield self.follow(nxt)

    async def parse_product(self, response):
        """Parse individual product page."""
        yield {
            "name":  response.css("h1.product-title::text").get(),
            "price": response.css("span.price::text").get(),
            "sku":   response.css("span.sku::text").get(),
        }
```

---

## Following Links

### `self.follow(url)`

The main way to enqueue new pages:

```python
yield self.follow("/page/2")
yield self.follow("https://example.com/page/2")
```

Full signature:

```python
self.follow(
    url: str,
    callback: str = "parse",        # which method to call
    meta: dict = {},                 # pass data to the next page
    use_playwright: bool = False,    # use browser for JS pages
)
```

### Passing data between pages

Use `meta` to carry data from one page to the next:

```python
async def parse(self, response):
    for link in response.css("a.product::attr(href)").getall():
        category = response.css("h1::text").get()
        yield self.follow(link, callback="parse_product",
                          meta={"category": category})

async def parse_product(self, response):
    category = response.request.meta.get("category", "")
    yield {
        "name":     response.css("h1::text").get(),
        "category": category,
    }
```

---

## JavaScript Pages

Mark a request to use Playwright (headless browser) instead of plain HTTP:

```python
async def parse(self, response):
    # This page loads content via JavaScript
    yield self.follow("/dynamic-content", use_playwright=True)

async def parse_dynamic(self, response):
    # response.body is now the fully rendered HTML
    yield {"data": response.css("div.loaded-content::text").get()}
```

Requires: `pip install "bitscrape[playwright]"` and `playwright install chromium`.

---

## Lifecycle Hooks

Override these methods to run code before/after the crawl:

```python
class MySpider(bitscrape.Spider):
    name = "my"
    start_urls = ["https://example.com"]

    async def open_spider(self):
        """Called once before crawling starts."""
        print("Spider starting...")
        self.seen_urls = set()

    async def close_spider(self):
        """Called once after crawling finishes."""
        print(f"Spider done. Saw {len(self.seen_urls)} URLs.")

    async def parse(self, response):
        self.seen_urls.add(response.url)
        yield {"url": response.url}
```

---

## Error Handling

Override `errback` to handle failed requests:

```python
class MySpider(bitscrape.Spider):
    name = "my"
    start_urls = ["https://example.com"]

    async def errback(self, request, exc):
        self.logger.error("Failed: %s — %s", request.url, exc)
        # Optionally yield a fallback request:
        # yield self.follow(fallback_url)

    async def parse(self, response):
        yield {"url": response.url}
```

---

## Using the Logger

Every spider has a built-in logger named after the spider:

```python
class MySpider(bitscrape.Spider):
    name = "my"
    start_urls = ["https://example.com"]

    async def parse(self, response):
        self.logger.info("Parsing: %s", response.url)
        self.logger.debug("Status: %d", response.status)
        yield {"url": response.url}
```

---

## Full Spider Example

```python
import bitscrape

class BookItem(bitscrape.Item):
    title: str
    price: float
    in_stock: bool = True
    url: str = ""

class BooksSpider(bitscrape.Spider):
    name = "books"
    start_urls = ["https://books.toscrape.com/"]

    custom_settings = {
        "concurrent_requests": 8,
        "robotstxt_obey": False,
    }

    async def open_spider(self):
        self.logger.info("Starting books spider")

    async def parse(self, response):
        for book in response.css("article.product_pod"):
            raw = book.css("p.price_color::text").get(default="0")
            try:
                price = float(raw.replace("£", "").replace("Â", "").strip())
            except ValueError:
                price = 0.0

            yield BookItem(
                title=book.css("h3 a::attr(title)").get(default=""),
                price=price,
                in_stock="In stock" in (book.css("p.availability::text").get() or ""),
                url=response.url,
            )

        nxt = response.css("li.next a::attr(href)").get()
        if nxt:
            from urllib.parse import urljoin
            yield self.follow(urljoin(response.url, nxt))

    async def close_spider(self):
        self.logger.info("Books spider finished")

if __name__ == "__main__":
    stats = bitscrape.run(BooksSpider, output="books.jsonl")
    print(f"Scraped {stats.items_scraped} books in {stats.elapsed:.1f}s")
```
