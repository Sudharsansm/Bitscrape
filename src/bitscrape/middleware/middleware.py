"""
Bitscrape Middleware
====================
Request/response middleware hooks.
The middleware chain is traversed in order for requests and in reverse for responses.

Built-in middleware:
  - RetryMiddleware     – retries on HTTP error codes / network failures
  - UserAgentMiddleware – rotates user-agent strings
  - RobotsMiddleware    – blocks disallowed URLs (obeys robots.txt)
  - CookieMiddleware    – maintains cookie jar per domain
"""

from __future__ import annotations

import asyncio
import logging
import random
import re
from abc import ABC, abstractmethod
from typing import Any
from urllib.parse import urlparse

from bitscrape.core.models import Request, Response
from bitscrape.middleware.robots import RobotsCache, RobotsInfo

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Base
# ---------------------------------------------------------------------------


class BaseMiddleware(ABC):
    @abstractmethod
    async def process_request(self, request: Request, spider: Any) -> Request | Response | None:
        """
        Return None to continue, a modified Request to replace it,
        or a Response to short-circuit the download.
        """
        return None

    async def process_response(
        self, request: Request, response: Response, spider: Any
    ) -> Response | Request | None:
        """
        Return the (modified) Response, a new Request to re-fetch,
        or None to drop.
        """
        return response

    async def process_exception(
        self, request: Request, exc: Exception, spider: Any
    ) -> Request | Response | None:
        return None


# ---------------------------------------------------------------------------
# User-Agent rotation
# ---------------------------------------------------------------------------

DEFAULT_USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/123.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) Gecko/20100101 Firefox/124.0",
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_4 like Mac OS X) AppleWebKit/605.1.15",
]


class UserAgentMiddleware(BaseMiddleware):
    def __init__(self, user_agents: list[str] | None = None, rotate: bool = False) -> None:
        self._agents = user_agents or DEFAULT_USER_AGENTS
        self._rotate = rotate
        self._idx = 0

    async def process_request(self, request: Request, spider: Any) -> None:
        ua = random.choice(self._agents) if self._rotate else spider.settings.user_agent
        headers = {**request.headers, "User-Agent": ua}
        return request.model_copy(update={"headers": headers})  # type: ignore[return-value]


# ---------------------------------------------------------------------------
# Proxy rotation
# ---------------------------------------------------------------------------


class ProxyMiddleware(BaseMiddleware):
    """
    Rotates outbound proxies per request, the same way ``UserAgentMiddleware``
    rotates User-Agent strings. Without this, a single-IP crawler eventually
    hits IP-reputation or rate-based blocking on any site that watches for it.

    ``proxies`` accepts full proxy URLs, e.g. ``"http://user:pass@host:port"``.
    Sets ``request.meta["proxy"]``, which the downloader can read and pass
    through as ``aiohttp``'s ``proxy=`` kwarg (or Playwright's
    ``proxy={"server": ...}`` for the browser path).

    ``rotate=False`` (default) always uses the first proxy in the list — set
    ``True`` to pick a new one per request. Pass a callable via
    ``on_exhausted`` to be notified when a request is retried and you may
    want to swap out a dead proxy from the pool.
    """

    def __init__(self, proxies: list[str] | None = None, rotate: bool = True) -> None:
        self._proxies = proxies or []
        self._rotate = rotate
        self._idx = 0

    @property
    def proxies(self) -> list[str]:
        return list(self._proxies)

    def add_proxy(self, proxy_url: str) -> None:
        self._proxies.append(proxy_url)

    def remove_proxy(self, proxy_url: str) -> None:
        """Drop a proxy from rotation, e.g. after repeated failures."""
        self._proxies = [p for p in self._proxies if p != proxy_url]

    def _next_proxy(self) -> str | None:
        if not self._proxies:
            return None
        if self._rotate:
            return random.choice(self._proxies)
        proxy = self._proxies[self._idx % len(self._proxies)]
        self._idx += 1
        return proxy

    async def process_request(self, request: Request, spider: Any) -> None:
        proxy = self._next_proxy()
        if proxy is None:
            return None
        meta = {**request.meta, "proxy": proxy}
        return request.model_copy(update={"meta": meta})  # type: ignore[return-value]


