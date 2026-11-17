"""
Proxy rotation
==============

:class:`ProxyRotator` turns a flat list of proxy URLs into a stateful pool
the downloader can pull from automatically -- picking a strategy
(``random`` / ``round_robin`` / ``least_used`` / ``sticky``, or a custom one
of your own), tracking consecutive failures per proxy, and temporarily
benching a proxy once it fails too many times in a row instead of hammering a
dead/blocked proxy forever.

This is deliberately just rotation/bookkeeping over proxies *you already
have* (``settings.proxies`` or a ``proxy_file``, one URL per line, e.g.
``http://user:pass@host:port``) -- it does not source, buy, or verify
proxies, and it does nothing to disguise the fact that traffic is going
through a proxy (no fingerprint changes). That's a deliberate scope
boundary, matching the project's existing policy against building
detection-evasion tooling: this class is about *not wasting requests on a
proxy you already know is down*, not about hiding automation.

Usage::

    rotator = ProxyRotator(["http://p1:8080", "http://p2:8080"], strategy="round_robin")
    proxy = rotator.get_proxy(domain="example.com")   # None if pool is empty/exhausted
    ...
    rotator.report_success(proxy)
    # or, on a failed/blocked response:
    rotator.report_failure(proxy)

A custom strategy is a plain function of the proxies currently available (not
banned) and the request's domain, returning the one to use::

    def shortest_queue(available: list[ProxyState], domain: str | None) -> ProxyState:
        return min(available, key=lambda p: p.uses)

    rotator = ProxyRotator(["http://p1:8080", "http://p2:8080"], strategy=shortest_queue)

...or register it under a name, so it can be selected from ``Settings``
(a plain string field -- it can't hold a function) the same way a built-in
strategy is::

    register_proxy_strategy("shortest_queue", shortest_queue)
    settings = Settings(proxies=[...], proxy_strategy="shortest_queue")

``ProxyRotator`` is used the same way, with the same strategies (built-in or
custom), from every session type: ``HttpSession``/``AsyncFetcherSession`` pick
a proxy fresh for each request; ``BrowserSession``/``DynamicSession`` and
``protocol="http2"`` HTTP sessions pick one when they start, since neither a
browser nor an HTTP/2 client can switch proxy mid-connection -- see
``docs/proxies.md``.
"""

from __future__ import annotations

import random
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

BUILTIN_STRATEGIES = ("random", "round_robin", "least_used", "sticky")
#: Kept for anyone already importing the old name; identical to BUILTIN_STRATEGIES.
VALID_STRATEGIES = BUILTIN_STRATEGIES


@dataclass
class ProxyState:
    """One proxy's rotation bookkeeping, as seen by a custom strategy function:
    which proxy it is, how much it's been used, and its recent-failure streak.
    ``banned_until``/:meth:`is_banned` reflect *past* failures (already acted
    on) -- a strategy function only ever sees non-banned states to begin with,
    via the ``available`` list it's called with."""

    url: str
    uses: int = 0
    consecutive_failures: int = 0
    banned_until: float = 0.0

    def is_banned(self, now: float) -> bool:
        return now < self.banned_until


#: Kept for anyone already importing the old private name.
_ProxyState = ProxyState

#: A custom strategy: given the currently available (non-banned) proxies and
#: the request's domain (``None`` if the caller didn't supply one), return
#: which of ``available`` to use. Must return one of the objects it was
#: given -- returning anything else raises ``ValueError`` when the strategy
#: runs, so a buggy strategy fails loudly rather than silently corrupting
#: usage stats.
ProxyStrategyFn = Callable[[Sequence[ProxyState], "str | None"], ProxyState]

_STRATEGY_REGISTRY: dict[str, ProxyStrategyFn] = {}


