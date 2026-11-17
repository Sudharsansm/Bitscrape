"""
Adaptive politeness: AutoThrottle + DomainPacer
================================================

Two small, separately testable pieces that together stop you from guessing
crawl delays:

:class:`AutoThrottle`
    *Decides* how long to wait between requests to each domain. It learns the
    delay from how fast the site answers (``latency / target_concurrency``,
    smoothed), **doubles** it when the site blocks or rate-limits you, or -- when
    the site says how long to wait via ``Retry-After`` -- asks for a
    domain-wide cool-down of exactly that long. Every healthy response then
    pulls the delay back toward what the site can comfortably serve, so the
    crawl speeds up again once the pressure is off. A block can never speed the
    crawl up.

:class:`DomainPacer`
    *Enforces* it. A per-domain FIFO gate that spaces request **start times**
    at least ``delay`` apart and honours cool-downs. Unlike a check-then-sleep
    (which lets N concurrent workers all observe "enough time has passed" and
    fire at once) waiters are serialised, and the delay is re-read at the moment
    each one is released -- so a back-off decided while requests are queued
    applies to them too, not just to requests that arrive later.

Neither piece does anything to disguise the crawler: this is about being a
*better-behaved* client, not a harder-to-detect one.
"""

from __future__ import annotations

import asyncio
import logging
import math
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

logger = logging.getLogger(__name__)

#: When a domain's learned delay is (near) zero, doubling it would stay zero.
#: A block from zero therefore backs off to ``_BACKOFF_SEED * factor`` seconds.
_BACKOFF_SEED = 0.5


# ---------------------------------------------------------------------------
# Retry-After
# ---------------------------------------------------------------------------


def parse_retry_after(value: str | None) -> float | None:
    """Seconds a ``Retry-After`` value asks us to wait, or ``None``.

    Per RFC 9110 the value is either a non-negative number of seconds or an
    HTTP-date. Dates in the past yield ``0.0``; missing, empty, non-finite or
    unreadable values yield ``None``.
    """
    if not value:
        return None
    value = value.strip()
    if not value:
        return None
    try:
        seconds = float(value)
    except ValueError:
        pass
    else:
        return max(0.0, seconds) if math.isfinite(seconds) else None
    try:
        target = parsedate_to_datetime(value)
    except (ValueError, TypeError):
        return None
    if target is None:  # pragma: no cover - defensive; older Pythons returned None
        return None
    if target.tzinfo is None:
        target = target.replace(tzinfo=timezone.utc)
    return max(0.0, (target - datetime.now(timezone.utc)).total_seconds())


def header_value(headers: Mapping[str, str] | None, name: str) -> str | None:
    """Case-insensitive header lookup (browser fetches lower-case their keys)."""
    if not headers:
        return None
    direct = headers.get(name)
    if direct is not None:
        return direct
    lowered = name.lower()
    for key, value in headers.items():
        if key.lower() == lowered:
            return value
    return None


# ---------------------------------------------------------------------------
# AutoThrottle
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ThrottleDecision:
    """What :meth:`AutoThrottle.record` decided for one observed response."""

    delay: float  #: the domain's new learned delay (seconds between requests)
    cooldown: float = 0.0  #: pause the whole domain for this long (``Retry-After``)
    backed_off: bool = False  #: this observation was a block / rate-limit signal


