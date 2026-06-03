"""
Unit tests for Bitscrape core components.
Run with: pytest tests/ -v
"""

from __future__ import annotations

import asyncio
import pytest

from bitscrape.core.models import BaseItem, CrawlStats, Request, Response
from bitscrape.core.settings import Settings
from bitscrape.core.spider import Spider
from bitscrape.pipeline.pipelines import (
    DedupPipeline,
    DropItem,
    LoggingPipeline,
    PipelineManager,
    ValidationPipeline,
)
from bitscrape.scheduler.dupefilter import MemoryDupeFilter, fingerprint
from bitscrape.scheduler.scheduler import MemoryQueue, Scheduler

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def settings():
    return Settings(
        concurrent_requests=4,
        robotstxt_obey=False,
        dupefilter_enabled=True,
    )


@pytest.fixture
def dummy_spider(settings):
    class DummySpider(Spider):
        name = "dummy"
        start_urls = ["https://example.com"]

        async def parse(self, response):
            yield {"title": "test"}

    return DummySpider(settings=settings)


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------


def test_request_defaults():
    r = Request(url="https://example.com")
    assert r.method == "GET"
    assert r.retries == 0
    assert r.fingerprint is None


def test_response_text():
    req = Request(url="https://example.com")
    resp = Response(
        url="https://example.com",
        status=200,
        body=b"hello world",
        request=req,
        encoding="utf-8",
    )
    assert resp.text == "hello world"
    assert resp.ok is True


def test_crawl_stats_rps():
    import time

    s = CrawlStats(requests_made=100, start_time=time.time() - 10.0)
    assert s.rps == pytest.approx(10.0, abs=1.0)


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------


def test_settings_defaults():
    s = Settings()
    assert s.concurrent_requests == 16
    assert s.user_agent.startswith("BitscrapeBot")


def test_settings_env_override(monkeypatch):
    monkeypatch.setenv("BITSCRAPE_CONCURRENT_REQUESTS", "32")
    s = Settings()
    assert s.concurrent_requests == 32


# ---------------------------------------------------------------------------
# DupeFilter
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_memory_dupefilter_deduplicates():
    df = MemoryDupeFilter()
    r = Request(url="https://example.com")
    fp = fingerprint(r)
    assert await df.seen(fp) is False  # first time
    assert await df.seen(fp) is True  # second time = duplicate


def test_fingerprint_stable():
    r1 = Request(url="https://example.com/page")
    r2 = Request(url="https://example.com/page")
    assert fingerprint(r1) == fingerprint(r2)


def test_fingerprint_different_urls():
    r1 = Request(url="https://example.com/a")
    r2 = Request(url="https://example.com/b")
    assert fingerprint(r1) != fingerprint(r2)


# ---------------------------------------------------------------------------
# Scheduler
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_scheduler_enqueue_dequeue(settings):
    sched = Scheduler(settings)
    req = Request(url="https://example.com")
    enqueued = await sched.enqueue(req)
    assert enqueued is True
    assert sched.queue_size == 1

    got = await sched.next_request()
    assert got is not None
    assert got.url == "https://example.com"
    assert sched.queue_size == 0


@pytest.mark.asyncio
async def test_scheduler_dedup(settings):
    sched = Scheduler(settings)
    r = Request(url="https://example.com")
    assert await sched.enqueue(r) is True
    assert await sched.enqueue(r) is False  # duplicate
    assert sched.queue_size == 1


@pytest.mark.asyncio
async def test_scheduler_depth_limit():
    settings = Settings(max_depth=2, robotstxt_obey=False)
    sched = Scheduler(settings)
    deep_req = Request(url="https://example.com/deep", depth=3)
    enqueued = await sched.enqueue(deep_req)
    assert enqueued is False


# ---------------------------------------------------------------------------
# Pipelines
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_logging_pipeline(dummy_spider):
    p = LoggingPipeline()
    item = {"title": "hello"}
    result = await p.process_item(item, dummy_spider)
    assert result == item


@pytest.mark.asyncio
async def test_dedup_pipeline_drops_duplicates(dummy_spider):
    p = DedupPipeline()
    item = {"url": "https://example.com", "title": "foo"}
    r1 = await p.process_item(item, dummy_spider)
    assert r1 == item
    with pytest.raises(DropItem):
        await p.process_item(item, dummy_spider)


@pytest.mark.asyncio
async def test_validation_pipeline_passes_valid(dummy_spider):
    from pydantic import BaseModel

    class MyItem(BaseItem):
        name: str
        price: float

    p = ValidationPipeline(model=MyItem)
    result = await p.process_item({"name": "book", "price": "9.99"}, dummy_spider)
    # dict coercion + validation should pass
    assert isinstance(result, MyItem)
    assert result.price == pytest.approx(9.99)


@pytest.mark.asyncio
async def test_pipeline_manager_drop(dummy_spider):
    class AlwaysDrop(LoggingPipeline):
        async def process_item(self, item, spider):
            raise DropItem("always")

    mgr = PipelineManager([LoggingPipeline(), AlwaysDrop()])
    result = await mgr.process_item({"x": 1}, dummy_spider)
    assert result is None
    assert mgr.dropped == 1


# ---------------------------------------------------------------------------
# Parser / selectors
# ---------------------------------------------------------------------------


def test_parsed_response_css():
    req = Request(url="https://example.com")
    resp = Response(
        url="https://example.com",
        status=200,
        body=b"<html><body><h1>Hello</h1><a href='/page'>link</a></body></html>",
        request=req,
    )
    from bitscrape.parser.selector import ParsedResponse

    pr = ParsedResponse(resp)
    assert pr.css("h1::text").get() == "Hello"
    assert pr.css("a::attr(href)").get() == "/page"


def test_selector_list_getall():
    req = Request(url="https://example.com")
    resp = Response(
        url="https://example.com",
        status=200,
        body=b"<ul><li>a</li><li>b</li><li>c</li></ul>",
        request=req,
    )
    from bitscrape.parser.selector import ParsedResponse

    pr = ParsedResponse(resp)
    items = pr.css("li::text").getall()
    assert items == ["a", "b", "c"]


# ---------------------------------------------------------------------------
# Exporters
# ---------------------------------------------------------------------------


def test_jsonl_exporter(tmp_path):
    from bitscrape.exporters.feed import JSONLExporter

    out = tmp_path / "out.jsonl"
    exp = JSONLExporter(str(out))
    exp.open()
    exp.export_item({"title": "foo", "price": 1.99})
    exp.export_item({"title": "bar", "price": 2.99})
    exp.close()
    lines = out.read_text().strip().split("\n")
    assert len(lines) == 2
    import json

    assert json.loads(lines[0])["title"] == "foo"


def test_csv_exporter(tmp_path):
    import csv
    from bitscrape.exporters.feed import CSVExporter

    out = tmp_path / "out.csv"
    exp = CSVExporter(str(out))
    exp.open()
    exp.export_item({"name": "x", "val": 1})
    exp.export_item({"name": "y", "val": 2})
    exp.close()
    rows = list(csv.DictReader(out.open()))
    assert len(rows) == 2
    assert rows[0]["name"] == "x"