# ---------------------------------------------------------------------------
# Offsite filtering
# ---------------------------------------------------------------------------


class OffsiteDrop(Exception):
    """Raised by :class:`OffsiteMiddleware` for a request outside
    ``spider.allowed_domains``. The Engine catches this alongside
    :class:`~bitscrape.pipeline.pipelines.DropItem` -- both mean "silently
    decline this request, no traceback, don't touch requests_failed" -- and
    additionally recognises this specific type to bump
    ``CrawlStats.requests_offsite_dropped``.
    """


def _host_allowed(host: str, allowed_domains: list[str]) -> bool:
    host = host.lower().rstrip(".")
    return any(
        host == d or host.endswith("." + d)
        for d in (x.lower().lstrip(".") for x in allowed_domains)
    )


class OffsiteMiddleware(BaseMiddleware):
    """
    Drops any request whose host isn't in ``spider.allowed_domains`` (exact
    host or a subdomain of it) -- matching what that attribute's docstring has
    always promised. A no-op while ``allowed_domains`` is empty (the default:
    crawl anywhere), so it's always installed; there's nothing to configure.

    Runs first in the middleware chain: an offsite URL is rejected before
    robots.txt is ever fetched for it, sessions are touched, or anything else
    does work on a request that was never going anywhere.

    Counted as ``CrawlStats.requests_dropped`` /
    ``requests_offsite_dropped`` -- not ``requests_failed`` -- since this is
    policy, not an error. ``CrawlSpider``/``SitemapSpider`` rules can filter
    the same way with ``LinkExtractor(allow_domains=...)`` to avoid creating
    (and then dropping) the request at all; this middleware is what makes
    ``allowed_domains`` itself actually mean something, for hand-written
    spiders too.
    """

    async def process_request(self, request: Request, spider: Any) -> None:
        allowed = getattr(spider, "allowed_domains", None)
        if not allowed:
            return
        host = urlparse(request.url).hostname or ""
        if _host_allowed(host, allowed):
            return
        logger.debug("Offsite request dropped: %s (allowed_domains=%s)", request.url, allowed)
        raise OffsiteDrop(f"{request.url} is not within allowed_domains={list(allowed)}")


# ---------------------------------------------------------------------------
# Robots.txt
# ---------------------------------------------------------------------------


class RobotsMiddleware(BaseMiddleware):
    """
    Blocks disallowed requests and surfaces ``Crawl-delay`` / ``Request-rate`` /
    ``Sitemap`` directives, per :data:`bitscrape.core.settings.Settings.robotstxt_obey`.

    The parsing, caching, concurrency-safety and fail-safe policy live in
    :mod:`bitscrape.middleware.robots` (:class:`~bitscrape.middleware.robots.RobotsCache`);
    this class is the ``process_request`` adapter plus the historical
    ``mw._cache[domain]`` shape a couple of older call sites still poke at
    directly (a plain ``RobotsInfo``-like object with ``.parser`` /
    ``.crawl_delay`` / ``.sitemaps``).

    Effective per-domain delay is put in ``request.meta["crawl_delay"]`` as
    before -- now it's ``max(Crawl-delay, Request-rate's implied spacing)``,
    the latter only while its optional time-of-day window applies. The full
    ``Request-rate`` directive (if any) is additionally put in
    ``request.meta["request_rate"]`` as a plain dict, for spiders that want to
    reason about the window themselves. See ``docs/robots.md``.
    """

    def __init__(self, cache: RobotsCache | None = None) -> None:
        self._robots = cache or RobotsCache()

    @property
    def _cache(self) -> dict[str, RobotsInfo]:
        # Kept for introspection/tests written against the pre-upgrade shape;
        # RobotsCache._cache is the real, TTL'd, size-bounded store.
        return dict(self._robots._cache)

    def stats(self) -> dict[str, Any]:
        return self._robots.stats()

    async def process_request(self, request: Request, spider: Any) -> None:
        settings = spider.settings
        if not getattr(settings, "robotstxt_obey", False):
            return
        parsed = urlparse(request.url)
        domain, scheme = parsed.netloc, parsed.scheme
        ua = getattr(settings, "robots_user_agent", None) or settings.user_agent

        info = await self._robots.get(domain, scheme, ua)

        if info.sitemaps:
            request.meta.setdefault("sitemaps", info.sitemaps)
        delay = info.effective_delay()
        if delay is not None:
            request.meta.setdefault("crawl_delay", delay)
        rr = info.request_rate
        if rr is not None and getattr(settings, "robots_respect_request_rate", True):
            request.meta.setdefault(
                "request_rate",
                {
                    "requests": rr.requests,
                    "seconds": rr.seconds,
                    "delay": rr.delay,
                    "start_time": rr.start_time.isoformat() if rr.start_time else None,
                    "end_time": rr.end_time.isoformat() if rr.end_time else None,
                    "in_window": rr.in_window(),
                },
            )

        if not info.can_fetch(request.url, ua):
            logger.info("Blocked by robots.txt: %s", request.url)
            from bitscrape.pipeline.pipelines import DropItem

            reason = (
                f"robots.txt unavailable for {domain}; blocking until it can be verified"
                if not info.ok
                else f"robots.txt disallows: {request.url}"
            )
            raise DropItem(reason)


