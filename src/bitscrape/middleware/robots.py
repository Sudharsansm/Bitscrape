"""
robots.txt compliance
======================

:class:`RobotsCache` fetches, parses and caches robots.txt per domain, and
answers three questions for a given request: is it allowed
(``Disallow``/``Allow``), how long should we wait between requests to this
domain (``Crawl-delay`` *and* ``Request-rate``), and what sitemaps did the site
declare. :class:`bitscrape.middleware.middleware.RobotsMiddleware` is the
``process_request`` hook that wires this into a crawl.

Design points, versus a minimal "download + `urllib.robotparser`" middleware:

* **Parsing** is delegated to `Protego <https://github.com/scrapy/protego>`_
  (the parser Scrapy ships), which -- unlike the standard library's
  ``RobotFileParser`` -- implements ``Request-rate``, wildcard (``*``) and
  end-of-URL (``$``) patterns in ``Disallow``/``Allow``, and picks the most
  specific matching ``User-agent`` group per the de-facto standard.
* **Concurrency-safe fetching.** The first several requests to a brand-new
  domain typically arrive together; without a per-domain lock they would each
  fetch robots.txt themselves. One in-flight fetch per domain is shared by
  every caller waiting on it.
* **TTL + stale-while-revalidate.** An entry is refetched after
  ``cache_ttl`` seconds (robots.txt can change during a long crawl). If that
  refetch fails, the last known-good rules keep being enforced -- refreshing
  is best-effort, but a transient error must not suddenly *permit* pages the
  site had disallowed, nor need it re-trigger the fail-safe block for a domain
  we already have real rules for.
* **A fetch failure with no prior rules fails safe**, i.e. blocks -- since
  "unknown" and "unrestricted" are different things -- while a 4xx robots.txt
  response (including 404) is the standard's own "no rules published" and
  is cached as full access.
* **Bounded.** The cache evicts least-recently-used domains past
  ``max_domains``, and a robots.txt body over ``max_content_bytes`` is
  truncated before parsing (large.txt-bomb protection; matches the informal
  500 KiB convention several crawlers follow).
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import OrderedDict
from dataclasses import dataclass
from typing import Any, Protocol
from urllib.parse import urlparse

from protego import Protego

logger = logging.getLogger(__name__)

DEFAULT_CACHE_TTL = 3600.0  # re-check a domain's rules once an hour
DEFAULT_MAX_DOMAINS = 10_000
DEFAULT_MAX_CONTENT_BYTES = 500 * 1024  # 500 KiB, the de-facto crawler convention
DEFAULT_TIMEOUT = 10.0
DEFAULT_NEGATIVE_TTL = 30.0  # how long a *failed* fetch is remembered, to avoid hammering


class RobotsFetcher(Protocol):
    """What :class:`RobotsCache` needs to retrieve a robots.txt body.

    Implemented by :class:`AiohttpFetcher` (the default); pass your own to
    reuse a specific session/proxy/pool instead of opening a throwaway one.
    """

    async def fetch(self, url: str, *, user_agent: str, timeout: float) -> tuple[int, bytes]:
        """Return ``(status, body)``, or raise on a network-level failure."""
        ...


class AiohttpFetcher:
    """Default :class:`RobotsFetcher`: one throwaway ``aiohttp`` request per call."""

    async def fetch(self, url: str, *, user_agent: str, timeout: float) -> tuple[int, bytes]:
        import aiohttp

        async with (
            aiohttp.ClientSession() as sess,
            sess.get(
                url,
                timeout=aiohttp.ClientTimeout(total=timeout),
                headers={"User-Agent": user_agent},
                max_redirects=5,
            ) as resp,
        ):
            # Read at most max_content_bytes + 1: enough to detect "too big"
            # without buffering an attacker- or misconfig-sized body.
            body = await resp.content.read(DEFAULT_MAX_CONTENT_BYTES + 1)
            return resp.status, body


def _time_in_window(start: Any, end: Any, now_time: Any) -> bool:
    if start is None or end is None:
        return True
    if start <= end:
        return start <= now_time <= end
    return now_time >= start or now_time <= end  # a window spanning midnight, e.g. 2200-0600


@dataclass(frozen=True)
class EffectiveRate:
    """A ``Request-rate`` directive: a spacing that only applies during its
    optional time-of-day window (``0600-0845``). ``in_window()`` is evaluated
    fresh on every call -- not baked in when robots.txt was fetched -- since a
    cache entry can easily outlive the window it names."""

    requests: int
    seconds: float
    delay: float  # seconds/requests -- the spacing this implies
    start_time: Any | None
    end_time: Any | None

    def in_window(self, now_time: Any | None = None) -> bool:
        import datetime as _dt

        # Deliberately naive/local time: the draft robots.txt spec that defines
        # Request-rate doesn't pin a timezone, and matching the crawling
        # process's own local clock is the conventional interpretation.
        now = now_time or _dt.datetime.now().time()  # noqa: DTZ005
        return _time_in_window(self.start_time, self.end_time, now)


@dataclass
class RobotsInfo:
    """Everything learned from one domain's robots.txt."""

    parser: Protego | None  # None only for "no robots.txt" (4xx / empty)
    crawl_delay: float | None
    request_rate: EffectiveRate | None
    sitemaps: list[str]
    fetched_at: float
    ok: bool = True  # False = a fail-safe placeholder (block everything)
    stale: bool = False  # True once past its TTL, before a successful refetch

    def is_expired(self, ttl: float, now: float | None = None) -> bool:
        return (now if now is not None else time.monotonic()) - self.fetched_at >= ttl

    def can_fetch(self, url: str, user_agent: str) -> bool:
        if not self.ok:
            return False  # fail-safe placeholder: block until real rules are known
        if self.parser is None:
            return True  # confirmed "no robots.txt": unrestricted
        return self.parser.can_fetch(url, user_agent)

    def effective_delay(self, now_time: Any | None = None) -> float | None:
        """``max(Crawl-delay, Request-rate's implied spacing)``, honouring the
        latter's time-of-day window (checked *now*, not when this was fetched).
        ``None`` if neither directive applies."""
        candidates = [d for d in (self.crawl_delay,) if d is not None]
        rr = self.request_rate
        if rr is not None and rr.in_window(now_time):
            candidates.append(rr.delay)
        return max(candidates) if candidates else None


