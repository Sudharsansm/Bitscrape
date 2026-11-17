"""
Direct HTTP requests: ``Fetcher``, ``AsyncFetcher``, ``FetcherSession``.

A lightweight HTTP client for when you just want to hit a URL or API --
no spider, engine, scheduler or pipeline::

    from bitscrape import Fetcher

    page = Fetcher.get("https://example.com")
    print(page.status, page.css("h1::text").get())

    api = Fetcher.post("https://example.com/api", json={"q": "widgets"})
    print(api.json())

Four classes, one behaviour:

===================== ======== ===============================================
class                 style    lifetime
===================== ======== ===============================================
``Fetcher``           sync     one client per call (``requests.get`` style)
``AsyncFetcher``      async    one client per call
``FetcherSession``    sync     one client for the ``with`` block: cookies and
                               connections are reused across calls
``AsyncFetcherSession`` async  same, for ``async with``
===================== ======== ===============================================

Every call returns a :class:`~bitscrape.parser.selector.ParsedResponse` --
the same object a spider callback receives (``.css()``, ``.xpath()``,
``.json()``, ``.text``, ``.status``, ``.headers``, ``.http_version``).

**Protocols.** ``protocol="http1"`` (default) uses ``aiohttp`` and needs
nothing extra. ``protocol="http2"`` uses ``httpx`` (``pip install
bitscrape[http2]``) and *offers* HTTP/2 through the standard TLS ALPN
extension, exactly as ``curl --http2`` or any browser does. If the server
doesn't offer it -- or the URL is plain ``http://``, where HTTP/2 isn't
negotiated -- the connection quietly falls back to HTTP/1.1;
``response.http_version`` (``"1.1"`` or ``"2"``) always tells you what
actually happened. HTTP/3 (QUIC) is not supported.

**What this is not.** It's a plain, honest client: it sends an ordinary
``User-Agent`` and the ordinary ``Settings.default_headers``, and does not
imitate a browser's TLS handshake or header fingerprint (``impersonate=``,
``stealthy_headers=`` and ``http3=`` raise ``NotImplementedError``; see
ROADMAP.md, "Explicit non-goals"). It also does **not** consult robots.txt or
throttle itself -- it's a direct client, not a crawler. For polite,
rate-limited crawling use a ``Spider`` (``docs/robots.md``,
``docs/autothrottle.md``).

**Retries.** Connection errors, timeouts and 429/500/502/503/504 responses
are retried with exponential backoff (honouring ``Retry-After``) -- but only
for idempotent methods (GET, HEAD, OPTIONS, PUT, DELETE) by default, since
retrying a POST after a 503 can repeat its side effects. Widen it with
``retry_methods=``. When retries run out on a *status*, the last response is
returned (check ``.status``/``.ok``); when they run out on a *network
error*, :class:`FetchError` is raised.
"""

from __future__ import annotations

import asyncio
import base64
import json as _json

from bitscrape import jsonx as _jsonx
import ssl
import time
from collections.abc import Coroutine, Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, TypeVar
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from bitscrape.core.models import Request, Response
from bitscrape.core.settings import Settings
from bitscrape.downloader.throttle import header_value, parse_retry_after
from bitscrape.errors import FetchError
from bitscrape.parser.selector import ParsedResponse
from bitscrape.sessions import UNSUPPORTED_OPTIONS

if TYPE_CHECKING:  # pragma: no cover
    from bitscrape.downloader.proxy import ProxyRotator

T = TypeVar("T")

Protocol = Literal["http1", "http2"]

#: Statuses worth another attempt: rate-limited, or a transient server-side failure.
RETRY_STATUSES: frozenset[int] = frozenset({429, 500, 502, 503, 504})
#: Methods safe to repeat. POST/PATCH are excluded by default: a retry after a
#: 5xx (or a connection drop after the request was sent) can duplicate its effect.
IDEMPOTENT_METHODS: frozenset[str] = frozenset({"GET", "HEAD", "OPTIONS", "PUT", "DELETE"})

_MAX_BACKOFF = 30.0
#: "no override given" for `_AiohttpBackend.send(proxy=...)` -- distinct from
#: an explicit `None` override (meaning "no proxy for this one request").
_UNSET: Any = object()


@dataclass
class _Raw:
    """What a backend hands back, before it becomes a ``Response``."""

    status: int
    headers: dict[str, str]
    body: bytes
    url: str
    http_version: str | None
    encoding: str


# ---------------------------------------------------------------------------
# Backends
# ---------------------------------------------------------------------------


