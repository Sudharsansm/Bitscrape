"""
DNS-over-HTTPS resolver
========================

Resolve hostnames through a DoH server instead of the system resolver, so your
machine's DNS queries aren't sent in the clear to your ISP/router -- useful on
untrusted networks and whenever you want name resolution to be independent of
the local resolver. It is a plain **privacy** feature: it doesn't change what the
target site sees.

    from bitscrape.dns import DoHResolver
    resolver = DoHResolver("cloudflare")            # or "google", "quad9", or a URL
    connector = aiohttp.TCPConnector(resolver=resolver)

Or just set ``Settings(dns_over_https=True, doh_url="cloudflare")`` /
``AsyncFetcherSession(dns_over_https=True)`` / ``HttpSession(dns_over_https=True)``.

Details:

- Uses the JSON DoH API (``application/dns-json``) that Cloudflare, Google and
  Quad9 all serve. The named providers are addressed by **IP** (``1.1.1.1``,
  ``8.8.8.8``, ``9.9.9.9``), so locating the resolver itself needs no DNS lookup.
- Answers are cached for their TTL (clamped to ``[min_ttl, max_ttl]``);
  concurrent lookups of the same host share one query.
- IP-literal hosts and ``localhost`` never hit the network.
- By default a failed DoH lookup raises (``OSError``) rather than silently
  falling back to the system resolver -- a silent fallback would defeat the
  point. Pass ``fallback=True`` to opt in to it.
- This covers the HTTP (aiohttp) stack. Through a proxy the *proxy host* is what
  gets resolved locally; the browser (Playwright) resolves names itself.
"""

from __future__ import annotations

import asyncio
import ipaddress
import logging
import socket
import time
from typing import Any

import aiohttp
from aiohttp.abc import AbstractResolver, ResolveResult

logger = logging.getLogger("bitscrape.dns")

PROVIDERS: dict[str, str] = {
    "cloudflare": "https://1.1.1.1/dns-query",
    "google": "https://8.8.8.8/resolve",
    "quad9": "https://9.9.9.9:5053/dns-query",
}
DEFAULT_PROVIDER = "cloudflare"

_TYPE_A = 1
_TYPE_AAAA = 28


def resolve_doh_url(spec: str | None) -> str:
    """A provider name (``"cloudflare"``/``"google"``/``"quad9"``) or full ``https://`` URL → URL."""
    if not spec:
        return PROVIDERS[DEFAULT_PROVIDER]
    key = spec.strip().lower()
    if key in PROVIDERS:
        return PROVIDERS[key]
    if key.startswith("https://"):
        return spec.strip()
    raise ValueError(f"doh_url must be one of {sorted(PROVIDERS)} or an https:// URL, got {spec!r}")