class AutoThrottle:
    """Adaptive per-domain delay learned from latency and block signals.

    Args:
        start_delay: delay used for a domain's first requests.
        max_delay: ceiling for the *learned* delay. (A site's own ``Crawl-delay``
            passed as ``floor`` may exceed it -- the site's wishes win.)
        target_concurrency: requests you aim to have in flight per domain;
            ``latency / target_concurrency`` is the delay the site can sustain.
        block_backoff: react to blocks/rate-limits (``ok=False``) at all. With
            ``False`` every response is treated as a plain latency sample.
        backoff_factor: multiplier applied to the delay on a block (default 2).
        recovery: fraction of the gap to the target closed by each healthy
            response (``0.5`` = halve the gap; ``1.0`` = jump straight there).
        max_cooldown: cap on a ``Retry-After`` cool-down, in seconds.
    """

    def __init__(
        self,
        start_delay: float = 1.0,
        max_delay: float = 60.0,
        target_concurrency: float = 2.0,
        *,
        block_backoff: bool = True,
        backoff_factor: float = 2.0,
        recovery: float = 0.5,
        max_cooldown: float = 120.0,
    ) -> None:
        if not 0 < recovery <= 1:
            raise ValueError("`recovery` must be in (0, 1]")
        if backoff_factor < 1:
            raise ValueError("`backoff_factor` must be >= 1")
        self._start_delay = max(0.0, start_delay)
        self._max_delay = max(0.0, max_delay)
        self._target_concurrency = max(1e-6, target_concurrency)
        self.block_backoff = block_backoff
        self.backoff_factor = backoff_factor
        self.recovery = recovery
        self.max_cooldown = max(0.0, max_cooldown)
        self._delays: dict[str, float] = {}
        #: how many block signals each domain has produced
        self.backoff_counts: dict[str, int] = {}
        #: total block signals across all domains
        self.total_backoffs = 0

    # -- reading -------------------------------------------------------------

    def get_delay(self, domain: str) -> float:
        """The learned delay for ``domain`` (``start_delay`` if never seen)."""
        return self._delays.get(domain, self._start_delay)

    def delay_for(self, domain: str, floor: float = 0.0) -> float:
        """Effective delay: the learned one, but never below ``floor``.

        ``floor`` carries limits the site or the operator imposed (``Crawl-delay``,
        ``download_delay``, ``domain_delays``); it is applied at read time so the
        learned value stays a pure measurement of what the site can take.
        """
        return max(self.get_delay(domain), floor, 0.0)

    def snapshot(self) -> dict[str, dict[str, float | int]]:
        """``{domain: {"delay": s, "backoffs": n}}`` for every domain seen."""
        return {
            d: {"delay": round(v, 4), "backoffs": self.backoff_counts.get(d, 0)}
            for d, v in self._delays.items()
        }

    # -- feeding -------------------------------------------------------------

    def update(self, domain: str, latency_seconds: float) -> None:
        """Feed a healthy response's latency (kept for backward compatibility)."""
        self.record(domain, latency_seconds, ok=True)

    def record(
        self,
        domain: str,
        latency: float,
        *,
        ok: bool = True,
        retry_after: float | None = None,
        floor: float = 0.0,
    ) -> ThrottleDecision:
        """Feed one finished request back into the throttle.

        Args:
            domain: the domain (``netloc``) the request went to.
            latency: seconds the server took to answer this attempt (not
                including any retry sleeps).
            ok: ``False`` for a block, rate-limit, ban page, timeout or
                connection error -- anything that says "slow down".
            retry_after: seconds the site asked us to wait, if it said.
            floor: lowest delay allowed (see :meth:`delay_for`).

        Returns:
            The new learned delay and any cool-down the caller should apply.
        """
        current = self._delays.get(domain, self._start_delay)
        target = max(0.0, latency) / self._target_concurrency
        new = current + (target - current) * self.recovery

        cooldown = 0.0
        backed_off = False
        if not ok and self.block_backoff:
            backed_off = True
            self.backoff_counts[domain] = self.backoff_counts.get(domain, 0) + 1
            self.total_backoffs += 1
            if retry_after is not None:
                # The site told us exactly what it wants: honour that as a
                # domain-wide pause. No doubling -- it isn't overwhelmed by our
                # spacing, it asked for a break.
                cooldown = min(max(retry_after, 0.0), self.max_cooldown)
                new = max(new, current)
            else:
                base = max(current, floor, 0.0) or _BACKOFF_SEED
                new = max(new, base * self.backoff_factor)
            new = max(new, current)  # a block can never speed the crawl up

        self._delays[domain] = min(max(new, 0.0), self._max_delay)
        if backed_off:
            logger.info(
                "AutoThrottle(%s): block signal -> delay %.2fs -> %.2fs%s",
                domain,
                current,
                self._delays[domain],
                f", cool-down {cooldown:.1f}s" if cooldown else "",
            )
        return ThrottleDecision(self._delays[domain], cooldown, backed_off)

    def reset(self, domain: str | None = None) -> None:
        """Forget what was learned (for one domain, or all)."""
        if domain is None:
            self._delays.clear()
            self.backoff_counts.clear()
        else:
            self._delays.pop(domain, None)
            self.backoff_counts.pop(domain, None)


# ---------------------------------------------------------------------------
# DomainPacer
# ---------------------------------------------------------------------------


class DomainPacer:
    """Spaces request start times per domain and enforces cool-downs.

    ``clock`` / ``sleep`` are injectable so the behaviour can be tested
    deterministically without real waiting.
    """

    def __init__(
        self,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._clock = clock
        self._sleep = sleep
        self._next_slot: dict[str, float] = {}
        self._paused_until: dict[str, float] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        #: total seconds spent waiting in :meth:`wait`, across all callers
        self.total_wait = 0.0
        #: number of cool-downs applied
        self.cooldowns = 0

    def _lock(self, domain: str) -> asyncio.Lock:
        lock = self._locks.get(domain)
        if lock is None:
            lock = self._locks[domain] = asyncio.Lock()
        return lock

    def pause(self, domain: str, seconds: float) -> None:
        """Hold every request to ``domain`` for ``seconds`` from now (never shortens)."""
        if seconds <= 0:
            return
        until = self._clock() + seconds
        if until > self._paused_until.get(domain, 0.0):
            self._paused_until[domain] = until
            self.cooldowns += 1

    def paused_for(self, domain: str) -> float:
        """Seconds left on ``domain``'s cool-down (0 if none)."""
        return max(0.0, self._paused_until.get(domain, 0.0) - self._clock())

    async def wait(self, domain: str, delay: float | Callable[[], float]) -> float:
        """Block until a request to ``domain`` may start; returns seconds waited.

        ``delay`` may be a callable: it is evaluated when this caller's turn
        comes, so a back-off recorded while it was queued still applies.
        """
        waited = 0.0
        async with self._lock(domain):
            while True:
                now = self._clock()
                ready = max(self._next_slot.get(domain, 0.0), self._paused_until.get(domain, 0.0))
                if now >= ready:
                    break
                step = ready - now
                waited += step
                await self._sleep(step)
            spacing = delay() if callable(delay) else delay
            self._next_slot[domain] = self._clock() + max(0.0, spacing)
        self.total_wait += waited
        return waited

    def reset(self) -> None:
        self._next_slot.clear()
        self._paused_until.clear()


__all__ = [
    "AutoThrottle",
    "DomainPacer",
    "ThrottleDecision",
    "header_value",
    "parse_retry_after",
]
