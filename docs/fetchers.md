# Direct HTTP requests: `Fetcher`

A lightweight HTTP client for when you just want to hit a URL or an API --
no spider, engine, scheduler, or pipeline.

```python
from bitscrape import Fetcher

page = Fetcher.get("https://example.com")
print(page.status, page.css("h1::text").get())

api = Fetcher.post("https://example.com/api", json={"q": "widgets"})
print(api.json())
```

## Four classes, one behaviour

| Class | Style | Lifetime |
|---|---|---|
| `Fetcher` | sync | one client per call (`requests.get()` style) |
| `AsyncFetcher` | async | one client per call |
| `FetcherSession` | sync | one client for the `with` block -- cookies and connections persist |
| `AsyncFetcherSession` | async | same, for `async with` |

```python
from bitscrape import FetcherSession

with FetcherSession() as s:
    s.post("https://example.com/login", data={"user": "me", "pw": "x"})
    account = s.get("https://example.com/account")   # same cookies, still logged in
```

```python
from bitscrape import AsyncFetcherSession
import asyncio

async def main():
    async with AsyncFetcherSession() as s:
        pages = await asyncio.gather(*[s.get(f"https://example.com/{i}") for i in range(10)])

asyncio.run(main())
```

Every call returns a `ParsedResponse` -- the same object a spider callback
receives: `.css()`, `.xpath()`, `.json()`, `.text`, `.status`, `.ok`,
`.headers`, `.http_version`, `.request`.

## What this is, and isn't

`Fetcher` is a plain, honest HTTP client. It sends an ordinary `User-Agent`
and the ordinary `Settings.default_headers` (see below) -- it does not
imitate a browser's TLS handshake or header fingerprint. `impersonate=`,
`stealthy_headers=`, `http3=`, `fingerprint=` and similar options raise
`NotImplementedError` with a pointer to `ROADMAP.md`'s "Explicit non-goals"
(the same policy `sessions.py` already applies to `HttpSession`/
`BrowserSession`) -- Bitscrape does not implement browser-fingerprint
evasion, regardless of which corner of the API is asked for it.

It's also a **direct** client, not a crawler: no robots.txt check, no
per-domain pacing. For a polite, rate-limited crawl of a whole site, write a
`Spider` -- see `docs/robots.md` and `docs/autothrottle.md`.

## Protocols: real HTTP/2, no HTTP/3

```python
Fetcher.get("https://example.com", protocol="http2")
```

`protocol="http1"` (default) uses `aiohttp` -- nothing extra to install.
`protocol="http2"` uses `httpx` (`pip install bitscrape[http2]`) and *offers*
HTTP/2 through the standard TLS ALPN extension -- exactly what `curl --http2`
or any browser does; it is a real, negotiated protocol capability, not a
fingerprint. If the server doesn't offer HTTP/2 -- or the URL is plain
`http://`, where HTTP/2 is never negotiated -- the connection falls back to
HTTP/1.1 automatically, and `response.http_version` (`"1.1"` or `"2"`)
always tells you which one actually happened; nothing is silently faked.

HTTP/3 (QUIC) is not supported -- `protocol="http3"` raises a clear error
rather than silently falling back.

## Requests

```python
Fetcher.get(url, params={"q": "widgets", "tag": ["a", "b"]})   # ?q=widgets&tag=a&tag=b
Fetcher.post(url, json={"a": 1})                                # sets Content-Type: application/json
Fetcher.post(url, data={"user": "me"})                          # application/x-www-form-urlencoded
Fetcher.post(url, data=b"raw bytes")
Fetcher.get(url, auth=("user", "password"))                     # HTTP Basic
Fetcher.get(url, headers={"X-Api-Key": "..."})
Fetcher.get(url, timeout=5.0)
Fetcher.get(url, follow_redirects=False)
```

`put`, `patch`, `delete`, `head`, `options`, and the generic `request(method, url, ...)` all work the same way.

