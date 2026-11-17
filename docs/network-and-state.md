# Network capture, saved state and shadow DOM

New in 0.8.1 for browser sessions (`AsyncDynamicSession`, `DynamicSession`, `BrowserSession`).

## Background API capture (`record_requests`)

```python
from bitscrape import AsyncDynamicSession

async with AsyncDynamicSession(record_requests=True) as s:
    await s.fetch("https://example.com/app", network_idle=True)

    for r in s.network.search(url_pattern=r"/api/", resource_type="fetch", limit=None):
        print(r.network_id, r.status, r.url, r.json())

    prices = s.network.find_json("price", url_pattern=r"/api/products")
    s.network.get(7)          # one saved response by id (None if evicted)
    s.network.clear()         # ids keep increasing, never reused
```

`search()` / `get()` / `clear()` are plain calls, also on async sessions.
Arguments of `search`: `url_pattern` (regex), `method`, `resource_type`,
`status` (int, iterable or predicate), `after_id`, `limit` (default 100,
`None` = all), `include_static=False` (XHR/fetch + HTTP errors only).

A `RecordedResponse` has `network_id, url, method, status, headers,
request_headers, request_body, resource_type, body, body_note, page_url`,
plus `.text`, `.json()`, `.css()`, `.xpath()`, `.parsed()` and `.meta`.

**Bounds.** At most `max_recorded_requests` entries (default 1,000), 1 MiB per
body and 20 MiB of bodies in total; oldest entries are evicted first. Only
text-like bodies (text, JSON, XML, JavaScript, GraphQL, `+json`/`+xml`, forms)
are saved; others keep metadata and a `body_note`. Failed requests are not
recorded.

`s.network.to_har()` exports a HAR 1.2 dict for browser devtools.

In spiders: `Settings(browser_record_requests=True)` or a session config entry
`{"kind": "browser", "record_requests": True}`, then
`manager.get("render").network.search(...)`.

`capture_xhr=` (per-fetch, into `response.extra["captured_xhr"]`) still exists
for the simple "give me the XHR of *this* page" case.

## Saved login state

```python
async with AsyncDynamicSession() as s:
    await s.fetch("https://example.com/login", page_action=sign_in)
    await s.save_state("login.json")          # cookies + localStorage + IndexedDB

async with AsyncDynamicSession() as s:
    await s.load_state("login.json")           # cookies + localStorage
    page = await s.fetch("https://example.com/account")
```

`login.json` is Playwright's storage-state format, so
`AsyncDynamicSession(storage_state="login.json")` restores *everything*,
including IndexedDB (a live context can't have IndexedDB injected, so
`load_state` logs a warning if the file contains any). Treat the file like a
password: it grants the logged-in session.

## Shadow DOM

`pierce_shadow=True` (session default, or per fetch) returns HTML where every
**open** shadow root is inlined as a `<shadow-root>` child of its host:

```python
page = await s.fetch(url, pierce_shadow=True)
page.css("x-card shadow-root .title::text").get()
```

`<slot>` elements contain their assigned nodes (or fallback content); a host's
unassigned light children are omitted. Closed shadow roots and iframes are not
traversed. The live page is not modified.
