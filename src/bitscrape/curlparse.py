"""
curl → Request
================

Paste a curl command copied from a browser's "Copy as cURL" (DevTools
Network tab) and get back a bitscrape :class:`~bitscrape.core.models.Request`
with the same method, URL, headers, and body -- instead of retyping all of
it by hand. Handles the common flags curl-copy features actually emit
(``-X``/``--request``, ``-H``/``--header``, ``-d``/``--data``/``--data-raw``/
``--data-binary``, ``-b``/``--cookie``, ``-A``/``--user-agent``,
``-u``/``--user``); anything else is ignored rather than raising, since a
copied curl command often carries flags (``--compressed``, ``-k``) that
don't map onto a ``Request`` at all.

Used by ``bitscrape shell`` (as the ``from_curl`` helper in its namespace)
and usable directly::

    from bitscrape.curlparse import curl_to_request
    req = curl_to_request('''curl 'https://api.example.com/search' \\
        -H 'accept: application/json' \\
        --data-raw '{"q":"widgets"}' ''')
"""

from __future__ import annotations

import re
import shlex
from typing import Any

from bitscrape.core.models import Request

_DATA_FLAGS = frozenset({"-d", "--data", "--data-raw", "--data-binary", "--data-ascii"})
_HEADER_FLAGS = frozenset({"-H", "--header"})
_METHOD_FLAGS = frozenset({"-X", "--request"})
_COOKIE_FLAGS = frozenset({"-b", "--cookie"})
_USER_AGENT_FLAGS = frozenset({"-A", "--user-agent"})
_USER_FLAGS = frozenset({"-u", "--user"})

# Flags curl-copy commonly emits that take a value we don't use for a
# Request, but which we must still consume (skip the value) rather than
# accidentally treating the value as the URL.
_IGNORED_VALUE_FLAGS = frozenset(
    {"--compressed", "-k", "--insecure", "-s", "--silent", "-i", "--include", "-L", "--location"}
)


def curl_to_request(curl_command: str, **request_kwargs: Any) -> Request:
    """Parse a curl command string into a :class:`Request`.

    Args:
        curl_command: the full command, e.g. copied from a browser's
            "Copy as cURL". A leading ``curl`` token is optional.
        request_kwargs: extra keyword arguments forwarded to ``Request(...)``,
            letting you override/add things (e.g. ``callback="parse_item"``)
            without a second parsing pass.
    """
    # Normalise shell line-continuations ("...\<newline>...") into a single
    # line before tokenizing -- shlex doesn't treat a backslash-newline the
    # way a real shell does, and "Copy as cURL" output is usually multi-line.
    normalized = re.sub(r"\\\r?\n", " ", curl_command.strip())
    tokens = shlex.split(normalized)
    if tokens and tokens[0] == "curl":
        tokens = tokens[1:]

    method: str | None = None
    headers: dict[str, str] = {}
    body: bytes | None = None
    url: str | None = None
    cookie_value: str | None = None

    i = 0
    while i < len(tokens):
        token = tokens[i]

        if token in _METHOD_FLAGS:
            i += 1
            method = tokens[i].upper()
        elif token in _HEADER_FLAGS:
            i += 1
            key, _, value = tokens[i].partition(":")
            headers[key.strip()] = value.strip()
        elif token in _DATA_FLAGS:
            i += 1
            body = tokens[i].encode("utf-8")
            if method is None:
                method = "POST"
        elif token in _COOKIE_FLAGS:
            i += 1
            cookie_value = tokens[i]
        elif token in _USER_AGENT_FLAGS:
            i += 1
            headers["User-Agent"] = tokens[i]
        elif token in _USER_FLAGS:
            i += 1  # consume the "user:pass" value; not mapped onto Request
        elif token in _IGNORED_VALUE_FLAGS:
            pass  # boolean-style flag, no value to consume
        elif token.startswith("-"):
            # Unknown flag -- if the *next* token looks like this flag's
            # value (not another flag and not the URL-looking last token),
            # be conservative and leave it for the next loop iteration
            # rather than guessing; unknown flags are simply dropped.
            pass
        else:
            url = token

        i += 1

    if url is None:
        raise ValueError("Could not find a URL in the curl command")
    if cookie_value:
        headers.setdefault("Cookie", cookie_value)

    return Request(
        url=url,
        method=method or "GET",
        headers=headers,
        body=body,
        **request_kwargs,
    )
