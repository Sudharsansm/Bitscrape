# AI / Search Infrastructure

The "hyperscale search systems" pieces: link analysis (PageRank/HITS),
incremental recrawl scheduling, and hybrid lexical+semantic search
(BM25 + vector embeddings + fusion), plus the knowledge graph. These are
the surrounding infrastructure a search engine needs beyond the crawler
itself — real algorithms, real tests, with honest scope notes where a
piece (embedding computation) genuinely needs something this project
doesn't bundle.

## Link analysis

`bitscrape.link_analysis.LinkGraph` scores page importance from the
hyperlink graph your crawl discovers, using `networkx`'s real
implementations (not reimplemented from scratch):

```python
from bitscrape.link_analysis import LinkGraph

graph = LinkGraph()
graph.add_link("https://example.com/", "https://example.com/article-1")
graph.add_links("https://example.com/", ["https://example.com/a", "https://example.com/b"])

scores = graph.pagerank()                  # {url: score}, sums to ~1.0
top10 = graph.top_by_pagerank(n=10)         # [(url, score), ...] descending
hubs, authorities = graph.hits()            # HITS: complementary hub/authority scores
```

**PageRank**: standard damped random-walk importance. A page linked to by
many other pages scores higher than an orphan page; a page with no inbound
links still gets a small non-zero baseline share (that's correct PageRank
behavior, not a bug).

**HITS**: hubs (pages that link to many good authorities — e.g. curated
link lists) vs. authorities (pages linked to by many good hubs — e.g.
frequently-cited sources). A complementary lens when your crawl mixes
link-aggregator pages and content pages.

Feed a `LinkGraph` score into `RecrawlScheduler.record_crawl(..., importance=score)`
(below) to prioritize recrawling important pages more often.

## Incremental recrawling

`bitscrape.recrawl.RecrawlScheduler` decides when to revisit an
already-crawled page, based on importance and estimated change frequency —
in the spirit of Cho & Garcia-Molina's change-frequency estimation (a
Laplace-smoothed estimate of P(changed), not a from-scratch statistical
model claiming research-grade precision).

```python
from bitscrape.recrawl import RecrawlScheduler
from bitscrape.canonicalize import compute_fingerprint

scheduler = RecrawlScheduler(base_interval=86400, min_interval=3600, max_interval=2_592_000)

# After crawling a page:
content_hash = compute_fingerprint(response.text).hex
scheduler.record_crawl(response.url, content_hash, importance=pagerank_score)

# Later, to decide what to recrawl:
for url in scheduler.due_urls():
    ...  # re-crawl these, most-overdue first
```

Higher `importance` and a higher estimated change rate both *shorten* the
next-recrawl interval (clamped to `[min_interval, max_interval]`). Verified
by tests confirming important pages and frequently-changing pages both get
shorter intervals than unimportant/static ones.

## Hybrid search: BM25 + vector + fusion

`bitscrape.ranking` gives you a real, correct BM25 (Okapi) implementation,
a brute-force cosine-similarity vector index, and Reciprocal Rank Fusion
(RRF) to combine lexical and semantic search results into one ranking.

```python
from bitscrape.ranking import BM25Index, VectorIndex, HybridSearcher

bm25 = BM25Index()
bm25.add_document("doc1", "machine learning tutorial for beginners")

vectors = VectorIndex()
vectors.add_document("doc1", my_embedding_for_doc1)   # <-- you compute this

searcher = HybridSearcher(bm25, vectors)
results = searcher.search(query_text="machine learning", query_embedding=my_query_embedding)
# [HybridSearchResult(doc_id="doc1", fused_score=..., bm25_score=..., vector_score=...), ...]
```

### Scope note: bring your own embeddings

