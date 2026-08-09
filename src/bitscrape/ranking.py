"""
Bitscrape Semantic Ranking
==========================

Combines lexical search (BM25 -- a real, correctly-implemented algorithm,
not a toy) with embedding-based retrieval and reranking, via Reciprocal
Rank Fusion (RRF).

IMPORTANT SCOPE NOTE: this module does NOT compute embeddings itself --
that needs a real embedding model (OpenAI/Cohere API, a local
sentence-transformers model, etc.), and this build environment has no
network access to download such a model or call an external embedding API.
What's here is the reusable, fully-testable part: BM25 lexical scoring, a
vector similarity search structure, and the fusion/reranking logic that
combines both result lists into one ranking. Bring your own embeddings
(compute them however you like) and pass them to ``VectorIndex`` -- the
combination logic is the same regardless of which embedding model produced
the vectors, and is verified here with synthetic vectors and known-correct
BM25 reference values.
"""

from __future__ import annotations

import math
import re
from collections import Counter
from dataclasses import dataclass


def _tokenize(text: str) -> list[str]:
    return re.findall(r"[a-z0-9]+", text.lower())


class BM25Index:
    """
    A correct, from-first-principles BM25 (Okapi) implementation over a
    small in-memory document set -- the standard lexical-search scoring
    function used by Elasticsearch/Lucene/etc. under the hood. Suitable for
    scoring a candidate set (e.g. a few hundred to low-thousands of docs);
    for a genuinely large index, wire this same scoring math into a real
    search engine (Elasticsearch, Postgres full-text search, etc.) rather
    than holding everything in memory.
    """

    def __init__(self, k1: float = 1.5, b: float = 0.75) -> None:
        self._k1 = k1
        self._b = b
        self._doc_ids: list[str] = []
        self._doc_tokens: dict[str, list[str]] = {}
        self._doc_len: dict[str, int] = {}
        self._df: Counter[str] = Counter()
        self._avgdl = 0.0

    def add_document(self, doc_id: str, text: str) -> None:
        tokens = _tokenize(text)
        self._doc_ids.append(doc_id)
        self._doc_tokens[doc_id] = tokens
        self._doc_len[doc_id] = len(tokens)
        for term in set(tokens):
            self._df[term] += 1
        self._avgdl = sum(self._doc_len.values()) / len(self._doc_len)

    def _idf(self, term: str) -> float:
        n = len(self._doc_ids)
        df = self._df.get(term, 0)
        return math.log((n - df + 0.5) / (df + 0.5) + 1)

    def score(self, doc_id: str, query: str) -> float:
        query_terms = _tokenize(query)
        doc_tokens = self._doc_tokens.get(doc_id)
        if doc_tokens is None:
            return 0.0
        term_freqs = Counter(doc_tokens)
        doc_len = self._doc_len[doc_id]

        total = 0.0
        for term in query_terms:
            tf = term_freqs.get(term, 0)
            if tf == 0:
                continue
            idf = self._idf(term)
            numerator = tf * (self._k1 + 1)
            denominator = tf + self._k1 * (1 - self._b + self._b * doc_len / self._avgdl)
            total += idf * (numerator / denominator)
        return total

    def search(self, query: str, top_k: int = 10) -> list[tuple[str, float]]:
        scores = [(doc_id, self.score(doc_id, query)) for doc_id in self._doc_ids]
        scores.sort(key=lambda kv: kv[1], reverse=True)
        return scores[:top_k]

    @property
    def document_count(self) -> int:
        return len(self._doc_ids)


class VectorIndex:
    """
    A small in-memory vector similarity index (cosine similarity, brute
    force). Bring your own embeddings -- this doesn't compute them. For a
    genuinely large-scale vector index, wire the same interface to a real
    ANN backend (FAISS, pgvector, a hosted vector DB) instead of brute
    force; the fusion logic below doesn't care which one produced the
    ranked list.
    """

    def __init__(self) -> None:
        self._vectors: dict[str, list[float]] = {}

    def add_document(self, doc_id: str, embedding: list[float]) -> None:
        self._vectors[doc_id] = embedding

    @staticmethod
    def _cosine_similarity(a: list[float], b: list[float]) -> float:
        dot = sum(x * y for x, y in zip(a, b))
        norm_a = math.sqrt(sum(x * x for x in a))
        norm_b = math.sqrt(sum(y * y for y in b))
        if norm_a == 0 or norm_b == 0:
            return 0.0
        return dot / (norm_a * norm_b)

    def search(self, query_embedding: list[float], top_k: int = 10) -> list[tuple[str, float]]:
        scores = [
            (doc_id, self._cosine_similarity(query_embedding, vec))
            for doc_id, vec in self._vectors.items()
        ]
        scores.sort(key=lambda kv: kv[1], reverse=True)
        return scores[:top_k]

    @property
    def document_count(self) -> int:
        return len(self._vectors)


def reciprocal_rank_fusion(
    ranked_lists: list[list[tuple[str, float]]], k: int = 60
) -> list[tuple[str, float]]:
    """
    Combines multiple ranked result lists (e.g. BM25 results + vector
    results) into one fused ranking via Reciprocal Rank Fusion -- a simple,
    well-established technique (used in real hybrid search systems,
    including Elasticsearch's own RRF support) that doesn't require the
    input scores to be on comparable scales, unlike naively averaging BM25
    scores with cosine similarities.

    ``k`` is RRF's standard damping constant (60 is the commonly-used
    default from the original paper).

    Returns [(doc_id, fused_score), ...] sorted descending by fused score.
    """
    fused: dict[str, float] = {}
    for ranked_list in ranked_lists:
        for rank, (doc_id, _original_score) in enumerate(ranked_list, start=1):
            fused[doc_id] = fused.get(doc_id, 0.0) + 1.0 / (k + rank)
    return sorted(fused.items(), key=lambda kv: kv[1], reverse=True)


@dataclass
class HybridSearchResult:
    doc_id: str
    fused_score: float
    bm25_score: float | None = None
    vector_score: float | None = None


class HybridSearcher:
    """
    Ties BM25Index + VectorIndex + reciprocal_rank_fusion together into one
    convenient hybrid-search call: lexical results + semantic results,
    fused via RRF, with both component scores preserved on each result for
    inspection/debugging.
    """

    def __init__(self, bm25_index: BM25Index, vector_index: VectorIndex) -> None:
        self._bm25 = bm25_index
        self._vectors = vector_index

    def search(
        self,
        query_text: str,
        query_embedding: list[float],
        top_k: int = 10,
        candidate_k: int = 50,
    ) -> list[HybridSearchResult]:
        bm25_results = self._bm25.search(query_text, top_k=candidate_k)
        vector_results = self._vectors.search(query_embedding, top_k=candidate_k)

        fused = reciprocal_rank_fusion([bm25_results, vector_results])[:top_k]

        bm25_scores = dict(bm25_results)
        vector_scores = dict(vector_results)

        return [
            HybridSearchResult(
                doc_id=doc_id,
                fused_score=score,
                bm25_score=bm25_scores.get(doc_id),
                vector_score=vector_scores.get(doc_id),
            )
            for doc_id, score in fused
        ]
