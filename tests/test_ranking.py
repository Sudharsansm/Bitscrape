"""
Tests for bitscrape.ranking: BM25 lexical scoring, vector cosine similarity,
and RRF-based hybrid fusion.
"""

from __future__ import annotations

import pytest

from bitscrape.ranking import (
    BM25Index,
    HybridSearcher,
    VectorIndex,
    reciprocal_rank_fusion,
)


# ---------------------------------------------------------------------------
# BM25Index
# ---------------------------------------------------------------------------


def test_document_containing_query_term_scores_above_zero():
    index = BM25Index()
    index.add_document("doc1", "the quick brown fox jumps over the lazy dog")
    index.add_document("doc2", "a completely unrelated document about cooking recipes")
    assert index.score("doc1", "fox") > 0
    assert index.score("doc2", "fox") == 0


def test_document_with_more_query_term_occurrences_scores_higher():
    index = BM25Index()
    index.add_document("doc1", "python python python programming language")
    index.add_document("doc2", "python programming language basics")
    assert index.score("doc1", "python") > index.score("doc2", "python")


def test_rare_term_weighted_higher_than_common_term():
    index = BM25Index()
    index.add_document("doc1", "the cat sat on the mat")
    index.add_document("doc2", "the dog sat on the rug")
    index.add_document("doc3", "the bird sat on the branch")
    idf_the = index._idf("the")
    idf_cat = index._idf("cat")
    assert idf_cat > idf_the


def test_search_returns_sorted_results():
    index = BM25Index()
    index.add_document("doc1", "machine learning and artificial intelligence")
    index.add_document("doc2", "cooking pasta with tomato sauce")
    index.add_document("doc3", "deep learning neural networks and machine learning")
    results = index.search("machine learning")
    assert results[0][0] == "doc3"
    scores = [score for _, score in results]
    assert scores == sorted(scores, reverse=True)


def test_search_respects_top_k():
    index = BM25Index()
    for i in range(10):
        index.add_document(f"doc{i}", f"document number {i} about testing")
    results = index.search("testing", top_k=3)
    assert len(results) == 3


def test_query_with_no_matching_terms_scores_zero_for_all():
    index = BM25Index()
    index.add_document("doc1", "apples and oranges")
    index.add_document("doc2", "bananas and grapes")
    results = index.search("xyzzy nonexistent")
    assert all(score == 0.0 for _, score in results)


def test_document_count():
    index = BM25Index()
    index.add_document("doc1", "text one")
    index.add_document("doc2", "text two")
    assert index.document_count == 2


def test_unknown_doc_id_scores_zero():
    index = BM25Index()
    index.add_document("doc1", "some text")
    assert index.score("never-added", "text") == 0.0


# ---------------------------------------------------------------------------
# VectorIndex
# ---------------------------------------------------------------------------


def test_identical_vectors_score_similarity_one():
    index = VectorIndex()
    index.add_document("doc1", [1.0, 0.0, 0.0])
    results = index.search([1.0, 0.0, 0.0])
    assert results[0][0] == "doc1"
    assert results[0][1] == pytest.approx(1.0)


def test_orthogonal_vectors_score_similarity_zero():
    index = VectorIndex()
    index.add_document("doc1", [1.0, 0.0])
    results = index.search([0.0, 1.0])
    assert results[0][1] == pytest.approx(0.0, abs=1e-9)


def test_opposite_vectors_score_negative_similarity():
    index = VectorIndex()
    index.add_document("doc1", [1.0, 0.0])
    results = index.search([-1.0, 0.0])
    assert results[0][1] == pytest.approx(-1.0)


def test_closer_vector_ranks_higher():
    index = VectorIndex()
    index.add_document("close", [1.0, 0.1])
    index.add_document("far", [0.1, 1.0])
    results = index.search([1.0, 0.0])
    assert results[0][0] == "close"


