# Proxy rotation

`ProxyRotator` turns a flat list of proxy URLs into a stateful pool, drawn
from automatically instead of picking one proxy by hand. It tracks
consecutive failures per proxy and temporarily bans one that fails too many
times in a row, so a crawl doesn't keep hammering a dead or blocked proxy.

```python
from bitscrape.downloader.proxy import ProxyRotator

rotator = ProxyRotator(["http://p1:8080", "http://p2:8080"], strategy="round_robin")
proxy = rotator.get_proxy(domain="example.com")   # None if the pool is empty/exhausted
...
rotator.report_success(proxy)
# or, on a failed/blocked response:
rotator.report_failure(proxy)
```

This is deliberately just rotation/bookkeeping over proxies *you already
have* -- it doesn't source, buy or verify proxies, and does nothing to
disguise that traffic is going through one.

## Built-in strategies

- `random` -- uniform random choice among non-banned proxies.
- `round_robin` -- cycles through non-banned proxies in order.
- `least_used` -- the non-banned proxy with the fewest uses (spreads load
  evenly, useful when proxies are metered/paid).
- `sticky` -- the same proxy for the same domain for as long as it keeps
  working; only rotates that domain off it after a ban.

## Custom strategies

A strategy is a plain function of the currently available (non-banned)
proxies and the request's domain, returning the one to use:

```python
from bitscrape.downloader.proxy import ProxyRotator, ProxyState

def shortest_queue(available: list[ProxyState], domain: str | None) -> ProxyState:
    return min(available, key=lambda p: p.uses)

rotator = ProxyRotator(["http://p1:8080", "http://p2:8080"], strategy=shortest_queue)
```

It must return one of the `ProxyState` objects it was actually given;
anything else -- or an exception raised inside it -- becomes a
`ProxyStrategyError` when the rotator is asked for a proxy.

To select a custom strategy by **name** (e.g. from `Settings.proxy_strategy`,
a plain string field that can't hold a function), register it first:

```python
from bitscrape.downloader.proxy import register_proxy_strategy
from bitscrape.core.settings import Settings

register_proxy_strategy("shortest_queue", shortest_queue)
settings = Settings(proxies=["http://p1:8080", "http://p2:8080"], proxy_strategy="shortest_queue")
```

`unregister_proxy_strategy(name)` removes one; `list_proxy_strategies()`
lists every usable name, built-in and registered.

## Every session type

| Session | How a proxy is chosen |
|---|---|
| `HttpSession` / `AsyncFetcherSession` (`protocol="http1"`) | fresh, per request (and per retry attempt) |
| `AsyncFetcherSession` (`protocol="http2"`) | once, at construction -- httpx can't switch proxy mid-connection |
| `BrowserSession` / `DynamicSession` | once, when the browser launches -- a browser can't switch proxy mid-session |

```python
from bitscrape.sessions import HttpSession
from bitscrape.fetchers import AsyncFetcherSession
from bitscrape import DynamicSession

HttpSession(proxy_rotator=rotator)                           # a fresh proxy every request
AsyncFetcherSession(proxy_rotator=rotator)                    # same, for direct HTTP fetches
AsyncFetcherSession(protocol="http2", proxy_rotator=rotator)  # one proxy for this session
DynamicSession(proxy_rotator=rotator)                         # one proxy for this browser
```

For a browser or an HTTP/2 session, "rotation" means each *new* session (or
each one-shot `DynamicFetcher.fetch()` call, which launches a fresh browser
every time) gets a turn from the rotator -- not that one long-lived session
switches proxies partway through. Either way, every fetch still reports
success or failure back to the rotator, so a session stuck on a proxy that
starts failing gets it banned for the *next* launch, even though this one
keeps using it.

`proxy=` and `proxy_rotator=` are mutually exclusive on every session type.

## Per-request override

Set `request.meta["proxy"]` to skip the rotator entirely for one request.