## Headers: ordinary hygiene, not impersonation

Every request carries `Settings.default_headers` (`Accept`,
`Accept-Language`) plus `Settings.user_agent`, merged under whatever you pass
explicitly:

```python
from bitscrape import Settings

settings = Settings(default_headers={"Accept-Language": "de-DE"})
Fetcher.get(url, settings=settings)
```

This is one fixed, honest default -- not a per-browser profile chosen to make
the request look like it came from somewhere else. The same setting also
applies to the full spider/engine path (`Downloader`, `HttpSession`), so a
spider gets the same reasonable defaults a one-off `Fetcher` call does.

## Cookies

A one-off `Fetcher` call keeps no cookies between calls -- each is its own
client. `FetcherSession`/`AsyncFetcherSession` keep a cookie jar for the
whole `with` block:

```python
with FetcherSession() as s:
    s.get("https://example.com/set-session")
    s.get("https://example.com/account")  # sends the cookie the first call received
```

`cookies={...}` on a single call adds to (without replacing) the session's
stored cookies, for that call only.

### Inspecting, seeding and persisting cookies

```python
with FetcherSession(cookies={"seed": "x"}) as s:   # start with cookies already set
    s.get("https://example.com/login")
    print(s.cookies())                              # {"seed": "x", "sess": "...", ...}
    s.set_cookies({"token": "abc"})                  # add one without a Set-Cookie header
    s.save_cookies("cookies.json")                   # write the jar to disk

with FetcherSession() as s:                          # a later run, or a different session
    s.load_cookies("cookies.json")                   # resume logged in
    s.clear_cookies()                                 # or: forget everything (log out)
```

`save_cookies`/`load_cookies` write a flat `{name: value}` JSON snapshot --
domain, path and expiry aren't preserved, the same simplification
`HttpSession.cookies()` (the spider-side equivalent) uses, which suits a
session that stays on one site: save a login, resume it later. `AsyncFetcherSession`
has the same methods, `await`ed. With `protocol="http2"`, `cookies(url=...)`
returns every cookie in the jar regardless of `url` -- httpx has no per-URL
filter the way aiohttp does.

## Retries

Connection errors, timeouts, and `429`/`500`/`502`/`503`/`504` responses are
retried with exponential backoff, honouring `Retry-After` when the server
sends one (capped by `Settings.max_retry_after_seconds`) -- but **only for
idempotent methods** (`GET`, `HEAD`, `OPTIONS`, `PUT`, `DELETE`) by default,
since retrying a `POST` after a `503` can repeat whatever it did the first
time. Widen or narrow this explicitly:

```python
Fetcher.get(url, max_retries=3, retry_backoff=1.0)
Fetcher.post(url, max_retries=2, retry_methods={"POST"})   # opt a POST in
Fetcher.get(url, retry_statuses={418})                     # customise which statuses retry
```

When retries run out on a *status*, the last response is returned --
check `.status`/`.ok` yourself. When they run out on a *network* error,
`bitscrape.FetchError` is raised (`.url`, `.retryable`, and `__cause__` for
the underlying library exception). A redirect loop or a malformed URL is
never retried (`retryable=False`) -- more attempts can't fix either.

## Errors

```python
from bitscrape import FetchError

try:
    Fetcher.get("https://this-does-not-resolve.example", max_retries=1)
except FetchError as e:
    print(e.url, e.retryable, e.__cause__)
```

## `verify_ssl`

```python
Fetcher.get(url, verify_ssl=False)              # skip certificate verification
Fetcher.get(url, verify_ssl=my_ssl_context)      # trust a private/internal CA
```

## Async inside an already-running loop

`Fetcher`/`FetcherSession` are synchronous and raise a clear `RuntimeError`
if called from inside a running event loop (an `async def`, a Jupyter cell,
a spider callback) -- use `AsyncFetcher`/`AsyncFetcherSession` there instead.
