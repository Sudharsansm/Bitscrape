"""
Domain / ad / resource-type blocking (browser fetches)
=======================================================

Cuts noise and bandwidth out of Playwright-rendered pages by refusing to
load requests to known ad/tracker domains, a custom domain blocklist, or
whole resource types (images, fonts, media) that a scraper usually doesn't
need. This is a performance/hygiene feature, not an anti-detection one --
it makes crawls faster and lighter, and has nothing to do with how the
*target* site perceives the request that does go through.

``block_ads=True`` uses the bundled **core list** (``bitscrape/data/ad_domains.txt``,
~3,900 high-confidence ad/tracker domains: those listed by two independent
MIT-licensed community lists, plus the curated :data:`BUILTIN_AD_DOMAINS`).
Subdomains of every entry are matched. Matching is a set lookup along the
host's dotted suffixes -- O(labels), not O(list size) -- so a big blocklist
costs nothing per request.

Extend it with ``block_domains`` (your own list), ``block_hosts_file`` (one
domain per line, or hosts-file format) or refresh/extend the bundled data
with :func:`update_blocklist` / ``bitscrape blocklist update``.

Usage with Playwright::

    blocker = DomainBlocker(block_ads=True, extra_domains=["example-tracker.com"])
    await page.route("**/*", blocker.route_handler)
"""

from __future__ import annotations

import contextlib
import os
import re
import tempfile
import threading
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

# A small, curated set of widely-known ad/analytics/tracker domains.
# Not exhaustive -- see module docstring.
BUILTIN_AD_DOMAINS: frozenset[str] = frozenset(
    {
        "doubleclick.net",
        "googlesyndication.com",
        "googleadservices.com",
        "google-analytics.com",
        "googletagmanager.com",
        "googletagservices.com",
        "adservice.google.com",
        "adnxs.com",
        "advertising.com",
        "adsrvr.org",
        "criteo.com",
        "criteo.net",
        "taboola.com",
        "outbrain.com",
        "scorecardresearch.com",
        "quantserve.com",
        "moatads.com",
        "amazon-adsystem.com",
        "facebook.net",
        "connect.facebook.net",
        "pixel.facebook.com",
        "hotjar.com",
        "mixpanel.com",
        "segment.io",
        "segment.com",
        "fullstory.com",
        "newrelic.com",
        "nr-data.net",
        "bugsnag.com",
        "sentry.io",
        "cloudflareinsights.com",
        "yandex.ru/ads",
        "adform.net",
        "rubiconproject.com",
        "pubmatic.com",
        "openx.net",
        "media.net",
        "bidswitch.net",
        "casalemedia.com",
        "smartadserver.com",
    }
)


_DOMAIN_RE = re.compile(r"^[a-z0-9]([a-z0-9._-]*[a-z0-9])?$")
_SKIP_HOSTS = frozenset({"localhost", "local", "broadcasthost", "localhost.localdomain", "0.0.0.0"})


def _looks_like_ip(token: str) -> bool:
    parts = token.split(".")
    return len(parts) == 4 and all(p.isdigit() for p in parts)


def parse_blocklist(text: str) -> set[str]:
    """Parse a plain domain list (one per line) or ``/etc/hosts``-format text
    (``0.0.0.0 example.com``). Comments, blank lines, IPs, ``localhost`` and
    anything that isn't a bare hostname (paths, wildcards) are skipped."""
    domains: set[str] = set()
    for raw_line in text.splitlines():
        line = raw_line.split("#", 1)[0].strip()
        if not line:
            continue
        parts = line.split()
        # hosts format: "<ip> <domain> [aliases...]" -- take everything after
        # the first token; plain list format: the whole line is the domain.
        candidates = parts[1:] if len(parts) > 1 and _looks_like_ip(parts[0]) else parts
        for cand in candidates:
            cand = cand.lower().strip(".")
            if cand in _SKIP_HOSTS or _looks_like_ip(cand) or "." not in cand:
                continue
            if _DOMAIN_RE.match(cand):
                domains.add(cand)
    return domains


def _load_hosts_file(path: str | Path) -> set[str]:
    """Load domains from a plain list (one per line) or standard
    ``/etc/hosts``-format file (``0.0.0.0 example.com``)."""
    return parse_blocklist(Path(path).read_text(encoding="utf-8"))


_DATA_FILE = Path(__file__).resolve().parent.parent / "data" / "ad_domains.txt"
_user_override: Path | None = None
_cache_lock = threading.Lock()
_cached: tuple[tuple[str, float], frozenset[str]] | None = None


def user_blocklist_path() -> Path:
    """Where :func:`update_blocklist` writes by default (``$BITSCRAPE_BLOCKLIST``
    or ``~/.cache/bitscrape/ad_domains.txt``)."""
    env = os.environ.get("BITSCRAPE_BLOCKLIST")
    if env:
        return Path(env)
    return Path.home() / ".cache" / "bitscrape" / "ad_domains.txt"


