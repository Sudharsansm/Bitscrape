# Extractors

Structured extraction that goes beyond raw CSS/XPath selection: URL
canonicalization, near-duplicate content detection, and entity resolution.
(For extracting entities *and* building a graph of relationships between
them, see [ai/](../ai/index.md#knowledge-graph); this page covers the
underlying extraction primitives.)

## URL canonicalization

`bitscrape.canonicalize.canonicalize_url()` normalizes a URL so that two
URLs meaning "the same page" produce the same string:

```python
from bitscrape.canonicalize import canonicalize_url

canonicalize_url("HTTPS://Example.com:443/page/?utm_source=x&b=2&a=1#top")
# -> "https://example.com/page?a=1&b=2"
```

What it normalizes:
- Lowercases scheme and host (preserving userinfo case if present, e.g. `user:Pass@`).
- Strips default ports (`:443` for https, `:80` for http).
- Strips known tracking parameters (`utm_*` prefix, plus `fbclid`, `gclid`,
  `msclkid`, `ref`, `session_id`, `_ga`, `_gl`, `igshid`, `mkt_tok`, `spm`,
  and any you pass via `extra_tracking_params`).
- Sorts remaining query parameters (so `?a=1&b=2` and `?b=2&a=1` match).
- Strips the fragment (`#section`) by default.
- Strips a trailing slash from the path (except the bare root `/`).

Deliberately conservative — it won't strip a parameter that might carry
meaning (like `?page=2`), only ones on the known tracking-parameter list.

## Redirect chain resolution

```python
from bitscrape.canonicalize import resolve_redirect_chain, RedirectLoopError

final_url = resolve_redirect_chain([
    "https://example.com/old",
    "https://example.com/new",
])
# -> "https://example.com/new" (canonicalized)
```

Raises `RedirectLoopError` if a URL (after canonicalization) appears twice
in the chain — this catches loops even when disguised by different
tracking query parameters at each hop, since canonicalization strips them
before comparing. Raises `ValueError` if the chain exceeds `max_hops`
(default 20) or is empty.

## Near-duplicate content detection (SimHash)

Exact-hash comparison misses pages that are byte-different but
substantively the same (a changed timestamp in the footer, a different
tracking pixel). SimHash catches this:

```python
from bitscrape.canonicalize import compute_fingerprint, is_near_duplicate

is_near_duplicate(original_text, edited_text, max_hamming_distance=5)
# True if the two texts' 64-bit SimHash fingerprints differ in at most 5 bits
```

`compute_fingerprint(text, shingle_size=4)` builds a 64-bit fingerprint
from overlapping word-shingles; `ContentFingerprint.hamming_distance()`
compares two fingerprints. Identical text has distance 0; completely
unrelated text typically has a large distance (>10 for reasonably-sized
documents). The right threshold depends on document length and how
aggressive you want deduplication to be — shorter documents are more
sensitive to any given edit, so a fixed default threshold (3, conservative)
may need loosening for your corpus.

## Entity resolution

Recognizing that different textual mentions likely refer to the same
real-world entity — "Jon Smith", "John Smith", "J. Smith" clustering
together. This is a **heuristic string-similarity** approach, explicitly
not a trained entity-linking model (that would need a reference knowledge
base like Wikidata to link against, which isn't bundled here).

```python
from bitscrape.entity_resolution import EntityResolver

resolver = EntityResolver(similarity_threshold=0.85)
mapping = resolver.resolve_all(["John Smith", "Jon Smith", "J. Smith", "Maria Garcia"])
# {"John Smith": "John Smith", "Jon Smith": "John Smith",
#  "J. Smith": "John Smith", "Maria Garcia": "Maria Garcia"}
```

`similarity(name_a, name_b)` (0..1) combines, in order of preference:
1. Exact match after normalization (lowercase, punctuation stripped) → `1.0`.
2. Initials/abbreviation match (same last token, each earlier token a
   prefix of the corresponding one — "J Smith" vs "John Smith") → `0.95`.
3. Substring containment (one name's tokens are a subset of the other's —
   "Acme" vs "Acme Corp") → `0.9`.
4. Otherwise, `difflib.SequenceMatcher` ratio (catches typos/minor
   variations).

Raise `similarity_threshold` for stricter clustering (fewer false merges,
more missed matches); lower it for the opposite trade-off.

## Combining these

A typical pipeline: canonicalize URLs as you discover them → deduplicate
pages via SimHash before extracting entities from their content → resolve
entity mentions across pages → feed resolved canonical names into a
`KnowledgeGraph` (see [ai/](../ai/index.md)) instead of treating "Jon
Smith" and "John Smith" as unrelated nodes.

## See also

- [ai/](../ai/index.md) — knowledge graph construction and semantic ranking.
- [crawling/](../crawling/index.md) — where canonicalization fits into the request lifecycle.
- [api/index.md#canonicalization-bitscrapecanonicalize](../api/index.md#canonicalization-bitscrapecanonicalize) and [#entity-resolution-bitscrapeentity_resolution](../api/index.md#entity-resolution-bitscrapeentity_resolution) — full signatures.
