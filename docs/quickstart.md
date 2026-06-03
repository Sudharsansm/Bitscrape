# Quickstart

Get your first spider running in under 5 minutes.

---

## Step 1 — Install Bitscrape

```bash
pip install bitscrape
```

---

## Step 2 — Create a Spider File

Create a file called `myspider.py`:

```python
import bitscrape

class QuotesSpider(bitscrape.Spider):
    name = "quotes"
    start_urls = ["https://quotes.toscrape.com/"]

    async def parse(self, response):
        for quote in response.css("div.quote"):
            yield {
                "text":   quote.css("span.text::text").get(default=""),
                "author": quote.css("small.author::text").get(default=""),
                "tags":   quote.css("div.tags a.tag::text").getall(),
            }

        # Follow pagination
        next_page = response.css("li.next a::attr(href)").get()
        if next_page:
            yield self.follow(f"https://quotes.toscrape.com{next_page}")
```

---

## Step 3 — Run It

**Option A — CLI:**

```bash
bitscrape crawl myspider.py -o quotes.jsonl
```

**Option B — Python script** (add to bottom of myspider.py):

```python
if __name__ == "__main__":
    stats = bitscrape.run(QuotesSpider, output="quotes.jsonl")
    print(f"Done! {stats.items_scraped} items scraped.")
```

```bash
python myspider.py
```

---

## Step 4 — Check the Output

```bash
# View first 3 lines
head -3 quotes.jsonl
```

```json
{"text": "\u201cThe world as we have created it...", "author": "Albert Einstein", "tags": ["change", "deep-thoughts"]}
{"text": "\u201cIt is our choices, Harry...", "author": "J.K. Rowling", "tags": ["abilities", "choices"]}
{"text": "\u201cThere are only two ways...", "author": "Albert Einstein", "tags": ["simplicity", "life"]}
```

---

## Step 5 — Use Typed Items (Optional but Recommended)

Instead of plain dicts, define a Pydantic model for your data:

```python
import bitscrape

class QuoteItem(bitscrape.Item):
    text: str
    author: str
    tags: list[str] = []

class QuotesSpider(bitscrape.Spider):
    name = "quotes"
    start_urls = ["https://quotes.toscrape.com/"]

    async def parse(self, response):
        for quote in response.css("div.quote"):
            yield QuoteItem(
                text=quote.css("span.text::text").get(default=""),
                author=quote.css("small.author::text").get(default=""),
                tags=quote.css("div.tags a.tag::text").getall(),
            )
        next_page = response.css("li.next a::attr(href)").get()
        if next_page:
            yield self.follow(f"https://quotes.toscrape.com{next_page}")

if __name__ == "__main__":
    bitscrape.run(QuotesSpider, output="quotes.jsonl")
```

Pydantic automatically validates and converts field types at scrape time,
so bad data is caught before it reaches your database.

---

## What You Just Learned

| Concept | What it does |
|---------|--------------|
| `bitscrape.Spider` | Base class for all spiders |
| `start_urls` | Seed URLs the spider starts from |
| `async def parse(response)` | Called for every downloaded page |
| `response.css("selector")` | Extract data using CSS selectors |
| `::text` | Get the text content of a matched element |
| `::attr(name)` | Get an attribute value |
| `.get()` | Return the first match |
| `.getall()` | Return all matches as a list |
| `yield {...}` | Emit a scraped item |
| `yield self.follow(url)` | Enqueue a new URL to crawl |
| `bitscrape.run()` | Run the spider programmatically |

---

## Next Steps

- [spiders.md](spiders.md) — learn the full Spider API
- [selectors.md](selectors.md) — CSS and XPath selector reference
- [items.md](items.md) — define typed data models
- [settings.md](settings.md) — configure concurrency, delays, storage
- [cli.md](cli.md) — full CLI command reference
