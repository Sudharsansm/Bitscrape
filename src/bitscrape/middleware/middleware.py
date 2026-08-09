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
# Robots.txt
# ---------------------------------------------------------------------------


class RobotsInfo:
    """Parsed robots.txt data for one domain: the parser plus its directives."""

    __slots__ = ("crawl_delay", "parser", "sitemaps")

    def __init__(
        self,
        parser: Any,
        crawl_delay: float | None,
        sitemaps: list[str],
    ) -> None:
        self.parser = parser
        self.crawl_delay = crawl_delay
        self.sitemaps = sitemaps


class RobotsMiddleware(BaseMiddleware):
    """
    Downloads and caches robots.txt for each domain, blocks disallowed paths,
    and exposes ``Crawl-delay`` / ``Sitemap`` directives.

    ROOT-CAUSE FIX: the previous implementation called ``parser.feed(text)``,
    but ``urllib.robotparser.RobotFileParser`` has no ``feed()`` method (only
    ``.parse(lines)``). Every single robots.txt fetch therefore raised
    ``AttributeError``, which the surrounding ``except Exception`` silently
    swallowed by setting the cached parser to ``None`` — meaning
    ``robotstxt_obey`` was **always a no-op in practice**, regardless of its
    setting, because ``info.parser`` was always ``None`` and
    ``can_fetch()`` was never actually called. This was a correctness bug,
    not just a missing-feature gap.

    Once ``.parse()`` is used correctly, ``RobotFileParser`` already exposes
    ``Crawl-delay`` via ``.crawl_delay(useragent)`` and ``Sitemap`` entries
    via ``.site_maps()`` natively — no manual re-parsing of the raw text is
    needed for those either.

    Fail-safe behaviour: if robots.txt cannot be fetched (network error,
    timeout, non-200 status), we no longer treat that as "no restrictions".
    A 4xx (including 404) is treated as "robots.txt does not exist" — full
    access is allowed, matching the standard convention. Any other failure
    (timeout, connection error, 5xx) is fail-safe: requests to that domain
    are blocked until a successful fetch, since a server error means the
    real rules are unknown, not that there are none.
    """

    def __init__(self) -> None:
        self._cache: dict[str, RobotsInfo | None] = {}

    async def _get_info(self, domain: str, scheme: str, user_agent: str) -> RobotsInfo | None:
        if domain in self._cache:
            return self._cache[domain]
        try:
            from urllib.robotparser import RobotFileParser

            import aiohttp

            url = f"{scheme}://{domain}/robots.txt"
            async with (
                aiohttp.ClientSession() as sess,
                sess.get(url, timeout=aiohttp.ClientTimeout(total=5)) as resp,
            ):
                if 400 <= resp.status < 500:
                    # No robots.txt published -> unrestricted access, and we
                    # can safely cache "no rules" rather than fail-safe.
                    self._cache[domain] = RobotsInfo(parser=None, crawl_delay=None, sitemaps=[])
                    return self._cache[domain]
                resp.raise_for_status()
                text = await resp.text()

            parser = RobotFileParser()
            parser.set_url(url)
            parser.parse(text.splitlines())  # NOT .feed() -- see class docstring

            ua_token = user_agent.split("/")[0].strip() or "*"
            raw_delay = parser.crawl_delay(ua_token)
            if raw_delay is None and ua_token != "*":
                raw_delay = parser.crawl_delay("*")
            crawl_delay = float(raw_delay) if raw_delay is not None else None
            sitemaps = list(parser.site_maps() or [])

            info = RobotsInfo(parser=parser, crawl_delay=crawl_delay, sitemaps=sitemaps)
            self._cache[domain] = info
            return info
        except Exception as exc:  # noqa: BLE001 -- intentional: any failure must fail safe
            logger.warning(
                "robots.txt fetch failed for %s (%s) — failing safe, blocking until a "
                "successful fetch",
                domain,
                exc,
            )
            # Deliberately NOT cached: retry on the next request instead of
            # permanently locking the domain out over one transient error.
            return None

    async def process_request(self, request: Request, spider: Any) -> None:
        if not spider.settings.robotstxt_obey:
            return
        parsed = urlparse(request.url)
        domain = parsed.netloc
        scheme = parsed.scheme
        ua = spider.settings.user_agent

        info = await self._get_info(domain, scheme, ua)
        if info is None:
            # Fetch failed and nothing cached yet: fail safe rather than
            # silently allowing everything through.
            from bitscrape.pipeline.pipelines import DropItem

            raise DropItem(
                f"robots.txt unavailable for {domain}; blocking until it can be verified"
            )

        if info.sitemaps:
            request.meta.setdefault("sitemaps", info.sitemaps)
        if info.crawl_delay is not None:
            request.meta.setdefault("crawl_delay", info.crawl_delay)

        if info.parser and not info.parser.can_fetch(ua, request.url):
            logger.info("Blocked by robots.txt: %s", request.url)
            from bitscrape.pipeline.pipelines import DropItem

            raise DropItem(f"robots.txt disallows: {request.url}")
        return


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
