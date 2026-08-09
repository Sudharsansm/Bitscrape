"""
Tests for bitscrape.recrawl.RecrawlScheduler. Uses synthetic timestamps
(passed explicitly) rather than real sleeps, so the change-rate/interval
math is tested deterministically and fast.
"""

from __future__ import annotations

from bitscrape.recrawl import RecrawlScheduler


def test_never_crawled_url_is_always_due():
    scheduler = RecrawlScheduler()
    assert scheduler.is_due("https://example.com/new-page") is True


def test_record_crawl_creates_history():
    scheduler = RecrawlScheduler()
    scheduler.record_crawl("https://example.com/a", content_hash="hash1", crawled_at=1000.0)
    history = scheduler.get_history("https://example.com/a")
    assert history is not None
    assert history.last_crawled_at == 1000.0
    assert history.content_hash == "hash1"


def test_freshly_crawled_page_is_not_immediately_due_again():
    scheduler = RecrawlScheduler(base_interval=86400.0, min_interval=3600.0)
    scheduler.record_crawl("https://example.com/a", content_hash="h1", crawled_at=1000.0)
    assert scheduler.is_due("https://example.com/a", now=1000.0 + 60) is False


def test_page_becomes_due_after_scheduled_interval_passes():
    scheduler = RecrawlScheduler(base_interval=3600.0, min_interval=60.0, max_interval=86400.0)
    scheduler.record_crawl("https://example.com/a", content_hash="h1", crawled_at=1000.0)
    next_time = scheduler.next_recrawl_at("https://example.com/a")
    assert scheduler.is_due("https://example.com/a", now=next_time + 1) is True


def test_high_importance_page_gets_shorter_interval():
    scheduler = RecrawlScheduler(base_interval=86400.0)
    scheduler.record_crawl(
        "https://example.com/important", content_hash="h1", importance=1.0, crawled_at=1000.0
    )
    scheduler.record_crawl(
        "https://example.com/unimportant", content_hash="h1", importance=0.0, crawled_at=1000.0
    )
    important_next = scheduler.next_recrawl_at("https://example.com/important")
    unimportant_next = scheduler.next_recrawl_at("https://example.com/unimportant")
    assert important_next < unimportant_next


def test_frequently_changing_page_gets_shorter_interval_than_static_page():
    scheduler = RecrawlScheduler(base_interval=86400.0)

    t = 0.0
    for i in range(5):
        scheduler.record_crawl(
            "https://example.com/changing", content_hash=f"hash-{i}", crawled_at=t
        )
        t += 100.0
    last_t_changing = t - 100.0

    t = 0.0
    for _ in range(5):
        scheduler.record_crawl(
            "https://example.com/static", content_hash="same-hash-always", crawled_at=t
        )
        t += 100.0
    last_t_static = t - 100.0

    changing_interval = (
        scheduler.next_recrawl_at("https://example.com/changing") - last_t_changing
    )
    static_interval = scheduler.next_recrawl_at("https://example.com/static") - last_t_static
    assert changing_interval < static_interval


def test_estimated_change_rate_increases_with_observed_changes():
    scheduler = RecrawlScheduler()
    t = 0.0
    for i in range(4):
        scheduler.record_crawl("https://example.com/a", content_hash=f"h{i}", crawled_at=t)
        t += 10
    rate = scheduler.estimated_change_rate("https://example.com/a")
    assert rate is not None
    assert rate > 0.5


def test_estimated_change_rate_decreases_with_no_changes():
    scheduler = RecrawlScheduler()
    t = 0.0
    for _ in range(4):
        scheduler.record_crawl("https://example.com/a", content_hash="constant", crawled_at=t)
        t += 10
    rate = scheduler.estimated_change_rate("https://example.com/a")
    assert rate is not None
    assert rate < 0.5


def test_estimated_change_rate_none_for_unknown_url():
    scheduler = RecrawlScheduler()
    assert scheduler.estimated_change_rate("https://never-seen.com") is None


def test_interval_respects_min_interval_floor():
    scheduler = RecrawlScheduler(base_interval=86400.0, min_interval=7200.0)
    scheduler.record_crawl(
        "https://example.com/a", content_hash="h1", importance=1.0, crawled_at=1000.0
    )
    next_time = scheduler.next_recrawl_at("https://example.com/a")
    assert next_time - 1000.0 >= 7200.0


def test_interval_respects_max_interval_ceiling():
    scheduler = RecrawlScheduler(base_interval=100.0, min_interval=1.0, max_interval=500.0)
    scheduler.record_crawl(
        "https://example.com/a", content_hash="constant", importance=0.0, crawled_at=1000.0
    )
    next_time = scheduler.next_recrawl_at("https://example.com/a")
    assert next_time - 1000.0 <= 500.0


def test_due_urls_returns_most_overdue_first():
    scheduler = RecrawlScheduler(base_interval=100.0, min_interval=10.0, max_interval=1000.0)
    scheduler.record_crawl("https://example.com/a", content_hash="h1", crawled_at=0.0)
    scheduler.record_crawl("https://example.com/b", content_hash="h1", crawled_at=50.0)

    due = scheduler.due_urls(now=100_000)
    assert due[0] == "https://example.com/a"
    assert due[1] == "https://example.com/b"


def test_due_urls_excludes_not_yet_due():
    scheduler = RecrawlScheduler(base_interval=86400.0, min_interval=3600.0)
    scheduler.record_crawl("https://example.com/a", content_hash="h1", crawled_at=1000.0)
    due = scheduler.due_urls(now=1000.0 + 60)
    assert due == []


def test_tracked_count():
    scheduler = RecrawlScheduler()
    assert scheduler.tracked_count == 0
    scheduler.record_crawl("https://a.com", content_hash="h", crawled_at=0.0)
    scheduler.record_crawl("https://b.com", content_hash="h", crawled_at=0.0)
    assert scheduler.tracked_count == 2


def test_get_history_none_for_unknown_url():
    scheduler = RecrawlScheduler()
    assert scheduler.get_history("https://never-seen.com") is None


def test_content_hash_updates_on_change():
    scheduler = RecrawlScheduler()
    scheduler.record_crawl("https://a.com", content_hash="h1", crawled_at=0.0)
    scheduler.record_crawl("https://a.com", content_hash="h2", crawled_at=100.0)
    history = scheduler.get_history("https://a.com")
    assert history.content_hash == "h2"
    assert history.last_changed_at == 100.0


def test_last_changed_at_unchanged_when_content_stays_the_same():
    scheduler = RecrawlScheduler()
    scheduler.record_crawl("https://a.com", content_hash="h1", crawled_at=0.0)
    scheduler.record_crawl("https://a.com", content_hash="h1", crawled_at=100.0)
    history = scheduler.get_history("https://a.com")
    assert history.last_changed_at == 0.0
    assert history.last_crawled_at == 100.0
