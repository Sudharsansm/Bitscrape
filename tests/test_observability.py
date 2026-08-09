"""
Tests for bitscrape.observability.

  - CrawlMetrics: real prometheus_client counters/histograms, scraped via a
    real running aiohttp /metrics endpoint with a real HTTP client.
  - CrawlTracer: real OpenTelemetry SDK spans, verified via OTel's own
    in-memory exporter (a genuine part of the opentelemetry-sdk package,
    not a hand-rolled test double).
  - JSONLogFormatter: real logging.Formatter subclass, output parsed as JSON.
  - AlertManager: threshold rule evaluation and cooldown behavior.
"""

from __future__ import annotations

import json
import logging

import aiohttp
import pytest

from bitscrape.observability import (
    AlertManager,
    CrawlMetrics,
    JSONLogFormatter,
    make_in_memory_tracer,
    serve_metrics,
)


# ---------------------------------------------------------------------------
# CrawlMetrics + serve_metrics -- real HTTP endpoint, real Prometheus format
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_metrics_endpoint_serves_prometheus_format():
    metrics = CrawlMetrics()
    metrics.record_request(status="200", duration_seconds=0.05)
    metrics.record_request(status="200")
    metrics.record_request(status="500")
    metrics.record_item_scraped()
    metrics.record_item_scraped()
    metrics.record_item_dropped()
    metrics.record_error(error_type="timeout")

    runner = await serve_metrics(metrics, host="127.0.0.1", port=19100)
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get("http://127.0.0.1:19100/metrics") as resp:
                assert resp.status == 200
                text = await resp.text()
    finally:
        await runner.cleanup()

    assert 'bitscrape_requests_total{status="200"} 2.0' in text
    assert 'bitscrape_requests_total{status="500"} 1.0' in text
    assert "bitscrape_items_scraped_total 2.0" in text
    assert "bitscrape_items_dropped_total 1.0" in text
    assert 'bitscrape_errors_total{error_type="timeout"} 1.0' in text
    assert "bitscrape_request_duration_seconds" in text  # histogram present


def test_metrics_render_without_server():
    metrics = CrawlMetrics()
    metrics.record_item_scraped()
    output = metrics.render().decode()
    assert "bitscrape_items_scraped_total 1.0" in output


def test_separate_registries_do_not_collide():
    """Two independent CrawlMetrics instances (e.g. two spiders in one
    process) must not clash on Prometheus metric name registration."""
    metrics_a = CrawlMetrics()
    metrics_b = CrawlMetrics()
    metrics_a.record_item_scraped()
    metrics_b.record_item_scraped()
    metrics_b.record_item_scraped()

    assert "bitscrape_items_scraped_total 1.0" in metrics_a.render().decode()
    assert "bitscrape_items_scraped_total 2.0" in metrics_b.render().decode()


# ---------------------------------------------------------------------------
# CrawlTracer -- real OpenTelemetry spans
# ---------------------------------------------------------------------------


def test_span_context_manager_creates_a_span():
    tracer, exporter = make_in_memory_tracer()
    with tracer.span("fetch", url="https://example.com") as span:
        assert span is not None
    spans = exporter.get_finished_spans()
    assert len(spans) == 1
    assert spans[0].name == "fetch"
    assert spans[0].attributes["url"] == "https://example.com"


def test_multiple_spans_are_all_captured():
    tracer, exporter = make_in_memory_tracer()
    with tracer.span("fetch"):
        pass
    with tracer.span("parse"):
        pass
    with tracer.span("pipeline"):
        pass
    spans = exporter.get_finished_spans()
    names = {s.name for s in spans}
    assert names == {"fetch", "parse", "pipeline"}


def test_nested_spans_have_parent_child_relationship():
    tracer, exporter = make_in_memory_tracer()
    with tracer.span("crawl_request") as _outer:
        with tracer.span("fetch") as _inner:
            pass
    spans = {s.name: s for s in exporter.get_finished_spans()}
    assert spans["fetch"].parent.span_id == spans["crawl_request"].context.span_id


def test_start_span_manual_lifecycle():
    tracer, exporter = make_in_memory_tracer()
    span = tracer.start_span("long_running_op", stage="download")
    assert exporter.get_finished_spans() == ()  # not finished yet
    span.end()
    finished = exporter.get_finished_spans()
    assert len(finished) == 1
    assert finished[0].name == "long_running_op"
    assert finished[0].attributes["stage"] == "download"


def test_span_records_exception_attributes_via_status():
    tracer, exporter = make_in_memory_tracer()
    from opentelemetry.trace import Status, StatusCode

    with tracer.span("risky_operation") as span:
        span.set_status(Status(StatusCode.ERROR, "something failed"))
    spans = exporter.get_finished_spans()
    assert spans[0].status.status_code == StatusCode.ERROR


