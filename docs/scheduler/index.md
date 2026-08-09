# Scheduler

Everything about how requests are queued, deduplicated, and (optionally)
coordinated across worker processes.

## The basic Scheduler

`bitscrape.scheduler.scheduler.Scheduler.from_settings(settings)` builds a
`Scheduler` wrapping a queue and a dedup filter, chosen by
`Settings.scheduler_use_redis`:

| `scheduler_use_redis` | Queue | Dedup filter |
|---|---|---|
| `False` (default) | `MemoryQueue` | `MemoryDupeFilter` |
| `True` | `RedisQueue` | `RedisDupeFilter` |

```python
scheduler = await Scheduler.from_settings(settings)
await scheduler.enqueue(request)      # False if a duplicate and dupefilter_enabled
request = await scheduler.next_request()
await scheduler.close()
```

### `MemoryQueue`

A priority queue (`asyncio.PriorityQueue` under the hood) with a
monotonic tiebreaker counter — this fixes a real historical bug where two
same-priority requests pushed together would raise `TypeError` (comparing
`Request` objects directly, which have no `__lt__`). FIFO order is
preserved within the same priority tier. Optional `maxsize` (default `0` =
unbounded) provides backpressure for very large crawls.

### `RedisQueue`

The same priority semantics, backed by Redis, so multiple worker processes
share one queue.

### Dedup filters

`fingerprint(request)` computes a stable hash from a request's URL, method,
and body — two requests with the same fingerprint are considered
duplicates. `MemoryDupeFilter` tracks seen fingerprints in a Python `set`
(per-process only). `RedisDupeFilter` uses a Redis `SADD` (atomic), so two
independent worker processes agree on what's already been claimed —
verified by a test with two separate filter instances against the same
Redis key, where the second correctly sees the first's claim.

`RedisDupeFilter` state also survives a process restart (it's just data in
Redis) — useful for resuming an interrupted large crawl without
re-crawling everything already done.

## Distributed crawling, end to end

Three independent mechanisms combine for safe multi-process crawling, each
opt-in via `Settings`:

```python
settings = Settings(
    scheduler_use_redis=True,           # 1. shared queue
    dupefilter_enabled=True,            # 2. shared dedup (default True)
    distributed_throttle_enabled=True,  # 3. shared per-domain politeness
    redis_url="redis://localhost:6379/0",
)
```

1. **Shared queue** — all workers pull `Request`s from the same Redis
   priority queue.
2. **Shared dedup** — `RedisDupeFilter`'s atomic `SADD` guarantees no two
   workers claim the same URL.
3. **Shared politeness** — `DistributedThrottleMiddleware` (see
   [crawling/](../crawling/index.md)) uses a Redis-backed lease so requests
   to any one domain are spaced out across the whole cluster.

See [tutorials/](../tutorials/index.md) Tutorial 4 for a runnable example,
and [architecture/index.md#distributed-crawling](../architecture/index.md#distributed-crawling)
for the design rationale.

## The Frontier (large-scale, single-process)

`bitscrape.frontier.Frontier` is a separate, Mercator-style two-level
frontier for when you need *structurally guaranteed* per-domain politeness
within one process/scheduler instance — as distinct from the simple global
priority queue above, which doesn't guarantee politeness under concurrency
(two workers popping from the same `MemoryQueue`/`RedisQueue` could
in principle both grab URLs for the same domain back-to-back).

```python
from bitscrape.frontier import Frontier

frontier = Frontier(num_priority_tiers=3, default_delay=1.0)
frontier.set_domain_delay("slow-site.com", 5.0)   # per-domain override

frontier.add("https://example.com/a", priority=0)   # high priority
frontier.add("https://example.com/b", priority=2)   # low priority

result = frontier.get_next()   # (url, meta) or None -- never blocks
```

Internally: N "front queues" by priority tier, and one "back queue" per
domain, each with its own next-allowed-time. `get_next()` always returns
the highest-priority URL among domains that are currently past their
politeness delay — verified by tests including real-time politeness
enforcement (two URLs for the same domain genuinely can't both come back
before the delay elapses) and 20-domain round-robin fairness (no domain
starves another).

`Frontier` is not currently wired into `build_engine()`/`Engine`
automatically — it's available as a standalone building block for crawls
that need this stronger single-process guarantee. Combine it with
`DistributedThrottleMiddleware` if you also need the guarantee across
worker processes, not just within one.

## Incremental recrawling

`bitscrape.recrawl.RecrawlScheduler` is a separate concern from the
frontier/queue above — it decides **when to revisit** an already-crawled
page (as opposed to what order to crawl fresh URLs in). See
[ai/index.md#incremental-recrawling](../ai/index.md#incremental-recrawling).

## See also

- [architecture/](../architecture/index.md) — how the scheduler fits into the Engine's crawl loop.
- [crawling/](../crawling/index.md) — retries, robots.txt, politeness settings.
- [api/index.md#scheduler-bitscrapescheduler](../api/index.md#scheduler-bitscrapescheduler) and [#frontier-bitscrapefrontier](../api/index.md#frontier-bitscrapefrontier) — full signatures.
