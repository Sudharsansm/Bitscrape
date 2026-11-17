# AutoThrottle

Stop guessing crawl delays. With `autothrottle_enabled=True` the crawler tunes the
delay of **each domain** on its own:

1. **Learns from latency.** After every response the delay moves toward
   `latency / autothrottle_target_concurrency` - what the site can comfortably serve.
2. **Backs off on trouble.** A block or rate-limit **doubles** that domain's delay
   (`autothrottle_backoff_factor`, capped at `autothrottle_max_delay`).
3. **Waits what the site asks.** If the response carries `Retry-After`
   (seconds *or* an HTTP date), the **whole domain pauses** for that long instead
   (capped at `max_retry_after_seconds`).
4. **Speeds back up.** Every healthy response closes `autothrottle_recovery`
   (default half) of the gap back to the sustainable delay. A block never speeds
   anything up.

```python
settings = Settings(
    autothrottle_enabled=True,
    autothrottle_start_delay=1.0,      # first requests to a new domain
    autothrottle_max_delay=60.0,       # ceiling for the *learned* delay
    autothrottle_target_concurrency=2, # aim for ~2 requests in flight per domain
)
```

## What counts as "slow down"

| Signal | Reaction |
|--------|----------|
| status in `autothrottle_backoff_codes` (default `[429, 503]`) | double the delay; `Retry-After` -> domain pause |
| response flagged by the blocked-request detector (e.g. 403, "Access Denied" served as 200) | same |
| timeout / connection error (`autothrottle_backoff_on_errors`) | double the delay |
| anything else (200, 3xx, 404 ...) | latency sample only |

`autothrottle_block_backoff=False` turns the reactions off (latency tuning only).

## Floors: the delay never drops below

* `download_delay`, or `domain_delays["example.com"]` for that domain
  (matches `host:port`, bare host, or any parent domain);
* the site's own robots.txt `Crawl-delay` (put in `request.meta["crawl_delay"]`
  by `RobotsMiddleware`), which may even exceed `autothrottle_max_delay`.

`domain_concurrency={"example.com": 1}` likewise overrides
`concurrent_requests_per_domain` for that domain.

## How it is enforced

A per-domain `DomainPacer` spaces request **start times** and holds every request
during a cool-down. Waiters queue FIFO and the delay is read when a request's turn
comes - so a back-off decided while requests are already queued applies to *them*,
not just to later ones. (A check-then-sleep lets N concurrent workers all see "enough
time has passed" and fire at once.) Browser (Playwright) fetches go through the
same pacer and feed the same throttle.

## Observability

```python
stats = await Engine(...).run()
stats.throttle_backoffs       # block signals that slowed a domain
stats.throttle_cooldowns      # Retry-After pauses applied
stats.throttle_wait_seconds   # time spent waiting on pacing
stats.throttle_delays         # {domain: learned delay in seconds}
```

`Downloader.throttle_info()` returns the same live, per domain.

## Scope

AutoThrottle makes the crawler a *better-behaved* client. It does nothing to hide
that it is automated. To react to a block by other legitimate means (a different
session, a rendered page) see `Spider.retry_blocked_request()` in
[sessions](sessions.md). Per-process only; combine with
`distributed_throttle_enabled` for multi-worker crawls.