def load_ad_domains() -> frozenset[str]:
    """The ad/tracker domains ``block_ads=True`` blocks: the user's refreshed list
    (see :func:`update_blocklist`) if one exists, else the bundled core list;
    always unioned with :data:`BUILTIN_AD_DOMAINS`. Cached until the file changes."""
    global _cached
    path = user_blocklist_path()
    if not path.is_file():
        path = _DATA_FILE
    try:
        key = (str(path), path.stat().st_mtime)
    except OSError:
        return frozenset(d for d in BUILTIN_AD_DOMAINS if "/" not in d)
    with _cache_lock:
        if _cached is not None and _cached[0] == key:
            return _cached[1]
        domains = parse_blocklist(path.read_text(encoding="utf-8", errors="ignore"))
        domains |= {d for d in BUILTIN_AD_DOMAINS if "/" not in d}
        result = frozenset(domains)
        _cached = (key, result)
        return result


def update_blocklist(
    sources: list[str] | None = None,
    *,
    dest: str | Path | None = None,
    timeout: float = 60.0,
    mode: str = "union",
) -> int:
    """Download community blocklists and save the merged result for ``block_ads``.

    Args:
        sources: URLs of hosts-format/plain lists. Default: StevenBlack/hosts
            and anudeepND/blacklist (both MIT-licensed).
        dest: output file (default :func:`user_blocklist_path`).
        mode: ``"union"`` (everything any source lists -- ~100k domains, widest
            coverage) or ``"intersection"`` (only domains every source lists --
            the high-confidence core set, which is what ships in the package).

    Returns the number of domains written. Blocking is unaffected until the
    next process start (or the next :func:`load_ad_domains` call, which notices
    the new file).
    """
    import urllib.request

    if mode not in ("union", "intersection"):
        raise ValueError("mode must be 'union' or 'intersection'")
    urls = sources or [
        "https://raw.githubusercontent.com/StevenBlack/hosts/master/hosts",
        "https://raw.githubusercontent.com/anudeepND/blacklist/master/adservers.txt",
    ]
    sets: list[set[str]] = []
    for url in urls:
        if not url.startswith(("http://", "https://")):
            raise ValueError(f"blocklist source must be an http(s) URL: {url!r}")
        with urllib.request.urlopen(url, timeout=timeout) as resp:  # noqa: S310 -- scheme checked above
            sets.append(parse_blocklist(resp.read().decode("utf-8", errors="ignore")))
    merged = set.intersection(*sets) if mode == "intersection" else set.union(*sets)
    out = Path(dest) if dest else user_blocklist_path()
    out.parent.mkdir(parents=True, exist_ok=True)
    body = "# BitScrape ad/tracker list (refreshed)\n# sources: " + ", ".join(urls) + "\n"
    body += "\n".join(sorted(merged)) + "\n"
    fd, tmp = tempfile.mkstemp(dir=str(out.parent), prefix=".blocklist-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(body)
        os.replace(tmp, out)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise
    return len(merged)


def _domain_matches(host: str, blocked_domain: str) -> bool:
    host = host.lower()
    blocked_domain = blocked_domain.lower()
    return host == blocked_domain or host.endswith("." + blocked_domain)


class DomainBlocker:
    """Decides whether a browser-issued request should be blocked, and
    supplies a ready-to-use Playwright ``route`` handler.

    Args:
        block_ads: include the bundled ad/tracker list (:func:`load_ad_domains`).
        extra_domains: additional domains (and their subdomains) to block.
        hosts_file: optional path to load more domains from (see
            :func:`parse_blocklist`).
        block_resource_types: Playwright resource types to block outright,
            e.g. ``["image", "font", "media"]`` -- independent of domain.
        allow_domains: domains (and subdomains) that are *never* blocked, even
            if a list contains them -- the escape hatch for a false positive.

    :attr:`blocked_count` counts requests this blocker has refused.
    """

    def __init__(
        self,
        block_ads: bool = False,
        extra_domains: list[str] | None = None,
        hosts_file: str | None = None,
        block_resource_types: list[str] | None = None,
        allow_domains: list[str] | None = None,
    ) -> None:
        domains: set[str] = set(load_ad_domains()) if block_ads else set()
        domains.update(d.strip().lower().lstrip(".") for d in (extra_domains or []) if d.strip())
        if hosts_file:
            domains.update(_load_hosts_file(hosts_file))
        self._domains = {d.lower() for d in domains}
        self._allow = {d.strip().lower().lstrip(".") for d in (allow_domains or []) if d.strip()}
        self._resource_types = {t.lower() for t in (block_resource_types or [])}
        self.blocked_count = 0

    @property
    def domain_count(self) -> int:
        return len(self._domains)

    @staticmethod
    def _suffixes(host: str) -> list[str]:
        labels = host.lower().strip(".").split(".")
        return [".".join(labels[i:]) for i in range(len(labels))]

    def is_blocked_url(self, url: str) -> bool:
        host = urlparse(url).hostname or ""
        if not host:
            return False
        suffixes = self._suffixes(host)
        if self._allow and any(s in self._allow for s in suffixes):
            return False
        return any(s in self._domains for s in suffixes)

    def is_blocked_resource_type(self, resource_type: str) -> bool:
        return resource_type.lower() in self._resource_types

    def should_block(self, url: str, resource_type: str | None = None) -> bool:
        if resource_type is not None and self.is_blocked_resource_type(resource_type):
            return True
        return self.is_blocked_url(url)

    async def route_handler(self, route: Any) -> None:
        """Drop-in handler for ``page.route("**/*", blocker.route_handler)``."""
        request = route.request
        if self.should_block(request.url, getattr(request, "resource_type", None)):
            self.blocked_count += 1
            await route.abort()
        else:
            await route.continue_()
