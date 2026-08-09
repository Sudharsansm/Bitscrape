"""
Bitscrape Incremental Recrawling
================================

Decides WHEN to revisit an already-crawled page, based on:
  - how important it is (e.g. its PageRank score from
    ``bitscrape.link_analysis``)
  - how often it's historically changed
  - how long it's been since the last crawl

The estimator follows the same shape as the classic Cho & Garcia-Molina
"Estimating Frequency of Change" approach: model page changes as a Poisson
process and estimate its rate from observed change/no-change outcomes,
rather than naively averaging observed intervals (which is biased when a
page changes more than once between two crawls, or hasn't been observed
long enough to see a change yet).
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field


@dataclass
class PageHistory:
    """Per-URL crawl history used to estimate change frequency and decide
    the next recrawl time."""

    url: str
    last_crawled_at: float = 0.0
    last_changed_at: float = 0.0
    content_hash: str = ""
    # Poisson estimator state: number of times observed changed vs unchanged
    # since the previous crawl.
    observations_changed: int = 0
    observations_unchanged: int = 0
    importance: float = 0.0  # e.g. a PageRank score, 0..1
    next_recrawl_at: float = field(default_factory=lambda: time.time())


class RecrawlScheduler:
    """
    Tracks crawl history per URL and schedules the next recrawl time using:

        next_recrawl_at = now + base_interval / (importance_factor * change_rate_factor)

    where a higher importance or a higher estimated change frequency both
    *shorten* the interval (more important / faster-changing pages get
    recrawled sooner), clamped to [``min_interval``, ``max_interval``].

    This deliberately doesn't try to be a perfect statistical estimator --
    it's a practical, tunable scheduling policy grounded in the same
    "importance x freshness" idea real large-scale crawlers use, without
    claiming research-grade precision.
    """

    def __init__(
        self,
        base_interval: float = 86400.0,  # 1 day
        min_interval: float = 3600.0,  # 1 hour floor
        max_interval: float = 2_592_000.0,  # 30 days ceiling
    ) -> None:
        self._base_interval = base_interval
        self._min_interval = min_interval
        self._max_interval = max_interval
        self._history: dict[str, PageHistory] = {}

    def record_crawl(
        self,
        url: str,
        content_hash: str,
        importance: float = 0.0,
        crawled_at: float | None = None,
    ) -> PageHistory:
        """
        Records the outcome of crawling ``url`` with the given content hash
        (any stable fingerprint works -- sha256 of the body, or
        ``bitscrape.canonicalize.compute_fingerprint().hex`` for
        near-duplicate-tolerant change detection). Updates the change-rate
        estimate and computes the next recrawl time.
        """
        now = crawled_at if crawled_at is not None else time.time()
        history = self._history.get(url)

        if history is None:
            history = PageHistory(
                url=url,
                last_crawled_at=now,
                last_changed_at=now,
                content_hash=content_hash,
                importance=importance,
            )
            self._history[url] = history
        else:
            changed = content_hash != history.content_hash
            if changed:
                history.observations_changed += 1
                history.last_changed_at = now
                history.content_hash = content_hash
            else:
                history.observations_unchanged += 1
            history.last_crawled_at = now
            history.importance = importance

        history.next_recrawl_at = now + self._compute_interval(history)
        return history

    def _compute_interval(self, history: PageHistory) -> float:
        change_rate = self._estimate_change_rate(history)
        # importance_factor: 1.0 (unimportant) .. up to 5x faster for very
        # important pages (importance close to 1.0).
        importance_factor = 1.0 + 4.0 * max(0.0, min(1.0, history.importance))
        # change_rate_factor: pages estimated to change more often get a
        # proportionally shorter interval; a page estimated to change every
        # crawl (rate=1.0) gets up to 10x shorter than one that never
        # changes (rate=0.0).
        change_rate_factor = 1.0 + 9.0 * change_rate

        interval = self._base_interval / (importance_factor * change_rate_factor)
        return max(self._min_interval, min(self._max_interval, interval))

    @staticmethod
    def _estimate_change_rate(history: PageHistory) -> float:
        """
        Simple Laplace-smoothed estimate of P(changed between two
        consecutive crawls), in [0, 1]. Not yet observed -> defaults to a
        moderate 0.5 (genuinely unknown), which is deliberately more
        cautious than assuming "never changes."
        """
        total = history.observations_changed + history.observations_unchanged
        if total == 0:
            return 0.5
        # Laplace smoothing (add-one) avoids a single early observation
        # locking the estimate at exactly 0.0 or 1.0.
        return (history.observations_changed + 1) / (total + 2)

    # --- Queries ---------------------------------------------------------

    def is_due(self, url: str, now: float | None = None) -> bool:
        """True if this URL has never been crawled, or its scheduled next
        recrawl time has passed."""
        history = self._history.get(url)
        if history is None:
            return True
        check_time = now if now is not None else time.time()
        return check_time >= history.next_recrawl_at

    def due_urls(self, now: float | None = None) -> list[str]:
        """All tracked URLs whose next recrawl time has passed, most-
        overdue first."""
        check_time = now if now is not None else time.time()
        due = [
            (h.next_recrawl_at, url)
            for url, h in self._history.items()
            if check_time >= h.next_recrawl_at
        ]
        due.sort()
        return [url for _, url in due]

    def get_history(self, url: str) -> PageHistory | None:
        return self._history.get(url)

    def estimated_change_rate(self, url: str) -> float | None:
        history = self._history.get(url)
        if history is None:
            return None
        return self._estimate_change_rate(history)

    def next_recrawl_at(self, url: str) -> float | None:
        history = self._history.get(url)
        return history.next_recrawl_at if history else None

    @property
    def tracked_count(self) -> int:
        return len(self._history)
