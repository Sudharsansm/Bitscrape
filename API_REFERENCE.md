# API Reference

Condensed signatures for every public class/function, organized by module.
Everything listed here is importable directly from the top-level
`bitscrape` package (`from bitscrape import X`) as of 0.7.0, in addition to
its own submodule path. This is the flat, single-page version; see
[`docs/api/`](docs/api/index.md) for the same content with links out to
narrative documentation per topic.

## Top-level (`bitscrape`)

```python
bitscrape.__version__: str   # resolved from installed package metadata, not hardcoded

bitscrape.spider(name: str, start_urls: list[str] | None = None, **class_attrs) -> Callable
# Decorator: turns `async def parse(response): yield ...` into a Spider subclass.

bitscrape.run(
    spider_cls: type[Spider],
    *,
    output: str | None = None,
    fmt: str = "jsonl",
    settings: Settings | None = None,
    pipelines: list[BasePipeline] | None = None,
    middlewares: list[BaseMiddleware] | None = None,
    log_level: str = "INFO",
) -> CrawlStats
# Synchronous one-liner. Delegates to build_engine() internally.
```

## Core (`bitscrape.core`)

```python
class Spider:
    name: str                          # required
    start_urls: list[str]
    settings: Settings
    logger: logging.Logger

    def __init__(self, settings: Settings | None = None) -> None
    async def open_spider(self) -> None                 # override, default no-op
    async def close_spider(self) -> None                # override, default no-op
    def make_requests_from_url(self, url: str) -> Request
    def start_requests(self) -> list[Request]            # override for non-GET/custom seeds
    def follow(self, url: str, callback: str = "parse",
               meta: dict | None = None, use_playwright: bool = False) -> Request
               # NOTE: does not resolve relative URLs -- see user-guide
    async def parse(self, response: Response) -> SpiderOutput   # must override

class Request(BaseModel):
    url: str
    method: str = "GET"
    headers: dict[str, str] = {}
    body: bytes | None = None
    meta: dict[str, Any] = {}
    retries: int = 0
    max_retries: int = 3
    priority: RequestPriority = RequestPriority.NORMAL
    use_playwright: bool = False
    callback: str | None = None
    errback: str | None = None
    fingerprint: str | None = None
    depth: int = 0
    request_id: str

class FormRequest(Request):
    ...  # POST-with-form-data convenience, see bitscrape.core.models

class Response(BaseModel):
    url: str
    status: int
    headers: dict[str, str] = {}
    body: bytes = b""
    request: Request
    elapsed_ms: float = 0.0
    encoding: str = "utf-8"
    @property
    def text(self) -> str
    @property
    def ok(self) -> bool          # 200 <= status < 300

class CrawlStats(BaseModel):
    requests_made: int = 0
    requests_failed: int = 0
    responses_received: int = 0
    items_scraped: int = 0
    items_dropped: int = 0
    items_noindexed: int = 0
    links_nofollow_skipped: int = 0
    bytes_downloaded: int = 0
    start_time: float
    finish_time: float | None = None
    @property
    def elapsed(self) -> float
    @property
    def rps(self) -> float

class Settings(BaseSettings):
    # See user-guide/index.md#settings for the full annotated field table.
    ...
```

## Engine (`bitscrape.engine`)

```python
class Engine:
    def __init__(self, spider: Spider, settings: Settings | None = None,
                 pipelines: list | None = None, middlewares: list | None = None,
                 exporter: BaseExporter | None = None,
                 plugin_manager: PluginManager | None = None) -> None
    @property
    def stats(self) -> CrawlStats     # live, safe to read mid-crawl
    async def run(self) -> CrawlStats
```

## Factory (`bitscrape.factory`)

```python
def build_middlewares(settings: Settings, redis_client: Any = None) -> list[BaseMiddleware]

async def build_engine(
    spider: Spider,
    settings: Settings | None = None,
    exporter: BaseExporter | None = None,
    redis_client: Any = None,
    plugin_manager: PluginManager | None = None,
) -> Engine
```

## Downloader (`bitscrape.downloader.downloader`)

```python
class Downloader:
    def __init__(self, settings: Settings) -> None
    async def open(self) -> None
    async def close(self) -> None
    async def fetch(self, request: Request) -> Response

class AutoThrottle:
    def __init__(self, start_delay=1.0, max_delay=60.0, target_concurrency=2.0) -> None
    def get_delay(self, domain: str) -> float
    def update(self, domain: str, latency_seconds: float) -> None
    def reset(self, domain: str | None = None) -> None

class BrowserPool:
    def __init__(self, playwright_browser: str, headless: bool, pool_size: int) -> None
    async def start(self) -> None
    async def stop(self) -> None
    def acquire_context(self, proxy: dict | None = None, **context_kwargs) -> AsyncContextManager
    @property
    def size(self) -> int
    @property
    def available(self) -> int

async def scroll_to_bottom(page, max_scrolls=20, pause_ms=300, stable_rounds=2,
                            click_selector: str | None = None) -> int

class DownloadError(Exception): ...
```

