# Storage

Pluggable storage backends for persisting scraped items beyond flat files
(`bitscrape.exporters.feed` already covers JSONL/JSON/CSV/XML — use storage
backends when you want items in a queryable database or object store
instead of/in addition to a file).

Every backend implements the same small `BaseStorageBackend` contract:

```python
class BaseStorageBackend(ABC):
    async def open(self) -> None
    async def save_item(self, item: dict | BaseModel) -> None
    async def count(self) -> int
    async def close(self) -> None
    # also usable as: async with backend: ...
```

## Confidence level per backend

| Backend | Status |
|---|---|
| `SQLiteStorageBackend` | **Fully tested** against a real, on-disk SQLite database. The reference implementation. |
| `S3StorageBackend` | **Tested against a real S3 API emulation** (`moto`, a genuine AWS API emulator, not a hand-rolled mock) — including verifying the actual objects written. |
| `PostgresStorageBackend` | **Fully tested against a real, live PostgreSQL server** (as of 0.8.0) — real connection pooling, real JSONB storage/querying, persistence across backend instances/process restarts, concurrent writes. Same confidence tier as SQLite/S3. |
| `MongoStorageBackend` | **Implemented against the real `motor` API, tested against `mongomock_motor`** (as of 0.8.0) — a genuine async MongoDB API emulator (same category as `moto`), not a hand-rolled mock. No longer a stub. A live MongoDB server wasn't available (needs its own separate apt repository), so treat this the same tier Postgres was at before a live server became available for it — very likely correct, but confirm against your own live server if you need that specific guarantee. |
| `ElasticsearchStorageBackend` | **Documented stub.** Raises `NotImplementedError` on instantiation. No comparable, easily-installable Elasticsearch emulator exists (unlike Mongo/S3) to test a real implementation against. |

## SQLite

```python
from bitscrape.storage.backends import SQLiteStorageBackend

backend = SQLiteStorageBackend("items.db", table="items")
await backend.open()
await backend.save_item({"title": "Widget", "price": 9.99})
print(await backend.count())      # 1
await backend.close()

# Or as a context manager:
async with SQLiteStorageBackend("items.db") as backend:
    await backend.save_item({"title": "Gadget"})
```

Stores each item as a JSON blob in a single table plus a generated
`scraped_at` timestamp — deliberately schema-less, since items can differ
in shape across a crawl. Query the `data` column with SQLite's
`json_extract()` if you need typed queries, or project into your own table
in a pipeline stage instead.

## S3 (or any S3-compatible endpoint)

```python
from bitscrape.storage.backends import S3StorageBackend

backend = S3StorageBackend(bucket="my-bucket", prefix="crawl1")
# For MinIO/R2/other S3-compatible services:
backend = S3StorageBackend(bucket="my-bucket", endpoint_url="https://my-minio:9000")

await backend.open()   # creates the bucket if it doesn't exist
await backend.save_item({"title": "Widget"})
await backend.close()
```

Writes each item as its own object (`{prefix}/{n}.json`), plus a rewritten
`{prefix}/items.jsonl` on every save for convenient bulk download. Uses
`boto3` (sync) via a background thread, since `aioboto3` is an extra
dependency this project doesn't otherwise need.

## PostgreSQL

```python
from bitscrape.storage.backends import PostgresStorageBackend

# Fully tested against a real live server as of 0.8.0 -- see
# tests/test_postgres_live.py and tests/test_pipelines.py.
backend = PostgresStorageBackend("postgresql://user:pass@localhost:5432/bitscrape")
await backend.open()   # creates the table if it doesn't exist, using a connection pool
await backend.save_item({"title": "Widget"})
await backend.close()
```

Same JSON-blob-per-row design as SQLite, using PostgreSQL's native `JSONB`
column type (indexable and queryable with Postgres's own JSON operators).
**Run your own integration test against a real server** before depending
on this in production — see the confidence table above.

## MongoDB / Elasticsearch (not implemented)

```python
from bitscrape.storage.backends import MongoStorageBackend

backend = MongoStorageBackend(...)   # raises NotImplementedError immediately
```

Both raise `NotImplementedError` with a message pointing you to the
class's docstring, which shows the intended implementation shape (using
`motor` for Mongo, `elasticsearch-py` for Elasticsearch). If you implement
either, follow the same pattern as `SQLiteStorageBackend`/`S3StorageBackend`
— test against a real instance, not a mock, before calling it done.

## Using a backend with the plugin system

The easiest way to wire a storage backend into a running crawl is
`StorageConnectorPlugin` (see [plugins/](../plugins/index.md)):

```python
from bitscrape.plugins import PluginManager, StorageConnectorPlugin
from bitscrape.storage.backends import SQLiteStorageBackend

pm = PluginManager()
pm.register_plugin(StorageConnectorPlugin(SQLiteStorageBackend("out.db")))

engine = await bitscrape.build_engine(MySpider(), bitscrape.Settings(), plugin_manager=pm)
await engine.run()
```

This opens the backend on `spider_opened`, writes every item as it's
scraped, and closes the backend on `spider_closed` — no manual lifecycle
management needed.

## See also

- [plugins/](../plugins/index.md) — `StorageConnectorPlugin`.
- [tutorials/](../tutorials/index.md) — Tutorial 5, a full worked SQLite example.
- [api/index.md#storage-backends-bitscrapestoragebackends](../api/index.md#storage-backends-bitscrapestoragebackends) — full signatures.