**This module does not compute embeddings.** `VectorIndex.add_document()`
and `HybridSearcher.search()` expect you to already have embedding vectors
— from an OpenAI/Cohere API call, a local `sentence-transformers` model, or
anything else. This project's build environment had no network access to
download an embedding model or call an external embedding API, so that
piece genuinely isn't included. What **is** real and tested here: correct
BM25 scoring (verified against known properties — rare terms weighted
higher via IDF, more term occurrences scoring higher), correct cosine
similarity, and RRF's actual fusion behavior (a document ranked
consistently-moderate across both lexical and semantic search can beat a
document that's rank-1 in one signal but absent from the other — verified
by test).

### Why RRF instead of averaging scores

BM25 scores and cosine similarities are on completely different, arbitrary
scales — averaging a BM25 score of `5000` with a cosine similarity of
`0.01` would be meaningless. RRF sidesteps this by fusing on **rank
position**, not raw score value: `fused_score = sum(1 / (k + rank))` across
each ranked list a document appears in (`k=60` is RRF's standard damping
constant from the original paper, also used by Elasticsearch's own RRF
support). Verified by test: two documents with wildly different underlying
score scales but identical rank positions across the two lists get
identical fused scores.

### Scaling beyond in-memory

`BM25Index` and `VectorIndex` are suitable for scoring a candidate set (a
few hundred to low-thousands of documents) held in memory. For a genuinely
large index, wire the same BM25 math into a real search engine
(Elasticsearch, PostgreSQL full-text search) or the same fusion logic onto
a real ANN vector backend (FAISS, pgvector, a hosted vector DB) — the
fusion/reranking logic in this module doesn't care which backend produced
the ranked lists it's combining.

## Knowledge graph

`bitscrape.knowledge_graph.KnowledgeGraph` records (subject, predicate,
object) triples in a real, working directed graph (built on
`networkx.MultiDiGraph`, not reinvented), from two sources:

**Reliably, from structured item fields** (no NLP needed):
```python
from bitscrape.knowledge_graph import KnowledgeGraph

kg = KnowledgeGraph()
item = {"company": "Acme Corp", "founder": "Jane Doe", "hq": "Austin, TX"}
kg.add_item(item, subject_field="company",
            relations={"founded_by": "founder", "headquartered_in": "hq"})
# -> Acme Corp --[founded_by]--> Jane Doe
#    Acme Corp --[headquartered_in]--> Austin, TX
```

**Roughly, from free text** (heuristic entity extraction):
```python
entities = kg.add_entities_from_text(article_text, source="article-123")
# source --[mentions]--> each extracted entity
```

`extract_entities()` is a heuristic capitalized-phrase extractor — the
classic "proper noun sequence" baseline used before statistical NER became
cheap. It will over-match (sentence-initial capitals, acronyms) and
under-match (lowercase entity names, multi-word entities split by
stopwords). Treat it as a rough starting signal, not ground truth. For
production-grade entity extraction, pair this module with a real NER
model or an LLM prompt that populates `kg.add_relation()` directly — the
graph itself doesn't care where triples came from.

**Combine with entity resolution** (see [extractors/](../extractors/index.md))
before adding to the graph, so "Jon Smith" and "John Smith" resolve to one
node instead of two:
```python
from bitscrape.entity_resolution import EntityResolver

resolver = EntityResolver()
canonical_subject = resolver.resolve(item["founder"])
kg.add_relation("Acme Corp", "founded_by", canonical_subject)
```

**Export for real analysis:**
```python
kg.export_json("graph.json")
kg.export_graphml("graph.graphml")   # open directly in Gephi/Cytoscape
```

## See also

- [extractors/](../extractors/index.md) — canonicalization and entity resolution primitives that feed into this.
- [architecture/](../architecture/index.md) — how this fits alongside the rest of the crawl pipeline.
- [api/index.md#link-analysis-bitscrapelink_analysis](../api/index.md#link-analysis-bitscrapelink_analysis), [#incremental-recrawl-bitscraperecrawl](../api/index.md#incremental-recrawl-bitscraperecrawl), [#ranking-bitscraperanking](../api/index.md#ranking-bitscraperanking), [#knowledge-graph-bitscrapeknowledge_graph](../api/index.md#knowledge-graph-bitscrapeknowledge_graph) — full signatures.