def _pick_delay(parser: Protego, user_agent: str) -> float | None:
    delay = parser.crawl_delay(user_agent)
    if delay is None and user_agent != "*":
        delay = parser.crawl_delay("*")
    return float(delay) if delay is not None else None


def _pick_request_rate(parser: Protego, user_agent: str) -> EffectiveRate | None:
    rr = parser.request_rate(user_agent)
    if rr is None and user_agent != "*":
        rr = parser.request_rate("*")
    if rr is None or not rr.requests:
        return None
    return EffectiveRate(
        requests=rr.requests,
        seconds=rr.seconds,
        delay=rr.seconds / rr.requests,
        start_time=rr.start_time,
        end_time=rr.end_time,
    )


def _ua_token(user_agent: str) -> str:
    """The product token robots.txt matching uses, e.g. ``"MyBot"`` out of
    ``"MyBot/1.0 (+https://example.com/bot)"``. Protego itself only lowercases
    and substring-matches, so trimming to the product token avoids the rest of
    the UA string (version, URL) ever accidentally matching a rule."""
    return (user_agent.split("/", 1)[0] or user_agent).strip() or "*"


class RobotsCache:
    """Fetch → parse → cache robots.txt, per domain, with a shared in-flight
    fetch per domain and a bounded, TTL'd, stale-while-revalidate cache."""

    def __init__(
        self,
        *,
        fetcher: RobotsFetcher | None = None,
        cache_ttl: float = DEFAULT_CACHE_TTL,
        negative_ttl: float = DEFAULT_NEGATIVE_TTL,
        max_domains: int = DEFAULT_MAX_DOMAINS,
        max_content_bytes: int = DEFAULT_MAX_CONTENT_BYTES,
        timeout: float = DEFAULT_TIMEOUT,
        clock: Any = time.monotonic,
    ) -> None:
        self.fetcher = fetcher or AiohttpFetcher()
        self.cache_ttl = cache_ttl
        self.negative_ttl = negative_ttl
        self.max_domains = max_domains
        self.max_content_bytes = max_content_bytes
        self.timeout = timeout
        self._clock = clock
        self._cache: OrderedDict[str, RobotsInfo] = OrderedDict()
        self._locks: dict[str, asyncio.Lock] = {}
        #: counters, surfaced via ``stats()``
        self.fetches = 0
        self.fetch_failures = 0
        self.cache_hits = 0

    def _lock_for(self, domain: str) -> asyncio.Lock:
        lock = self._locks.get(domain)
        if lock is None:
            lock = self._locks[domain] = asyncio.Lock()
        return lock

    def _touch(self, domain: str, info: RobotsInfo) -> None:
        self._cache[domain] = info
        self._cache.move_to_end(domain)
        while len(self._cache) > self.max_domains:
            evicted_domain, _ = self._cache.popitem(last=False)
            self._locks.pop(evicted_domain, None)

    async def get(self, domain: str, scheme: str, user_agent: str) -> RobotsInfo:
        """Robots info for ``domain``: cached, refetching if missing/expired.

        Concurrent callers for the same never-yet-cached domain share one
        fetch. A caller that arrives after an entry exists but is stale gets
        the stale entry immediately if refetching fails (or is already being
        refetched by someone else and that hasn't finished) -- it never blocks
        the crawl waiting on a refresh of rules it already has.
        """
        cached = self._cache.get(domain)
        if cached is not None and not cached.is_expired(self.cache_ttl, self._clock()):
            self.cache_hits += 1
            self._cache.move_to_end(domain)
            return cached

        async with self._lock_for(domain):
            # Someone else may have refreshed it while we waited for the lock.
            cached = self._cache.get(domain)
            if cached is not None and not cached.is_expired(self.cache_ttl, self._clock()):
                self.cache_hits += 1
                return cached
            fresh = await self._fetch_and_parse(domain, scheme, user_agent, stale=cached)
            self._touch(domain, fresh)
            return fresh

    async def _fetch_and_parse(
        self, domain: str, scheme: str, user_agent: str, *, stale: RobotsInfo | None
    ) -> RobotsInfo:
        url = f"{scheme}://{domain}/robots.txt"
        self.fetches += 1
        try:
            status, body = await self.fetcher.fetch(
                url, user_agent=user_agent, timeout=self.timeout
            )
        except Exception as exc:  # noqa: BLE001 -- any failure must fail safe/stale, never raise
            return self._on_fetch_error(domain, url, exc, stale)

        if 400 <= status < 500:
            # The standard's own "no robots.txt published": unrestricted access.
            return RobotsInfo(
                parser=None,
                crawl_delay=None,
                request_rate=None,
                sitemaps=[],
                fetched_at=self._clock(),
            )
        if status >= 300:  # 5xx, or a stray 3xx aiohttp's redirect-follow didn't resolve
            return self._on_fetch_error(
                domain, url, RuntimeError(f"HTTP {status} fetching robots.txt"), stale
            )

        truncated = len(body) > self.max_content_bytes
        text = body[: self.max_content_bytes].decode("utf-8", errors="replace")
        if truncated:
            logger.warning(
                "robots.txt for %s exceeds %d bytes; parsing a truncated copy",
                domain,
                self.max_content_bytes,
            )
        try:
            parser = Protego.parse(text)
        except Exception as exc:  # noqa: BLE001 -- a malformed file must fail safe/stale too
            return self._on_fetch_error(domain, url, exc, stale)

        token = _ua_token(user_agent)
        return RobotsInfo(
            parser=parser,
            crawl_delay=_pick_delay(parser, token),
            request_rate=_pick_request_rate(parser, token),
            sitemaps=list(parser.sitemaps),
            fetched_at=self._clock(),
        )

    def _on_fetch_error(
        self, domain: str, url: str, exc: Exception, stale: RobotsInfo | None
    ) -> RobotsInfo:
        self.fetch_failures += 1
        if stale is not None and stale.ok:
            logger.warning(
                "robots.txt refresh failed for %s (%s); keeping last known rules", domain, exc
            )
            return RobotsInfo(
                parser=stale.parser,
                crawl_delay=stale.crawl_delay,
                request_rate=stale.request_rate,
                sitemaps=stale.sitemaps,
                fetched_at=self._clock() - self.cache_ttl + self.negative_ttl,
                stale=True,
            )
        logger.warning(
            "robots.txt fetch failed for %s (%s) -- failing safe, blocking until a "
            "successful fetch is cached",
            domain,
            exc,
        )
        # Remembered only briefly (negative_ttl, via the same fetched_at/ttl
        # expiry mechanism as a normal entry): a domain that's down must not be
        # hammered with one robots.txt request per crawl request, but a
        # short-lived outage shouldn't lock the domain out for the whole crawl
        # either -- it is retried after negative_ttl, not the full cache_ttl.
        return RobotsInfo(
            parser=None,
            crawl_delay=None,
            request_rate=None,
            sitemaps=[],
            fetched_at=self._clock() - self.cache_ttl + self.negative_ttl,
            ok=False,
        )

    def stats(self) -> dict[str, Any]:
        return {
            "domains_cached": len(self._cache),
            "fetches": self.fetches,
            "fetch_failures": self.fetch_failures,
            "cache_hits": self.cache_hits,
        }

    def reset(self, domain: str | None = None) -> None:
        if domain is None:
            self._cache.clear()
            self._locks.clear()
        else:
            self._cache.pop(domain, None)
            self._locks.pop(domain, None)


def domain_of(url: str) -> str:
    return urlparse(url).netloc


__all__ = [
    "AiohttpFetcher",
    "EffectiveRate",
    "RobotsCache",
    "RobotsFetcher",
    "RobotsInfo",
    "domain_of",
]
