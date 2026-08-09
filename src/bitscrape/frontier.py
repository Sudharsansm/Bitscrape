"""
Bitscrape URL Frontier
======================

A proper large-scale frontier, as distinct from the simple global priority
queue in ``bitscrape.scheduler`` (which is fine for small/medium crawls but
doesn't guarantee per-domain politeness under concurrency -- two workers
could both pop URLs for the same domain back-to-back).

This implements the classic Mercator-style two-level architecture:

  - N "front queues" by priority tier -- higher-priority URLs are more
    likely to be selected next.
  - One "back queue" per domain currently being crawled, each with its own
    next-allowed-time, so no two requests to the same domain are ever
    handed out closer together than that domain's crawl delay --
    regardless of how many workers are pulling from the frontier
    concurrently. This is real politeness enforcement, not the "hope the
    single process's rate limiter catches it" approach.

For actual cross-PROCESS (multi-worker) distributed politeness, combine
this with ``bitscrape.middleware.middleware.DistributedThrottleMiddleware``
(Redis-backed) -- this Frontier class enforces politeness within one
process/scheduler instance; the distributed throttle extends the same
guarantee across a fleet of worker processes.
"""

from __future__ import annotations

import heapq
import itertools
import logging
import time
from collections import defaultdict, deque
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlparse

logger = logging.getLogger(__name__)


@dataclass(order=True)
class _FrontierEntry:
    priority: int
    counter: int
    url: str = field(compare=False)
    domain: str = field(compare=False)
    meta: dict[str, Any] = field(compare=False, default_factory=dict)


class Frontier:
    """
    A politeness-aware, priority-ordered URL frontier.

    ``num_priority_tiers``: number of front queues (lower tier number =
    higher priority, like the existing ``RequestPriority`` enum: 0 is
    highest). URLs are assigned a tier via ``priority`` on ``add()``.

    ``default_delay``: minimum seconds between two requests handed out for
    the same domain, unless overridden per-domain via ``set_domain_delay()``
    (e.g. from a site's own robots.txt Crawl-delay).
    """

    def __init__(self, num_priority_tiers: int = 3, default_delay: float = 0.0) -> None:
        self._num_tiers = max(1, num_priority_tiers)
        self._default_delay = default_delay
        self._counter = itertools.count()

        # domain -> deque of pending _FrontierEntry, oldest first (FIFO
        # within a domain's own back queue).
        self._domain_queues: dict[str, deque[_FrontierEntry]] = defaultdict(deque)
        # domain -> earliest monotonic time it may next be served.
        self._domain_next_allowed: dict[str, float] = {}
        # domain -> per-domain override delay (falls back to default_delay).
        self._domain_delay: dict[str, float] = {}

        # A heap of (next_allowed_time, tier, domain) for domains that
        # currently have pending entries -- lets get_next() efficiently
        # find the earliest-available domain without scanning every domain
        # on every call.
        self._ready_heap: list[tuple[float, int, str]] = []
        self._domains_in_heap: set[str] = set()

        self._size = 0

    # --- Configuration ---------------------------------------------------

    def set_domain_delay(self, domain: str, delay: float) -> None:
        """Overrides the minimum spacing for one domain, e.g. from that
        site's robots.txt Crawl-delay directive."""
        self._domain_delay[domain] = delay

    def _delay_for(self, domain: str) -> float:
        return self._domain_delay.get(domain, self._default_delay)

    # --- Mutation ----------------------------------------------------------

    def add(self, url: str, priority: int = 1, meta: dict[str, Any] | None = None) -> None:
        """Adds a URL at the given priority tier (clamped into range)."""
        tier = max(0, min(priority, self._num_tiers - 1))
        domain = urlparse(url).netloc
        entry = _FrontierEntry(
            priority=tier, counter=next(self._counter), url=url, domain=domain, meta=meta or {}
        )
        self._domain_queues[domain].append(entry)
        self._size += 1
        self._touch_domain(domain)

    def _touch_domain(self, domain: str) -> None:
        """Ensures a domain with pending entries has an entry in the ready
        heap reflecting when it can next be served."""
        if domain in self._domains_in_heap:
            return
        if not self._domain_queues.get(domain):
            return
        next_allowed = self._domain_next_allowed.get(domain, 0.0)
        top_tier = self._domain_queues[domain][0].priority
        heapq.heappush(self._ready_heap, (next_allowed, top_tier, domain))
        self._domains_in_heap.add(domain)

    def get_next(self) -> tuple[str, dict[str, Any]] | None:
        """
        Returns (url, meta) for the next URL that's both available (its
        domain's politeness delay has elapsed) and highest-priority among
        currently-available domains, or ``None`` if the frontier is empty
        OR everything pending is still waiting out its domain's delay (in
        which case, check again shortly -- this call never blocks).
        """
        now = time.monotonic()

        # Pop candidates off the heap; re-push any whose domain isn't ready
        # yet isn't correct since heap order is by next_allowed_time already
        # -- the earliest-ready domain is always at the top. If even that
        # one isn't ready, nothing is.
        while self._ready_heap:
            next_allowed, _tier, domain = self._ready_heap[0]
            queue = self._domain_queues.get(domain)
            if not queue:
                heapq.heappop(self._ready_heap)
                self._domains_in_heap.discard(domain)
                continue
            if next_allowed > now:
                return None  # earliest-ready domain still isn't ready
            heapq.heappop(self._ready_heap)
            self._domains_in_heap.discard(domain)

            entry = queue.popleft()
            self._size -= 1
            self._domain_next_allowed[domain] = now + self._delay_for(domain)
            self._touch_domain(domain)  # re-insert if more entries remain
            return entry.url, entry.meta

        return None

    # --- Introspection -------------------------------------------------------

    @property
    def size(self) -> int:
        return self._size

    def domain_count(self) -> int:
        return sum(1 for q in self._domain_queues.values() if q)

    def pending_for_domain(self, domain: str) -> int:
        return len(self._domain_queues.get(domain, ()))

    def seconds_until_ready(self, domain: str) -> float:
        """How much longer until this domain's next request may be served
        (0 if it's ready now or unknown)."""
        next_allowed = self._domain_next_allowed.get(domain, 0.0)
        return max(0.0, next_allowed - time.monotonic())
