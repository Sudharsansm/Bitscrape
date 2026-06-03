# CLI Reference

Bitscrape provides a command-line interface for running spiders and
managing projects.

---

## `bitscrape crawl`

Run a spider and optionally save output to a file.

```bash
bitscrape crawl SPIDER_PATH [OPTIONS]
```

**Arguments:**

| Argument | Description |
|----------|-------------|
| `SPIDER_PATH` | Path to a `.py` file or a dotted module path |

**Options:**

| Option | Default | Description |
|--------|---------|-------------|
| `-o, --output PATH` | None | Output file (e.g. `data.jsonl`) |
| `--fmt FORMAT` | `jsonl` | Output format: `jsonl` \| `json` \| `csv` \| `xml` |
| `--log-level LEVEL` | `INFO` | Log level: `DEBUG` \| `INFO` \| `WARNING` \| `ERROR` |
| `--no-robots` | off | Ignore robots.txt |
| `--concurrency N` | 16 | Max concurrent requests |

**Examples:**

```bash
# Run a spider, save as JSONL
bitscrape crawl spiders/quotes.py -o quotes.jsonl

# Run from module path
bitscrape crawl myproject.spiders.products -o products.csv --fmt csv

# Debug mode with low concurrency
bitscrape crawl spiders/myspider.py --log-level DEBUG --concurrency 1

# Ignore robots.txt
bitscrape crawl spiders/myspider.py --no-robots -o data.jsonl

# Output XML
bitscrape crawl spiders/myspider.py -o data.xml --fmt xml
```

---

## `bitscrape startproject`

Create a new Bitscrape project with the recommended folder structure.

```bash
bitscrape startproject PROJECT_NAME
```

**Example:**

```bash
bitscrape startproject myproject
cd myproject
```

**Created structure:**

```
myproject/
├── spiders/
│   └── __init__.py
├── pipelines/
│   └── __init__.py
├── items/
│   └── __init__.py
├── settings.py
└── README.md
```

---

## `bitscrape genspider`

Generate a spider file from a template.

```bash
bitscrape genspider NAME DOMAIN [OPTIONS]
```

**Arguments:**

| Argument | Description |
|----------|-------------|
| `NAME` | Spider name (also used as filename) |
| `DOMAIN` | Target domain (e.g. `example.com`) |

**Options:**

| Option | Default | Description |
|--------|---------|-------------|
| `--template TYPE` | `basic` | Template: `basic` \| `crawl` \| `sitemap` |

**Examples:**

```bash
# Basic spider
bitscrape genspider products shop.example.com
# → creates spiders/products.py

# Crawl spider (follows links + pagination)
bitscrape genspider news news.example.com --template crawl

# Sitemap spider (reads sitemap.xml)
bitscrape genspider catalog catalog.example.com --template sitemap
```

**Template: `basic`** — simple single-page spider:

```python
class ProductsSpider(bitscrape.Spider):
    name = "products"
    start_urls = ["https://shop.example.com/"]

    async def parse(self, response):
        title = response.css("title::text").get()
        yield {"url": response.url, "title": title}
```

**Template: `crawl`** — spider that follows links and pagination:

```python
class NewsSpider(bitscrape.Spider):
    name = "news"
    start_urls = ["https://news.example.com/"]

    async def parse(self, response):
        for item in response.css("article"):
            yield {"title": item.css("h2::text").get(),
                   "link":  item.css("a::attr(href)").get()}
        nxt = response.css("a.next::attr(href)").get()
        if nxt:
            yield self.follow(nxt)
```

**Template: `sitemap`** — spider that reads sitemap.xml:

```python
class CatalogSpider(bitscrape.Spider):
    name = "catalog"
    start_urls = ["https://catalog.example.com/sitemap.xml"]

    async def parse(self, response):
        for url in response.css("loc::text").getall():
            yield self.follow(url, callback="parse_page")

    async def parse_page(self, response):
        yield {"url": response.url,
               "title": response.css("title::text").get()}
```

---

## `bitscrape list`

List all spiders found in the `spiders/` directory.

```bash
bitscrape list [--dir DIRECTORY]
```

**Options:**

| Option | Default | Description |
|--------|---------|-------------|
| `--dir DIRECTORY` | `spiders` | Directory to scan for spiders |

**Example:**

```bash
$ bitscrape list
  • books
  • news
  • products
  • quotes
```

---

## `bitscrape --version`

Print the installed Bitscrape version.

```bash
$ bitscrape --version
bitscrape, version 0.1.0
```

---

## Environment Variables in CLI

All settings can be passed as environment variables:

```bash
BITSCRAPE_CONCURRENT_REQUESTS=32 \
BITSCRAPE_DOWNLOAD_DELAY=0.5 \
BITSCRAPE_LOG_LEVEL=DEBUG \
bitscrape crawl spiders/myspider.py -o data.jsonl
```
