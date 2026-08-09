# FAQ

**Is Bitscrape like Scrapy?**
Conceptually similar (async spiders, middleware, pipelines, a CLI), but a
separate, smaller codebase. If you know Scrapy, most of the vocabulary
(`Spider`, `start_urls`, `parse()`, `self.follow()`, pipelines, item
exporters) will feel familiar. `bitscrape startproject` even writes a
`scrapy.cfg`-shaped file for tooling compatibility.

**Does it bypass CAPTCHAs or anti-bot detection?**
No, deliberately. See [security/](../security/index.md) -- this project
will not build CAPTCHA-solving or fingerprint-spoofing tooling regardless
of how a request for it is framed.

**Can it render JavaScript?**
Yes, via Playwright (`use_playwright=True`) -- see [browser/](../browser/index.md).
Requires a separate `playwright install chromium` step beyond the Python
package.

**Does it respect robots.txt?**
Yes, by default (`Settings.robotstxt_obey = True`), including
`Crawl-delay` and `Sitemap` discovery, and it fails *safe* (blocks) rather
than *open* (allows) on a robots.txt fetch error. Meta-robots
(`noindex`/`nofollow`) is also respected by default. See
[crawling/](../crawling/index.md).

**Can I run it distributed across multiple machines?**
Yes -- set `scheduler_use_redis=True` and `distributed_throttle_enabled=True`
pointing at a shared Redis instance; the same spider code runs unchanged.
See [scheduler/](../scheduler/index.md) and
[tutorials/](../tutorials/index.md) Tutorial 4.

**Does `self.follow()` handle relative URLs?**
Yes, as of 0.8.0 — `self.follow(href)` resolves relative URLs against the
current response automatically when called from within a callback. This
was a real, fixed bug on earlier versions; see
[user-guide/index.md#relative-urls-fixed-in-080](../user-guide/index.md#relative-urls-fixed-in-080)
for the concurrency-safe mechanism (`contextvars`, not a shared instance
attribute) and the one remaining edge case (calling `follow()` before any
response exists, e.g. in `start_requests()`, still needs an absolute URL).

**Does it compute text embeddings for semantic search?**
No -- `bitscrape.ranking` gives you correct BM25 scoring, a vector
similarity index, and Reciprocal Rank Fusion, but you must compute
embeddings yourself (any model/API) and pass them in. See
[ai/](../ai/index.md).

**Is the entity/knowledge-graph extraction "real NLP"?**
`extract_entities()` is a heuristic capitalized-phrase extractor (the
classic pre-statistical-NER baseline) -- useful as a rough signal, not
production-grade NER. `EntityResolver` is heuristic string similarity, not
a trained entity-linking model. Both are explicitly scoped this way in
their docstrings; pair them with a real NER model/LLM if you need
production-grade extraction. See [ai/](../ai/index.md) and
[extractors/](../extractors/index.md).

**What storage backends are actually production-ready?**
`SQLiteStorageBackend`, `S3StorageBackend`, and (as of 0.8.0)
`PostgresStorageBackend` are all fully tested against real
databases/emulations (real on-disk SQLite, real `moto` S3 emulation, real
live PostgreSQL server). `MongoStorageBackend` (also new in 0.8.0) is
tested against `mongomock_motor`, a genuine emulator -- very likely
correct, but confirm against your own live MongoDB if you need that exact
guarantee. `ElasticsearchStorageBackend` remains an honest stub, not
implemented (no comparable emulator exists). See [storage/](../storage/index.md).

**Are the Kubernetes/Docker manifests actually deployed and tested?**
They're syntax- and structure-validated (13 automated tests), not
live-cluster-tested -- no Docker daemon or Kubernetes cluster was available
in this project's build environment. Validate against your own
environment before production use. See [deployment/](../deployment/index.md).

**What Python versions are supported?**
3.11 and 3.12 (per `pyproject.toml`'s `requires-python = ">=3.11"` and the
classifiers listed there).

**How do I contribute?**
See [`CONTRIBUTING.md`](../../CONTRIBUTING.md) and
[developer-guide/](../developer-guide/index.md).

**Where do I report a security issue?**
See [`SECURITY.md`](../../SECURITY.md) -- please don't open a public issue for
anything sensitive.

**What license is this under?**
MIT -- see [`LICENSE`](../../LICENSE).