## Middleware (`bitscrape.middleware.middleware`)

```python
class BaseMiddleware(ABC):
    async def process_request(self, request: Request, spider) -> Request | Response | None
    async def process_response(self, request: Request, response: Response, spider) -> ...

class UserAgentMiddleware(BaseMiddleware): ...
class RobotsMiddleware(BaseMiddleware): ...
class MetaRobotsMiddleware(BaseMiddleware): ...
class CookieMiddleware(BaseMiddleware): ...

class SessionPoolMiddleware(BaseMiddleware):
    def __init__(self, pool_size: int = 1, rotate_every: int = 0) -> None
    def session_count(self, domain: str) -> int

class ProxyMiddleware(BaseMiddleware):
    def __init__(self, proxies: list[str] | None = None, rotate: bool = True) -> None
    def add_proxy(self, proxy_url: str) -> None
    def remove_proxy(self, proxy_url: str) -> None
    @property
    def proxies(self) -> list[str]

class DistributedThrottleMiddleware(BaseMiddleware):
    def __init__(self, redis_client, key_prefix: str = "bitscrape:throttle:") -> None

class MiddlewareManager:
    def __init__(self, middlewares: list[BaseMiddleware]) -> None
```

## Scheduler (`bitscrape.scheduler`)

```python
class BaseQueue(ABC): ...
class MemoryQueue(BaseQueue):
    def __init__(self, maxsize: int = 0) -> None
    async def push(self, request: Request) -> None
    async def pop(self) -> Request | None
    @property
    def size(self) -> int
class RedisQueue(BaseQueue):
    def __init__(self, redis_client, key: str = "bitscrape:queue") -> None

class BaseDupeFilter(ABC): ...
class MemoryDupeFilter(BaseDupeFilter): ...
class RedisDupeFilter(BaseDupeFilter):
    def __init__(self, redis_client, key: str = "bitscrape:dupes") -> None
def fingerprint(request: Request) -> str

class Scheduler:
    @classmethod
    async def from_settings(cls, settings: Settings) -> Scheduler
    async def enqueue(self, request: Request) -> bool
    async def next_request(self) -> Request | None
    async def close(self) -> None
```

## Frontier (`bitscrape.frontier`)

```python
class Frontier:
    def __init__(self, num_priority_tiers: int = 3, default_delay: float = 0.0) -> None
    def set_domain_delay(self, domain: str, delay: float) -> None
    def add(self, url: str, priority: int = 1, meta: dict | None = None) -> None
    def get_next(self) -> tuple[str, dict] | None   # never blocks
    @property
    def size(self) -> int
    def domain_count(self) -> int
    def pending_for_domain(self, domain: str) -> int
    def seconds_until_ready(self, domain: str) -> float
```

## Link analysis (`bitscrape.link_analysis`)

```python
class LinkGraph:
    def __init__(self) -> None
    def add_link(self, from_url: str, to_url: str) -> None
    def add_links(self, from_url: str, to_urls: list[str]) -> None
    def add_page(self, url: str) -> None
    def pagerank(self, damping=0.85, max_iter=100, tol=1e-6) -> dict[str, float]
    def hits(self, max_iter=100) -> tuple[dict[str, float], dict[str, float]]
    def top_by_pagerank(self, n: int = 10, **kwargs) -> list[tuple[str, float]]
    def in_degree(self, url: str) -> int
    def out_degree(self, url: str) -> int
    @property
    def node_count(self) -> int
    @property
    def edge_count(self) -> int
```

## Incremental recrawl (`bitscrape.recrawl`)

```python
class PageHistory:
    url: str; last_crawled_at: float; last_changed_at: float
    content_hash: str; importance: float; next_recrawl_at: float

class RecrawlScheduler:
    def __init__(self, base_interval=86400.0, min_interval=3600.0, max_interval=2_592_000.0) -> None
    def record_crawl(self, url: str, content_hash: str, importance: float = 0.0,
                      crawled_at: float | None = None) -> PageHistory
    def is_due(self, url: str, now: float | None = None) -> bool
    def due_urls(self, now: float | None = None) -> list[str]
    def get_history(self, url: str) -> PageHistory | None
    def estimated_change_rate(self, url: str) -> float | None
    def next_recrawl_at(self, url: str) -> float | None
    @property
    def tracked_count(self) -> int
```

