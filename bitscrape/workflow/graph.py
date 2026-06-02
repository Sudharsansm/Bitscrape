"""
Bitscrape Workflow (LangGraph State Machine)
============================================
Defines a directed crawl graph:

  START → fetch → parse → pipeline → [loop back | END]

LangGraph manages state, persistence, and conditional transitions.
No LLMs are used — this is pure orchestration.
"""
from __future__ import annotations

import logging
from typing import Any, TypedDict

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# State schema
# ---------------------------------------------------------------------------

class CrawlState(TypedDict, total=False):
    """Shared state passed between graph nodes."""
    request_url: str
    response_body: bytes
    response_status: int
    items: list[Any]
    new_requests: list[str]
    error: str | None
    done: bool


# ---------------------------------------------------------------------------
# Graph builder
# ---------------------------------------------------------------------------

def build_crawl_graph(engine: Any) -> Any:
    """
    Build and compile a LangGraph StateGraph for a single crawl cycle.
    ``engine`` is the Engine instance providing _fetch and _parse coroutines.

    Returns a compiled graph ready for ``.ainvoke(state)``.
    """
    try:
        from langgraph.graph import END, START, StateGraph
    except ImportError:
        raise ImportError(
            "langgraph is required for workflow orchestration. "
            "pip install langgraph"
        )

    builder: StateGraph = StateGraph(CrawlState)  # type: ignore[type-arg]

    # ------------------------------------------------------------------
    # Node: fetch
    # ------------------------------------------------------------------
    async def fetch_node(state: CrawlState) -> CrawlState:
        url = state["request_url"]
        logger.debug("[workflow] fetch: %s", url)
        try:
            from bitscrape.core.models import Request
            req = Request(url=url)
            resp = await engine._downloader.fetch(req)
            return {
                **state,
                "response_body": resp.body,
                "response_status": resp.status,
                "error": None,
            }
        except Exception as exc:
            logger.error("[workflow] fetch error %s: %s", url, exc)
            return {**state, "error": str(exc), "done": True}

    # ------------------------------------------------------------------
    # Node: parse
    # ------------------------------------------------------------------
    async def parse_node(state: CrawlState) -> CrawlState:
        if state.get("error"):
            return state
        logger.debug("[workflow] parse: %s", state["request_url"])
        from bitscrape.core.models import Request, Response
        from bitscrape.parser.selector import ParsedResponse
        req = Request(url=state["request_url"])
        resp = Response(
            url=state["request_url"],
            status=state["response_status"],
            body=state["response_body"],
            request=req,
        )
        pr = ParsedResponse(resp)
        items: list[Any] = []
        new_requests: list[str] = []
        spider = engine._spider
        async for out in spider.parse(pr):  # type: ignore[arg-type]
            if isinstance(out, dict) or hasattr(out, "model_dump"):
                items.append(out)
            else:
                new_requests.append(out.url)
        return {**state, "items": items, "new_requests": new_requests}

    # ------------------------------------------------------------------
    # Node: pipeline
    # ------------------------------------------------------------------
    async def pipeline_node(state: CrawlState) -> CrawlState:
        if state.get("error"):
            return state
        logger.debug("[workflow] pipeline: %d items", len(state.get("items", [])))
        for item in state.get("items", []):
            await engine._pipeline_manager.process_item(item, engine._spider)
        return {**state, "done": True}

    # ------------------------------------------------------------------
    # Conditional edge: continue or end
    # ------------------------------------------------------------------
    def should_continue(state: CrawlState) -> str:
        return END if state.get("done") or state.get("error") else "fetch"

    # Build graph
    builder.add_node("fetch", fetch_node)
    builder.add_node("parse", parse_node)
    builder.add_node("pipeline", pipeline_node)

    builder.add_edge(START, "fetch")
    builder.add_edge("fetch", "parse")
    builder.add_edge("parse", "pipeline")
    builder.add_conditional_edges("pipeline", should_continue, {"fetch": "fetch", END: END})

    return builder.compile()