# ---------------------------------------------------------------------------
# Page-level indexing directives (meta robots / X-Robots-Tag)
# ---------------------------------------------------------------------------

_META_ROBOTS_RE = re.compile(
    r"""<meta\s+[^>]*name=["']robots["'][^>]*content=["']([^"']*)["']""",
    re.IGNORECASE,
)
# Some sites scope directives to a specific crawler, e.g.
# <meta name="googlebot" content="noindex">. We also honour a directive
# scoped to "robots" generically, or to a name matching our own UA token.
_META_ROBOTS_SCOPED_RE_TEMPLATE = (
    r"""<meta\s+[^>]*name=["']{name}["'][^>]*content=["']([^"']*)["']"""
)


def _parse_robots_directives(value: str) -> set[str]:
    return {token.strip().lower() for token in value.split(",") if token.strip()}


class MetaRobotsMiddleware(BaseMiddleware):
    """
    Honours page-level indexing directives that robots.txt can't express:

      - ``X-Robots-Tag`` HTTP response header
      - ``<meta name="robots" content="...">`` HTML tag (and a UA-scoped
        variant like ``<meta name="googlebot" content="noindex">``)

    This is what Googlebot / ClaudeBot / PerplexityBot-class crawlers honour
    in addition to robots.txt: a page can be fetchable but marked
    ``noindex`` (don't index/use its content) or ``nofollow`` (don't follow
    links found on it). ``none`` is shorthand for both.

    Sets ``request.meta["noindex"]`` / ``request.meta["nofollow"]`` (bool),
    which the Engine reads to skip counting/exporting items and to skip
    enqueueing follow-up links, when ``settings.respect_meta_robots`` is
    enabled (default True). This middleware never drops the response itself
    — fetching is still allowed; only indexing/following is affected, which
    matches the real-world semantics of these directives.
    """

    async def process_request(self, request: Request, spider: Any) -> None:
        return None  # this middleware only acts on responses

    async def process_response(self, request: Request, response: Response, spider: Any) -> Any:
        if not getattr(spider.settings, "respect_meta_robots", True):
            return response

        directives: set[str] = set()

        header_value = response.headers.get("X-Robots-Tag")
        if header_value:
            directives |= _parse_robots_directives(header_value)

        content_type = response.headers.get("Content-Type", "")
        if "html" in content_type.lower() or not content_type:
            try:
                text = response.text
            except Exception:  # noqa: BLE001 -- any decode failure just means no directives found
                text = ""
            if text:
                for match in _META_ROBOTS_RE.finditer(text):
                    directives |= _parse_robots_directives(match.group(1))

                ua_token = spider.settings.user_agent.split("/")[0].strip().lower()
                if ua_token:
                    scoped_re = re.compile(
                        _META_ROBOTS_SCOPED_RE_TEMPLATE.format(name=re.escape(ua_token)),
                        re.IGNORECASE,
                    )
                    for match in scoped_re.finditer(text):
                        directives |= _parse_robots_directives(match.group(1))

        noindex = "noindex" in directives or "none" in directives
        nofollow = "nofollow" in directives or "none" in directives

        if noindex or nofollow:
            logger.info(
                "Meta robots directives on %s: noindex=%s nofollow=%s",
                request.url,
                noindex,
                nofollow,
            )

        request.meta["noindex"] = noindex
        request.meta["nofollow"] = nofollow
        return response