class _AiohttpBackend:
    """HTTP/1.1 via aiohttp -- no extra dependencies."""

    def __init__(
        self,
        *,
        headers: dict[str, str],
        proxy: str | None,
        verify_ssl: bool | ssl.SSLContext,
        max_redirects: int,
        cookies: dict[str, str] | None = None,
        resolver: Any = None,
    ) -> None:
        self._headers = headers
        self._proxy = proxy
        self._verify = verify_ssl
        self._max_redirects = max_redirects
        self._seed_cookies = cookies
        self._resolver = resolver
        self._client: Any = None

    async def _ensure(self) -> Any:
        if self._client is None:
            import aiohttp

            connector = aiohttp.TCPConnector(
                ssl=self._verify,
                ttl_dns_cache=300,
                enable_cleanup_closed=True,
                **({"resolver": self._resolver} if self._resolver is not None else {}),
            )
            self._client = aiohttp.ClientSession(
                connector=connector,
                headers=self._headers,
                # unsafe=True: keep cookies from IP-address hosts (a local or
                # internal service), which aiohttp's default jar silently drops.
                cookie_jar=aiohttp.CookieJar(unsafe=True),
                cookies=self._seed_cookies or None,
            )
        return self._client

    async def cookies(self, url: str | None = None) -> dict[str, str]:
        client = await self._ensure()
        if url:
            from yarl import URL

            morsels = client.cookie_jar.filter_cookies(URL(url))
        else:
            morsels = {m.key: m for m in client.cookie_jar}
        return {k: m.value for k, m in morsels.items()}

    async def clear_cookies(self) -> None:
        (await self._ensure()).cookie_jar.clear()

    async def set_cookies(self, cookies: dict[str, str]) -> None:
        (await self._ensure()).cookie_jar.update_cookies(cookies)

    async def send(
        self,
        method: str,
        url: str,
        *,
        headers: dict[str, str],
        body: bytes | None,
        cookies: dict[str, str] | None,
        timeout: float,
        follow_redirects: bool,
        proxy: str | None = _UNSET,
    ) -> _Raw:
        import aiohttp

        client = await self._ensure()
        try:
            async with client.request(
                method,
                url,
                headers=headers,
                data=body,
                cookies=cookies,
                proxy=self._proxy if proxy is _UNSET else proxy,
                timeout=aiohttp.ClientTimeout(total=timeout),
                allow_redirects=follow_redirects,
                max_redirects=self._max_redirects,
            ) as resp:
                content = await resp.read()
                version = resp.version
                return _Raw(
                    status=resp.status,
                    headers=dict(resp.headers),
                    body=content,
                    url=str(resp.url),
                    http_version=f"{version.major}.{version.minor}",
                    encoding=resp.charset or "utf-8",
                )
        except aiohttp.TooManyRedirects as exc:
            raise FetchError(
                f"too many redirects fetching {url}", url=url, retryable=False
            ) from exc
        except aiohttp.InvalidURL as exc:
            raise FetchError(f"invalid URL {url!r}", url=url, retryable=False) from exc
        except (aiohttp.ClientError, TimeoutError) as exc:
            raise FetchError(f"{type(exc).__name__} fetching {url}: {exc}", url=url) from exc

    async def close(self) -> None:
        if self._client is not None:
            await self._client.close()
            self._client = None
        if self._resolver is not None:
            await self._resolver.close()