class DoHResolver(AbstractResolver):
    """An aiohttp resolver backed by a DNS-over-HTTPS (JSON API) server."""

    def __init__(
        self,
        url: str | None = None,
        *,
        timeout: float = 5.0,
        min_ttl: float = 30.0,
        max_ttl: float = 3600.0,
        fallback: bool = False,
        ipv6: bool = True,
        max_cache: int = 4096,
    ) -> None:
        self.url = resolve_doh_url(url)
        self.timeout = timeout
        self.min_ttl = min_ttl
        self.max_ttl = max_ttl
        self.fallback = fallback
        self.ipv6 = ipv6
        self.max_cache = max_cache
        self.queries = 0  # DoH requests actually sent (cache hits don't count)
        self.cache_hits = 0
        self._http: aiohttp.ClientSession | None = None
        self._cache: dict[tuple[str, int], tuple[float, list[str]]] = {}
        self._inflight: dict[tuple[str, int], asyncio.Future[list[str]]] = {}
        self._system: AbstractResolver | None = None

    # -- aiohttp API ---------------------------------------------------------

    async def resolve(
        self, host: str, port: int = 0, family: socket.AddressFamily = socket.AF_INET
    ) -> list[ResolveResult]:
        if _is_ip(host):
            return [_result(host, port, host)]
        if host.lower() in ("localhost", "localhost.localdomain"):
            return await self._system_resolver().resolve(host, port, family)
        families: list[int]
        if family == socket.AF_INET6:
            families = [_TYPE_AAAA]
        elif family == socket.AF_INET or not self.ipv6:
            families = [_TYPE_A]
        else:  # AF_UNSPEC
            families = [_TYPE_A, _TYPE_AAAA]
        addrs: list[str] = []
        error: BaseException | None = None
        results = await asyncio.gather(
            *(self._lookup(host, t) for t in families), return_exceptions=True
        )
        for res in results:
            if isinstance(res, BaseException):
                error = res
            else:
                addrs.extend(res)
        if not addrs:
            if self.fallback:
                logger.warning(
                    "DoH lookup for %s failed (%s); using the system resolver", host, error
                )
                return await self._system_resolver().resolve(host, port, family)
            raise OSError(f"DNS-over-HTTPS could not resolve {host!r}: {error or 'no records'}")
        return [_result(host, port, a) for a in dict.fromkeys(addrs)]

    async def close(self) -> None:
        if self._http is not None:
            await self._http.close()
            self._http = None
        if self._system is not None:
            await self._system.close()
            self._system = None

    # -- internals -----------------------------------------------------------

    def _system_resolver(self) -> AbstractResolver:
        if self._system is None:
            self._system = aiohttp.ThreadedResolver()
        return self._system

    async def _lookup(self, host: str, rtype: int) -> list[str]:
        key = (host.lower(), rtype)
        cached = self._cache.get(key)
        now = time.monotonic()
        if cached is not None and cached[0] > now:
            self.cache_hits += 1
            return list(cached[1])
        pending = self._inflight.get(key)
        if pending is not None:
            return list(await asyncio.shield(pending))
        fut: asyncio.Future[list[str]] = asyncio.get_running_loop().create_future()
        self._inflight[key] = fut
        try:
            addrs, ttl = await self._query(host, rtype)
            ttl = min(max(ttl, self.min_ttl), self.max_ttl)
            if len(self._cache) >= self.max_cache:
                self._cache.pop(next(iter(self._cache)))
            self._cache[key] = (time.monotonic() + ttl, addrs)
            fut.set_result(addrs)
            return list(addrs)
        except BaseException as exc:
            fut.set_exception(exc)
            fut.exception()  # mark retrieved so asyncio doesn't warn if nobody awaits
            raise
        finally:
            self._inflight.pop(key, None)

    async def _query(self, host: str, rtype: int) -> tuple[list[str], float]:
        if self._http is None:
            self._http = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=self.timeout),
                headers={"Accept": "application/dns-json"},
            )
        self.queries += 1
        params = {"name": host, "type": "A" if rtype == _TYPE_A else "AAAA"}
        async with self._http.get(self.url, params=params) as resp:
            if resp.status != 200:
                raise OSError(f"DoH server answered HTTP {resp.status}")
            data: dict[str, Any] = await resp.json(content_type=None)
        status = data.get("Status", 0)
        if status == 3:  # NXDOMAIN
            raise OSError(f"{host}: NXDOMAIN")
        if status != 0:
            raise OSError(f"{host}: DNS error {status}")
        addrs: list[str] = []
        ttls: list[float] = []
        for ans in data.get("Answer") or []:
            if ans.get("type") == rtype and _is_ip(str(ans.get("data", ""))):
                addrs.append(str(ans["data"]))
                ttls.append(float(ans.get("TTL", self.min_ttl)))
        return addrs, (min(ttls) if ttls else self.min_ttl)


def _is_ip(value: str) -> bool:
    try:
        ipaddress.ip_address(value)
    except ValueError:
        return False
    return True


def _result(host: str, port: int, addr: str) -> ResolveResult:
    fam = socket.AF_INET6 if ":" in addr else socket.AF_INET
    return {
        "hostname": host,
        "host": addr,
        "port": port,
        "family": fam,
        "proto": 0,
        "flags": socket.AI_NUMERICHOST | socket.AI_NUMERICSERV,
    }


def make_resolver(enabled: bool | str | None, doh_url: str | None = None) -> DoHResolver | None:
    """``None`` unless DoH is on. ``enabled`` may be ``True`` (use ``doh_url`` / the
    default provider) or a provider name / URL directly."""
    if not enabled:
        return None
    spec = enabled if isinstance(enabled, str) else doh_url
    return DoHResolver(spec)


__all__ = ["PROVIDERS", "DoHResolver", "make_resolver", "resolve_doh_url"]