# ---------------------------------------------------------------------------
# Cookie jar
# ---------------------------------------------------------------------------


class CookieMiddleware(BaseMiddleware):
    """
    Maintains a per-domain cookie jar (extracted from Set-Cookie headers).
    """

    def __init__(self) -> None:
        self._cookies: dict[str, dict[str, str]] = {}

    async def process_request(self, request: Request, spider: Any) -> None:
        domain = urlparse(request.url).netloc
        jar = self._cookies.get(domain, {})
        if jar:
            cookie_header = "; ".join(f"{k}={v}" for k, v in jar.items())
            headers = {**request.headers, "Cookie": cookie_header}
            return request.model_copy(update={"headers": headers})  # type: ignore[return-value]
        return None

    async def process_response(self, request: Request, response: Response, spider: Any) -> Response:
        domain = urlparse(request.url).netloc
        set_cookie = response.headers.get("Set-Cookie", "")
        if set_cookie:
            if domain not in self._cookies:
                self._cookies[domain] = {}
            for part in set_cookie.split(";"):
                kv = part.strip()
                if "=" in kv:
                    k, _, v = kv.partition("=")
                    self._cookies[domain][k.strip()] = v.strip()
        return response


# ---------------------------------------------------------------------------
# Distributed throttling (multi-worker, Redis-backed)
# ---------------------------------------------------------------------------


class DistributedThrottleMiddleware(BaseMiddleware):
    """
    A Redis-backed per-domain lease so multiple worker PROCESSES sharing the
    same Redis don't independently hammer the same domain at once. Solves the
    gap flagged in earlier fixes: "no distributed coordination beyond the
    Redis queue itself -- multiple workers crawling the same domain
    simultaneously risk duplicate load / rate-limit violations."

    This is a lease, not a mutex: it enforces MINIMUM SPACING between
    requests to a domain across the whole cluster (``SET key val NX PX
    <delay_ms>``), rather than one-at-a-time exclusive access, since crawling
    is throughput-oriented, not a critical section.

    Uses ``request.meta["crawl_delay"]`` (set by RobotsMiddleware from the
    site's own robots.txt) if present, falling back to
    ``settings.download_delay``. A delay of 0 means "no throttling" and is a
    fast no-op (skips Redis entirely).
    """

    def __init__(self, redis_client: Any, key_prefix: str = "bitscrape:throttle:") -> None:
        self._redis = redis_client
        self._prefix = key_prefix

    async def process_request(self, request: Request, spider: Any) -> None:
        if not getattr(spider.settings, "distributed_throttle_enabled", False):
            return

        delay = request.meta.get("crawl_delay") or getattr(spider.settings, "download_delay", 0.0)
        if not delay or delay <= 0:
            return

        domain = urlparse(request.url).netloc
        key = f"{self._prefix}{domain}"

        # Bounded retry loop: try to acquire the lease; if another worker
        # holds it, sleep out its remaining TTL and try again. Capped so a
        # pathological clock/Redis issue can't stall a worker forever.
        for _ in range(50):
            acquired = await self._redis.set(key, "1", nx=True, px=int(delay * 1000))
            if acquired:
                return
            ttl_ms = await self._redis.pttl(key)
            wait = (ttl_ms / 1000) if ttl_ms and ttl_ms > 0 else delay
            await asyncio.sleep(min(wait, delay))
        logger.warning(
            "DistributedThrottleMiddleware gave up waiting for lease on %s after 50 attempts",
            domain,
        )
        return


# ---------------------------------------------------------------------------
# Session pooling (rotating cookie-jar sessions per domain)
# ---------------------------------------------------------------------------


