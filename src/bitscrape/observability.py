"""
Bitscrape Observability
=======================

Distributed tracing, metrics, alerting, and centralized-logging support --
built on real, industry-standard libraries rather than reinvented:

  - **Metrics**: ``prometheus_client`` (the actual library Prometheus
    scraping expects) -- counters/histograms for requests, items, errors,
    latency, exposed on a real ``/metrics`` HTTP endpoint any Prometheus
    server can scrape.
  - **Tracing**: OpenTelemetry's real API/SDK -- spans around
    request/parse/pipeline stages, exportable to any OTLP-compatible
    backend (Jaeger, Tempo, Honeycomb, etc.) by swapping the exporter.
    Tested here with OpenTelemetry's own in-memory exporter, so the actual
    span creation/attribute logic is verified without needing a live
    tracing backend.
  - **Structured logging**: a JSON log formatter, the standard shape
    centralized-logging systems (ELK, Loki, Datadog) expect to ingest.
  - **Alerting**: a threshold-based hook system (fire a callback when e.g.
    error rate crosses X%). This is a genuine, testable building block --
    NOT a full Alertmanager/PagerDuty client. Wire your callback to send to
    whatever real alerting backend you use (a webhook, PagerDuty's API,
    Slack, etc.); this module decides *when* to alert, not *how* to deliver
    the alert to a specific vendor.
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Metrics (Prometheus)
# ---------------------------------------------------------------------------


class CrawlMetrics:
    """
    Real Prometheus metrics for a crawl, using ``prometheus_client``.
    Registered on a dedicated ``CollectorRegistry`` (not the global default
    registry) so multiple ``CrawlMetrics`` instances in the same process
    (e.g. in tests, or multiple spiders) don't collide on metric names.
    """

    def __init__(self, registry: Any = None) -> None:
        from prometheus_client import CollectorRegistry, Counter, Histogram

        self.registry = registry or CollectorRegistry()
        self.requests_total = Counter(
            "bitscrape_requests_total",
            "Total requests made",
            ["status"],
            registry=self.registry,
        )
        self.items_scraped_total = Counter(
            "bitscrape_items_scraped_total",
            "Total items scraped",
            registry=self.registry,
        )
        self.items_dropped_total = Counter(
            "bitscrape_items_dropped_total",
            "Total items dropped by pipelines",
            registry=self.registry,
        )
        self.errors_total = Counter(
            "bitscrape_errors_total",
            "Total errors encountered",
            ["error_type"],
            registry=self.registry,
        )
        self.request_duration_seconds = Histogram(
            "bitscrape_request_duration_seconds",
            "Request latency in seconds",
            registry=self.registry,
        )

    def record_request(self, status: str, duration_seconds: float | None = None) -> None:
        self.requests_total.labels(status=status).inc()
        if duration_seconds is not None:
            self.request_duration_seconds.observe(duration_seconds)

    def record_item_scraped(self) -> None:
        self.items_scraped_total.inc()

    def record_item_dropped(self) -> None:
        self.items_dropped_total.inc()

    def record_error(self, error_type: str) -> None:
        self.errors_total.labels(error_type=error_type).inc()

    def render(self) -> bytes:
        """Renders current metrics in Prometheus text exposition format --
        exactly what a Prometheus scraper expects at a `/metrics` endpoint."""
        from prometheus_client import generate_latest

        return generate_latest(self.registry)


async def serve_metrics(metrics: CrawlMetrics, host: str = "127.0.0.1", port: int = 9100) -> Any:
    """
    Starts a tiny aiohttp server exposing ``metrics`` at ``/metrics`` in
    Prometheus text format. Returns the ``web.AppRunner`` -- call
    ``await runner.cleanup()`` to stop it.
    """
    from aiohttp import web

    async def handler(request: web.Request) -> web.Response:
        return web.Response(body=metrics.render(), content_type="text/plain", charset="utf-8")

    app = web.Application()
    app.router.add_get("/metrics", handler)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, host, port)
    await site.start()
    logger.info("Prometheus metrics exposed at http://%s:%d/metrics", host, port)
    return runner


# ---------------------------------------------------------------------------
# Tracing (OpenTelemetry)
# ---------------------------------------------------------------------------


class CrawlTracer:
    """
    Thin wrapper around an OpenTelemetry tracer for the common crawl spans
    (fetch, parse, pipeline). Pass a real OTLP exporter's TracerProvider in
    production; defaults to a fresh in-process SDK provider with no
    exporter attached (spans are created but go nowhere) unless you use
    ``make_in_memory_tracer()`` for testing/local inspection.
    """

    def __init__(self, tracer_provider: Any = None, service_name: str = "bitscrape") -> None:
        self._service_name = service_name
        if tracer_provider is not None:
            self._provider = tracer_provider
        else:
            from opentelemetry.sdk.resources import Resource
            from opentelemetry.sdk.trace import TracerProvider

            self._provider = TracerProvider(
                resource=Resource.create({"service.name": service_name})
            )
        self._tracer = self._provider.get_tracer(service_name)

    def span(self, name: str, **attributes: Any) -> Any:
        """Context manager: `with tracer.span("fetch", url=...) as span: ...`"""
        span_cm = self._tracer.start_as_current_span(name)
        span = span_cm.__enter__()
        for key, value in attributes.items():
            span.set_attribute(key, value)
        return _SpanContext(span_cm, span)

    def start_span(self, name: str, **attributes: Any) -> Any:
        """Non-context-manager variant: returns a live span you must
        manually ``.end()`` -- useful when a span's lifetime spans several
        non-nested async steps."""
        span = self._tracer.start_span(name)
        for key, value in attributes.items():
            span.set_attribute(key, value)
        return span


class _SpanContext:
    """Wraps the raw span context-manager so ``with tracer.span(...) as
    span:`` gives you the span object directly (attributes already set)."""

    def __init__(self, cm: Any, span: Any) -> None:
        self._cm = cm
        self.span = span

    def __enter__(self) -> Any:
        return self.span

    def __exit__(self, *exc: object) -> Any:
        return self._cm.__exit__(*exc)


def make_in_memory_tracer(service_name: str = "bitscrape-test") -> tuple[CrawlTracer, Any]:
    """
    Convenience for tests/local debugging: a CrawlTracer wired to an
    in-memory span exporter, so you can assert on captured spans without
    needing a real tracing backend. Returns (tracer, span_exporter) --
    call ``span_exporter.get_finished_spans()`` to inspect captured spans.
    """
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

    exporter = InMemorySpanExporter()
    provider = TracerProvider(resource=Resource.create({"service.name": service_name}))
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    tracer = CrawlTracer(tracer_provider=provider, service_name=service_name)
    return tracer, exporter


# ---------------------------------------------------------------------------
# Structured (JSON) logging
# ---------------------------------------------------------------------------


_STANDARD_LOG_RECORD_ATTRS = frozenset(
    logging.LogRecord("", 0, "", 0, "", (), None).__dict__.keys()
) | {"message"}


class JSONLogFormatter(logging.Formatter):
    """
    Formats log records as single-line JSON -- the shape centralized
    logging systems (ELK, Loki, Datadog, CloudWatch Logs Insights) expect
    to parse and index. Attach to a handler:

        handler = logging.StreamHandler()
        handler.setFormatter(JSONLogFormatter())
        logging.getLogger().addHandler(handler)
    """

    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "timestamp": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "module": record.module,
            "line": record.lineno,
        }
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        for key, value in record.__dict__.items():
            if key not in _STANDARD_LOG_RECORD_ATTRS and key not in payload:
                payload[key] = value
        return json.dumps(payload, default=str)


# ---------------------------------------------------------------------------
# Alerting (threshold-based hooks)
# ---------------------------------------------------------------------------


@dataclass
class AlertRule:
    name: str
    check: Callable[[dict[str, Any]], bool]
    message: Callable[[dict[str, Any]], str]
    cooldown_seconds: float = 300.0
    _last_fired_at: float = field(default=0.0, repr=False)


class AlertManager:
    """
    Evaluates a set of threshold rules against a metrics snapshot and fires
    callbacks for any that trip -- e.g. "error rate > 10%" or "RPS dropped
    to 0 for a running crawl." Each rule has its own cooldown so a
    persistently-tripped condition doesn't spam alerts every check.

    This decides WHEN to alert; delivering the alert to a real backend
    (a Slack/Discord webhook, PagerDuty, email) is your callback's job --
    register any callable via ``on_alert()``.
    """

    def __init__(self) -> None:
        self._rules: list[AlertRule] = []
        self._callbacks: list[Callable[[str, str], None]] = []

    def add_rule(
        self,
        name: str,
        check: Callable[[dict[str, Any]], bool],
        message: Callable[[dict[str, Any]], str],
        cooldown_seconds: float = 300.0,
    ) -> None:
        self._rules.append(
            AlertRule(name=name, check=check, message=message, cooldown_seconds=cooldown_seconds)
        )

    def on_alert(self, callback: Callable[[str, str], None]) -> None:
        """Registers a callback(rule_name, message) called whenever a rule
        trips (respecting its cooldown)."""
        self._callbacks.append(callback)

    def evaluate(self, snapshot: dict[str, Any], now: float | None = None) -> list[str]:
        """
        Checks every rule against ``snapshot`` (e.g. a StatsMonitor
        snapshot dict). Returns the names of rules that fired THIS call
        (i.e. tripped and weren't in cooldown). Fires registered callbacks
        for each.
        """
        check_time = now if now is not None else time.time()
        fired: list[str] = []
        for rule in self._rules:
            try:
                tripped = rule.check(snapshot)
            except Exception:
                logger.exception("Alert rule %r check raised", rule.name)
                continue
            if not tripped:
                continue
            if check_time - rule._last_fired_at < rule.cooldown_seconds:
                continue
            rule._last_fired_at = check_time
            message = rule.message(snapshot)
            fired.append(rule.name)
            for callback in self._callbacks:
                try:
                    callback(rule.name, message)
                except Exception:
                    logger.exception("Alert callback raised")
        return fired

    @property
    def rule_count(self) -> int:
        return len(self._rules)