class _HttpxBackend:
    """HTTP/2 (offered via ALPN, falling back to HTTP/1.1) via httpx --
    ``pip install bitscrape[http2]``."""

    def __init__(
        self,
        *,
        headers: dict[str, str],
        proxy: str | None,
        verify_ssl: bool | ssl.SSLContext,
        max_redirects: int,
        cookies: dict[str, str] | None = None,
    ) -> None:
        try:
            import h2  # noqa: F401
            import httpx
        except ImportError as exc:
            raise ImportError(
                "protocol='http2' requires the 'httpx' and 'h2' packages. "
                "Install with: pip install bitscrape[http2]"
            ) from exc
        self._httpx = httpx
        self._client = httpx.AsyncClient(
            http2=True,
            headers=headers,
            proxy=proxy,
            verify=verify_ssl,
            max_redirects=max_redirects,
            cookies=cookies,
        )

    async def cookies(self, url: str | None = None) -> dict[str, str]:
        # httpx has no public "cookies applicable to this URL" filter (unlike
        # aiohttp's jar.filter_cookies); url is accepted for a uniform signature
        # across both backends but every cookie in the jar is returned regardless.
        return dict(self._client.cookies)

    async def clear_cookies(self) -> None:
        self._client.cookies.clear()

    async def set_cookies(self, cookies: dict[str, str]) -> None:
        for name, value in cookies.items():
            self._client.cookies.set(name, value)

    async def send(
        self,
        method: str,
        url: str,
        *,
        headers: dict[str, str],
        body: bytes | None,
        cookies: dict[str, str] | None,
        timeout: float,
        follow_redirects: bool,
    ) -> _Raw:
        httpx = self._httpx
        try:
            # build_request(cookies=) merges with the session's cookie jar
            # (like aiohttp does) without mutating it; the per-request
            # `cookies=` argument of client.request() is deprecated.
            request = self._client.build_request(
                method, url, headers=headers, content=body, cookies=cookies, timeout=timeout
            )
            resp = await self._client.send(request, follow_redirects=follow_redirects)
        except httpx.TooManyRedirects as exc:
            raise FetchError(
                f"too many redirects fetching {url}", url=url, retryable=False
            ) from exc
        except (httpx.InvalidURL, httpx.UnsupportedProtocol) as exc:
            raise FetchError(f"invalid URL {url!r}", url=url, retryable=False) from exc
        except httpx.HTTPError as exc:
            raise FetchError(f"{type(exc).__name__} fetching {url}: {exc}", url=url) from exc
        return _Raw(
            status=resp.status_code,
            headers=dict(resp.headers),
            body=resp.content,
            url=str(resp.url),
            http_version=resp.http_version.removeprefix("HTTP/"),
            encoding=resp.charset_encoding or "utf-8",
        )

    async def close(self) -> None:
        await self._client.aclose()


# ---------------------------------------------------------------------------
# Request building helpers
# ---------------------------------------------------------------------------


def _reject_unsupported(kwargs: dict[str, Any], where: str) -> None:
    """Turn evasion-flavoured keyword arguments (habits from other libraries)
    into a clear explanation instead of a bare ``TypeError``."""
    bad = sorted(k for k in kwargs if k in UNSUPPORTED_OPTIONS)
    if bad:
        raise NotImplementedError(
            f"{where} got {bad}. bitscrape deliberately does not implement browser "
            "TLS/header fingerprint impersonation, stealth or HTTP/3 spoofing -- "
            "see ROADMAP.md's 'Explicit non-goals'. Use protocol='http2' for real "
            "HTTP/2, and pass ordinary headers= if a site needs particular ones."
        )
    if kwargs:
        raise TypeError(f"{where} got unexpected keyword argument(s): {sorted(kwargs)}")


def _merge_params(url: str, params: dict[str, Any] | Iterable[tuple[str, Any]] | None) -> str:
    """``url`` with ``params`` appended to (not replacing) any existing query.
    A list/tuple value becomes repeated keys (``?tag=a&tag=b``)."""
    if not params:
        return url
    parts = urlsplit(url)
    items = params.items() if isinstance(params, dict) else params
    query: list[tuple[str, str]] = parse_qsl(parts.query, keep_blank_values=True)
    for key, value in items:
        skey = str(key)
        if isinstance(value, list | tuple):
            query.extend((skey, str(v)) for v in value)
        else:
            query.append((skey, str(value)))
    return urlunsplit(parts._replace(query=urlencode(query)))


def _has_header(headers: dict[str, str], name: str) -> bool:
    lowered = name.lower()
    return any(k.lower() == lowered for k in headers)


def _build_body(
    data: bytes | str | dict[str, Any] | None,
    json: Any,
    headers: dict[str, str],
) -> bytes | None:
    if data is not None and json is not None:
        raise ValueError("Pass either `data=` or `json=`, not both.")
    if json is not None:
        if not _has_header(headers, "Content-Type"):
            headers["Content-Type"] = "application/json"
        return _jsonx.dumps_bytes(json)
    if data is None:
        return None
    if isinstance(data, dict):
        if not _has_header(headers, "Content-Type"):
            headers["Content-Type"] = "application/x-www-form-urlencoded"
        return urlencode(data, doseq=True).encode("utf-8")
    return data.encode("utf-8") if isinstance(data, str) else data


def _basic_auth(auth: tuple[str, str]) -> str:
    token = base64.b64encode(f"{auth[0]}:{auth[1]}".encode()).decode("ascii")
    return f"Basic {token}"


