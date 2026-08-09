"""
Tests for MongoStorageBackend, implemented for real against motor's
official async MongoDB API and tested against mongomock_motor -- a genuine
async MongoDB API emulator (the same category of tool as moto for S3),
injected via the `client=` constructor parameter. A live MongoDB server
wasn't available in this build environment (it requires its own separate
apt repository not present in this environment's package mirror), so this
is "verified against a real emulator," the same honest tier PostgreSQL was
at before a live server became available -- not a live-server guarantee,
but a genuine, non-trivial API-compatibility test, not a hand-rolled mock
standing in for the assertion itself.
"""

from __future__ import annotations

import pytest
from mongomock_motor import AsyncMongoMockClient
from pydantic import BaseModel

from bitscrape.storage.backends import MongoStorageBackend


def _make_backend(collection: str = "items") -> MongoStorageBackend:
    client = AsyncMongoMockClient()
    return MongoStorageBackend(database="test_db", collection=collection, client=client)


@pytest.mark.asyncio
async def test_open_and_count_empty():
    backend = _make_backend()
    await backend.open()
    try:
        assert await backend.count() == 0
    finally:
        await backend.close()


@pytest.mark.asyncio
async def test_save_and_count_items():
    backend = _make_backend()
    await backend.open()
    try:
        await backend.save_item({"title": "Item 1"})
        await backend.save_item({"title": "Item 2"})
        assert await backend.count() == 2
    finally:
        await backend.close()


@pytest.mark.asyncio
async def test_saved_items_are_genuinely_queryable():
    backend = _make_backend()
    await backend.open()
    try:
        await backend.save_item({"title": "Widget", "category": "tools"})
        await backend.save_item({"title": "Gadget", "category": "electronics"})
        items = await backend.all_items()
        titles = {item["title"] for item in items}
        assert titles == {"Widget", "Gadget"}
        assert all("_id" not in item for item in items)
    finally:
        await backend.close()


@pytest.mark.asyncio
async def test_handles_pydantic_model():
    class MyItem(BaseModel):
        title: str
        price: float

    backend = _make_backend()
    await backend.open()
    try:
        await backend.save_item(MyItem(title="Widget", price=9.99))
        items = await backend.all_items()
        assert items[0]["title"] == "Widget"
        assert items[0]["price"] == 9.99
    finally:
        await backend.close()


@pytest.mark.asyncio
async def test_save_item_does_not_mutate_callers_dict():
    """MongoDB drivers mutate the dict passed to insert_one() in place,
    injecting an _id key -- confirm we defensively copy so the caller's
    original dict isn't silently changed underneath them."""
    backend = _make_backend()
    await backend.open()
    try:
        original = {"title": "Widget"}
        await backend.save_item(original)
        assert original == {"title": "Widget"}
    finally:
        await backend.close()


@pytest.mark.asyncio
async def test_different_collections_are_isolated():
    client = AsyncMongoMockClient()
    backend_a = MongoStorageBackend(database="test_db", collection="coll_a", client=client)
    backend_b = MongoStorageBackend(database="test_db", collection="coll_b", client=client)
    await backend_a.open()
    await backend_b.open()
    try:
        await backend_a.save_item({"x": 1})
        assert await backend_a.count() == 1
        assert await backend_b.count() == 0
    finally:
        await backend_a.close()
        await backend_b.close()


@pytest.mark.asyncio
async def test_concurrent_writes():
    import asyncio

    backend = _make_backend()
    await backend.open()
    try:
        await asyncio.gather(*(backend.save_item({"index": i}) for i in range(20)))
        assert await backend.count() == 20
    finally:
        await backend.close()


@pytest.mark.asyncio
async def test_context_manager_usage():
    client = AsyncMongoMockClient()
    async with MongoStorageBackend(
        database="test_db", collection="ctx", client=client
    ) as backend:
        await backend.save_item({"x": 1})
        assert await backend.count() == 1
