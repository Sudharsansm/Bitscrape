# Selectors

Bitscrape provides CSS and XPath selectors via `ParsedResponse` — the object
passed to your spider's `parse` method.

The CSS backend uses **selectolax** (a fast C parser). XPath uses **parsel/lxml**.

---

## CSS Selectors

### Basic usage

```python
async def parse(self, response):
    # Select by tag
    title = response.css("h1").get()

    # Select by class
    price = response.css(".price").get()

    # Select by ID
    header = response.css("#header").get()

    # Nested selector
    link = response.css("nav ul li a").get()
```

### Extract text — `::text`

```python
# Text of a single element
title = response.css("h1::text").get()

# Text of all matching elements
items = response.css("ul li::text").getall()
# → ["Item 1", "Item 2", "Item 3"]
```

### Extract attributes — `::attr(name)`

```python
# href of a link
href = response.css("a::attr(href)").get()

# src of all images
images = response.css("img::attr(src)").getall()

# data attribute
value = response.css("div::attr(data-id)").get()
```

---

## XPath Selectors

```python
# Text content
title = response.xpath("//h1/text()").get()

# Attribute
href = response.xpath("//a/@href").get()

# All matching text
authors = response.xpath("//span[@class='author']/text()").getall()

# Complex condition
price = response.xpath("//p[contains(@class,'price')]/text()").get()
```

---

## SelectorList Methods

Both `.css()` and `.xpath()` return a `SelectorList`.

| Method | Returns | Description |
|--------|---------|-------------|
| `.get()` | `str \| None` | First match or `None` |
| `.get(default="")` | `str` | First match or default value |
| `.getall()` | `list[str]` | All matches as strings |
| `len(selector_list)` | `int` | Number of matches |
| `bool(selector_list)` | `bool` | True if any matches |

```python
# Safe extraction with default
price = response.css(".price::text").get(default="0.00")

# Check if element exists
if response.css("div.out-of-stock"):
    return  # skip out-of-stock pages

# Count matches
num_products = len(response.css("article.product"))
```

---

## Iterating Over Elements

This is one of the most common patterns — iterate over matched elements and
apply sub-selectors on each:

```python
async def parse(self, response):
    for product in response.css("div.product-card"):
        # Each `product` is a NodeSelector
        name  = product.css("h2.name::text").get(default="")
        price = product.css("span.price::text").get(default="0")
        link  = product.css("a::attr(href)").get(default="")
        yield {"name": name, "price": price, "link": link}
```

---

## Chained Selectors

You can chain `.css()` calls on any `NodeSelector`:

```python
async def parse(self, response):
    # Get the nav element first
    nav = response.css("nav.main-nav")

    # Then query inside it
    links = nav.css("a::attr(href)").getall()
    active = nav.css("a.active::text").get()
```

---

## Practical Examples

### Extract a table

```python
async def parse(self, response):
    for row in response.css("table.data tr"):
        cells = row.css("td::text").getall()
        if len(cells) >= 3:
            yield {
                "name":  cells[0],
                "value": cells[1],
                "date":  cells[2],
            }
```

### Extract all links on a page

```python
async def parse(self, response):
    for link in response.css("a::attr(href)").getall():
        if link.startswith("http"):
            yield self.follow(link)
```

### Extract structured article data

```python
async def parse(self, response):
    yield {
        "title":      response.css("h1::text").get(),
        "author":     response.css("span.author::text").get(),
        "date":       response.css("time::attr(datetime)").get(),
        "body":       " ".join(response.css("article p::text").getall()),
        "tags":       response.css("a.tag::text").getall(),
        "image_url":  response.css("img.hero::attr(src)").get(),
    }
```

### Handle missing elements safely

```python
async def parse(self, response):
    # Use default="" to avoid None values
    title = response.css("h1::text").get(default="Untitled")
    price_text = response.css(".price::text").get(default="0")

    try:
        price = float(price_text.replace("$", "").strip())
    except ValueError:
        price = 0.0

    yield {"title": title, "price": price}
```

---

## CSS Selector Reference

| Selector | Matches |
|----------|---------|
| `div` | All `<div>` elements |
| `.class` | Elements with that class |
| `#id` | Element with that ID |
| `div.card` | `<div>` elements with class `card` |
| `div > p` | `<p>` directly inside `<div>` |
| `div p` | All `<p>` anywhere inside `<div>` |
| `a[href]` | `<a>` elements that have an `href` attribute |
| `a[href="/page"]` | `<a>` with exact `href="/page"` |
| `p:first-child` | First `<p>` child |
| `li:nth-child(2)` | Second `<li>` child |
| `::text` | Text content (Bitscrape extension) |
| `::attr(name)` | Attribute value (Bitscrape extension) |

---

## Tips

**Always use `.get(default=...)` to avoid `None` errors:**
```python
# Bad — price could be None if element missing
price = float(response.css(".price::text").get())

# Good — safe fallback
price = float(response.css(".price::text").get(default="0"))
```

**Use `.getall()` for lists, `.get()` for single values:**
```python
# Single value
title = response.css("h1::text").get()

# Multiple values
tags = response.css("a.tag::text").getall()
```

**Test selectors in your browser:**
1. Open the target page in Chrome
2. Press F12 → Console
3. Type: `document.querySelectorAll("div.product")` to test CSS
4. Use the Elements panel to inspect the HTML structure
