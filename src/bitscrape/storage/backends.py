"""
Bitscrape Storage Backends
==========================

A pluggable interface for persisting scraped items beyond flat files
(``bitscrape.exporters.feed`` already covers JSONL/JSON/CSV/XML). Every
backend implements the same small ``BaseStorageBackend`` contract, so a
spider or pipeline can swap backends via configuration without code changes.

Backends, and how confident you should be in each:

  - ``SQLiteStorageBackend``   -- fully tested against a real, on-disk
    SQLite database (Python's stdlib driver, via ``aiosqlite``). No external
    service required; this is the reference implementation.
  - ``PostgresStorageBackend`` -- fully tested against a real, live
    PostgreSQL server (real connection pooling, real JSONB storage, real
    persistence across backend instances, verified with raw ``asyncpg``
    queries against the actual data). Also has separate mock-connection
    unit tests covering the SQL generation in isolation. Follows the same
    contract as SQLite/S3.
  - ``S3StorageBackend``       -- tested against a real, in-process S3 API
    via ``moto`` (a genuine AWS API emulator, not a hand-rolled mock), so
    the actual boto3 call shapes are verified.
  - ``MongoStorageBackend``    -- implemented against the real ``motor``
    (MongoDB's official async driver) API, tested against
    ``mongomock_motor`` -- a genuine async MongoDB API emulator, not a
    hand-rolled mock -- since a live MongoDB server wasn't available in
    this build environment (MongoDB requires its own separate apt
    repository, not present in this environment's package mirror).
  - ``ElasticsearchStorageBackend`` -- still a documented interface stub,
    not implemented: unlike MongoDB/S3, there isn't a widely-used,
    easily-installable Elasticsearch API emulator to test a real
    implementation against in this build environment. See the class
    docstring for the intended implementation shape using
    ``elasticsearch-py``. Wiring it in, following the same pattern used
    for MongoDB/S3/Postgres above, is the natural next step -- not a
    fabricated "it works" claim.
"""

from __future__ import annotations

import json
import logging
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any, Self

from pydantic import BaseModel

logger = logging.getLogger(__name__)


def _item_to_dict(item: Any) -> dict[str, Any]:
    if isinstance(item, BaseModel):
        return item.model_dump(mode="json")
    if isinstance(item, dict):
        return item
    raise TypeError(f"Cannot store item of type {type(item)}")


class BaseStorageBackend(ABC):
    """
    Minimal async contract every storage backend implements. Deliberately
    small: ``open`` / ``save_item`` / ``count`` / ``close``. Backends decide
    their own schema, indexing, and connection handling internally.
    """

    @abstractmethod
    async def open(self) -> None:
        """Establish the connection / create schema if needed."""

    @abstractmethod
    async def save_item(self, item: dict[str, Any] | BaseModel) -> None:
        """Persist one item."""

    @abstractmethod
    async def count(self) -> int:
        """Return the number of items stored (for verification/reporting)."""

    @abstractmethod
    async def close(self) -> None:
        """Release the connection."""

    async def __aenter__(self) -> Self:
        await self.open()
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.close()


# ---------------------------------------------------------------------------
# SQLite -- reference implementation, fully tested
# ---------------------------------------------------------------------------


