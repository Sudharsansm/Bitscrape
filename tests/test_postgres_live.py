"""
Real integration tests for PostgresStorageBackend against a LIVE local
PostgreSQL server -- not the mock-connection tests in test_storage_backends.py.

Previously this backend was "implemented against the real asyncpg API,
unit-tested with a mock connection" because no live PostgreSQL server was
available in this project's build environment. A live server is now
available, so this file verifies the real thing end-to-end: real
connection pooling, real JSONB storage, real table creation, real
persistence across backend instances.

Requires PostgreSQL running locally with:
    database: bitscrape_test
    user: postgres / password: postgres
    host: 127.0.0.1  port: 5432
"""

from __future__ import annotations

import json

import pytest
import pytest_asyncio

from bitscrape.storage.backends import PostgresStorageBackend

DSN = "postgresql://postgres:postgres@127.0.0.1:5432/bitscrape_test"


@pytest_asyncio.fixture
async def clean_table():
    """Cleans up test tables after each test."""
    import asyncpg

    yield
    conn = await asyncpg.connect(DSN)
    try:
        tables = await conn.fetch(
            "SELECT tablename FROM pg_tables WHERE schemaname='public' AND tablename LIKE 'test_%'"
        )
        for row in tables:
            await conn.execute(f"DROP TABLE IF EXISTS {row['tablename']}")
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_open_creates_table_on_real_server(clean_table):
    backend = PostgresStorageBackend(DSN, table="test_pg_create")
    await backend.open()
    try:
        assert await backend.count() == 0
    finally:
        await backend.close()


@pytest.mark.asyncio
async def test_save_and_count_real_items(clean_table):
    backend = PostgresStorageBackend(DSN, table="test_pg_save")
    await backend.open()
    try:
        await backend.save_item({"title": "Item 1", "price": 9.99})
        await backend.save_item({"title": "Item 2", "price": 19.99})
        assert await backend.count() == 2
    finally:
        await backend.close()


@pytest.mark.asyncio
async def test_data_is_genuinely_stored_as_jsonb(clean_table):
    """Verifies the data actually lands in Postgres as queryable JSONB,
    not just that count() increments -- query it back with raw asyncpg
    using Postgres's own JSON operators."""
    import asyncpg

    backend = PostgresStorageBackend(DSN, table="test_pg_jsonb")
    await backend.open()
    try:
        await backend.save_item({"title": "Widget", "category": "tools"})
        await backend.save_item({"title": "Gadget", "category": "electronics"})
    finally:
        await backend.close()

    conn = await asyncpg.connect(DSN)
    try:
        rows = await conn.fetch(
            "SELECT data->>'title' AS title FROM test_pg_jsonb WHERE data->>'category' = 'tools'"
        )
        assert len(rows) == 1
        assert rows[0]["title"] == "Widget"
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_data_persists_across_backend_instances(clean_table):
    """Simulates a process restart: a fresh PostgresStorageBackend
    instance against the same table sees data written by a previous one."""
    backend1 = PostgresStorageBackend(DSN, table="test_pg_persist")
    await backend1.open()
    await backend1.save_item({"title": "Persisted Item"})
    await backend1.close()

    backend2 = PostgresStorageBackend(DSN, table="test_pg_persist")
    await backend2.open()
    try:
        assert await backend2.count() == 1
    finally:
        await backend2.close()


@pytest.mark.asyncio
async def test_handles_pydantic_model(clean_table):
    from pydantic import BaseModel

    class MyItem(BaseModel):
        title: str
        price: float

    backend = PostgresStorageBackend(DSN, table="test_pg_pydantic")
    await backend.open()
    try:
        await backend.save_item(MyItem(title="Widget", price=9.99))
        assert await backend.count() == 1
    finally:
        await backend.close()


@pytest.mark.asyncio
async def test_connection_pool_handles_concurrent_writes(clean_table):
    """Real connection pooling under concurrent load -- asyncpg's pool
    should handle multiple simultaneous save_item() calls without
    dropping any."""
    import asyncio

    backend = PostgresStorageBackend(DSN, table="test_pg_concurrent")
    await backend.open()
    try:
        await asyncio.gather(*(backend.save_item({"index": i}) for i in range(20)))
        assert await backend.count() == 20
    finally:
        await backend.close()


@pytest.mark.asyncio
async def test_custom_table_name_isolates_data(clean_table):
    backend_a = PostgresStorageBackend(DSN, table="test_pg_table_a")
    backend_b = PostgresStorageBackend(DSN, table="test_pg_table_b")
    await backend_a.open()
    await backend_b.open()
    try:
        await backend_a.save_item({"x": 1})
        assert await backend_a.count() == 1
        assert await backend_b.count() == 0  # different table, no leakage
    finally:
        await backend_a.close()
        await backend_b.close()


@pytest.mark.asyncio
async def test_special_characters_in_json_values_round_trip_correctly(clean_table):
    """Confirms JSONB storage correctly round-trips values with quotes,
    unicode, and nested structures -- not just simple flat strings."""
    backend = PostgresStorageBackend(DSN, table="test_pg_special_chars")
    await backend.open()
    try:
        item = {
            "title": 'Widget "Deluxe" & Co. \u2014 \u65e5\u672c\u8a9e',
            "tags": ["a", "b", "c"],
            "nested": {"key": "value"},
        }
        await backend.save_item(item)
    finally:
        await backend.close()

    import asyncpg

    conn = await asyncpg.connect(DSN)
    try:
        row = await conn.fetchrow("SELECT data FROM test_pg_special_chars")
        stored = json.loads(row["data"]) if isinstance(row["data"], str) else row["data"]
        assert stored["title"] == 'Widget "Deluxe" & Co. \u2014 \u65e5\u672c\u8a9e'
        assert stored["tags"] == ["a", "b", "c"]
        assert stored["nested"] == {"key": "value"}
    finally:
        await conn.close()
