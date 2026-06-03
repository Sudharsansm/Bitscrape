# Settings

All Bitscrape settings are defined as a Pydantic `BaseSettings` model.
Every setting can be overridden via:

1. Environment variables (prefixed `BITSCRAPE_`)
2. A `.env` file in the project root
3. Passing a `Settings` instance directly to `bitscrape.run()` or `Engine`

---

## Complete Settings Reference

### Concurrency

| Setting | Default | Env Var | Description |
|---------|---------|---------|-------------|
| `concurrent_requests` | `16` | `BITSCRAPE_CONCURRENT_REQUESTS` | Max simultaneous in-flight requests |
| `concurrent_requests_per_domain` | `4` | `BITSCRAPE_CONCURRENT_REQUESTS_PER_DOMAIN` | Max requests to a single domain at once |
| `download_delay` | `0.0` | `BITSCRAPE_DOWNLOAD_DELAY` | Seconds to wait between requests to same domain |

### Downloader

| Setting | Default | Env Var | Description |
|---------|---------|---------|-------------|
| `download_timeout` | `30.0` | `BITSCRAPE_DOWNLOAD_TIMEOUT` | Seconds before a request times out |
| `retry_http_codes` | `[500,502,503,504,429]` | `BITSCRAPE_RETRY_HTTP_CODES` | HTTP status codes that trigger a retry |
| `user_agent` | `BitscrapeBot/0.1 ...` | `BITSCRAPE_USER_AGENT` | Default User-Agent header |
| `follow_redirects` | `True` | `BITSCRAPE_FOLLOW_REDIRECTS` | Follow HTTP redirects |
| `max_redirect_count` | `10` | `BITSCRAPE_MAX_REDIRECT_COUNT` | Maximum redirect chain length |

### Scheduler

| Setting | Default | Env Var | Description |
|---------|---------|---------|-------------|
| `scheduler_use_redis` | `False` | `BITSCRAPE_SCHEDULER_USE_REDIS` | Use Redis queue for distributed mode |
| `redis_url` | `redis://localhost:6379/0` | `BITSCRAPE_REDIS_URL` | Redis connection URL |
| `dupefilter_enabled` | `True` | `BITSCRAPE_DUPEFILTER_ENABLED` | Deduplicate requests by URL fingerprint |
| `max_depth` | `None` | `BITSCRAPE_MAX_DEPTH` | Max crawl depth (None = unlimited) |

### Playwright

| Setting | Default | Env Var | Description |
|---------|---------|---------|-------------|
| `playwright_headless` | `True` | `BITSCRAPE_PLAYWRIGHT_HEADLESS` | Run browser in headless mode |
| `playwright_browser` | `chromium` | `BITSCRAPE_PLAYWRIGHT_BROWSER` | Browser type: chromium / firefox / webkit |
| `playwright_pool_size` | `2` | `BITSCRAPE_PLAYWRIGHT_POOL_SIZE` | Number of browser instances to keep open |

### Storage

| Setting | Default | Env Var | Description |
|---------|---------|---------|-------------|
| `database_url` | `None` | `BITSCRAPE_DATABASE_URL` | asyncpg PostgreSQL DSN |
| `supabase_url` | `None` | `BITSCRAPE_SUPABASE_URL` | Supabase project URL |
| `supabase_key` | `None` | `BITSCRAPE_SUPABASE_KEY` | Supabase anon/service key |

### Logging & Observability

| Setting | Default | Env Var | Description |
|---------|---------|---------|-------------|
| `log_level` | `INFO` | `BITSCRAPE_LOG_LEVEL` | Logging level: DEBUG / INFO / WARNING / ERROR |
| `stats_dump_interval` | `60.0` | `BITSCRAPE_STATS_DUMP_INTERVAL` | Seconds between stats log lines |

### Feed Exports

| Setting | Default | Env Var | Description |
|---------|---------|---------|-------------|
| `feed_uri` | `None` | `BITSCRAPE_FEED_URI` | Output file path (e.g. `data.jsonl`) |
| `feed_format` | `jsonl` | `BITSCRAPE_FEED_FORMAT` | Format: jsonl / json / csv / xml |

### Robots.txt

| Setting | Default | Env Var | Description |
|---------|---------|---------|-------------|
| `robotstxt_obey` | `True` | `BITSCRAPE_ROBOTSTXT_OBEY` | Respect robots.txt rules |

---

## Usage Examples

### In Python code

```python
import bitscrape

# Use defaults
settings = bitscrape.Settings()

# Override specific values
settings = bitscrape.Settings(
    concurrent_requests=32,
    download_delay=0.5,
    robotstxt_obey=False,
    log_level="DEBUG",
)

# Pass to run()
bitscrape.run(MySpider, settings=settings)
```

### Via .env file

Create a `.env` file in your project root:

```env
BITSCRAPE_CONCURRENT_REQUESTS=32
BITSCRAPE_DOWNLOAD_DELAY=0.5
BITSCRAPE_ROBOTSTXT_OBEY=false
BITSCRAPE_LOG_LEVEL=DEBUG
BITSCRAPE_DATABASE_URL=postgresql://user:pass@localhost/mydb
BITSCRAPE_REDIS_URL=redis://localhost:6379/0
```

### Via environment variables

```bash
export BITSCRAPE_CONCURRENT_REQUESTS=32
export BITSCRAPE_LOG_LEVEL=DEBUG
bitscrape crawl myspider.py -o data.jsonl
```

Or inline for a single run:

```bash
BITSCRAPE_CONCURRENT_REQUESTS=64 bitscrape crawl myspider.py -o data.jsonl
```

---

## Per-Spider Settings

Override settings for a single spider using `custom_settings`:

```python
class FastSpider(bitscrape.Spider):
    name = "fast"
    start_urls = ["https://example.com"]

    custom_settings = {
        "concurrent_requests": 64,
        "download_delay": 0.0,
        "robotstxt_obey": False,
    }
```

---

## Settings for Common Use Cases

### Polite crawling (respect the server)

```python
settings = bitscrape.Settings(
    concurrent_requests=4,
    concurrent_requests_per_domain=1,
    download_delay=2.0,
    robotstxt_obey=True,
)
```

### Maximum speed

```python
settings = bitscrape.Settings(
    concurrent_requests=64,
    concurrent_requests_per_domain=16,
    download_delay=0.0,
    robotstxt_obey=False,
)
```

### Debug mode

```python
settings = bitscrape.Settings(
    concurrent_requests=1,
    log_level="DEBUG",
)
```

### Distributed crawl

```python
settings = bitscrape.Settings(
    scheduler_use_redis=True,
    redis_url="redis://redis-host:6379/0",
    concurrent_requests=32,
)
```
