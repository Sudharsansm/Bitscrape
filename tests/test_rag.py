"""Tests for bitscrape.rag.RagPipeline -- the html_to_markdown ->
chunk_markdown -> BM25/hybrid-search wiring."""

from __future__ import annotations

from bitscrape.rag import RagPipeline, RagResult

HTML = """
<html><body>
<h1>Widgets</h1>
<p>Our widgets are blue and extremely fast.</p>
<h2>Pricing</h2>
<p>Each widget costs ten dollars.</p>
<h2>Shipping</h2>
<p>Widgets ship within two business days.</p>
</body></html>
"""


def test_ingest_html_chunks_by_heading():
    pipeline = RagPipeline()
    ids = pipeline.ingest_html(HTML, url="https://example.com/widgets", title="Widget Page")
    assert len(ids) == 3  # top section + Pricing + Shipping
    assert pipeline.chunk_count == 3
    assert all(i.startswith("https://example.com/widgets#") for i in ids)


def test_bm25_only_search_finds_exact_term_matches():
    pipeline = RagPipeline()
    pipeline.ingest_html(HTML, url="https://example.com/widgets")
    results = pipeline.search("dollars")
    assert results, "expected at least one match for 'dollars'"
    assert isinstance(results[0], RagResult)
    assert "dollars" in results[0].chunk.text.lower()
    assert results[0].bm25_score is not None
    assert results[0].vector_score is None
    assert not pipeline.hybrid_enabled


def test_hybrid_search_uses_embed_fn_and_fuses_scores():
    # A trivial but deterministic "embedding": bag-of-length-buckets.
    def fake_embed(text: str) -> list[float]:
        return [float(text.lower().count(w)) for w in ("widget", "ship", "dollar")]

    pipeline = RagPipeline(embed_fn=fake_embed)
    assert pipeline.hybrid_enabled
    pipeline.ingest_html(HTML, url="https://example.com/widgets")

    results = pipeline.search("shipping widgets")
    assert results
    top = results[0]
    assert top.bm25_score is not None
    assert top.vector_score is not None


def test_ingest_markdown_directly_without_html():
    pipeline = RagPipeline()
    ids = pipeline.ingest_markdown(
        "# Intro\n\nHello there.\n\n## Details\n\nMore specific content here.",
        source="doc-1",
        title="My Doc",
    )
    assert len(ids) == 2
    assert ids == ["doc-1#0", "doc-1#1"]


def test_anonymous_source_gets_counter_based_ids():
    pipeline = RagPipeline()
    ids1 = pipeline.ingest_markdown("Some text with no source given at all here.")
    ids2 = pipeline.ingest_markdown("Different text, also no source given at all here.")
    assert ids1 == ["chunk-0"]
    assert ids2 == ["chunk-1"]


def test_ingest_response_reads_body_and_url():
    from bitscrape.core.models import Request, Response

    req = Request(url="https://example.com/page")
    resp = Response(
        url="https://example.com/page",
        status=200,
        body=b"<html><body><h1>Hi</h1><p>Some real content lives here.</p></body></html>",
        request=req,
    )
    pipeline = RagPipeline()
    ids = pipeline.ingest_response(resp)
    assert len(ids) >= 1
    results = pipeline.search("real content")
    assert results
    assert "real content" in results[0].chunk.text.lower()


def test_to_context_labels_each_chunk_and_respects_max_chars():
    pipeline = RagPipeline()
    pipeline.ingest_html(HTML, url="https://example.com/widgets")
    results = pipeline.search("widgets")
    context = pipeline.to_context(results, max_chars=10_000)
    assert "example.com/widgets" in context
    assert "###" in context

    # Tight budget still returns at least one full chunk (never truncates
    # mid-chunk), even if that means slightly exceeding max_chars.
    tight_context = pipeline.to_context(results, max_chars=1)
    assert tight_context.strip() != ""


def test_empty_pipeline_search_returns_nothing():
    pipeline = RagPipeline()
    assert pipeline.search("anything") == []
    assert pipeline.chunk_count == 0
