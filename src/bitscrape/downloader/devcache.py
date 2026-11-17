"""
Dev mode: cache responses on disk, replay on the next run
==========================================================

When you're iterating on a spider's ``parse()`` logic, re-fetching every
page on every run is slow and, on rate-limited or courtesy-sensitive
sites, actively unfriendly. :class:`DevModeCache` writes each response to
disk keyed by request fingerprint (method + URL + body) the first time it's
fetched, and serves that cached copy on every subsequent run instead of
hitting the network again -- until the entry expires (``dev_cache_ttl``,
0 = never) or the cache directory is cleared.

This is a *development* aid, not a general HTTP cache: it's on-disk (not
in-process, so it survives between process runs, unlike the existing
in-memory conditional-GET cache in ``Downloader``), unconditional (doesn't
care about ``Cache-Control``/``ETag`` semantics -- once written, an entry
is treated as a perfect stand-in for the live page until it expires), and
meant to be turned off before an actual production crawl (``dev_mode`` is
``False`` by default).
"""

from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path
from typing import Any


def _fingerprint(method: str, url: str, body: bytes | None) -> str:
    h = hashlib.sha256()
    h.update(method.upper().encode("utf-8"))
    h.update(b"\x00")
    h.update(url.encode("utf-8"))
    h.update(b"\x00")
    h.update(body or b"")
    return h.hexdigest()


class DevModeCache:
    """Disk-backed record/replay cache for the downloader's dev mode.

    Args:
        cache_dir: directory to store cache entries in (created if missing).
        ttl: seconds before a cached entry is considered stale and
            re-fetched. ``0`` means entries never expire.
        cacheable_statuses: only responses with one of these HTTP statuses
            are written to the cache (a cached 500 would be useless, and
            arguably actively misleading, to replay).
    """

    def __init__(
        self,
        cache_dir: str | Path,
        ttl: float = 0.0,
        cacheable_statuses: list[int] | None = None,
    ) -> None:
        self._dir = Path(cache_dir)
        self._dir.mkdir(parents=True, exist_ok=True)
        self._ttl = max(0.0, ttl)
        self._cacheable_statuses = set(cacheable_statuses or [200])

    def _path_for(self, method: str, url: str, body: bytes | None) -> Path:
        return self._dir / f"{_fingerprint(method, url, body)}.json"

    def get(self, method: str, url: str, body: bytes | None = None) -> dict[str, Any] | None:
        """Return the cached entry (a dict with url/status/headers/body/encoding),
        or ``None`` if there's no entry or it has expired."""
        path = self._path_for(method, url, body)
        if not path.exists():
            return None
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return None

        if self._ttl > 0 and (time.time() - raw.get("cached_at", 0.0)) > self._ttl:
            return None

        import base64

        return {
            "url": raw["url"],
            "status": raw["status"],
            "headers": raw["headers"],
            "body": base64.b64decode(raw["body_b64"]),
            "encoding": raw.get("encoding", "utf-8"),
        }

    def put(
        self,
        method: str,
        url: str,
        status: int,
        headers: dict[str, str],
        body: bytes,
        encoding: str = "utf-8",
        request_body: bytes | None = None,
    ) -> bool:
        """Write a response to the cache. Returns ``False`` (no-op) if
        ``status`` isn't in ``cacheable_statuses``."""
        if status not in self._cacheable_statuses:
            return False
        import base64

        path = self._path_for(method, url, request_body)
        entry = {
            "url": url,
            "status": status,
            "headers": headers,
            "body_b64": base64.b64encode(body).decode("ascii"),
            "encoding": encoding,
            "cached_at": time.time(),
        }
        path.write_text(json.dumps(entry), encoding="utf-8")
        return True

    def clear(self) -> int:
        """Delete every cached entry. Returns the number of files removed."""
        removed = 0
        for f in self._dir.glob("*.json"):
            f.unlink(missing_ok=True)
            removed += 1
        return removed

    def __len__(self) -> int:
        return sum(1 for _ in self._dir.glob("*.json"))
