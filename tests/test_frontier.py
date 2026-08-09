"""
Tests for bitscrape.frontier.Frontier -- priority-tiered, politeness-aware
URL frontier. Timing-based politeness tests use real wall-clock delays (kept
short, ~0.1-0.3s) rather than mocking time, since the actual guarantee being
tested is real spacing between get_next() calls.
"""

from __future__ import annotations

import time

from bitscrape.frontier import Frontier


def test_empty_frontier_returns_none():
    f = Frontier()
    assert f.get_next() is None


def test_single_url_no_delay():
    f = Frontier(default_delay=0.0)
    f.add("https://example.com/page")
    result = f.get_next()
    assert result is not None
    url, meta = result
    assert url == "https://example.com/page"


def test_size_tracks_pending_urls():
    f = Frontier()
    assert f.size == 0
    f.add("https://a.com/1")
    f.add("https://b.com/1")
    assert f.size == 2
    f.get_next()
    assert f.size == 1


def test_higher_priority_tier_served_first_across_domains():
    f = Frontier(num_priority_tiers=3, default_delay=0.0)
    f.add("https://a.com/low", priority=2)
    f.add("https://b.com/high", priority=0)
    f.add("https://c.com/mid", priority=1)

    first = f.get_next()
    assert first[0] == "https://b.com/high"


def test_fifo_within_same_domain_and_priority():
    f = Frontier(default_delay=0.0)
    f.add("https://a.com/1", priority=1)
    f.add("https://a.com/2", priority=1)
    f.add("https://a.com/3", priority=1)

    urls = [f.get_next()[0] for _ in range(3)]
    assert urls == ["https://a.com/1", "https://a.com/2", "https://a.com/3"]


def test_priority_clamped_into_valid_range():
    f = Frontier(num_priority_tiers=3, default_delay=0.0)
    f.add("https://a.com/x", priority=99)  # way out of range
    f.add("https://b.com/y", priority=0)
    # Should not crash, and the valid-priority one should still come first.
    first = f.get_next()
    assert first[0] == "https://b.com/y"


def test_meta_is_preserved():
    f = Frontier(default_delay=0.0)
    f.add("https://example.com/page", meta={"depth": 2})
    url, meta = f.get_next()
    assert meta == {"depth": 2}


def test_politeness_enforced_within_a_single_domain():
    """The core guarantee: two URLs for the same domain must not both come
    back before that domain's delay has elapsed."""
    f = Frontier(default_delay=0.2)
    f.add("https://example.com/a")
    f.add("https://example.com/b")

    first = f.get_next()
    assert first is not None

    # Immediately after, the second URL for the same domain should NOT be
    # available yet.
    assert f.get_next() is None

    time.sleep(0.25)
    second = f.get_next()
    assert second is not None
    assert second[0] == "https://example.com/b"


def test_different_domains_do_not_block_each_other():
    f = Frontier(default_delay=1.0)  # large delay, but different domains
    f.add("https://a.com/1")
    f.add("https://b.com/1")

    first = f.get_next()
    second = f.get_next()
    assert first is not None
    assert second is not None
    assert {first[0], second[0]} == {"https://a.com/1", "https://b.com/1"}


def test_per_domain_delay_override():
    f = Frontier(default_delay=5.0)  # would normally block for a long time
    f.set_domain_delay("fast.com", 0.1)
    f.add("https://fast.com/a")
    f.add("https://fast.com/b")

    assert f.get_next() is not None
    assert f.get_next() is None  # not ready yet even with override
    time.sleep(0.15)
    assert f.get_next() is not None  # ready now, using the override delay


def test_domain_count():
    f = Frontier(default_delay=0.0)
    f.add("https://a.com/1")
    f.add("https://a.com/2")
    f.add("https://b.com/1")
    assert f.domain_count() == 2


def test_pending_for_domain():
    f = Frontier(default_delay=0.0)
    f.add("https://a.com/1")
    f.add("https://a.com/2")
    assert f.pending_for_domain("a.com") == 2
    assert f.pending_for_domain("never-added.com") == 0


def test_seconds_until_ready_reflects_remaining_wait():
    f = Frontier(default_delay=0.5)
    f.add("https://example.com/a")
    f.get_next()
    remaining = f.seconds_until_ready("example.com")
    assert 0.0 < remaining <= 0.5


def test_seconds_until_ready_zero_for_untouched_domain():
    f = Frontier()
    assert f.seconds_until_ready("never-touched.com") == 0.0


def test_round_robin_fairness_across_many_domains():
    """With no delay, priority-equal URLs across many domains should all
    become available -- no domain should starve another."""
    f = Frontier(default_delay=0.0)
    domains = [f"https://site{i}.com/page" for i in range(20)]
    for url in domains:
        f.add(url)

    served = set()
    for _ in range(20):
        result = f.get_next()
        assert result is not None
        served.add(result[0])
    assert served == set(domains)


def test_get_next_returns_none_when_all_domains_still_cooling_down():
    f = Frontier(default_delay=10.0)
    f.add("https://a.com/1")
    f.add("https://b.com/1")
    f.get_next()
    f.get_next()
    # Both domains now cooling down for 10s -- nothing should be available.
    assert f.get_next() is None


def test_adding_more_urls_after_draining_a_domain_works():
    f = Frontier(default_delay=0.0)
    f.add("https://a.com/1")
    assert f.get_next() is not None
    assert f.get_next() is None  # drained
    f.add("https://a.com/2")  # re-add after drain
    result = f.get_next()
    assert result is not None
    assert result[0] == "https://a.com/2"