def test_vector_search_respects_top_k():
    index = VectorIndex()
    for i in range(10):
        index.add_document(f"doc{i}", [float(i), 1.0])
    results = index.search([5.0, 1.0], top_k=3)
    assert len(results) == 3


def test_zero_vector_scores_zero_not_nan():
    index = VectorIndex()
    index.add_document("doc1", [0.0, 0.0])
    results = index.search([1.0, 1.0])
    assert results[0][1] == 0.0


def test_vector_document_count():
    index = VectorIndex()
    index.add_document("doc1", [1.0])
    index.add_document("doc2", [2.0])
    assert index.document_count == 2


# ---------------------------------------------------------------------------
# reciprocal_rank_fusion
# ---------------------------------------------------------------------------


def test_document_ranked_first_in_both_lists_wins_fusion():
    bm25_results = [("doc_a", 5.0), ("doc_b", 3.0)]
    vector_results = [("doc_a", 0.9), ("doc_b", 0.5)]
    fused = reciprocal_rank_fusion([bm25_results, vector_results])
    assert fused[0][0] == "doc_a"


def test_document_only_in_one_list_still_included():
    bm25_results = [("doc_a", 5.0)]
    vector_results = [("doc_b", 0.9)]
    fused = reciprocal_rank_fusion([bm25_results, vector_results])
    doc_ids = {doc_id for doc_id, _ in fused}
    assert doc_ids == {"doc_a", "doc_b"}


def test_fusion_rewards_consistent_moderate_ranking_over_one_great_one_bad():
    """RRF's classic property: a doc ranked #2 in both lists can beat a doc
    ranked #1 in one list but absent from the other, since RRF rewards
    consistency across signals."""
    bm25_results = [("doc_a", 10.0), ("doc_b", 8.0)]
    vector_results = [("doc_b", 0.9), ("doc_c", 0.8)]  # doc_a absent here
    fused = reciprocal_rank_fusion([bm25_results, vector_results])
    fused_scores = dict(fused)
    assert fused_scores["doc_b"] > fused_scores["doc_a"]


def test_empty_lists_produce_empty_fusion():
    assert reciprocal_rank_fusion([[], []]) == []


def test_fusion_score_uses_rank_not_original_score_scale():
    """A BM25 score of 5000 and a cosine score of 0.01 shouldn't be
    naively comparable -- RRF uses rank position, so wildly different
    score scales don't distort the fusion."""
    bm25_results = [("doc_a", 5000.0), ("doc_b", 1.0)]
    vector_results = [("doc_b", 0.99), ("doc_a", 0.01)]
    fused = reciprocal_rank_fusion([bm25_results, vector_results])
    fused_scores = dict(fused)
    assert fused_scores["doc_a"] == fused_scores["doc_b"]


# ---------------------------------------------------------------------------
# HybridSearcher
# ---------------------------------------------------------------------------


def test_hybrid_searcher_combines_both_indexes():
    bm25 = BM25Index()
    bm25.add_document("doc1", "machine learning tutorial for beginners")
    bm25.add_document("doc2", "cooking recipes for pasta dishes")

    vectors = VectorIndex()
    vectors.add_document("doc1", [1.0, 0.0])
    vectors.add_document("doc2", [0.0, 1.0])

    searcher = HybridSearcher(bm25, vectors)
    results = searcher.search(
        query_text="machine learning", query_embedding=[1.0, 0.0], top_k=2
    )
    assert results[0].doc_id == "doc1"
    assert results[0].bm25_score is not None
    assert results[0].vector_score is not None


def test_hybrid_searcher_preserves_component_scores():
    bm25 = BM25Index()
    bm25.add_document("doc1", "test document one")
    vectors = VectorIndex()
    vectors.add_document("doc1", [1.0])

    searcher = HybridSearcher(bm25, vectors)
    results = searcher.search(query_text="test", query_embedding=[1.0])
    assert results[0].bm25_score > 0
    assert results[0].vector_score == pytest.approx(1.0)
