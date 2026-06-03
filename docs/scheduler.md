# Scheduler

The Scheduler manages the crawl queue and URL deduplication. It ensures each
URL is crawled only once (unless deduplication is disabled) and controls the
order requests are processed.

---

## How It Works

```
Spider yields Request
       │
       ▼
  Scheduler.enqueue()
       │
       ├── Depth check (max_depth setting)
       │     └── too deep → discard
       │
       ├── Fingerprint the URL (blake2b hash)
       │
       ├── DupeFilter.seen(fingerprint)
       │     └── already seen → discard
       │
       └── Queue.push(request)  ← stored by priority

Engine calls Scheduler.next_request()
       │
       └── Queue.pop()  → returns highest-priority request
```

---

## Backends

### MemoryQueue (default)

Single-process in-memory priority queue using `asyncio.PriorityQueue`.
Fast, zero dependencies. Data is lost if the process exits.

```python
settings = bitscrape.Settings(scheduler_use_redis=False)  # default
```

### RedisQueue (distributed)

Persistent Redis sorted-set queue. Multiple workers share one queue.
Survives crashes — resume by restarting workers.

```python
settings = bitscrape.Settings(
    scheduler_use_redis=True,
    redis_url="redis://localhost:6379/0",
)
```

See [distributed.md](distributed.md) for full distributed setup.

---

## Request Priority

Requests are processed by priority. Lower number = higher urgency.

```python
from bitscrape.core.models import Request, RequestPriority

# High priority — processed first
yield Request(url="/important", priority=RequestPriority.HIGH)   # value 0

# Normal priority — default
yield Request(url="/normal",    priority=RequestPriority.NORMAL) # value 5

# Low priority — processed last
yield Request(url="/later",     priority=RequestPriority.LOW)    # value 10
```

---

## URL Deduplication

Every request is fingerprinted before enqueuing. The fingerprint is a
16-byte blake2b hash of `METHOD:URL[:body_hash]`.

```python
# Enabled by default
settings = bitscrape.Settings(dupefilter_enabled=True)

# Disable (scrape same URL multiple times)
settings = bitscrape.Settings(dupefilter_enabled=False)
```

Duplicate requests are silently discarded and counted in `scheduler.stats["duped"]`.

---

## Depth Limiting

Limit how deep the crawler follows links from the start URLs:

```python
settings = bitscrape.Settings(max_depth=3)
# depth 0 = start_urls
# depth 1 = pages linked from start_urls
# depth 2 = pages linked from depth-1 pages
# depth 3 = last level crawled
# depth 4+ = discarded
```

Default is `None` (unlimited depth).

---

## Scheduler Stats

```python
scheduler = await Scheduler.from_settings(settings)
print(scheduler.stats)
# {"enqueued": 150, "duped": 23}
print(scheduler.queue_size)
# 47
```
