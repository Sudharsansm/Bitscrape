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

import logging
import random
from abc import ABC, abstractmethod
from typing import Any
from urllib.parse import urlparse

from bitscrape.core.models import Request, Response

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Base
# ---------------------------------------------------------------------------

class BaseMiddleware(ABC):
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
        if self._rotate:
            ua = random.choice(self._agents)
        else:
            ua = spider.settings.user_agent
        headers = {**request.headers, "User-Agent": ua}
        return request.model_copy(update={"headers": headers})  # type: ignore[return-value]


# ---------------------------------------------------------------------------
# Robots.txt
# ---------------------------------------------------------------------------

class RobotsMiddleware(BaseMiddleware):
    """
    Downloads and caches robots.txt for each domain and blocks disallowed paths.
    """

    def __init__(self) -> None:
        self._parsers: dict[str, Any] = {}

    async def _get_parser(self, domain: str, scheme: str) -> Any:
        if domain in self._parsers:
            return self._parsers[domain]
        try:
            from urllib.robotparser import RobotFileParser
            import aiohttp
            url = f"{scheme}://{domain}/robots.txt"
            async with aiohttp.ClientSession() as sess:
                async with sess.get(url, timeout=aiohttp.ClientTimeout(total=5)) as resp:
                    text = await resp.text()
            parser = RobotFileParser()
            parser.set_url(url)
            parser.feed(text)
            self._parsers[domain] = parser
        except Exception:
            self._parsers[domain] = None
        return self._parsers[domain]

    async def process_request(self, request: Request, spider: Any) -> None:
        if not spider.settings.robotstxt_obey:
            return None
        parsed = urlparse(request.url)
        domain = parsed.netloc
        scheme = parsed.scheme
        ua = spider.settings.user_agent
        parser = await self._get_parser(domain, scheme)
        if parser and not parser.can_fetch(ua, request.url):
            logger.info("Blocked by robots.txt: %s", request.url)
            from bitscrape.pipeline.pipelines import DropItem
            raise DropItem(f"robots.txt disallows: {request.url}")
        return None


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
