"""
Bitscrape Link Analysis
=======================

Authority/importance scoring over the link graph discovered by a crawl,
built on ``networkx``'s real, widely-used graph algorithms (not a
reimplementation of PageRank's linear algebra from scratch) -- pairs
naturally with ``bitscrape.knowledge_graph`` (a different kind of graph:
entity relations, not hyperlinks) and with ``bitscrape.recrawl`` (importance
feeds into recrawl priority).
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


class LinkGraph:
    """
    Accumulates (from_url, to_url) hyperlink edges as a crawl discovers
    them, and computes authority scores over the resulting graph.
    """

    def __init__(self) -> None:
        import networkx as nx

        self._graph = nx.DiGraph()

    def add_link(self, from_url: str, to_url: str) -> None:
        """Records a single hyperlink from_url -> to_url."""
        self._graph.add_edge(from_url, to_url)

    def add_links(self, from_url: str, to_urls: list[str]) -> None:
        """Convenience: records every link found on one page at once --
        the natural shape after parsing a page's <a href> list."""
        for to_url in to_urls:
            self.add_link(from_url, to_url)

    def add_page(self, url: str) -> None:
        """Registers a page with no known outbound/inbound links yet (e.g.
        a freshly-discovered URL with an empty page), so it still appears
        in scores/counts even with zero edges."""
        self._graph.add_node(url)

    # --- Ranking -------------------------------------------------------------

    def pagerank(
        self, damping: float = 0.85, max_iter: int = 100, tol: float = 1.0e-6
    ) -> dict[str, float]:
        """
        Standard PageRank over the discovered link graph. Returns
        {url: score}, scores summing to ~1.0 across all nodes. Pages with no
        inbound links score at the algorithm's minimum (not zero) -- that's
        correct PageRank behaviour, not a bug: every page gets some baseline
        share of the "random surfer" probability mass.
        """
        import networkx as nx

        if self._graph.number_of_nodes() == 0:
            return {}
        return nx.pagerank(self._graph, alpha=damping, max_iter=max_iter, tol=tol)

    def hits(self, max_iter: int = 100) -> tuple[dict[str, float], dict[str, float]]:
        """
        HITS algorithm: returns (hub_scores, authority_scores). Hubs are
        pages that link to many good authorities (e.g. curated link lists);
        authorities are pages linked to by many good hubs (e.g. frequently-
        cited sources). A complementary lens to PageRank, useful when your
        crawl includes both link-aggregator pages and content pages.
        """
        import networkx as nx

        if self._graph.number_of_nodes() == 0:
            return {}, {}
        hubs, authorities = nx.hits(self._graph, max_iter=max_iter)
        return hubs, authorities

    def in_degree(self, url: str) -> int:
        """Raw inbound-link count -- the crudest authority signal, useful
        as a sanity check against PageRank's more nuanced score."""
        return self._graph.in_degree(url) if url in self._graph else 0

    def out_degree(self, url: str) -> int:
        return self._graph.out_degree(url) if url in self._graph else 0

    def top_by_pagerank(self, n: int = 10, **pagerank_kwargs: Any) -> list[tuple[str, float]]:
        """Convenience: the N highest-scoring URLs, sorted descending."""
        scores = self.pagerank(**pagerank_kwargs)
        return sorted(scores.items(), key=lambda kv: kv[1], reverse=True)[:n]

    # --- Introspection -------------------------------------------------------

    @property
    def node_count(self) -> int:
        return self._graph.number_of_nodes()

    @property
    def edge_count(self) -> int:
        return self._graph.number_of_edges()

    def has_page(self, url: str) -> bool:
        return url in self._graph
