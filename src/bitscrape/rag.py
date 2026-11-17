"""
Ready-made RAG pipeline
========================

Ties three pieces that already existed separately -- ``html_to_markdown``
(:mod:`bitscrape.markdown`), ``chunk_markdown`` (:mod:`bitscrape.chunking`),
and ``BM25Index``/``VectorIndex``/``HybridSearcher``
(:mod:`bitscrape.ranking`) -- into one object you can actually hand a
scraped page and later ask a question against, instead of wiring the three
together yourself every time::

    pipeline = RagPipeline()
    pipeline.ingest_response(response)               # from a spider callback
    pipeline.ingest_html(html, url="https://...")     # or straight from HTML
    results = pipeline.search("what does the pricing page say?")
    context = pipeline.to_context(results)            # ready to paste into a prompt

**Embeddings are still bring-your-own** -- same scope note as
:mod:`bitscrape.ranking`: this environment has no network access to call an
embedding API or download a model, so there's nothing genuine to ship here
for that part. Pass an ``embed_fn: Callable[[str], list[float]]`` to get
full BM25+vector hybrid search via RRF; without one, ``search()`` silently
degrades to BM25-only (still real lexical search, just not hybrid) rather
than raising, since a keyword-search pipeline with no configured embedder
is a completely reasonable, common configuration -- not a misuse.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from bitscrape.chunking import Chunk, chunk_markdown
from bitscrape.markdown import html_to_markdown
from bitscrape.ranking import BM25Index, HybridSearcher, VectorIndex

EmbedFn = Callable[[str], list[float]]


@dataclass
class RagResult:
    """One retrieved chunk, with whichever component scores applied."""

    chunk: Chunk
    score: float
    bm25_score: float | None = None
    vector_score: float | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            **self.chunk.to_dict(),
            "score": self.score,
            "bm25_score": self.bm25_score,
            "vector_score": self.vector_score,
        }


class RagPipeline:
    """Ingest scraped pages, then search them -- BM25-only by default,
    full BM25+vector hybrid search (via Reciprocal Rank Fusion) if you
    supply ``embed_fn``.

    Args:
        embed_fn: turns a chunk's (or a query's) text into an embedding
            vector. Omit for BM25-only search.
        max_tokens / overlap: passed straight through to
            :func:`~bitscrape.chunking.chunk_markdown` for every document
            ingested.
    """

    def __init__(
        self,
        embed_fn: EmbedFn | None = None,
        max_tokens: int = 512,
        overlap: int = 48,
    ) -> None:
        self._embed_fn = embed_fn
        self._max_tokens = max_tokens
        self._overlap = overlap
        self._bm25 = BM25Index()
        self._vectors: VectorIndex | None = VectorIndex() if embed_fn else None
        self._chunks: dict[str, Chunk] = {}
        self._next_anon_id = 0

    @property
    def chunk_count(self) -> int:
        return len(self._chunks)

    @property
    def hybrid_enabled(self) -> bool:
        return self._vectors is not None

    def ingest_markdown(self, markdown: str, *, source: str = "", title: str = "") -> list[str]:
        """Chunk ``markdown`` and index every chunk. Returns the chunk IDs
        added (``f"{source}#0"``, ``f"{source}#1"``, ... -- or an anonymous
        counter if ``source`` is blank)."""
        chunks = chunk_markdown(
            markdown,
            max_tokens=self._max_tokens,
            overlap=self._overlap,
            source=source,
            title=title,
        )
        ids: list[str] = []
        for i, chunk in enumerate(chunks):
            if source:
                chunk_id = f"{source}#{i}"
            else:
                chunk_id = f"chunk-{self._next_anon_id}"
                self._next_anon_id += 1
            self._chunks[chunk_id] = chunk
            self._bm25.add_document(chunk_id, chunk.text)
            if self._vectors is not None:
                assert self._embed_fn is not None
                self._vectors.add_document(chunk_id, self._embed_fn(chunk.text))
            ids.append(chunk_id)
        return ids

    def ingest_html(self, html: str | bytes, *, url: str = "", title: str = "") -> list[str]:
        """Convert ``html`` to markdown (main content only, hidden text
        stripped -- see :func:`~bitscrape.markdown.html_to_markdown`) and
        ingest it."""
        markdown = html_to_markdown(html, base_url=url)
        return self.ingest_markdown(markdown, source=url, title=title)

    def ingest_response(self, response: Any) -> list[str]:
        """Ingest a bitscrape ``Response`` (or ``ParsedResponse`` -- anything
        with ``.url``/``.body``/``.text``) straight from a spider callback
        or the downloader."""
        url = getattr(response, "url", "")
        body = getattr(response, "body", None)
        html = body if body is not None else getattr(response, "text", "")
        return self.ingest_html(html, url=url)

    def search(self, query: str, top_k: int = 10, candidate_k: int = 50) -> list[RagResult]:
        """Search everything ingested so far. Hybrid (BM25 + vector, fused
        via RRF) if ``embed_fn`` was supplied at construction; BM25-only
        otherwise."""
        if self._vectors is not None:
            assert self._embed_fn is not None
            query_embedding = self._embed_fn(query)
            searcher = HybridSearcher(self._bm25, self._vectors)
            hits = searcher.search(query, query_embedding, top_k=top_k, candidate_k=candidate_k)
            return [
                RagResult(
                    chunk=self._chunks[h.doc_id],
                    score=h.fused_score,
                    bm25_score=h.bm25_score,
                    vector_score=h.vector_score,
                )
                for h in hits
                if h.doc_id in self._chunks
            ]

        bm25_hits = self._bm25.search(query, top_k=top_k)
        return [
            RagResult(chunk=self._chunks[doc_id], score=score, bm25_score=score)
            for doc_id, score in bm25_hits
            if doc_id in self._chunks
        ]

    @staticmethod
    def to_context(results: list[RagResult], max_chars: int = 8000) -> str:
        """Format search results into one context string ready to paste
        into an LLM prompt -- each chunk labelled with its source/heading
        so the model (and you) can tell where a fact came from. Truncates
        at a chunk boundary once ``max_chars`` is reached."""
        parts: list[str] = []
        total = 0
        for result in results:
            label_bits = [b for b in (result.chunk.source, result.chunk.heading) if b]
            label = " — ".join(label_bits) if label_bits else "(untitled)"
            piece = f"### {label}\n{result.chunk.text}\n"
            if total + len(piece) > max_chars and parts:
                break
            parts.append(piece)
            total += len(piece)
        return "\n".join(parts)
