"""
Tests for bitscrape.workflow.graph -- the optional LangGraph-based
orchestration path (fetch -> parse -> pipeline -> loop/end), an alternative
to the main Engine.run() loop. Closes the coverage gap flagged in
BITSCRAPE_QA_REPORT.md ("workflow/graph.py -- 0% coverage; needs langgraph
installed; entirely gated behind an optional import, currently untested
even with the package present").

Runs the compiled graph against a REAL local HTTP server -- not a mocked
downloader -- so this exercises genuine fetch/parse/pipeline behavior
through LangGraph's actual state machine, not just that build_crawl_graph()
returns something.
"""

from __future__ import annotations

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from bitscrape.core.settings import Settings
from bitscrape.core.spider import Spider
from bitscrape.engine import Engine
from bitscrape.workflow.graph import CrawlState, build_crawl_graph


class _WorkflowDemoSpider(Spider):
    name = "workflow_demo"
    start_urls = []

    async def parse(self, response):
        yield {"title": response.css("h1::text").get()}


class _WorkflowLinkSpider(Spider):
    """Yields both an item and a follow-up Request, to exercise the
    new_requests half of parse_node's output classification."""

    name = "workflow_link_demo"
    start_urls = []

    async def parse(self, response):
        yield {"seen": response.url}
        href = response.css("a::attr(href)").get()
        if href:
            yield self.follow(href)


@pytest.mark.asyncio
async def test_build_crawl_graph_returns_a_compiled_graph():
    engine = Engine(spider=_WorkflowDemoSpider(), settings=Settings())
    graph = build_crawl_graph(engine)
    assert graph is not None
    assert hasattr(graph, "ainvoke")


@pytest.mark.asyncio
async def test_graph_fetches_and_parses_a_real_page():
    async def handler(request: web.Request) -> web.Response:
        return web.Response(text="<html><body><h1>Hello Graph</h1></body></html>")

    app = web.Application()
    app.router.add_get("/page", handler)
    server = TestServer(app)
    await server.start_server()
    try:
        url = f"http://{server.host}:{server.port}/page"
        spider = _WorkflowDemoSpider()
        engine = Engine(spider=spider, settings=Settings())
        await engine._downloader.open()
        try:
            graph = build_crawl_graph(engine)
            initial_state: CrawlState = {"request_url": url}
            final_state = await graph.ainvoke(initial_state)

            assert final_state["error"] is None
            assert final_state["response_status"] == 200
            assert final_state["done"] is True
            assert len(final_state["items"]) == 1
            assert final_state["items"][0]["title"] == "Hello Graph"
        finally:
            await engine._downloader.close()
    finally:
        await server.close()


@pytest.mark.asyncio
async def test_graph_handles_fetch_error_gracefully():
    """A completely unreachable URL should end the graph with an error
    state, not raise/hang."""
    spider = _WorkflowDemoSpider()
    engine = Engine(spider=spider, settings=Settings(download_timeout=1.0))
    await engine._downloader.open()
    try:
        graph = build_crawl_graph(engine)
        initial_state: CrawlState = {"request_url": "http://127.0.0.1:1/unreachable"}
        final_state = await graph.ainvoke(initial_state)

        assert final_state["error"] is not None
        assert final_state["done"] is True
    finally:
        await engine._downloader.close()


@pytest.mark.asyncio
async def test_graph_classifies_items_and_requests_separately():
    async def handler(request: web.Request) -> web.Response:
        return web.Response(
            text='<html><body><a href="https://example.com/next">next</a></body></html>'
        )

    app = web.Application()
    app.router.add_get("/page", handler)
    server = TestServer(app)
    await server.start_server()
    try:
        url = f"http://{server.host}:{server.port}/page"
        spider = _WorkflowLinkSpider()
        engine = Engine(spider=spider, settings=Settings())
        await engine._downloader.open()
        try:
            graph = build_crawl_graph(engine)
            final_state = await graph.ainvoke({"request_url": url})

            assert len(final_state["items"]) == 1
            assert final_state["items"][0]["seen"] == url
            assert final_state["new_requests"] == ["https://example.com/next"]
        finally:
            await engine._downloader.close()
    finally:
        await server.close()


@pytest.mark.asyncio
async def test_graph_runs_pipeline_on_scraped_items():
    """Confirms pipeline_node actually invokes the PipelineManager, not
    just that items pass through untouched."""
    processed_items = []

    class _RecordingPipeline:
        async def process_item(self, item, spider):
            processed_items.append(item)
            return item

    async def handler(request: web.Request) -> web.Response:
        return web.Response(text="<html><body><h1>Piped</h1></body></html>")

    app = web.Application()
    app.router.add_get("/page", handler)
    server = TestServer(app)
    await server.start_server()
    try:
        url = f"http://{server.host}:{server.port}/page"
        spider = _WorkflowDemoSpider()
        engine = Engine(
            spider=spider, settings=Settings(), pipelines=[_RecordingPipeline()]
        )
        await engine._downloader.open()
        try:
            graph = build_crawl_graph(engine)
            await graph.ainvoke({"request_url": url})
            assert len(processed_items) == 1
            assert processed_items[0]["title"] == "Piped"
        finally:
            await engine._downloader.close()
    finally:
        await server.close()


def test_import_error_message_is_clear_when_langgraph_missing(monkeypatch):
    """Confirms the documented ImportError guidance actually fires when
    langgraph genuinely can't be imported, with a clear install hint."""
    import builtins

    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name == "langgraph.graph" or name.startswith("langgraph"):
            raise ImportError("simulated: langgraph not installed")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)

    engine = Engine(spider=_WorkflowDemoSpider(), settings=Settings())
    with pytest.raises(ImportError, match="pip install langgraph"):
        build_crawl_graph(engine)
