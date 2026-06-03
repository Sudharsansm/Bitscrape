# Downloader

The Bitscrape downloader handles all HTTP requests. It is built on **aiohttp**
for async HTTP and **Playwright** for JavaScript-rendered pages.

---

## How It Works

1. The Engine pops a `Request` from the Scheduler
2. Middleware processes the request (add headers, check robots.txt, etc.)
3. The Downloader fetches the URL — via aiohttp or Playwright
4. The raw response is wrapped in a `Response` object
5. Middleware processes the response
6. The response is passed to the spider's `parse` method

---

## HTTP Downloader (aiohttp)

The default downloader uses **aiohttp** — a high-performance async HTTP
client. It supports:

- Configurable concurrency (global + per-domain)
- Automatic retries with exponential backoff
- HTTP redirects
- Custom headers and cookies
- Proxy support (via middleware)
- Response streaming

### Concurrency

```python
settings = bitscrape.Settings(
    concurrent_requests=16,              # total simultaneous requests
    concurrent_requests_per_domain=4,    # per-domain limit
)
```

### Retries

Failed requests are automatically retried for these HTTP codes (by default):
`500, 502, 503, 504, 429`

```python
settings = bitscrape.Settings(
    retry_http_codes=[500, 502, 503, 504, 429, 403],
)
```

Each retry waits longer (exponential backoff): 2s, 4s, 8s... up to 30s max.
The number of retries is set per-request via `Request.max_retries` (default: 3).

### Timeout

```python
settings = bitscrape.Settings(
    download_timeout=30.0,    # seconds
)
```

### Custom Headers

Set headers on individual requests:

```python
yield bitscrape.Request(
    url="https://api.example.com/data",
    headers={
        "Authorization": "Bearer mytoken",
        "Accept": "application/json",
    },
    callback="parse_api",
)
```

Or set a global User-Agent:

```python
settings = bitscrape.Settings(
    user_agent="MyBot/1.0 (https://mysite.com/bot)"
)
```

---

## Playwright (JavaScript Rendering)

For pages that load content via JavaScript (React, Vue, Angular SPAs, infinite
scroll, etc.), Bitscrape uses **Playwright** to launch a real browser.

### Install

```bash
pip install "bitscrape[playwright]"
playwright install chromium
```

### Mark a request for Playwright

```python
async def parse(self, response):
    # This page needs JavaScript to load products
    yield self.follow("/products", use_playwright=True)

async def parse_products(self, response):
    # response.body is now fully rendered HTML
    for product in response.css("div.product-card"):
        yield {"name": product.css("h2::text").get()}
```

### Or set it on a Request directly

```python
yield bitscrape.Request(
    url="https://spa.example.com/products",
    use_playwright=True,
    callback="parse_products",
)
```

### Playwright Settings

```python
settings = bitscrape.Settings(
    playwright_headless=True,       # False to see the browser window
    playwright_browser="chromium",  # chromium | firefox | webkit
    playwright_pool_size=2,         # browser instances to keep open
)
```

### Watching Playwright Run (Debug Mode)

```python
settings = bitscrape.Settings(
    playwright_headless=False,   # opens a visible browser window
)
```

---

## Download Delay

Add a delay between requests to the same domain to be polite:

```python
settings = bitscrape.Settings(
    download_delay=1.0,   # 1 second between requests per domain
)
```

---

## Request Object

Every crawl request is a `bitscrape.Request` Pydantic model:

```python
request = bitscrape.Request(
    url="https://example.com/page",
    method="GET",                          # GET | POST | PUT | DELETE
    headers={"Accept": "text/html"},
    body=None,                             # bytes for POST body
    meta={"category": "electronics"},     # pass data to callback
    max_retries=3,
    use_playwright=False,
    callback="parse",                      # spider method to call
    depth=0,                               # auto-managed by engine
)
```

## FormRequest (POST Requests)

Use `bitscrape.FormRequest` for form submissions:

```python
yield bitscrape.FormRequest(
    url="https://example.com/search",
    formdata={
        "query": "laptop",
        "category": "electronics",
        "page": "1",
    },
    callback="parse_results",
)
```

`FormRequest` automatically:
- Sets `method = "POST"`
- URL-encodes `formdata` as the request body
- Sets `Content-Type: application/x-www-form-urlencoded`

---

## Response Object

The `Response` object passed to middleware and available via
`response.request` in your spider:

| Property | Type | Description |
|----------|------|-------------|
| `url` | `str` | Final URL (after redirects) |
| `status` | `int` | HTTP status code |
| `headers` | `dict` | Response headers |
| `body` | `bytes` | Raw response bytes |
| `text` | `str` | Decoded response body |
| `ok` | `bool` | True if status 200–299 |
| `request` | `Request` | The original request |
| `elapsed_ms` | `float` | Time taken in milliseconds |
| `encoding` | `str` | Character encoding |
