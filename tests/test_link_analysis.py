"""
Tests for bitscrape.link_analysis.LinkGraph.

PageRank/HITS correctness is verified against well-known graph structures
with predictable relative orderings (a hub page linked by everyone should
score highest; an isolated page should score lowest), rather than exact
literature values (which vary slightly by damping factor / convergence
settings) -- what matters is that the real networkx algorithm is wired up
correctly and produces sane, correctly-ordered results.
"""

from __future__ import annotations

import pytest

from bitscrape.link_analysis import LinkGraph


def test_empty_graph_returns_empty_pagerank():
    graph = LinkGraph()
    assert graph.pagerank() == {}


def test_single_page_no_links():
    graph = LinkGraph()
    graph.add_page("https://example.com/lonely")
    scores = graph.pagerank()
    assert "https://example.com/lonely" in scores
    assert scores["https://example.com/lonely"] == pytest.approx(1.0)


def test_page_linked_by_many_scores_higher_than_unlinked_page():
    """Classic PageRank sanity check: a page every other page links to
    should score higher than a page nobody links to."""
    graph = LinkGraph()
    hub = "https://example.com/popular"
    isolated = "https://example.com/orphan"

    for i in range(10):
        source = f"https://example.com/page{i}"
        graph.add_link(source, hub)
    graph.add_page(isolated)

    scores = graph.pagerank()
    assert scores[hub] > scores[isolated]


def test_scores_sum_to_approximately_one():
    graph = LinkGraph()
    graph.add_link("a", "b")
    graph.add_link("b", "c")
    graph.add_link("c", "a")
    scores = graph.pagerank()
    assert sum(scores.values()) == pytest.approx(1.0, abs=1e-6)


def test_symmetric_cycle_gives_equal_scores():
    """A -> B -> C -> A: perfectly symmetric, every node should score the same."""
    graph = LinkGraph()
    graph.add_link("a", "b")
    graph.add_link("b", "c")
    graph.add_link("c", "a")
    scores = graph.pagerank()
    values = list(scores.values())
    assert values[0] == pytest.approx(values[1], abs=1e-6)
    assert values[1] == pytest.approx(values[2], abs=1e-6)


def test_add_links_bulk_convenience():
    graph = LinkGraph()
    graph.add_links(
        "https://example.com/index",
        [
            "https://example.com/a",
            "https://example.com/b",
            "https://example.com/c",
        ],
    )
    assert graph.out_degree("https://example.com/index") == 3
    assert graph.in_degree("https://example.com/a") == 1


def test_in_degree_and_out_degree():
    graph = LinkGraph()
    graph.add_link("a", "b")
    graph.add_link("c", "b")
    graph.add_link("b", "d")
    assert graph.in_degree("b") == 2
    assert graph.out_degree("b") == 1
    assert graph.in_degree("d") == 1
    assert graph.out_degree("d") == 0


def test_in_degree_of_unknown_url_is_zero():
    graph = LinkGraph()
    assert graph.in_degree("https://never-added.com") == 0


def test_top_by_pagerank_returns_sorted_descending():
    graph = LinkGraph()
    hub = "https://example.com/hub"
    for i in range(5):
        graph.add_link(f"https://example.com/page{i}", hub)
    graph.add_page("https://example.com/orphan")

    top = graph.top_by_pagerank(n=2)
    assert len(top) == 2
    assert top[0][0] == hub
    assert top[0][1] >= top[1][1]


def test_hits_returns_hub_and_authority_scores():
    """Classic HITS test structure: 'hub' pages link to many 'authority'
    pages; authorities should score higher on the authority metric than
    the hubs do, and vice versa for hub scores."""
    graph = LinkGraph()
    authorities = ["auth1", "auth2", "auth3"]
    hubs = ["hub1", "hub2"]
    for hub in hubs:
        for auth in authorities:
            graph.add_link(hub, auth)

    hub_scores, auth_scores = graph.hits()
    assert all(h in hub_scores for h in hubs)
    assert all(a in auth_scores for a in authorities)
    # Authorities should have near-zero hub score (they don't link anywhere).
    assert hub_scores[authorities[0]] < hub_scores[hubs[0]]


def test_hits_on_empty_graph_returns_empty_dicts():
    graph = LinkGraph()
    hubs, auths = graph.hits()
    assert hubs == {}
    assert auths == {}


def test_node_and_edge_counts():
    graph = LinkGraph()
    graph.add_link("a", "b")
    graph.add_link("a", "c")
    assert graph.node_count == 3
    assert graph.edge_count == 2


def test_has_page():
    graph = LinkGraph()
    graph.add_link("a", "b")
    assert graph.has_page("a") is True
    assert graph.has_page("z") is False


def test_realistic_crawl_scenario_ranks_frequently_cited_page_highest():
    """Simulates a small realistic crawl: a homepage links to several
    articles, and one article is cited by all the others (like a canonical
    reference page) -- it should end up top-ranked."""
    graph = LinkGraph()
    home = "https://site.com/"
    articles = [f"https://site.com/article-{i}" for i in range(5)]
    reference = "https://site.com/glossary"

    graph.add_links(home, articles)
    for article in articles:
        graph.add_link(article, reference)  # every article cites the glossary

    ranking = graph.top_by_pagerank(n=1)
    assert ranking[0][0] == reference