# ---------------------------------------------------------------------------
# JSONLogFormatter
# ---------------------------------------------------------------------------


def test_json_formatter_produces_valid_json(caplog):
    logger = logging.getLogger("test.json.formatter")
    logger.setLevel(logging.INFO)
    import io

    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(JSONLogFormatter())
    logger.addHandler(handler)
    try:
        logger.info("hello world")
    finally:
        logger.removeHandler(handler)

    output = stream.getvalue().strip()
    data = json.loads(output)
    assert data["message"] == "hello world"
    assert data["level"] == "INFO"
    assert data["logger"] == "test.json.formatter"


def test_json_formatter_includes_extra_fields():
    import io

    logger = logging.getLogger("test.json.extra")
    logger.setLevel(logging.INFO)
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(JSONLogFormatter())
    logger.addHandler(handler)
    try:
        logger.info("crawl event", extra={"url": "https://example.com", "status": 200})
    finally:
        logger.removeHandler(handler)

    data = json.loads(stream.getvalue().strip())
    assert data["url"] == "https://example.com"
    assert data["status"] == 200


def test_json_formatter_includes_exception_info():
    import io

    logger = logging.getLogger("test.json.exc")
    logger.setLevel(logging.ERROR)
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(JSONLogFormatter())
    logger.addHandler(handler)
    try:
        try:
            raise ValueError("boom")
        except ValueError:
            logger.exception("something broke")
    finally:
        logger.removeHandler(handler)

    data = json.loads(stream.getvalue().strip())
    assert "exception" in data
    assert "ValueError" in data["exception"]


# ---------------------------------------------------------------------------
# AlertManager
# ---------------------------------------------------------------------------


def test_alert_fires_when_threshold_exceeded():
    manager = AlertManager()
    manager.add_rule(
        name="high_error_rate",
        check=lambda s: s["requests_failed"] / max(s["requests_made"], 1) > 0.5,
        message=lambda s: f"Error rate too high: {s['requests_failed']}/{s['requests_made']}",
    )
    fired = manager.evaluate({"requests_made": 10, "requests_failed": 6})
    assert fired == ["high_error_rate"]


def test_alert_does_not_fire_when_threshold_not_met():
    manager = AlertManager()
    manager.add_rule(
        name="high_error_rate",
        check=lambda s: s["requests_failed"] / max(s["requests_made"], 1) > 0.5,
        message=lambda s: "error",
    )
    fired = manager.evaluate({"requests_made": 10, "requests_failed": 1})
    assert fired == []


def test_callback_receives_rule_name_and_message():
    manager = AlertManager()
    received = []
    manager.on_alert(lambda name, msg: received.append((name, msg)))
    manager.add_rule(
        name="test_rule", check=lambda s: True, message=lambda s: "custom message"
    )
    manager.evaluate({})
    assert received == [("test_rule", "custom message")]


def test_cooldown_prevents_repeated_firing():
    manager = AlertManager()
    manager.add_rule(name="always_true", check=lambda s: True, message=lambda s: "x", cooldown_seconds=100.0)
    fired_first = manager.evaluate({}, now=1000.0)
    fired_second = manager.evaluate({}, now=1050.0)  # within cooldown
    assert fired_first == ["always_true"]
    assert fired_second == []


def test_fires_again_after_cooldown_elapses():
    manager = AlertManager()
    manager.add_rule(name="always_true", check=lambda s: True, message=lambda s: "x", cooldown_seconds=100.0)
    manager.evaluate({}, now=1000.0)
    fired = manager.evaluate({}, now=1200.0)  # cooldown elapsed
    assert fired == ["always_true"]


def test_broken_rule_check_does_not_crash_evaluation():
    manager = AlertManager()
    manager.add_rule(
        name="broken", check=lambda s: 1 / 0, message=lambda s: "unreachable"  # raises
    )
    manager.add_rule(name="working", check=lambda s: True, message=lambda s: "ok")
    fired = manager.evaluate({})  # should not raise
    assert fired == ["working"]


def test_broken_callback_does_not_stop_other_callbacks():
    manager = AlertManager()
    received = []
    manager.on_alert(lambda name, msg: (_ for _ in ()).throw(RuntimeError("bad callback")))
    manager.on_alert(lambda name, msg: received.append(name))
    manager.add_rule(name="rule1", check=lambda s: True, message=lambda s: "x")
    manager.evaluate({})  # should not raise
    assert received == ["rule1"]


def test_rule_count():
    manager = AlertManager()
    assert manager.rule_count == 0
    manager.add_rule(name="a", check=lambda s: True, message=lambda s: "x")
    manager.add_rule(name="b", check=lambda s: True, message=lambda s: "x")
    assert manager.rule_count == 2
