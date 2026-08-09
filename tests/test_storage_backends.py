"""
Tests for bitscrape.storage.backends.

  - SQLite: real, on-disk database -- full integration test.
  - S3: real S3 API emulation via moto (not a hand-rolled mock).
  - Postgres: unit-tested with a mock asyncpg pool here (see
    tests/test_postgres_live.py for full integration tests against a real
    live PostgreSQL server).
  - Mongo: see tests/test_mongo_backend.py for full tests against a real
    mongomock_motor emulator. No longer raises NotImplementedError.
  - Elasticsearch: confirms it still fails loudly (NotImplementedError)
    rather than silently pretending to work -- no widely-available,
    easily-installable Elasticsearch emulator existed in this build
    environment to implement and test a real backend against.
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from bitscrape.storage.backends import (
    ElasticsearchStorageBackend,
    MongoStorageBackend,
    PostgresStorageBackend,
    S3StorageBackend,
    SQLiteStorageBackend,
)


# ---------------------------------------------------------------------------
# SQLite -- real, full integration test
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_sqlite_save_and_count(tmp_path):
    db_path = str(tmp_path / "items.db")
    backend = SQLiteStorageBackend(db_path)
    await backend.open()
    try:
        assert await backend.count() == 0
        await backend.save_item({"title": "Item 1"})
        await backend.save_item({"title": "Item 2"})
        assert await backend.count() == 2
        items = await backend.all_items()
        assert [i["title"] for i in items] == ["Item 1", "Item 2"]
    finally:
        await backend.close()


@pytest.mark.asyncio
async def test_sqlite_persists_across_reopen(tmp_path):
    db_path = str(tmp_path / "persist.db")
    backend1 = SQLiteStorageBackend(db_path)
    await backend1.open()
    await backend1.save_item({"title": "Persisted"})
    await backend1.close()

    backend2 = SQLiteStorageBackend(db_path)
    await backend2.open()
    try:
        assert await backend2.count() == 1
    finally:
        await backend2.close()


@pytest.mark.asyncio
async def test_sqlite_handles_pydantic_model(tmp_path):
    from pydantic import BaseModel

    class MyItem(BaseModel):
        title: str
        price: float

    db_path = str(tmp_path / "pydantic.db")
    backend = SQLiteStorageBackend(db_path)
    await backend.open()
    try:
        await backend.save_item(MyItem(title="Widget", price=9.99))
        items = await backend.all_items()
        assert items[0] == {"title": "Widget", "price": 9.99}
    finally:
        await backend.close()


@pytest.mark.asyncio
async def test_sqlite_rejects_unsupported_item_type(tmp_path):
    db_path = str(tmp_path / "bad.db")
    backend = SQLiteStorageBackend(db_path)
    await backend.open()
    try:
        with pytest.raises(TypeError):
            await backend.save_item("not a dict or model")  # type: ignore[arg-type]
    finally:
        await backend.close()


@pytest.mark.asyncio
async def test_sqlite_context_manager():
    import tempfile
    from pathlib import Path

    with tempfile.TemporaryDirectory() as d:
        db_path = str(Path(d) / "ctx.db")
        async with SQLiteStorageBackend(db_path) as backend:
            await backend.save_item({"x": 1})
            assert await backend.count() == 1


@pytest.mark.asyncio
async def test_sqlite_custom_table_name(tmp_path):
    db_path = str(tmp_path / "custom.db")
    backend = SQLiteStorageBackend(db_path, table="scraped_products")
    await backend.open()
    try:
        await backend.save_item({"sku": "abc123"})
        assert await backend.count() == 1
    finally:
        await backend.close()


# ---------------------------------------------------------------------------
# S3 -- real S3 API emulation via moto
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_s3_save_and_count():
    from moto import mock_aws

    with mock_aws():
        backend = S3StorageBackend(bucket="test-bitscrape-bucket", prefix="crawl1")
        await backend.open()
        try:
            await backend.save_item({"title": "Item 1"})
            await backend.save_item({"title": "Item 2"})
            assert await backend.count() == 2

            # Verify the objects genuinely exist in the (emulated) bucket.
            import boto3

            client = boto3.client("s3", region_name="us-east-1")
            obj0 = client.get_object(Bucket="test-bitscrape-bucket", Key="crawl1/0.json")
            data0 = json.loads(obj0["Body"].read())
            assert data0 == {"title": "Item 1"}

            bulk = client.get_object(Bucket="test-bitscrape-bucket", Key="crawl1/items.jsonl")
            lines = bulk["Body"].read().decode().splitlines()
            assert len(lines) == 2
        finally:
            await backend.close()


@pytest.mark.asyncio
async def test_s3_creates_bucket_if_missing():
    from moto import mock_aws

    with mock_aws():
        backend = S3StorageBackend(bucket="brand-new-bucket")
        await backend.open()  # should not raise even though bucket doesn't exist yet
        try:
            await backend.save_item({"ok": True})
            assert await backend.count() == 1
        finally:
            await backend.close()


@pytest.mark.asyncio
async def test_s3_handles_pydantic_model():
    from moto import mock_aws
    from pydantic import BaseModel

    class MyItem(BaseModel):
        name: str

    with mock_aws():
        backend = S3StorageBackend(bucket="pydantic-bucket")
        await backend.open()
        try:
            await backend.save_item(MyItem(name="Widget"))
            assert await backend.count() == 1
        finally:
            await backend.close()


# ---------------------------------------------------------------------------
# Postgres -- mock-tested (no live server available; see module docstring)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_postgres_open_creates_table_via_mock_pool():
    mock_conn = AsyncMock()
    mock_pool = MagicMock()
    mock_pool.acquire.return_value.__aenter__ = AsyncMock(return_value=mock_conn)
    mock_pool.acquire.return_value.__aexit__ = AsyncMock(return_value=False)

    with patch("asyncpg.create_pool", new=AsyncMock(return_value=mock_pool)):
        backend = PostgresStorageBackend("postgresql://fake/dsn")
        await backend.open()

    mock_conn.execute.assert_called_once()
    assert "CREATE TABLE IF NOT EXISTS items" in mock_conn.execute.call_args[0][0]


@pytest.mark.asyncio
async def test_postgres_save_item_calls_insert_with_jsonb():
    mock_conn = AsyncMock()
    mock_pool = MagicMock()
    mock_pool.acquire.return_value.__aenter__ = AsyncMock(return_value=mock_conn)
    mock_pool.acquire.return_value.__aexit__ = AsyncMock(return_value=False)

    with patch("asyncpg.create_pool", new=AsyncMock(return_value=mock_pool)):
        backend = PostgresStorageBackend("postgresql://fake/dsn")
        await backend.open()
        await backend.save_item({"title": "Item 1"})

    insert_call = mock_conn.execute.call_args_list[-1]
    assert "INSERT INTO items" in insert_call[0][0]
    assert json.loads(insert_call[0][1]) == {"title": "Item 1"}


@pytest.mark.asyncio
async def test_postgres_count_calls_fetchval():
    mock_conn = AsyncMock()
    mock_conn.fetchval = AsyncMock(return_value=5)
    mock_pool = MagicMock()
    mock_pool.acquire.return_value.__aenter__ = AsyncMock(return_value=mock_conn)
    mock_pool.acquire.return_value.__aexit__ = AsyncMock(return_value=False)

    with patch("asyncpg.create_pool", new=AsyncMock(return_value=mock_pool)):
        backend = PostgresStorageBackend("postgresql://fake/dsn")
        await backend.open()
        assert await backend.count() == 5


# ---------------------------------------------------------------------------
# Elasticsearch -- must still fail loudly, not silently pretend to work
# (Mongo is now a real implementation -- see tests/test_mongo_backend.py)
# ---------------------------------------------------------------------------


def test_mongo_backend_no_longer_raises_not_implemented():
    """Regression guard: MongoStorageBackend used to be a documented stub
    that raised NotImplementedError on instantiation. It's now a real
    implementation (see tests/test_mongo_backend.py for full coverage) --
    this just confirms the stub-era behavior is gone, so nobody
    accidentally reverts it."""
    from mongomock_motor import AsyncMongoMockClient

    backend = MongoStorageBackend(database="db", client=AsyncMongoMockClient())
    assert backend is not None


def test_elasticsearch_backend_raises_not_implemented():
    with pytest.raises(NotImplementedError):
        ElasticsearchStorageBackend(["http://fake:9200"])