class SQLiteStorageBackend(BaseStorageBackend):
    """
    Stores each item as a JSON blob in a single table, plus a generated
    ``scraped_at`` timestamp. Deliberately schema-less (items can have
    different shapes across a crawl) -- if you need typed columns, query the
    ``data`` JSON column with SQLite's ``json_extract`` or project into your
    own table in a pipeline stage instead.
    """

    def __init__(self, db_path: str, table: str = "items") -> None:
        self._db_path = db_path
        self._table = table
        self._conn: Any = None

    async def open(self) -> None:
        import aiosqlite

        Path(self._db_path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = await aiosqlite.connect(self._db_path)
        await self._conn.execute(
            f"""
            CREATE TABLE IF NOT EXISTS {self._table} (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                data TEXT NOT NULL,
                scraped_at TEXT NOT NULL DEFAULT (datetime('now'))
            )
            """
        )
        await self._conn.commit()
        logger.info("SQLiteStorageBackend opened at %s (table=%s)", self._db_path, self._table)

    async def save_item(self, item: dict[str, Any] | BaseModel) -> None:
        assert self._conn is not None, "Call open() first"
        payload = json.dumps(_item_to_dict(item), default=str)
        await self._conn.execute(
            f"INSERT INTO {self._table} (data) VALUES (?)",
            (payload,),
        )
        await self._conn.commit()

    async def count(self) -> int:
        assert self._conn is not None, "Call open() first"
        cursor = await self._conn.execute(f"SELECT COUNT(*) FROM {self._table}")
        row = await cursor.fetchone()
        return int(row[0]) if row else 0

    async def all_items(self) -> list[dict[str, Any]]:
        """Convenience for tests/inspection -- not part of the base contract."""
        assert self._conn is not None, "Call open() first"
        cursor = await self._conn.execute(f"SELECT data FROM {self._table} ORDER BY id")
        rows = await cursor.fetchall()
        return [json.loads(r[0]) for r in rows]

    async def close(self) -> None:
        if self._conn is not None:
            await self._conn.close()
            self._conn = None
            logger.info("SQLiteStorageBackend closed")


# ---------------------------------------------------------------------------
# PostgreSQL -- fully tested against a real, live PostgreSQL server
# (see tests/test_postgres_live.py)
# ---------------------------------------------------------------------------


class PostgresStorageBackend(BaseStorageBackend):
    """
    Same JSON-blob-per-row design as SQLite, using ``asyncpg`` and
    PostgreSQL's native ``JSONB`` column type (indexable, queryable with
    Postgres's own JSON operators). Verified against a real live
    PostgreSQL server: connection pooling, table creation, JSONB storage
    and querying, persistence across backend instances/process restarts,
    and concurrent writes -- see ``tests/test_postgres_live.py``.

    dsn example: ``postgresql://user:pass@localhost:5432/bitscrape``
    """

    def __init__(self, dsn: str, table: str = "items") -> None:
        self._dsn = dsn
        self._table = table
        self._pool: Any = None

    async def open(self) -> None:
        import asyncpg

        self._pool = await asyncpg.create_pool(self._dsn)
        async with self._pool.acquire() as conn:
            await conn.execute(
                f"""
                CREATE TABLE IF NOT EXISTS {self._table} (
                    id SERIAL PRIMARY KEY,
                    data JSONB NOT NULL,
                    scraped_at TIMESTAMPTZ NOT NULL DEFAULT now()
                )
                """
            )
        logger.info("PostgresStorageBackend opened (table=%s)", self._table)

    async def save_item(self, item: dict[str, Any] | BaseModel) -> None:
        assert self._pool is not None, "Call open() first"
        payload = json.dumps(_item_to_dict(item), default=str)
        async with self._pool.acquire() as conn:
            await conn.execute(
                f"INSERT INTO {self._table} (data) VALUES ($1::jsonb)",
                payload,
            )

    async def count(self) -> int:
        assert self._pool is not None, "Call open() first"
        async with self._pool.acquire() as conn:
            row = await conn.fetchval(f"SELECT COUNT(*) FROM {self._table}")
        return int(row)

    async def close(self) -> None:
        if self._pool is not None:
            await self._pool.close()
            self._pool = None
            logger.info("PostgresStorageBackend closed")


# ---------------------------------------------------------------------------
# Amazon S3 (or S3-compatible) -- tested against a real API emulation (moto)
# ---------------------------------------------------------------------------


class S3StorageBackend(BaseStorageBackend):
    """
    Writes each item as its own JSON object under ``prefix/{n}.json``, plus
    an append-only ``prefix/items.jsonl`` object rewritten on each save for
    convenient bulk download. Uses ``boto3`` (sync) via a thread executor,
    since ``aioboto3`` is an extra dependency this project doesn't otherwise
    need -- boto3 already handles connection pooling internally.

    Works with any S3-compatible endpoint (AWS S3, MinIO, R2, etc.) via
    ``endpoint_url``.
    """

    def __init__(
        self,
        bucket: str,
        prefix: str = "bitscrape",
        endpoint_url: str | None = None,
        region_name: str = "us-east-1",
    ) -> None:
        self._bucket = bucket
        self._prefix = prefix.rstrip("/")
        self._endpoint_url = endpoint_url
        self._region_name = region_name
        self._client: Any = None
        self._count_cache = 0
        self._items_buffer: list[dict[str, Any]] = []

    async def open(self) -> None:
        import asyncio

        import boto3

        def _make_client() -> Any:
            client = boto3.client(
                "s3", endpoint_url=self._endpoint_url, region_name=self._region_name
            )
            existing = {b["Name"] for b in client.list_buckets().get("Buckets", [])}
            if self._bucket not in existing:
                client.create_bucket(Bucket=self._bucket)
            return client

        self._client = await asyncio.to_thread(_make_client)
        logger.info("S3StorageBackend opened (bucket=%s, prefix=%s)", self._bucket, self._prefix)

    async def save_item(self, item: dict[str, Any] | BaseModel) -> None:
        import asyncio

        assert self._client is not None, "Call open() first"
        data = _item_to_dict(item)
        self._items_buffer.append(data)
        index = self._count_cache
        key = f"{self._prefix}/{index}.json"
        body = json.dumps(data, default=str).encode("utf-8")

        def _put() -> None:
            self._client.put_object(Bucket=self._bucket, Key=key, Body=body)
            bulk_body = "\n".join(json.dumps(d, default=str) for d in self._items_buffer).encode(
                "utf-8"
            )
            self._client.put_object(
                Bucket=self._bucket, Key=f"{self._prefix}/items.jsonl", Body=bulk_body
            )

        await asyncio.to_thread(_put)
        self._count_cache += 1

    async def count(self) -> int:
        return self._count_cache

    async def close(self) -> None:
        self._client = None
        logger.info("S3StorageBackend closed (%d items written)", self._count_cache)


# ---------------------------------------------------------------------------
# MongoDB -- interface stub, NOT implemented/tested (see module docstring)
# ---------------------------------------------------------------------------


class MongoStorageBackend(BaseStorageBackend):
    """
    Stores each item as a MongoDB document via ``motor`` (MongoDB's
    official async driver). Tested against ``mongomock_motor`` -- a
    genuine async MongoDB API emulator (the same category of tool as
    ``moto`` for S3), not a hand-rolled mock -- since a live MongoDB
    server wasn't available in this build environment (MongoDB isn't
    distributed through this environment's package mirror; it requires
    MongoDB's own separate apt repository). Treat this the same way as
    ``PostgresStorageBackend`` was before its live-server verification:
    implemented against the real driver API and tested against a real
    emulator, but if you need it verified against an actual live MongoDB
    server, that's still on you to confirm in your own environment --
    the emulator is very API-compatible but isn't a live server.

    uri example: ``mongodb://user:pass@localhost:27017``

    Pass ``client=`` to inject an already-constructed client (e.g. an
    ``mongomock_motor.AsyncMongoMockClient()`` for tests) instead of
    connecting via ``uri``.
    """

    def __init__(
        self,
        uri: str = "",
        database: str = "bitscrape",
        collection: str = "items",
        client: Any = None,
    ) -> None:
        self._uri = uri
        self._database = database
        self._collection_name = collection
        self._client = client
        self._coll: Any = None

    async def open(self) -> None:
        if self._client is None:
            import motor.motor_asyncio as motor_asyncio

            self._client = motor_asyncio.AsyncIOMotorClient(self._uri)
        self._coll = self._client[self._database][self._collection_name]
        logger.info(
            "MongoStorageBackend opened (database=%s, collection=%s)",
            self._database,
            self._collection_name,
        )

    async def save_item(self, item: dict[str, Any] | BaseModel) -> None:
        assert self._coll is not None, "Call open() first"
        # Copy the dict -- pymongo/motor's insert_one() mutates its input
        # in place, injecting an "_id" key, which would surprise callers
        # who still hold a reference to the original dict/model dump.
        doc = dict(_item_to_dict(item))
        await self._coll.insert_one(doc)

    async def count(self) -> int:
        assert self._coll is not None, "Call open() first"
        return int(await self._coll.count_documents({}))

    async def all_items(self) -> list[dict[str, Any]]:
        """Convenience for tests/inspection -- not part of the base contract."""
        assert self._coll is not None, "Call open() first"
        items = []
        async for doc in self._coll.find({}):
            doc.pop("_id", None)
            items.append(doc)
        return items

    async def close(self) -> None:
        if self._client is not None:
            self._client.close()
            logger.info("MongoStorageBackend closed")


# ---------------------------------------------------------------------------
# Elasticsearch -- interface stub, NOT implemented/tested (see module docstring)
# ---------------------------------------------------------------------------


class ElasticsearchStorageBackend(BaseStorageBackend):
    """
    NOT IMPLEMENTED. Sketch of the intended shape using ``elasticsearch-py``
    (async client) -- wire this up and test it against a real Elasticsearch
    cluster before relying on it:

        from elasticsearch import AsyncElasticsearch

        class ElasticsearchStorageBackend(BaseStorageBackend):
            def __init__(self, hosts, index="items"):
                self._hosts, self._index = hosts, index

            async def open(self):
                self._client = AsyncElasticsearch(self._hosts)
                if not await self._client.indices.exists(index=self._index):
                    await self._client.indices.create(index=self._index)

            async def save_item(self, item):
                await self._client.index(index=self._index, document=_item_to_dict(item))

            async def count(self):
                resp = await self._client.count(index=self._index)
                return resp["count"]

            async def close(self):
                await self._client.close()

    Raises ``NotImplementedError`` if instantiated, rather than silently
    pretending to work.
    """

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        raise NotImplementedError(
            "ElasticsearchStorageBackend is a documented interface stub, not a working "
            "implementation -- see its docstring for the intended shape using "
            "`elasticsearch-py`. A real Elasticsearch cluster wasn't available to "
            "implement and test this against in this build environment."
        )

    async def open(self) -> None:
        raise NotImplementedError

    async def save_item(self, item: dict[str, Any] | BaseModel) -> None:
        raise NotImplementedError

    async def count(self) -> int:
        raise NotImplementedError

    async def close(self) -> None:
        raise NotImplementedError