## Canonicalization (`bitscrape.canonicalize`)

```python
def canonicalize_url(url: str, strip_tracking_params=True, strip_fragment=True,
                      strip_trailing_slash=True, extra_tracking_params=None) -> str
def resolve_redirect_chain(hops: list[str], max_hops: int = 20) -> str
class RedirectLoopError(Exception): ...

class ContentFingerprint:
    bits: int
    @property
    def hex(self) -> str
    def hamming_distance(self, other: ContentFingerprint) -> int
def compute_fingerprint(text: str, shingle_size: int = 4) -> ContentFingerprint
def is_near_duplicate(text_a, text_b, max_hamming_distance=3, shingle_size=4) -> bool
```

## Ranking (`bitscrape.ranking`)

```python
class BM25Index:
    def __init__(self, k1: float = 1.5, b: float = 0.75) -> None
    def add_document(self, doc_id: str, text: str) -> None
    def score(self, doc_id: str, query: str) -> float
    def search(self, query: str, top_k: int = 10) -> list[tuple[str, float]]
    @property
    def document_count(self) -> int

class VectorIndex:
    def add_document(self, doc_id: str, embedding: list[float]) -> None
    def search(self, query_embedding: list[float], top_k: int = 10) -> list[tuple[str, float]]
    @property
    def document_count(self) -> int

def reciprocal_rank_fusion(ranked_lists: list[list[tuple[str, float]]], k: int = 60) -> list[tuple[str, float]]

class HybridSearchResult:
    doc_id: str; fused_score: float; bm25_score: float | None; vector_score: float | None

class HybridSearcher:
    def __init__(self, bm25_index: BM25Index, vector_index: VectorIndex) -> None
    def search(self, query_text: str, query_embedding: list[float],
               top_k: int = 10, candidate_k: int = 50) -> list[HybridSearchResult]
```

> Bring your own embeddings — `VectorIndex` and `HybridSearcher` don't
> compute them. See [`docs/ai/`](docs/ai/index.md).

## Entity resolution (`bitscrape.entity_resolution`)

```python
def normalize_entity_name(name: str) -> str
def similarity(name_a: str, name_b: str) -> float   # 0..1

class EntityCluster:
    canonical_name: str; mentions: list[str]

class EntityResolver:
    def __init__(self, similarity_threshold: float = 0.85) -> None
    def resolve(self, mention: str) -> str
    def resolve_all(self, mentions: list[str]) -> dict[str, str]
    def clusters(self) -> list[EntityCluster]
    def cluster_for(self, mention: str) -> EntityCluster | None
    @property
    def cluster_count(self) -> int
```

## Knowledge graph (`bitscrape.knowledge_graph`)

```python
def extract_entities(text: str, min_length: int = 3) -> list[str]

class KnowledgeGraph:
    def __init__(self) -> None
    def add_relation(self, subject: str, predicate: str, obj: str, **attrs) -> None
    def add_item(self, item: dict, subject_field: str, relations: dict[str, str], **shared_attrs) -> None
    def add_entities_from_text(self, text: str, source: str, predicate: str = "mentions") -> list[str]
    def neighbors(self, entity: str) -> list[str]
    def relations_from(self, entity: str) -> list[tuple[str, str]]
    def to_dict(self) -> dict
    def export_json(self, path: str) -> None
    def export_graphml(self, path: str) -> None
    @property
    def node_count(self) -> int
    @property
    def edge_count(self) -> int
```

## Storage backends (`bitscrape.storage.backends`)

```python
class BaseStorageBackend(ABC):
    async def open(self) -> None
    async def save_item(self, item: dict | BaseModel) -> None
    async def count(self) -> int
    async def close(self) -> None
    async def __aenter__(self) -> Self
    async def __aexit__(self, *exc) -> None

class SQLiteStorageBackend(BaseStorageBackend):
    def __init__(self, db_path: str, table: str = "items") -> None
    async def all_items(self) -> list[dict]     # test/inspection convenience

class S3StorageBackend(BaseStorageBackend):
    def __init__(self, bucket: str, prefix: str = "bitscrape",
                 endpoint_url: str | None = None, region_name: str = "us-east-1") -> None

class PostgresStorageBackend(BaseStorageBackend):
    def __init__(self, dsn: str, table: str = "items") -> None
    # Implemented against real asyncpg API; mock-tested, not verified against a live server.

class MongoStorageBackend(BaseStorageBackend):
    # NotImplementedError on instantiation -- documented stub, see docstring for intended shape.

class ElasticsearchStorageBackend(BaseStorageBackend):
    # NotImplementedError on instantiation -- documented stub, see docstring for intended shape.
```