class SessionPoolMiddleware(BaseMiddleware):
    """
    Maintains a POOL of ``session_pool_size`` independent cookie-jar sessions
    per domain (instead of ``CookieMiddleware``'s single shared jar), and
    rotates between them -- useful for maintaining several parallel
    logged-in sessions, or avoiding a single session's request pattern
    standing out.

    Session selection: sticky if the request already carries
    ``request.meta["session_id"]`` (e.g. you deliberately want a specific
    login session for a sequence of requests); otherwise round-robin per
    domain.

    Auto-rotation: if ``settings.session_rotate_every > 0``, a session's jar
    is cleared (starting fresh, e.g. logged out) after that many requests
    through it -- set to ``0`` (default) to disable and keep sessions
    indefinitely.

    Don't combine with ``CookieMiddleware`` -- this is a superset (pool of
    N jars instead of 1); use one or the other.
    """

    def __init__(self, pool_size: int = 1, rotate_every: int = 0) -> None:
        self._pool_size = max(1, pool_size)
        self._rotate_every = rotate_every
        # domain -> list of per-session cookie jars
        self._pools: dict[str, list[dict[str, str]]] = {}
        # domain -> next round-robin index
        self._next_index: dict[str, int] = {}
        # (domain, session_id) -> request count since last rotation
        self._use_count: dict[tuple[str, int], int] = {}

    def _pool_for(self, domain: str) -> list[dict[str, str]]:
        if domain not in self._pools:
            self._pools[domain] = [{} for _ in range(self._pool_size)]
            self._next_index[domain] = 0
        return self._pools[domain]

    def _pick_session_id(self, domain: str, requested: int | None) -> int:
        pool = self._pool_for(domain)
        if requested is not None:
            return requested % len(pool)
        idx = self._next_index[domain]
        self._next_index[domain] = (idx + 1) % len(pool)
        return idx

    async def process_request(self, request: Request, spider: Any) -> Any:
        domain = urlparse(request.url).netloc
        session_id = self._pick_session_id(domain, request.meta.get("session_id"))
        jar = self._pool_for(domain)[session_id]

        headers = dict(request.headers)
        if jar:
            headers["Cookie"] = "; ".join(f"{k}={v}" for k, v in jar.items())
        meta = {**request.meta, "session_id": session_id}
        return request.model_copy(update={"headers": headers, "meta": meta})

    async def process_response(self, request: Request, response: Response, spider: Any) -> Response:
        domain = urlparse(request.url).netloc
        session_id = request.meta.get("session_id", 0)
        pool = self._pool_for(domain)
        session_id = session_id % len(pool)
        jar = pool[session_id]

        set_cookie = response.headers.get("Set-Cookie", "")
        if set_cookie:
            for part in set_cookie.split(";"):
                kv = part.strip()
                if "=" in kv:
                    k, _, v = kv.partition("=")
                    jar[k.strip()] = v.strip()

        if self._rotate_every > 0:
            use_key = (domain, session_id)
            count = self._use_count.get(use_key, 0) + 1
            if count >= self._rotate_every:
                pool[session_id] = {}  # rotate: fresh session
                logger.info(
                    "Rotated session %d for %s after %d requests", session_id, domain, count
                )
                count = 0
            self._use_count[use_key] = count

        return response

    def session_count(self, domain: str) -> int:
        return len(self._pools.get(domain, []))


# ---------------------------------------------------------------------------
# Middleware manager
# ---------------------------------------------------------------------------


class MiddlewareManager:
    """
    Applies middleware in order for requests, reverse order for responses.
    """

    def __init__(self, middlewares: list[BaseMiddleware]) -> None:
        self._middlewares = middlewares

    async def process_request(self, request: Request, spider: Any) -> Request | Response | None:
        for mw in self._middlewares:
            result = await mw.process_request(request, spider)
            if result is None:
                continue
            if isinstance(result, Request):
                request = result
            else:
                return result  # short-circuit with a Response
        return request

    async def process_response(
        self, request: Request, response: Response, spider: Any
    ) -> Response | Request | None:
        for mw in reversed(self._middlewares):
            result = await mw.process_response(request, response, spider)
            if result is None:
                return None
            if isinstance(result, Request):
                return result  # re-enqueue
            response = result
        return response

    async def process_exception(
        self, request: Request, exc: Exception, spider: Any
    ) -> Request | Response | None:
        for mw in reversed(self._middlewares):
            result = await mw.process_exception(request, exc, spider)
            if result is not None:
                return result
        return None