def register_proxy_strategy(name: str, fn: ProxyStrategyFn) -> None:
    """Register ``fn`` under ``name`` so ``ProxyRotator(strategy=name)`` --
    including from ``Settings.proxy_strategy``, a plain string -- uses it.
    ``name`` must not collide with a built-in strategy name. Registering the
    same name again replaces the previous function (handy for reloading code
    in a REPL); use :func:`unregister_proxy_strategy` to remove it outright.
    """
    if name in BUILTIN_STRATEGIES:
        raise ValueError(f"{name!r} is a built-in strategy name; choose another.")
    if not callable(fn):
        raise TypeError(f"strategy {name!r} must be callable, got {type(fn).__name__}")
    _STRATEGY_REGISTRY[name] = fn


def unregister_proxy_strategy(name: str) -> None:
    """Remove a strategy registered with :func:`register_proxy_strategy`.
    Does nothing if ``name`` isn't registered."""
    _STRATEGY_REGISTRY.pop(name, None)


def list_proxy_strategies() -> list[str]:
    """Every usable strategy name: the four built-ins, then registered custom
    ones in registration order."""
    return [*BUILTIN_STRATEGIES, *_STRATEGY_REGISTRY]


class ProxyStrategyError(ValueError):
    """A custom strategy function misbehaved: raised an exception (chained as
    ``__cause__``) or returned something other than one of the ``available``
    proxies it was called with."""