def _ensure_no_running_loop(what: str, use_instead: str | None = None) -> None:
    """Refuse to run a synchronous API inside a running event loop, naming the
    async class to use there. ``use_instead`` defaults to ``Async`` + ``what``'s
    own name (``Fetcher`` -> ``AsyncFetcher``); the session classes pass theirs."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return
    alternative = use_instead or f"Async{what}"
    raise RuntimeError(
        f"{what} is synchronous and can't be used inside a running event loop "
        "(an async function, a Jupyter cell, an async spider callback). "
        f"Use {alternative} there instead."
    )


# ---------------------------------------------------------------------------
# AsyncFetcherSession: the core
# ---------------------------------------------------------------------------


class AsyncFetcherSession:
    """A reusable async HTTP client: cookies and connections persist across
    calls. Use as ``async with AsyncFetcherSession() as s:`` (or call
    :meth:`close` yourself).

    Client-level options (fixed for the session's lifetime):

    Args:
        protocol: ``"http1"`` (default, aiohttp) or ``"http2"`` (httpx; needs
            ``bitscrape[http2]``; falls back to HTTP/1.1 where HTTP/2 isn't
            offered).
        cookies: initial cookies (``{name: value}``), e.g. restored with
            :meth:`load_cookies` from a previous run.
        headers: default headers for every request in this session, merged
            over ``settings.default_headers``.
        user_agent: overrides ``settings.user_agent``.
        proxy: one proxy URL (``http://user:pass@host:port``) for the session.
        proxy_rotator: a :class:`~bitscrape.downloader.proxy.ProxyRotator` to
            draw the proxy from instead of a fixed ``proxy=``. With
            ``protocol="http1"`` (aiohttp), a fresh proxy is drawn **for every
            request** (and each attempt of a retry), with success/failure
            reported back to the rotator -- this is where rotation is most
            useful. With ``protocol="http2"``, httpx fixes its proxy at
            construction and can't change it per request, so one proxy is
            drawn **once**, like a :class:`~bitscrape.sessions.BrowserSession`
            (see its docstring for what "rotation" means there too).
        verify_ssl: ``True``/``False``, or an ``ssl.SSLContext`` (e.g. one that
            trusts your private CA).
        max_redirects: redirect-chain cap (default ``settings.max_redirect_count``).
        settings: a :class:`~bitscrape.core.settings.Settings` to take
            defaults from (timeout, redirects, TLS verification, headers...).
        max_retries / retry_backoff / retry_statuses / retry_methods: see the
            module docstring; ``max_retries`` can also be overridden per request.

    Request-level options are the arguments of :meth:`request`.
    """

    def __init__(
        self,
        *,
        protocol: Protocol = "http1",
        cookies: dict[str, str] | None = None,
        headers: dict[str, str] | None = None,
        user_agent: str | None = None,
        proxy: str | None = None,
        proxy_rotator: ProxyRotator | None = None,
        timeout: float | None = None,
        follow_redirects: bool | None = None,
        max_redirects: int | None = None,
        verify_ssl: bool | ssl.SSLContext | None = None,
        max_retries: int = 2,
        retry_backoff: float = 0.5,
        retry_statuses: Iterable[int] = RETRY_STATUSES,
        retry_methods: Iterable[str] = IDEMPOTENT_METHODS,
        settings: Settings | None = None,
        dns_over_https: bool | str | None = None,
        **unsupported: Any,
    ) -> None:
        _reject_unsupported(unsupported, "AsyncFetcherSession()")
        if protocol not in ("http1", "http2"):
            hint = " HTTP/3 (QUIC) isn't supported." if protocol == "http3" else ""
            raise ValueError(f"protocol must be 'http1' or 'http2', got {protocol!r}.{hint}")
        if max_retries < 0:
            raise ValueError("max_retries must be >= 0")
        if proxy is not None and proxy_rotator is not None:
            raise ValueError("Use either `proxy` or `proxy_rotator`, not both.")

        cfg = settings or Settings()
        self.settings = cfg
        self.protocol: Protocol = protocol
        self.proxy_rotator = proxy_rotator
        #: The proxy currently in use for a fixed-proxy backend
        #: (``protocol="http2"``) -- ``None`` for ``"http1"``, where each
        #: request draws its own and this is never set. Read by the engine/
        #: your own code for introspection; :meth:`request` reports success/
        #: failure for both cases on its own.
        self.resolved_proxy: str | None = proxy
        if proxy_rotator is not None and protocol == "http2":
            # httpx fixes its proxy at construction; draw once, like BrowserSession.
            self.resolved_proxy = proxy_rotator.get_proxy()
            proxy = self.resolved_proxy
        self.timeout = float(timeout if timeout is not None else cfg.download_timeout)
        self.follow_redirects = (
            cfg.follow_redirects if follow_redirects is None else follow_redirects
        )
        self.max_retries = max_retries
        self.retry_backoff = retry_backoff
        self.retry_statuses = frozenset(retry_statuses)
        self.retry_methods = frozenset(m.upper() for m in retry_methods)

        base_headers = {
            **cfg.default_headers,
            "User-Agent": user_agent or cfg.user_agent,
            **(headers or {}),
        }
        backend_cls = _HttpxBackend if protocol == "http2" else _AiohttpBackend
        doh = cfg.dns_over_https if dns_over_https is None else dns_over_https
        extra_backend_kwargs: dict[str, Any] = {}
        if doh:
            if protocol == "http2":
                raise ValueError(
                    "dns_over_https is supported with protocol='http1' (aiohttp) only; "
                    "the httpx HTTP/2 backend resolves names itself."
                )
            from bitscrape.dns import make_resolver

            extra_backend_kwargs["resolver"] = make_resolver(doh, cfg.doh_url)
        # Constructing the httpx backend imports httpx/h2 right away, so a
        # missing extra fails here, at construction, with an actionable message
        # -- not on the first request.
        self._backend: _AiohttpBackend | _HttpxBackend = backend_cls(
            headers=base_headers,
            proxy=proxy,
            verify_ssl=cfg.verify_ssl if verify_ssl is None else verify_ssl,
            max_redirects=max_redirects if max_redirects is not None else cfg.max_redirect_count,
            cookies=cookies,
            **extra_backend_kwargs,
        )
        self._closed = False

    # -- lifecycle -------------------------------------------------------

    async def __aenter__(self) -> AsyncFetcherSession:  # noqa: PYI034 -- typing.Self needs 3.11
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.close()

    async def close(self) -> None:
        """Close the underlying client. Idempotent; the session can't be used afterwards."""
        self._closed = True
        await self._backend.close()

    # -- cookies / state ---------------------------------------------------

    async def cookies(self, url: str | None = None) -> dict[str, str]:
        """This session's cookies, as ``{name: value}``. With ``url``, only
        those that would actually be sent to it (``protocol="http2"``: every
        cookie in the jar is returned regardless -- httpx has no per-URL
        filter)."""
        return await self._backend.cookies(url)

    async def set_cookies(self, cookies: dict[str, str]) -> None:
        """Add ``cookies`` to the session's jar (e.g. ones a login API returned
        in its JSON body, not a ``Set-Cookie`` header the session would have
        picked up on its own)."""
        await self._backend.set_cookies(cookies)

    async def clear_cookies(self) -> None:
        """Forget every cookie in this session (e.g. to log out)."""
        await self._backend.clear_cookies()

    async def save_cookies(self, path: str | Path) -> None:
        """Write this session's cookies to ``path`` as JSON, for
        :meth:`load_cookies` (here, or in a later process) to restore. A flat
        ``{name: value}`` snapshot, the same simplification
        :class:`~bitscrape.sessions.HttpSession` uses -- domain, path and
        expiry aren't preserved, so this suits a session that stays on one
        site, which covers the common case (save a login, resume later).
        """
        Path(path).write_text(_json.dumps(await self.cookies()), encoding="utf-8")

    async def load_cookies(self, path: str | Path) -> None:
        """Restore cookies written by :meth:`save_cookies`, adding them to
        whatever this session already has."""
        await self.set_cookies(_json.loads(Path(path).read_text(encoding="utf-8")))

    # -- requests --------------------------------------------------------

    async def request(
        self,
        method: str,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        params: dict[str, Any] | None = None,
        data: bytes | str | dict[str, Any] | None = None,
        json: Any = None,
        cookies: dict[str, str] | None = None,
        auth: tuple[str, str] | None = None,
        timeout: float | None = None,
        follow_redirects: bool | None = None,
        max_retries: int | None = None,
        **unsupported: Any,
    ) -> ParsedResponse:
        """Send one request and return a :class:`ParsedResponse`.

        Args:
            headers: extra headers for this request (override the session's).
            params: query parameters, appended to any already in ``url``; a
                list value repeats the key.
            data: a form ``dict`` (sent urlencoded), ``str`` or raw ``bytes``.
            json: a JSON-serialisable body (sets ``Content-Type`` unless you did).
            cookies: cookies for this request only, sent in addition to the
                session's stored cookies.
            auth: ``(user, password)`` for HTTP Basic auth.
            timeout / follow_redirects / max_retries: per-request overrides.

        Raises:
            FetchError: a network-level failure after retries.
        """
        _reject_unsupported(unsupported, "request()")
        if self._closed:
            raise RuntimeError("This fetcher session is closed.")
        method = method.upper()
        req_headers = dict(headers or {})
        if auth is not None and not _has_header(req_headers, "Authorization"):
            req_headers["Authorization"] = _basic_auth(auth)
        body = _build_body(data, json, req_headers)
        final_url = _merge_params(url, params)

        attempts = (self.max_retries if max_retries is None else max_retries) + 1
        if method not in self.retry_methods:
            attempts = 1
        eff_timeout = float(timeout) if timeout is not None else self.timeout
        eff_redirects = self.follow_redirects if follow_redirects is None else follow_redirects
        domain = urlsplit(final_url).hostname

        for attempt in range(attempts):
            last = attempt == attempts - 1
            # http1 (aiohttp): a fresh proxy per attempt, so a retry after a
            # failure gets a different one. http2 (httpx): the one proxy
            # resolved_proxy was fixed to at construction -- see __init__.
            proxy_for_attempt: str | None = None
            if self.proxy_rotator is not None:
                proxy_for_attempt = (
                    self.proxy_rotator.get_proxy(domain)
                    if self.protocol == "http1"
                    else self.resolved_proxy
                )
            t0 = time.monotonic()
            try:
                if isinstance(self._backend, _AiohttpBackend) and self.proxy_rotator is not None:
                    raw = await self._backend.send(
                        method,
                        final_url,
                        headers=req_headers,
                        body=body,
                        cookies=cookies,
                        timeout=eff_timeout,
                        follow_redirects=eff_redirects,
                        proxy=proxy_for_attempt,
                    )
                else:
                    raw = await self._backend.send(
                        method,
                        final_url,
                        headers=req_headers,
                        body=body,
                        cookies=cookies,
                        timeout=eff_timeout,
                        follow_redirects=eff_redirects,
                    )
            except FetchError as exc:
                if proxy_for_attempt is not None and self.proxy_rotator is not None:
                    self.proxy_rotator.report_failure(proxy_for_attempt)
                if last or not exc.retryable:
                    raise
                await asyncio.sleep(self._delay(attempt, None))
                continue
            elapsed_ms = (time.monotonic() - t0) * 1000
            if raw.status in self.retry_statuses:
                if proxy_for_attempt is not None and self.proxy_rotator is not None:
                    self.proxy_rotator.report_failure(proxy_for_attempt)
                if not last:
                    await asyncio.sleep(self._delay(attempt, raw.headers))
                    continue
                return self._to_response(method, final_url, req_headers, body, raw, elapsed_ms)
            if proxy_for_attempt is not None and self.proxy_rotator is not None:
                self.proxy_rotator.report_success(proxy_for_attempt)
            return self._to_response(method, final_url, req_headers, body, raw, elapsed_ms)
        raise AssertionError("unreachable: the loop always returns or raises")  # pragma: no cover

    def _delay(self, attempt: int, headers: dict[str, str] | None) -> float:
        """Seconds to wait before retry ``attempt + 1``: the server's
        ``Retry-After`` if it sent one (capped), else exponential backoff."""
        if headers is not None and self.settings.respect_retry_after:
            asked = parse_retry_after(header_value(headers, "Retry-After"))
            if asked is not None:
                return min(asked, self.settings.max_retry_after_seconds)
        return min(self.retry_backoff * (2**attempt), _MAX_BACKOFF)

    @staticmethod
    def _to_response(
        method: str,
        url: str,
        headers: dict[str, str],
        body: bytes | None,
        raw: _Raw,
        elapsed_ms: float,
    ) -> ParsedResponse:
        return ParsedResponse(
            Response(
                url=raw.url,
                status=raw.status,
                headers=raw.headers,
                body=raw.body,
                request=Request(url=url, method=method, headers=headers, body=body),
                elapsed_ms=elapsed_ms,
                encoding=raw.encoding,
                http_version=raw.http_version,
            )
        )

    async def get(self, url: str, **kwargs: Any) -> ParsedResponse:
        return await self.request("GET", url, **kwargs)

    async def post(self, url: str, **kwargs: Any) -> ParsedResponse:
        return await self.request("POST", url, **kwargs)

    async def put(self, url: str, **kwargs: Any) -> ParsedResponse:
        return await self.request("PUT", url, **kwargs)

    async def patch(self, url: str, **kwargs: Any) -> ParsedResponse:
        return await self.request("PATCH", url, **kwargs)

    async def delete(self, url: str, **kwargs: Any) -> ParsedResponse:
        return await self.request("DELETE", url, **kwargs)

    async def head(self, url: str, **kwargs: Any) -> ParsedResponse:
        return await self.request("HEAD", url, **kwargs)

    async def options(self, url: str, **kwargs: Any) -> ParsedResponse:
        return await self.request("OPTIONS", url, **kwargs)


# ---------------------------------------------------------------------------
# FetcherSession: the same, synchronous
# ---------------------------------------------------------------------------


class FetcherSession:
    """The synchronous twin of :class:`AsyncFetcherSession`: one client (so
    cookies and connections persist) across the ``with`` block::

        with FetcherSession(protocol="http2") as s:
            s.post("https://example.com/login", data={"user": "me", "pw": "x"})
            page = s.get("https://example.com/account")   # logged in

    Takes the same constructor arguments as :class:`AsyncFetcherSession`.
    Not usable inside a running event loop -- use the async class there.
    Internally it owns a private event loop, so it isn't thread-safe: use one
    session per thread.
    """

    def __init__(self, **kwargs: Any) -> None:
        _ensure_no_running_loop("FetcherSession", "AsyncFetcherSession")
        self._inner = AsyncFetcherSession(**kwargs)
        self._loop: asyncio.AbstractEventLoop | None = asyncio.new_event_loop()

    def _run(self, coro: Coroutine[Any, Any, T]) -> T:
        _ensure_no_running_loop("FetcherSession", "AsyncFetcherSession")
        if self._loop is None:
            coro.close()  # don't leak "coroutine was never awaited"
            raise RuntimeError("This fetcher session is closed.")
        return self._loop.run_until_complete(coro)

    def __enter__(self) -> FetcherSession:  # noqa: PYI034 -- typing.Self needs 3.11
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def close(self) -> None:
        """Close the client and its event loop. Idempotent."""
        if self._loop is None:
            return
        try:
            self._loop.run_until_complete(self._inner.close())
        finally:
            self._loop.close()
            self._loop = None

    # -- cookies / state ---------------------------------------------------

    def cookies(self, url: str | None = None) -> dict[str, str]:
        """This session's cookies; see :meth:`AsyncFetcherSession.cookies`."""
        return self._run(self._inner.cookies(url))

    def set_cookies(self, cookies: dict[str, str]) -> None:
        """Add ``cookies`` to the session's jar."""
        self._run(self._inner.set_cookies(cookies))

    def clear_cookies(self) -> None:
        """Forget every cookie in this session (e.g. to log out)."""
        self._run(self._inner.clear_cookies())

    def save_cookies(self, path: str | Path) -> None:
        """Write this session's cookies to ``path``; see
        :meth:`AsyncFetcherSession.save_cookies`."""
        self._run(self._inner.save_cookies(path))

    def load_cookies(self, path: str | Path) -> None:
        """Restore cookies written by :meth:`save_cookies`."""
        self._run(self._inner.load_cookies(path))

    def request(self, method: str, url: str, **kwargs: Any) -> ParsedResponse:
        return self._run(self._inner.request(method, url, **kwargs))

    def get(self, url: str, **kwargs: Any) -> ParsedResponse:
        return self.request("GET", url, **kwargs)

    def post(self, url: str, **kwargs: Any) -> ParsedResponse:
        return self.request("POST", url, **kwargs)

    def put(self, url: str, **kwargs: Any) -> ParsedResponse:
        return self.request("PUT", url, **kwargs)

    def patch(self, url: str, **kwargs: Any) -> ParsedResponse:
        return self.request("PATCH", url, **kwargs)

    def delete(self, url: str, **kwargs: Any) -> ParsedResponse:
        return self.request("DELETE", url, **kwargs)

    def head(self, url: str, **kwargs: Any) -> ParsedResponse:
        return self.request("HEAD", url, **kwargs)

    def options(self, url: str, **kwargs: Any) -> ParsedResponse:
        return self.request("OPTIONS", url, **kwargs)


# ---------------------------------------------------------------------------
# Fetcher / AsyncFetcher: one-shot facades
# ---------------------------------------------------------------------------

#: Keyword arguments that configure the *client* (so they go to the session
#: constructor); everything else is per-request and goes to ``request()``.
_CLIENT_KEYS = frozenset(
    {
        "protocol",
        "user_agent",
        "proxy",
        "proxy_rotator",
        "verify_ssl",
        "max_redirects",
        "settings",
        "dns_over_https",
        "retry_backoff",
        "retry_statuses",
        "retry_methods",
    }
)


def _split_kwargs(kwargs: dict[str, Any], where: str) -> tuple[dict[str, Any], dict[str, Any]]:
    _reject_unsupported(
        {k: v for k, v in kwargs.items() if k in UNSUPPORTED_OPTIONS},
        where,
    )
    client = {k: kwargs.pop(k) for k in list(kwargs) if k in _CLIENT_KEYS}
    return client, kwargs


class Fetcher:
    """One-shot synchronous requests -- ``Fetcher.get(url)`` -- each with its
    own short-lived client (nothing is shared between calls; use
    :class:`FetcherSession` when you want cookies/connections to persist).

    Client options (``protocol=``, ``proxy=``, ``user_agent=``, ``verify_ssl=``,
    ``max_redirects=``, ``settings=``, ``retry_*``) and request options
    (``headers=``, ``params=``, ``data=``, ``json=``, ``cookies=``, ``auth=``,
    ``timeout=``, ``follow_redirects=``, ``max_retries=``) are all plain keyword
    arguments; see :class:`AsyncFetcherSession` / :meth:`AsyncFetcherSession.request`.
    """

    @classmethod
    def request(cls, method: str, url: str, **kwargs: Any) -> ParsedResponse:
        _ensure_no_running_loop("Fetcher")
        client_kwargs, request_kwargs = _split_kwargs(kwargs, "Fetcher")
        with FetcherSession(**client_kwargs) as session:
            return session.request(method, url, **request_kwargs)

    @classmethod
    def get(cls, url: str, **kwargs: Any) -> ParsedResponse:
        return cls.request("GET", url, **kwargs)

    @classmethod
    def post(cls, url: str, **kwargs: Any) -> ParsedResponse:
        return cls.request("POST", url, **kwargs)

    @classmethod
    def put(cls, url: str, **kwargs: Any) -> ParsedResponse:
        return cls.request("PUT", url, **kwargs)

    @classmethod
    def patch(cls, url: str, **kwargs: Any) -> ParsedResponse:
        return cls.request("PATCH", url, **kwargs)

    @classmethod
    def delete(cls, url: str, **kwargs: Any) -> ParsedResponse:
        return cls.request("DELETE", url, **kwargs)

    @classmethod
    def head(cls, url: str, **kwargs: Any) -> ParsedResponse:
        return cls.request("HEAD", url, **kwargs)

    @classmethod
    def options(cls, url: str, **kwargs: Any) -> ParsedResponse:
        return cls.request("OPTIONS", url, **kwargs)


class AsyncFetcher:
    """One-shot asynchronous requests -- ``await AsyncFetcher.get(url)`` --
    each with its own short-lived client. Same options as :class:`Fetcher`.
    For many requests, open one :class:`AsyncFetcherSession` instead and share
    it (with ``asyncio.gather`` if you want them concurrent)."""

    @classmethod
    async def request(cls, method: str, url: str, **kwargs: Any) -> ParsedResponse:
        client_kwargs, request_kwargs = _split_kwargs(kwargs, "AsyncFetcher")
        async with AsyncFetcherSession(**client_kwargs) as session:
            return await session.request(method, url, **request_kwargs)

    @classmethod
    async def get(cls, url: str, **kwargs: Any) -> ParsedResponse:
        return await cls.request("GET", url, **kwargs)

    @classmethod
    async def post(cls, url: str, **kwargs: Any) -> ParsedResponse:
        return await cls.request("POST", url, **kwargs)

    @classmethod
    async def put(cls, url: str, **kwargs: Any) -> ParsedResponse:
        return await cls.request("PUT", url, **kwargs)

    @classmethod
    async def patch(cls, url: str, **kwargs: Any) -> ParsedResponse:
        return await cls.request("PATCH", url, **kwargs)

    @classmethod
    async def delete(cls, url: str, **kwargs: Any) -> ParsedResponse:
        return await cls.request("DELETE", url, **kwargs)

    @classmethod
    async def head(cls, url: str, **kwargs: Any) -> ParsedResponse:
        return await cls.request("HEAD", url, **kwargs)

    @classmethod
    async def options(cls, url: str, **kwargs: Any) -> ParsedResponse:
        return await cls.request("OPTIONS", url, **kwargs)


__all__ = [
    "IDEMPOTENT_METHODS",
    "RETRY_STATUSES",
    "AsyncFetcher",
    "AsyncFetcherSession",
    "FetchError",
    "Fetcher",
    "FetcherSession",
]
