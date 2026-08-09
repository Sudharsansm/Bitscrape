"""
Tests for bitscrape.pipeline.pipelines -- closes the coverage gap flagged
in BITSCRAPE_QA_REPORT.md ("pipeline/pipelines.py -- 41% coverage").

PostgresPipeline is tested against the same real, live PostgreSQL server
as tests/test_postgres_live.py (see that file's docstring for setup
requirements) -- not a mock, now that a live server is available.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from pydantic import BaseModel, ValidationError

from bitscrape.pipeline.pipelines import (
    DedupPipeline,
    DropItem,
    LoggingPipeline,
    PipelineManager,
    PostgresPipeline,
    ValidationPipeline,
)

DSN = "postgresql://postgres:postgres@127.0.0.1:5432/bitscrape_test"


class _Product(BaseModel):
    title: str
    price: float


def _fake_spider(**settings_kwargs):
    return SimpleNamespace(name="test_spider", settings=SimpleNamespace(**settings_kwargs))


# ---------------------------------------------------------------------------
# LoggingPipeline
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_logging_pipeline_passes_item_through_unchanged():
    pipeline = LoggingPipeline()
    item = {"title": "Widget"}
    result = await pipeline.process_item(item, _fake_spider())
    assert result is item


@pytest.mark.asyncio
async def test_logging_pipeline_logs_at_debug_level(caplog):
    import logging

    pipeline = LoggingPipeline()
    with caplog.at_level(logging.DEBUG, logger="bitscrape.pipeline.pipelines"):
        await pipeline.process_item({"title": "Widget"}, _fake_spider())
    assert any("Widget" in r.getMessage() for r in caplog.records)


# ---------------------------------------------------------------------------
# ValidationPipeline
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_validation_pipeline_coerces_valid_dict_to_model():
    pipeline = ValidationPipeline(model=_Product)
    result = await pipeline.process_item({"title": "Widget", "price": 9.99}, _fake_spider())
    assert isinstance(result, _Product)
    assert result.title == "Widget"


@pytest.mark.asyncio
async def test_validation_pipeline_drops_invalid_dict():
    pipeline = ValidationPipeline(model=_Product)
    with pytest.raises(DropItem):
        await pipeline.process_item({"title": "Widget"}, _fake_spider())


@pytest.mark.asyncio
async def test_validation_pipeline_passes_through_already_valid_model():
    pipeline = ValidationPipeline(model=_Product)
    item = _Product(title="Widget", price=9.99)
    result = await pipeline.process_item(item, _fake_spider())
    assert result is item


@pytest.mark.asyncio
async def test_validation_pipeline_without_model_passes_through_dicts():
    pipeline = ValidationPipeline()
    item = {"anything": "goes"}
    result = await pipeline.process_item(item, _fake_spider())
    assert result == item


# ---------------------------------------------------------------------------
# DedupPipeline
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dedup_pipeline_allows_first_occurrence():
    pipeline = DedupPipeline()
    result = await pipeline.process_item({"title": "Widget"}, _fake_spider())
    assert result == {"title": "Widget"}


@pytest.mark.asyncio
async def test_dedup_pipeline_drops_exact_duplicate():
    pipeline = DedupPipeline()
    await pipeline.process_item({"title": "Widget"}, _fake_spider())
    with pytest.raises(DropItem):
        await pipeline.process_item({"title": "Widget"}, _fake_spider())


@pytest.mark.asyncio
async def test_dedup_pipeline_allows_different_items():
    pipeline = DedupPipeline()
    await pipeline.process_item({"title": "Widget"}, _fake_spider())
    result = await pipeline.process_item({"title": "Gadget"}, _fake_spider())
    assert result == {"title": "Gadget"}


@pytest.mark.asyncio
async def test_dedup_pipeline_custom_key_fn():
    pipeline = DedupPipeline(key_fn=lambda item: item["sku"])
    await pipeline.process_item({"sku": "A1", "title": "v1"}, _fake_spider())
    with pytest.raises(DropItem):
        await pipeline.process_item({"sku": "A1", "title": "v2 updated"}, _fake_spider())


@pytest.mark.asyncio
async def test_dedup_pipeline_handles_pydantic_models():
    pipeline = DedupPipeline()
    item = _Product(title="Widget", price=9.99)
    await pipeline.process_item(item, _fake_spider())
    with pytest.raises(DropItem):
        await pipeline.process_item(_Product(title="Widget", price=9.99), _fake_spider())


# ---------------------------------------------------------------------------
# PipelineManager
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_pipeline_manager_runs_pipelines_in_order():
    calls = []

    class _RecordingPipeline:
        def __init__(self, name):
            self.name = name

        async def open_spider(self, spider):
            pass

        async def process_item(self, item, spider):
            calls.append(self.name)
            return item

        async def close_spider(self, spider):
            pass

    manager = PipelineManager([_RecordingPipeline("a"), _RecordingPipeline("b")])
    await manager.process_item({}, _fake_spider())
    assert calls == ["a", "b"]


@pytest.mark.asyncio
async def test_pipeline_manager_stops_at_first_drop():
    calls = []

    class _DroppingPipeline:
        async def open_spider(self, spider):
            pass

        async def process_item(self, item, spider):
            raise DropItem("nope")

        async def close_spider(self, spider):
            pass

    class _NeverReachedPipeline:
        async def open_spider(self, spider):
            pass

        async def process_item(self, item, spider):
            calls.append("reached")
            return item

        async def close_spider(self, spider):
            pass

    manager = PipelineManager([_DroppingPipeline(), _NeverReachedPipeline()])
    result = await manager.process_item({}, _fake_spider())
    assert result is None
    assert calls == []


@pytest.mark.asyncio
async def test_pipeline_manager_tracks_dropped_and_processed_counts():
    manager = PipelineManager([DedupPipeline()])
    await manager.process_item({"title": "A"}, _fake_spider())
    await manager.process_item({"title": "A"}, _fake_spider())
    await manager.process_item({"title": "B"}, _fake_spider())

    assert manager.processed == 2
    assert manager.dropped == 1


@pytest.mark.asyncio
async def test_pipeline_manager_lifecycle_hooks_called_on_all_pipelines():
    opened = []
    closed = []

    class _LifecyclePipeline:
        def __init__(self, name):
            self.name = name

        async def open_spider(self, spider):
            opened.append(self.name)

        async def process_item(self, item, spider):
            return item

        async def close_spider(self, spider):
            closed.append(self.name)

    manager = PipelineManager([_LifecyclePipeline("a"), _LifecyclePipeline("b")])
    await manager.open_spider(_fake_spider())
    await manager.close_spider(_fake_spider())
    assert opened == ["a", "b"]
    assert closed == ["a", "b"]


@pytest.mark.asyncio
async def test_pipeline_manager_empty_pipeline_list_passes_through():
    manager = PipelineManager([])
    result = await manager.process_item({"title": "Widget"}, _fake_spider())
    assert result == {"title": "Widget"}


# ---------------------------------------------------------------------------
# PostgresPipeline -- against a REAL live PostgreSQL server
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_postgres_pipeline_skips_gracefully_with_no_database_url(caplog):
    import logging

    pipeline = PostgresPipeline(table="test_pipeline_noop")
    spider = _fake_spider(database_url=None)
    with caplog.at_level(logging.WARNING, logger="bitscrape.pipeline.pipelines"):
        await pipeline.open_spider(spider)
    assert any("no database_url configured" in r.getMessage() for r in caplog.records)

    item = {"title": "Widget"}
    result = await pipeline.process_item(item, spider)
    assert result is item


@pytest.mark.asyncio
async def test_postgres_pipeline_connection_failure_is_handled_gracefully(caplog):
    import logging

    pipeline = PostgresPipeline(table="test_pipeline_badconn")
    spider = _fake_spider(
        database_url="postgresql://baduser:badpass@127.0.0.1:5432/nonexistent_db"
    )
    with caplog.at_level(logging.ERROR, logger="bitscrape.pipeline.pipelines"):
        await pipeline.open_spider(spider)
    assert any("connection failed" in r.getMessage().lower() for r in caplog.records)


@pytest.mark.asyncio
async def test_postgres_pipeline_inserts_real_rows():
    import asyncpg

    conn = await asyncpg.connect(DSN)
    await conn.execute("DROP TABLE IF EXISTS test_pipeline_insert")
    await conn.execute("CREATE TABLE test_pipeline_insert (title TEXT, price DOUBLE PRECISION)")
    await conn.close()

    pipeline = PostgresPipeline(table="test_pipeline_insert")
    spider = _fake_spider(database_url=DSN)
    await pipeline.open_spider(spider)
    try:
        await pipeline.process_item(_Product(title="Widget", price=9.99), spider)
        await pipeline.process_item(_Product(title="Gadget", price=19.99), spider)
    finally:
        await pipeline.close_spider(spider)

    conn = await asyncpg.connect(DSN)
    try:
        rows = await conn.fetch("SELECT title, price FROM test_pipeline_insert ORDER BY title")
        assert len(rows) == 2
        assert rows[0]["title"] == "Gadget"
        assert rows[1]["title"] == "Widget"
    finally:
        await conn.execute("DROP TABLE IF EXISTS test_pipeline_insert")
        await conn.close()


@pytest.mark.asyncio
async def test_postgres_pipeline_upsert_on_conflict():
    """Confirms the ON CONFLICT ... DO UPDATE path actually works against
    a real server, not just that the SQL string looks right."""
    import asyncpg

    conn = await asyncpg.connect(DSN)
    await conn.execute("DROP TABLE IF EXISTS test_pipeline_upsert")
    await conn.execute(
        "CREATE TABLE test_pipeline_upsert "
        "(sku TEXT PRIMARY KEY, title TEXT, price DOUBLE PRECISION)"
    )
    await conn.close()

    pipeline = PostgresPipeline(table="test_pipeline_upsert", conflict_cols=["sku"])
    spider = _fake_spider(database_url=DSN)
    await pipeline.open_spider(spider)
    try:
        await pipeline.process_item({"sku": "A1", "title": "Widget v1", "price": 9.99}, spider)
        await pipeline.process_item({"sku": "A1", "title": "Widget v2", "price": 12.99}, spider)
    finally:
        await pipeline.close_spider(spider)

    conn = await asyncpg.connect(DSN)
    try:
        rows = await conn.fetch("SELECT * FROM test_pipeline_upsert")
        assert len(rows) == 1
        assert rows[0]["title"] == "Widget v2"
        assert rows[0]["price"] == 12.99
    finally:
        await conn.execute("DROP TABLE IF EXISTS test_pipeline_upsert")
        await conn.close()


@pytest.mark.asyncio
async def test_postgres_pipeline_close_spider_closes_connection():
    pipeline = PostgresPipeline(table="test_pipeline_close")
    spider = _fake_spider(database_url=DSN)
    await pipeline.open_spider(spider)
    assert pipeline._conn is not None
    await pipeline.close_spider(spider)
    assert pipeline._conn.is_closed()


# ---------------------------------------------------------------------------
# ValidationError propagation sanity check
# ---------------------------------------------------------------------------


def test_pydantic_validation_error_is_the_real_thing():
    with pytest.raises(ValidationError):
        _Product(title="Widget")