class ProxyRotator:
    """Stateful rotation over a fixed list of proxy URLs.

    Args:
        proxies: proxy URLs, e.g. ``["http://host:port", "http://u:p@host:port"]``.
        strategy: a built-in name, a name registered with
            :func:`register_proxy_strategy`, or a strategy function passed
            directly (see the module docstring for both).
            - ``random``: uniform random choice among non-banned proxies.
            - ``round_robin``: cycles through non-banned proxies in order.
            - ``least_used``: picks the non-banned proxy with the fewest uses
              (spreads load evenly -- useful when proxies are metered/paid).
            - ``sticky``: same proxy for the same domain for as long as it
              keeps working; only rotates that domain off it after a ban.
        ban_threshold: consecutive failures before a proxy is put in cool-down.
        ban_seconds: how long a banned proxy is skipped before being retried.
    """

    def __init__(
        self,
        proxies: list[str],
        strategy: str | ProxyStrategyFn = "random",
        ban_threshold: int = 3,
        ban_seconds: float = 300.0,
    ) -> None:
        self._strategy_name, self._custom_strategy = self._resolve_strategy(strategy)
        self._ban_threshold = max(1, ban_threshold)
        self._ban_seconds = max(0.0, ban_seconds)
        self._states: dict[str, ProxyState] = {url: ProxyState(url) for url in proxies}
        self._order: list[str] = list(proxies)
        self._rr_index = 0
        self._sticky: dict[str, str] = {}  # domain -> proxy url

    @staticmethod
    def _resolve_strategy(strategy: str | ProxyStrategyFn) -> tuple[str, ProxyStrategyFn | None]:
        """``(name, custom_fn)``: ``custom_fn`` is ``None`` for a built-in
        strategy (the fast, well-tested paths in :meth:`get_proxy` handle
        those directly) and set for anything else -- a registered name or a
        callable passed straight in."""
        if callable(strategy) and not isinstance(strategy, str):
            return getattr(strategy, "__name__", "<custom>"), strategy
        if strategy in BUILTIN_STRATEGIES:
            return strategy, None
        if strategy in _STRATEGY_REGISTRY:
            return strategy, _STRATEGY_REGISTRY[strategy]
        known = list_proxy_strategies()
        raise ValueError(
            f"strategy must be one of {known} or a callable, got {strategy!r}. "
            "Register a custom strategy first with register_proxy_strategy(), "
            "or pass the function directly as strategy=."
        )

    @property
    def strategy(self) -> str:
        """The strategy's name (a built-in, a registered name, or the
        function's ``__name__`` for one passed in directly)."""
        return self._strategy_name

    @classmethod
    def from_file(
        cls,
        path: str | Path,
        strategy: str | ProxyStrategyFn = "random",
        ban_threshold: int = 3,
        ban_seconds: float = 300.0,
    ) -> ProxyRotator:
        """Load one proxy URL per line (blank lines and ``#`` comments ignored)."""
        lines = Path(path).read_text(encoding="utf-8").splitlines()
        proxies = [
            line.strip() for line in lines if line.strip() and not line.strip().startswith("#")
        ]
        return cls(proxies, strategy=strategy, ban_threshold=ban_threshold, ban_seconds=ban_seconds)

    def __len__(self) -> int:
        return len(self._states)

    @property
    def available_count(self) -> int:
        now = time.monotonic()
        return sum(1 for s in self._states.values() if not s.is_banned(now))

    def _available(self) -> list[ProxyState]:
        now = time.monotonic()
        return [s for s in self._states.values() if not s.is_banned(now)]

    def get_proxy(self, domain: str | None = None) -> str | None:
        """Return a proxy URL to use next, or ``None`` if the pool is empty
        or every proxy is currently banned.

        Raises:
            ProxyStrategyError: a custom strategy function raised, or
                returned something other than one of the available proxies
                it was called with.
        """
        available = self._available()
        if not available:
            return None

        if self._custom_strategy is not None:
            chosen = self._run_custom_strategy(available, domain)
        elif self._strategy_name == "sticky" and domain is not None:
            chosen = self._sticky_choice(available, domain)
        elif self._strategy_name == "random":
            chosen = random.choice(available)
        elif self._strategy_name == "least_used":
            chosen = min(available, key=lambda s: s.uses)
        elif self._strategy_name == "round_robin":
            chosen = self._next_round_robin(available)
        else:  # "sticky" with domain=None: nothing to key stickiness on
            chosen = random.choice(available)

        chosen.uses += 1
        return chosen.url

    def _run_custom_strategy(self, available: list[ProxyState], domain: str | None) -> ProxyState:
        assert self._custom_strategy is not None
        try:
            chosen = self._custom_strategy(available, domain)
        except Exception as exc:
            raise ProxyStrategyError(
                f"Proxy strategy {self._strategy_name!r} raised {type(exc).__name__}: {exc}"
            ) from exc
        # Identity, not equality: ProxyState is a plain (unfrozen) dataclass, so
        # a strategy returning a *different* but field-equal ProxyState (e.g. a
        # fresh, default-valued one it built itself) would otherwise pass this
        # check despite not being one of the live objects it was handed.
        if not isinstance(chosen, ProxyState) or not any(chosen is p for p in available):
            raise ProxyStrategyError(
                f"Proxy strategy {self._strategy_name!r} must return one of the ProxyState "
                f"objects it was called with; got {chosen!r}."
            )
        return chosen

    def _sticky_choice(self, available: list[ProxyState], domain: str) -> ProxyState:
        current = self._sticky.get(domain)
        if current is not None:
            state = self._states.get(current)
            if state is not None and not state.is_banned(time.monotonic()):
                return state
        chosen = random.choice(available)
        self._sticky[domain] = chosen.url
        return chosen

    def _next_round_robin(self, available: list[ProxyState]) -> ProxyState:
        available_urls = {s.url for s in available}
        for _ in range(len(self._order)):
            candidate = self._order[self._rr_index % len(self._order)]
            self._rr_index += 1
            if candidate in available_urls:
                return self._states[candidate]
        # Shouldn't happen since `available` is non-empty, but stay safe.
        return available[0]

    def report_success(self, proxy_url: str) -> None:
        state = self._states.get(proxy_url)
        if state is not None:
            state.consecutive_failures = 0

    def report_failure(self, proxy_url: str) -> None:
        state = self._states.get(proxy_url)
        if state is None:
            return
        state.consecutive_failures += 1
        if state.consecutive_failures >= self._ban_threshold:
            state.banned_until = time.monotonic() + self._ban_seconds

    def status(self) -> dict[str, dict[str, float | int | bool]]:
        """A snapshot of every proxy's usage/ban state, for monitoring/debugging."""
        now = time.monotonic()
        return {
            url: {
                "uses": s.uses,
                "consecutive_failures": s.consecutive_failures,
                "banned": s.is_banned(now),
                "banned_seconds_left": max(0.0, s.banned_until - now),
            }
            for url, s in self._states.items()
        }
