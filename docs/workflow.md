# Workflow (LangGraph)

Bitscrape uses **LangGraph** as an optional state machine engine for
orchestrating complex crawl workflows. No LLMs are involved — LangGraph is
used purely for structured execution flow.

---

## When to Use the Workflow Engine

The default `Engine` handles most scraping tasks perfectly. Use the LangGraph
workflow for:

- Complex multi-stage crawls with conditional branching
- Workflows that need to resume after failure (durable execution)
- Pipelines where the next step depends on what was found
- Long-running crawls where you want structured state tracking

---

## Install

```bash
pip install "bitscrape[workflow]"
```

---

## Default Crawl Graph

The built-in graph defines this flow for each URL:

```
START
  │
  ▼
[fetch]         ← Downloads the URL (via Downloader)
  │
  ▼
[parse]         ← Runs spider.parse(), collects items + new URLs
  │
  ▼
[pipeline]      ← Passes items through PipelineManager
  │
  ├── new requests found → back to [fetch]
  └── done → END
```

---

## Using the Built-in Graph

```python
from bitscrape.workflow.graph import build_crawl_graph, CrawlState
from bitscrape.engine import Engine
import bitscrape

engine = Engine(spider=MySpider())
graph  = build_crawl_graph(engine)

# Run one URL through the graph
state: CrawlState = {
    "request_url": "https://example.com",
    "items": [],
    "new_requests": [],
    "done": False,
}

result = await graph.ainvoke(state)
print("Items found:", result["items"])
print("New URLs:", result["new_requests"])
```

---

## CrawlState Schema

```python
class CrawlState(TypedDict, total=False):
    request_url:     str          # URL to fetch
    response_body:   bytes        # raw HTML bytes
    response_status: int          # HTTP status code
    items:           list[Any]    # scraped items
    new_requests:    list[str]    # URLs to crawl next
    error:           str | None   # error message if failed
    done:            bool         # True when this URL is finished
```

---

## Custom Workflow Graph

Build your own graph for custom logic:

```python
from langgraph.graph import StateGraph, START, END
from bitscrape.workflow.graph import CrawlState

def build_custom_graph(engine):
    builder = StateGraph(CrawlState)

    async def fetch_node(state):
        # ... fetch URL
        return state

    async def parse_node(state):
        # ... parse response
        return state

    async def validate_node(state):
        # ... validate items
        items = [i for i in state.get("items", []) if i.get("price", 0) > 0]
        return {**state, "items": items}

    async def store_node(state):
        # ... save to database
        return {**state, "done": True}

    # Build the graph
    builder.add_node("fetch",    fetch_node)
    builder.add_node("parse",    parse_node)
    builder.add_node("validate", validate_node)
    builder.add_node("store",    store_node)

    builder.add_edge(START,      "fetch")
    builder.add_edge("fetch",    "parse")
    builder.add_edge("parse",    "validate")
    builder.add_edge("validate", "store")
    builder.add_edge("store",    END)

    return builder.compile()
```

---

## Resumable Execution

LangGraph supports checkpointing — save state to resume after a crash:

```python
from langgraph.checkpoint.memory import MemorySaver

checkpointer = MemorySaver()
graph = build_crawl_graph(engine)
graph_with_checkpoints = graph.compile(checkpointer=checkpointer)

# Run with a thread ID for resumability
config = {"configurable": {"thread_id": "crawl-job-001"}}
result = await graph_with_checkpoints.ainvoke(state, config=config)
```

If the process crashes, re-run with the same `thread_id` to resume from the
last checkpoint.