## Plugins (`bitscrape.plugins`)

```python
KNOWN_EVENTS = {"spider_opened", "spider_closed", "request_scheduled",
                 "response_received", "item_scraped", "item_dropped", "error"}

class PluginManager:
    def on(self, event: str, callback) -> None
    def off(self, event: str, callback) -> None
    def register_plugin(self, plugin: BasePlugin) -> None
    async def fire(self, event: str, **kwargs) -> None
    def hook_count(self, event: str) -> int

class BasePlugin:
    async def spider_opened(self, spider) -> None
    async def spider_closed(self, spider, reason: str) -> None
    async def request_scheduled(self, request, spider) -> None
    async def response_received(self, request, response, spider) -> None
    async def item_scraped(self, item, spider) -> None
    async def item_dropped(self, item, exception, spider) -> None
    async def error(self, request, exception, spider) -> None

class BearerTokenAuthPlugin(BasePlugin):
    def __init__(self, domain: str, token: str) -> None

class StorageConnectorPlugin(BasePlugin):
    def __init__(self, backend: BaseStorageBackend) -> None
```

## Monitoring (`bitscrape.monitoring`)

```python
class StatsSnapshot:
    def __init__(self, stats_getter: Callable, extra: dict | None = None) -> None
    def to_dict(self) -> dict

class StatsMonitor:
    def __init__(self, stats_getter: Callable, host="127.0.0.1", port=8765, extra=None) -> None
    async def start(self) -> None
    async def stop(self) -> None
    def snapshot(self) -> dict
    def as_plugin(self) -> BasePlugin
```
Serves `GET /` (auto-refreshing HTML) and `GET /stats.json`.

## Observability (`bitscrape.observability`)

```python
class CrawlMetrics:
    def __init__(self, registry=None) -> None    # real prometheus_client Counters/Histogram
    def record_request(self, status: str, duration_seconds: float | None = None) -> None
    def record_item_scraped(self) -> None
    def record_item_dropped(self) -> None
    def record_error(self, error_type: str) -> None
    def render(self) -> bytes

async def serve_metrics(metrics: CrawlMetrics, host="127.0.0.1", port=9100) -> AppRunner

class CrawlTracer:
    def __init__(self, tracer_provider=None, service_name="bitscrape") -> None
    def span(self, name: str, **attributes) -> ContextManager   # real OpenTelemetry span
    def start_span(self, name: str, **attributes) -> Span

def make_in_memory_tracer(service_name="bitscrape-test") -> tuple[CrawlTracer, InMemorySpanExporter]

class JSONLogFormatter(logging.Formatter): ...

class AlertManager:
    def add_rule(self, name: str, check: Callable, message: Callable, cooldown_seconds=300.0) -> None
    def on_alert(self, callback: Callable[[str, str], None]) -> None
    def evaluate(self, snapshot: dict, now: float | None = None) -> list[str]
    @property
    def rule_count(self) -> int
```

## Parser (`bitscrape.parser.selector`)

```python
class ParsedResponse:
    def css(self, selector: str) -> SelectorList
    def xpath(self, selector: str) -> SelectorList
    # response.css(...)/.xpath(...) on a Response delegate here

class SelectorList:
    def get(self, default: str | None = None) -> str | None
    def getall(self) -> list[str]
    def css(self, selector: str) -> SelectorList   # chain from an element
```

## Exporters (`bitscrape.exporters.feed`)

```python
class BaseExporter(ABC):
    def open(self) -> None
    def export_item(self, item) -> None
    def close(self) -> None

class JSONLExporter(BaseExporter): ...
class JSONExporter(BaseExporter): ...
class CSVExporter(BaseExporter): ...
class XMLExporter(BaseExporter): ...

def get_exporter(fmt: str, uri: str | None = None) -> BaseExporter
```

## Pipelines (`bitscrape.pipeline.pipelines`)

```python
class DropItem(Exception): ...

class BasePipeline(ABC):
    async def process_item(self, item, spider): ...

class LoggingPipeline(BasePipeline): ...
class ValidationPipeline(BasePipeline): ...
class DedupPipeline(BasePipeline): ...
class PostgresPipeline(BasePipeline): ...

class PipelineManager:
    def __init__(self, pipelines: list[BasePipeline]) -> None
    async def process_item(self, item, spider)
```

## CLI (`bitscrape.cli.main`)

See [`docs/cli/`](docs/cli/index.md) for full command reference
(`crawl`, `list`, `genspider`, `startproject`).
