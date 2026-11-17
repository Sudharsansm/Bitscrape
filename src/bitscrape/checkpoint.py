"""
Pause and resume
================

A long crawl should survive Ctrl-C, a deploy, or a crash without starting over.
When ``Settings.jobdir`` is set, the engine persists its state there:

``requests.jsonl``
    Requests still to be fetched (queued + those in flight when the state was
    taken -- in-flight requests are re-run on resume, i.e. *at-least-once*).
``seen.txt``
    Fingerprints already scheduled, so resuming never re-crawls a page.
``state.json``
    Commit marker + counters. It is written **last**, atomically, so a crash
    part-way through a save leaves the previous consistent state untouched.

A crawl that finishes normally clears its checkpoint, so the next run starts
fresh instead of "resuming" a completed job.
"""

from __future__ import annotations

import json
import logging
import os
import time
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from bitscrape.core.models import Request
from bitscrape.scheduler.scheduler import request_from_bytes, request_to_bytes

logger = logging.getLogger(__name__)

STATE_VERSION = 1

# Counters worth carrying across a pause so totals keep accumulating.
_COUNTER_FIELDS = (
    "requests_made",
    "requests_failed",
    "responses_received",
    "items_scraped",
    "items_dropped",
    "items_noindexed",
    "bytes_downloaded",
    "requests_blocked",
    "requests_retried_blocked",
    "cache_hits",
)


@dataclass
class CheckpointData:
    pending: list[Request] = field(default_factory=list)
    seen: list[str] = field(default_factory=list)
    counters: dict[str, int] = field(default_factory=dict)
    status: str = "paused"  # paused | running
    spider: str = ""
    saved_at: float = 0.0


def _atomic_write(path: Path, data: bytes) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "wb") as fh:
        fh.write(data)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)


class Checkpoint:
    """Reads and writes a crawl's persisted state under ``jobdir``."""

    def __init__(self, jobdir: str | Path) -> None:
        self.dir = Path(jobdir).expanduser()

    @property
    def _state(self) -> Path:
        return self.dir / "state.json"

    def exists(self) -> bool:
        return self._state.is_file()

    def save(
        self,
        pending: Iterable[Request],
        seen: Iterable[str],
        stats: Any,
        *,
        spider: str = "",
        status: str = "paused",
    ) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        pend = list(pending)
        _atomic_write(
            self.dir / "requests.jsonl",
            b"".join(request_to_bytes(r) + b"\n" for r in pend),
        )
        seen_list = list(seen)
        _atomic_write(
            self.dir / "seen.txt", ("\n".join(seen_list) + "\n").encode() if seen_list else b""
        )
        counters = {k: int(getattr(stats, k, 0)) for k in _COUNTER_FIELDS}
        state = {
            "version": STATE_VERSION,
            "status": status,
            "spider": spider,
            "saved_at": time.time(),
            "pending": len(pend),
            "seen": len(seen_list),
            "counters": counters,
        }
        # Commit marker last.
        _atomic_write(self._state, json.dumps(state, indent=2).encode())
        logger.info(
            "Checkpoint saved: %d pending, %d seen (%s)", len(pend), len(seen_list), self.dir
        )

    def load(self) -> CheckpointData | None:
        if not self._state.is_file():
            return None
        try:
            state = json.loads(self._state.read_text())
            if state.get("version") != STATE_VERSION:
                logger.warning(
                    "Ignoring checkpoint with unsupported version %r", state.get("version")
                )
                return None
            pending: list[Request] = []
            req_file = self.dir / "requests.jsonl"
            if req_file.is_file():
                for line in req_file.read_bytes().splitlines():
                    if line.strip():
                        pending.append(request_from_bytes(line))
            seen: list[str] = []
            seen_file = self.dir / "seen.txt"
            if seen_file.is_file():
                seen = [ln for ln in seen_file.read_text().splitlines() if ln]
        except (OSError, ValueError, KeyError) as exc:
            logger.warning("Could not read checkpoint in %s (%s); starting fresh", self.dir, exc)
            return None
        return CheckpointData(
            pending=pending,
            seen=seen,
            counters={k: int(v) for k, v in state.get("counters", {}).items()},
            status=str(state.get("status", "paused")),
            spider=str(state.get("spider", "")),
            saved_at=float(state.get("saved_at", 0.0)),
        )

    def clear(self) -> None:
        for name in ("requests.jsonl", "seen.txt", "state.json"):
            try:
                (self.dir / name).unlink()
            except FileNotFoundError:
                pass
            except OSError as exc:  # pragma: no cover
                logger.warning("Could not remove %s: %s", self.dir / name, exc)
        for tmp in self.dir.glob("*.tmp") if self.dir.exists() else []:
            tmp.unlink(missing_ok=True)


def restore_counters(stats: Any, counters: dict[str, int]) -> None:
    """Add checkpointed counters onto a fresh stats object."""
    for k, v in counters.items():
        if k in _COUNTER_FIELDS and hasattr(stats, k):
            setattr(stats, k, getattr(stats, k) + v)
