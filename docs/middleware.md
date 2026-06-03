# Middleware

Middleware intercepts requests before they are sent and responses before they
reach your spider. This is where you add cross-cutting behaviour like
user-agent rotation, cookie handling, and robots.txt compliance.

---

## How Middleware Works

```
Request  →  [MW1] → [MW2] → [MW3] → Downloader
Response ←  [MW3] → [MW2] → [MW1] → Spider.parse()
```

- Requests flow **forward** through middleware in list order
- Responses flow **backward** through middleware in reverse order
- Any middleware can modify, replace, or short-circuit a request/response

---

## Built-in Middleware

### UserAgentMiddleware

Sets the User-Agent header on every request. Optionally rotates through a list.

```python
from bitscrape import UserAgentMiddleware

# Fixed user agent (uses settings.user_agent)
mw = UserAgentMiddleware()

# Rotate through a custom list
mw = UserAgentMiddleware(
    user_agents=[
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/124.0",
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) Chrome/123.0",
        "Mozilla/5.0 (X11; Linux x86_64) Firefox/124.0",
    ],
    rotate=True,
)
```

### RobotsMiddleware

Downloads each domain's `robots.txt` and blocks disallowed URLs automatically.

```python
from bitscrape import RobotsMiddleware

mw = RobotsMiddleware()
```

Controlled by the `robotstxt_obey` setting:

```python
settings = bitscrape.Settings(robotstxt_obey=True)   # enabled (default)
settings = bitscrape.Settings(robotstxt_obey=False)  # disabled
```

### CookieMiddleware

Maintains a per-domain cookie jar. Reads `Set-Cookie` response headers and
sends cookies on subsequent requests to the same domain.

```python
from bitscrape import CookieMiddleware

mw = CookieMiddleware()
```

---

## Default Middleware Stack

When you call `bitscrape.run()`, these middlewares are active by default:

```python
[
    RobotsMiddleware(),      # only if robotstxt_obey=True
    UserAgentMiddleware(),
    CookieMiddleware(),
]
```

---

## Writing Custom Middleware

Subclass `bitscrape.BaseMiddleware` and override any of the three hook methods:

```python
import bitscrape
from bitscrape.core.models import Request, Response

class MyMiddleware(bitscrape.BaseMiddleware):

    async def process_request(self, request: Request, spider) -> Request | Response | None:
        """
        Called before every request is sent.
        Return:
          - None          → continue (use request as-is)
          - Request       → use this modified request instead
          - Response      → skip download, use this response directly
        """
        return None

    async def process_response(self, request: Request, response: Response, spider) -> Response | Request | None:
        """
        Called after every response is received.
        Return:
          - Response      → continue to spider with this response
          - Request       → re-fetch this new request instead
          - None          → drop the response entirely
        """
        return response

    async def process_exception(self, request: Request, exc: Exception, spider) -> Request | Response | None:
        """
        Called when a download error occurs.
        Return:
          - None          → re-raise the exception
          - Request       → retry with this request
          - Response      → use this response instead of erroring
        """
        return None
```

---

## Custom Middleware Examples

### Add authentication headers

```python
class AuthMiddleware(bitscrape.BaseMiddleware):
    def __init__(self, token: str):
        self._token = token

    async def process_request(self, request, spider):
        headers = {**request.headers, "Authorization": f"Bearer {self._token}"}
        return request.model_copy(update={"headers": headers})
```

### Log all requests

```python
import logging

class LoggingMiddleware(bitscrape.BaseMiddleware):
    def __init__(self):
        self._log = logging.getLogger("bitscrape.requests")

    async def process_request(self, request, spider):
        self._log.info("→ %s %s", request.method, request.url)
        return None

    async def process_response(self, request, response, spider):
        self._log.info("← %d %s (%.0fms)",
                       response.status, response.url, response.elapsed_ms)
        return response
```

### Retry on 403 with a different user agent

```python
import random

AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/124.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) Safari/537.36",
]

class RetryOn403Middleware(bitscrape.BaseMiddleware):
    async def process_response(self, request, response, spider):
        if response.status == 403 and request.retries < 2:
            new_req = request.model_copy(update={
                "retries": request.retries + 1,
                "headers": {**request.headers, "User-Agent": random.choice(AGENTS)},
            })
            return new_req   # re-fetch with new headers
        return response
```

### Block requests to certain domains

```python
class BlocklistMiddleware(bitscrape.BaseMiddleware):
    BLOCKED = {"ads.example.com", "tracker.example.com"}

    async def process_request(self, request, spider):
        from urllib.parse import urlparse
        domain = urlparse(request.url).netloc
        if domain in self.BLOCKED:
            from bitscrape.core.models import Response
            # Return a fake empty response to skip download
            return Response(
                url=request.url, status=200,
                body=b"", request=request,
            )
        return None
```

---

## Using Custom Middleware

Pass middleware to `bitscrape.run()` or `Engine`:

```python
import bitscrape

stats = bitscrape.run(
    MySpider,
    middlewares=[
        bitscrape.RobotsMiddleware(),
        AuthMiddleware(token="my-api-token"),
        LoggingMiddleware(),
        bitscrape.UserAgentMiddleware(rotate=True),
        bitscrape.CookieMiddleware(),
    ],
)
```

Order matters: middlewares are applied left-to-right for requests,
right-to-left for responses.
