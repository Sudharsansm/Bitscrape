# Monitoring & Observability

Live stats, Prometheus metrics, OpenTelemetry tracing, structured logging,
and alerting -- built on real, industry-standard libraries rather than
reinvented where a real library exists.

## Live local stats (`StatsMonitor`)

A small local HTTP server showing the current `CrawlStats` plus real
process CPU/RAM (via `psutil`) -- **not** a hosted, multi-crawl dashboard
product, just something you run alongside one crawl.

```python
import bitscrape

stats = bitscrape.run(
    MySpider,
    settings=bitscrape.Settings(monitoring_enabled=True, monitoring_port=8765),
)
```

While the crawl runs:
- `http://localhost:8765/` -- auto-refreshing HTML view.
- `http://localhost:8765/stats.json` -- raw JSON (for scripts/automation).

Enabling this via `Settings.monitoring_enabled` registers a plugin that
starts the server on `spider_opened` and stops it on `spider_closed` -- no
manual lifecycle management needed. You can also use it standalone:

```python
from bitscrape.monitoring import StatsMonitor

monitor = StatsMonitor(stats_getter=lambda: engine.stats, port=8765)
await monitor.start()
...
await monitor.stop()
```

## Prometheus metrics (`CrawlMetrics`)

Real `prometheus_client` counters/histogram, served on a real `/metrics`
endpoint any Prometheus server can scrape:

```python
stats = bitscrape.run(
    MySpider,
    settings=bitscrape.Settings(metrics_enabled=True, metrics_port=9100),
)
```

`http://localhost:9100/metrics` exposes:
- `bitscrape_requests_total{status="..."}` (counter)
- `bitscrape_items_scraped_total` (counter)
- `bitscrape_items_dropped_total` (counter)
- `bitscrape_errors_total{error_type="..."}` (counter)
- `bitscrape_request_duration_seconds` (histogram)

Each `CrawlMetrics()` instance uses its own dedicated
`prometheus_client.CollectorRegistry` (not the global default one), so
multiple instances in the same process (e.g. two spiders) don't collide on
metric name registration.

### Point Prometheus at it

```yaml
# prometheus.yml
scrape_configs:
  - job_name: bitscrape
    static_configs:
      - targets: ["localhost:9100"]
```

This is the realistic path to a production dashboard -- feed these metrics
into Grafana, or use them for Kubernetes HPA custom-metric scaling (see
[deployment/](../deployment/index.md)) -- rather than this project
reinventing dashboarding/alerting infrastructure that already exists and
is better maintained elsewhere.

## Tracing (`CrawlTracer`)

Real OpenTelemetry SDK spans, exportable to any OTLP-compatible backend
(Jaeger, Tempo, Honeycomb, etc.) by swapping the exporter:

```python
from bitscrape.observability import CrawlTracer

tracer = CrawlTracer(service_name="my-crawler")

with tracer.span("fetch", url=request.url) as span:
    response = await downloader.fetch(request)
```

For tests or local inspection without a real tracing backend, use the
in-memory variant:
```python
from bitscrape.observability import make_in_memory_tracer

tracer, exporter = make_in_memory_tracer()
with tracer.span("fetch"):
    ...
spans = exporter.get_finished_spans()   # real OTel span objects
```

`tracer.start_span(name, **attrs)` (no context manager) is available when
a span's lifetime spans several non-nested async steps -- call `.end()`
manually when done.

## Structured logging (`JSONLogFormatter`)

Single-line JSON per log record -- the shape centralized-logging systems
(ELK, Loki, Datadog, CloudWatch Logs Insights) expect to parse and index:

```python
import logging
from bitscrape.observability import JSONLogFormatter

handler = logging.StreamHandler()
handler.setFormatter(JSONLogFormatter())
logging.getLogger().addHandler(handler)
```

Includes `timestamp`, `level`, `logger`, `message`, `module`, `line`, and
(if present) a formatted exception traceback -- plus any `extra={...}`
fields passed to the log call, merged in automatically.

## Alerting (`AlertManager`)

Threshold-based rule evaluation with per-rule cooldowns -- this decides
**when** to alert; delivering the alert to a specific vendor (a Slack/
Discord webhook, PagerDuty, email) is your registered callback's job. This
is **not** a full Alertmanager/PagerDuty client.

```python
from bitscrape.observability import AlertManager

alerts = AlertManager()
alerts.add_rule(
    name="high_error_rate",
    check=lambda s: s["requests_failed"] / max(s["requests_made"], 1) > 0.1,
    message=lambda s: f"Error rate: {s['requests_failed']}/{s['requests_made']}",
    cooldown_seconds=300,
)
alerts.on_alert(lambda name, msg: send_to_slack(msg))   # your delivery mechanism

# Periodically (e.g. from a StatsMonitor snapshot):
alerts.evaluate(monitor.snapshot())
```

A rule's `check`/`message` raising an exception is caught and logged -- it
won't crash your monitoring loop or stop other rules from being evaluated.
A tripped rule won't fire again until `cooldown_seconds` has elapsed, so a
persistently-bad condition doesn't spam alerts every check.

## Combining everything

```python
import bitscrape

stats = bitscrape.run(MySpider, settings=bitscrape.Settings(
    monitoring_enabled=True,
    metrics_enabled=True,
))
```

Both can run simultaneously -- `monitoring_enabled` for a quick human-
readable view, `metrics_enabled` for anything you'd actually alert on or
graph over time.

## See also

- [deployment/](../deployment/index.md) -- how the Kubernetes manifests probe these endpoints.
- [architecture/](../architecture/index.md) -- where these plugins hook into the Engine lifecycle.
- [api/index.md#monitoring-bitscrapemonitoring](../api/index.md#monitoring-bitscrapemonitoring) and [#observability-bitscrapeobservability](../api/index.md#observability-bitscrapeobservability) -- full signatures.
