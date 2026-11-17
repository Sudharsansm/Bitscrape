"""
Tests for the upgraded AutoThrottle (``bitscrape.downloader.throttle``).

Layers:
  * pure unit tests of the delay algorithm and Retry-After parsing;
  * DomainPacer tests on a fake clock (deterministic, no real sleeping);
  * end-to-end tests of the Downloader/Engine against a real local server,
    including back-off, Retry-After cool-downs and recovery.
"""

from __future__ import annotations

import asyncio
import time
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
from typing import Any

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from bitscrape.core.models import Request
from bitscrape.core.settings import Settings
from bitscrape.core.spider import Spider
from bitscrape.downloader.downloader import Downloader, DownloadError, _override_for
from bitscrape.downloader.throttle import (
    AutoThrottle,
    DomainPacer,
    header_value,
    parse_retry_after,
)
from bitscrape.engine import Engine
from bitscrape.sessions import SessionManager
from tests.test_multi_session import _browser_session

# ---------------------------------------------------------------------------
# parse_retry_after / header_value
# ---------------------------------------------------------------------------


def test_retry_after_seconds_and_fractions():
    assert parse_retry_after("120") == 120.0
    assert parse_retry_after(" 1.5 ") == 1.5
    assert parse_retry_after("0") == 0.0


def test_retry_after_negative_clamps_to_zero():
    assert parse_retry_after("-5") == 0.0


@pytest.mark.parametrize("bad", [None, "", "   ", "soon", "nan", "inf", "-inf"])
def test_retry_after_unreadable_or_non_finite_is_none(bad):
    assert parse_retry_after(bad) is None


def test_retry_after_http_date_future_and_past():
    future = format_datetime(datetime.now(timezone.utc) + timedelta(seconds=40), usegmt=True)
    past = format_datetime(datetime.now(timezone.utc) - timedelta(seconds=40), usegmt=True)
    assert 35 <= parse_retry_after(future) <= 45  # type: ignore[operator]
    assert parse_retry_after(past) == 0.0


def test_header_value_is_case_insensitive():
    assert header_value({"retry-after": "7"}, "Retry-After") == "7"
    assert header_value({"Retry-After": "7"}, "Retry-After") == "7"
    assert header_value({}, "Retry-After") is None
    assert header_value(None, "Retry-After") is None


# ---------------------------------------------------------------------------
# AutoThrottle algorithm
# ---------------------------------------------------------------------------


def _at(**kw: Any) -> AutoThrottle:
    base: dict[str, Any] = {"start_delay": 1.0, "max_delay": 60.0, "target_concurrency": 1.0}
    base.update(kw)
    return AutoThrottle(**base)


def test_healthy_response_matches_legacy_smoothing():
    at = _at(target_concurrency=2.0)
    at.record("d", 10.0)  # target 5, halfway from 1 -> 3
    assert at.get_delay("d") == pytest.approx(3.0)


def test_block_without_retry_after_doubles_delay():
    at = _at(start_delay=2.0)
    d = at.record("d", 0.01, ok=False)
    assert d.backed_off and d.cooldown == 0.0
    assert at.get_delay("d") == pytest.approx(4.0)
    at.record("d", 0.01, ok=False)
    assert at.get_delay("d") == pytest.approx(8.0)


def test_repeated_blocks_are_capped_at_max_delay():
    at = _at(start_delay=2.0, max_delay=10.0)
    for _ in range(10):
        at.record("d", 0.01, ok=False)
    assert at.get_delay("d") == 10.0


def test_block_from_zero_delay_still_backs_off():
    at = _at(start_delay=0.0)
    at.record("d", 0.0, ok=False)
    assert at.get_delay("d") > 0.0  # 0 * 2 must not stay 0


def test_a_block_never_speeds_the_crawl_up():
    at = _at(start_delay=8.0)
    at.record("d", 0.0, ok=False)  # instant answer, but it was a block
    assert at.get_delay("d") >= 8.0


def test_retry_after_yields_cooldown_not_doubling():
    at = _at(start_delay=2.0)
    d = at.record("d", 0.01, ok=False, retry_after=30.0)
    assert d.cooldown == 30.0
    assert at.get_delay("d") == pytest.approx(2.0, abs=1.1)  # smoothed toward latency, never doubled
    assert at.get_delay("d") < 4.0


def test_retry_after_cooldown_is_capped():
    at = _at(max_cooldown=5.0)
    assert at.record("d", 0.01, ok=False, retry_after=3600.0).cooldown == 5.0


def test_retry_after_zero_means_no_cooldown():
    assert _at().record("d", 0.01, ok=False, retry_after=0.0).cooldown == 0.0


def test_recovers_after_blocks_stop():
    at = _at(start_delay=1.0)
    for _ in range(3):
        at.record("d", 0.1, ok=False)
    peak = at.get_delay("d")
    assert peak == pytest.approx(8.0)
    seen = []
    for _ in range(12):
        at.record("d", 0.1)
        seen.append(at.get_delay("d"))
    assert seen == sorted(seen, reverse=True)  # monotonically speeding back up
    assert seen[-1] < peak / 50
    assert seen[-1] >= 0.1 - 1e-9  # ...but not below what the site can sustain


def test_recovery_parameter_controls_how_fast():
    slow, fast = _at(recovery=0.25), _at(recovery=1.0)
    for a in (slow, fast):
        for _ in range(3):
            a.record("d", 0.1, ok=False)
        a.record("d", 0.1)
    assert fast.get_delay("d") == pytest.approx(0.1)  # jumped straight to the target
    assert slow.get_delay("d") > fast.get_delay("d")


def test_block_backoff_can_be_disabled():
    at = _at(start_delay=1.0, block_backoff=False)
    d = at.record("d", 0.1, ok=False, retry_after=30.0)
    assert not d.backed_off and d.cooldown == 0.0
    assert at.get_delay("d") < 1.0  # just a latency sample


def test_floor_applies_at_read_time_and_can_exceed_max():
    at = _at(max_delay=5.0)
    at.record("d", 0.01)
    assert at.delay_for("d", floor=2.0) == 2.0
    assert at.delay_for("d", floor=30.0) == 30.0  # the site's own Crawl-delay wins
    assert at.get_delay("d") < 1.0  # the learned value stayed a pure measurement


def test_doubling_uses_the_floor_as_its_base():
    at = _at(start_delay=0.0)
    at.record("d", 0.0, ok=False, floor=4.0)
    assert at.get_delay("d") == pytest.approx(8.0)


def test_snapshot_and_counts_and_reset():
    at = _at()
    at.record("a", 0.1, ok=False)
    at.record("b", 0.1)
    snap = at.snapshot()
    assert snap["a"]["backoffs"] == 1 and snap["b"]["backoffs"] == 0
    assert at.total_backoffs == 1
    at.reset("a")
    assert "a" not in at.snapshot() and at.total_backoffs == 1
    at.reset()
    assert at.snapshot() == {}


@pytest.mark.parametrize("kw", [{"recovery": 0.0}, {"recovery": 1.5}, {"backoff_factor": 0.5}])
def test_invalid_parameters_are_rejected(kw):
    with pytest.raises(ValueError):
        _at(**kw)


def test_settings_reject_max_below_start_when_enabled():
    with pytest.raises(ValueError, match="autothrottle_max_delay"):
        Settings(autothrottle_enabled=True, autothrottle_start_delay=5.0, autothrottle_max_delay=1.0)
    Settings(autothrottle_start_delay=5.0, autothrottle_max_delay=1.0)  # disabled: not checked


# ---------------------------------------------------------------------------
# override lookup
# ---------------------------------------------------------------------------


def test_override_lookup_order():
    m = {"example.com": 1.0, "api.example.com:8080": 2.0, "cdn.example.org": 3.0}
    assert _override_for(m, "api.example.com:8080") == 2.0  # exact host:port
    assert _override_for(m, "example.com:443") == 1.0  # bare host
    assert _override_for(m, "www.shop.example.com") == 1.0  # parent domain
    assert _override_for(m, "cdn.example.org") == 3.0
    assert _override_for(m, "other.net") is None
    assert _override_for({}, "example.com") is None


# ---------------------------------------------------------------------------
# DomainPacer on a fake clock
# ---------------------------------------------------------------------------


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.now += seconds
        await asyncio.sleep(0)


def _pacer() -> tuple[DomainPacer, FakeClock]:
    c = FakeClock()
    return DomainPacer(clock=c, sleep=c.sleep), c


async def test_pacer_first_request_is_immediate_then_spaced():
    p, c = _pacer()
    assert await p.wait("d", 2.0) == 0.0
    assert await p.wait("d", 2.0) == 2.0
    assert c.now == 2.0


async def test_pacer_serialises_concurrent_waiters():
    """The bug in check-then-sleep: N workers all see 'enough time passed' and burst."""
    p, c = _pacer()
    starts: list[float] = []

    async def worker() -> None:
        await p.wait("d", 1.0)
        starts.append(c.now)

    await asyncio.gather(*[worker() for _ in range(5)])
    assert starts == [0.0, 1.0, 2.0, 3.0, 4.0]


async def test_pacer_rereads_delay_when_a_waiter_is_released():
    p, c = _pacer()
    delay = {"v": 1.0}
    starts: list[float] = []

    async def worker() -> None:
        await p.wait("d", lambda: delay["v"])
        starts.append(c.now)
        delay["v"] = 5.0  # a back-off decided after the first request went out

    await asyncio.gather(*[worker() for _ in range(3)])
    # 2nd waits the old 1s slot; the 3rd is held for the *new* 5s spacing.
    assert starts == [0.0, 1.0, 6.0]


async def test_pacer_cooldown_holds_every_waiter():
    p, _c = _pacer()
    p.pause("d", 10.0)
    assert p.paused_for("d") == 10.0
    assert await p.wait("d", 0.0) == 10.0
    assert p.paused_for("d") == 0.0


async def test_pacer_pause_never_shortens_and_counts():
    p, _c = _pacer()
    p.pause("d", 10.0)
    p.pause("d", 3.0)  # ignored
    assert p.paused_for("d") == 10.0 and p.cooldowns == 1
    p.pause("d", 0.0)
    p.pause("d", -1.0)
    assert p.cooldowns == 1


async def test_pacer_pause_set_while_queued_extends_the_wait():
    """A cool-down that arrives while a request is already waiting for its slot
    must still hold it: the wait loop re-evaluates after every sleep."""
    c = FakeClock()
    gate = asyncio.Event()
    sleeping = asyncio.Event()

    async def held_sleep(seconds: float) -> None:
        sleeping.set()
        await gate.wait()  # the test decides when the "sleep" finishes
        c.now += seconds

    p = DomainPacer(clock=c, sleep=held_sleep)
    await p.wait("d", 2.0)  # first request: slot for the next one is t=2.0
    released: list[float] = []

    async def waiter() -> None:
        await p.wait("d", 0.0)
        released.append(c.now)

    t = asyncio.create_task(waiter())
    await sleeping.wait()  # waiter is now genuinely blocked on the 2.0s slot
    p.pause("d", 7.0)  # ...and a Retry-After cool-down arrives (until t=7.0)
    gate.set()
    await t
    assert released == [7.0]  # not 2.0: it was held for the cool-down too


async def test_pacer_domains_are_independent_and_total_wait_accumulates():
    p, _c = _pacer()
    await p.wait("a", 5.0)
    assert await p.wait("b", 5.0) == 0.0
    await p.wait("a", 5.0)
    assert p.total_wait == 5.0


# ---------------------------------------------------------------------------
# Local server
# ---------------------------------------------------------------------------


class _State:
    def __init__(self) -> None:
        self.hits: list[float] = []
        self.rl_left = 1
        self.flaky_left = 1
        self.in_flight = 0
        self.max_in_flight = 0


def _make_app(st: _State) -> web.Application:
    async def ok(request: web.Request) -> web.Response:
        st.hits.append(time.monotonic())
        return web.Response(text="ok")

    async def rate_limited(request: web.Request) -> web.Response:
        st.hits.append(time.monotonic())
        if st.rl_left > 0:
            st.rl_left -= 1
            return web.Response(
                status=429, headers={"Retry-After": request.query.get("ra", "1")}, text="slow down"
            )
        return web.Response(text="ok")

    async def five03(request: web.Request) -> web.Response:
        st.hits.append(time.monotonic())
        return web.Response(status=503, text="busy")

    async def flaky(request: web.Request) -> web.Response:
        st.hits.append(time.monotonic())
        if st.flaky_left > 0:
            st.flaky_left -= 1
            return web.Response(status=503, text="busy")
        return web.Response(text="ok")

    async def forbidden(request: web.Request) -> web.Response:
        st.hits.append(time.monotonic())
        return web.Response(status=403, text="Access Denied")

    async def slow(request: web.Request) -> web.Response:
        st.in_flight += 1
        st.max_in_flight = max(st.max_in_flight, st.in_flight)
        await asyncio.sleep(0.05)
        st.in_flight -= 1
        return web.Response(text="ok")

    app = web.Application()
    app.router.add_get("/ok", ok)
    app.router.add_get("/rl", rate_limited)
    app.router.add_get("/503", five03)
    app.router.add_get("/flaky", flaky)
    app.router.add_get("/403", forbidden)
    app.router.add_get("/slow", slow)
    return app


@pytest.fixture
async def srv():
    st = _State()
    server = TestServer(_make_app(st))
    await server.start_server()
    server.st = st  # type: ignore[attr-defined]
    server.domain = f"{server.host}:{server.port}"  # type: ignore[attr-defined]
    try:
        yield server
    finally:
        await server.close()


def _u(server: TestServer, path: str) -> str:
    return str(server.make_url(path))


async def _open(**settings: Any) -> Downloader:
    d = Downloader(Settings(**settings))
    await d.open()
    return d


_AT: dict[str, Any] = {
    "autothrottle_enabled": True,
    "autothrottle_start_delay": 0.02,
    "autothrottle_max_delay": 5.0,
    "autothrottle_target_concurrency": 1.0,
    "retry_http_codes": [],  # observe the throttle, not the retry loop
}


# ---------------------------------------------------------------------------
# Pacing (works with or without AutoThrottle)
# ---------------------------------------------------------------------------


async def test_download_delay_spaces_concurrent_requests(srv):
    d = await _open(download_delay=0.1)
    try:
        await asyncio.gather(*[d.fetch(Request(url=_u(srv, "/ok"))) for _ in range(5)])
        gaps = [b - a for a, b in zip(sorted(srv.st.hits), sorted(srv.st.hits)[1:], strict=False)]
        assert min(gaps) >= 0.08  # not a burst
    finally:
        await d.close()


async def test_autothrottle_never_goes_below_download_delay(srv):
    d = await _open(**{**_AT, "autothrottle_start_delay": 0.0, "download_delay": 0.15})
    try:
        t0 = time.monotonic()
        for _ in range(4):
            await d.fetch(Request(url=_u(srv, "/ok")))
        assert time.monotonic() - t0 >= 0.4  # 3 gaps of >= 0.15s
    finally:
        await d.close()


async def test_robots_crawl_delay_in_meta_is_honoured(srv):
    d = await _open()
    try:
        t0 = time.monotonic()
        for _ in range(3):
            await d.fetch(Request(url=_u(srv, "/ok"), meta={"crawl_delay": 0.2}))
        assert time.monotonic() - t0 >= 0.35
    finally:
        await d.close()


async def test_domain_delays_override_is_applied_by_host(srv):
    d = await _open(domain_delays={srv.host: 0.2})
    try:
        t0 = time.monotonic()
        for _ in range(3):
            await d.fetch(Request(url=_u(srv, "/ok")))
        assert time.monotonic() - t0 >= 0.35
    finally:
        await d.close()


async def test_domain_delays_for_other_domains_dont_apply(srv):
    d = await _open(domain_delays={"unrelated.test": 30.0})
    try:
        t0 = time.monotonic()
        for _ in range(3):
            await d.fetch(Request(url=_u(srv, "/ok")))
        assert time.monotonic() - t0 < 1.0
    finally:
        await d.close()


async def test_domain_concurrency_override_caps_in_flight(srv):
    d = await _open(concurrent_requests_per_domain=8, domain_concurrency={srv.host: 1})
    try:
        await asyncio.gather(*[d.fetch(Request(url=_u(srv, "/slow"))) for _ in range(5)])
        assert srv.st.max_in_flight == 1
    finally:
        await d.close()


# ---------------------------------------------------------------------------
# Back-off, Retry-After, recovery
# ---------------------------------------------------------------------------


async def test_503s_double_the_domain_delay(srv):
    d = await _open(**_AT)
    try:
        delays = []
        for _ in range(3):
            r = await d.fetch(Request(url=_u(srv, "/503")))
            assert r.status == 503
            delays.append(d._autothrottle.get_delay(srv.domain))
        assert delays[0] == pytest.approx(0.04, abs=0.005)
        assert delays[1] == pytest.approx(0.08, abs=0.01)
        assert delays[2] == pytest.approx(0.16, abs=0.02)
        assert d.throttle_info()["backoffs"] == 3
    finally:
        await d.close()


async def test_404_is_not_a_throttle_signal(srv):
    d = await _open(**_AT)
    try:
        r = await d.fetch(Request(url=_u(srv, "/does-not-exist")))
        assert r.status == 404
        assert d.throttle_info()["backoffs"] == 0
    finally:
        await d.close()


async def test_speeds_back_up_once_blocking_stops(srv):
    d = await _open(**_AT)
    try:
        for _ in range(3):
            await d.fetch(Request(url=_u(srv, "/503")))
        peak = d._autothrottle.get_delay(srv.domain)
        for _ in range(6):
            await d.fetch(Request(url=_u(srv, "/ok")))
        assert d._autothrottle.get_delay(srv.domain) < peak / 4
    finally:
        await d.close()


async def test_retry_after_pauses_the_whole_domain(srv):
    d = await _open(**_AT)
    try:
        first = await d.fetch(Request(url=_u(srv, "/rl")))  # 429, Retry-After: 1
        assert first.status == 429
        assert d.throttle_info()["cooldowns"] == 1
        t0 = time.monotonic()
        await d.fetch(Request(url=_u(srv, "/ok")))  # a *different* request to the domain
        assert time.monotonic() - t0 >= 0.85  # waited what the site asked
        # ...and it was a pause, not a doubling of the spacing:
        assert d._autothrottle.get_delay(srv.domain) < 0.1
    finally:
        await d.close()


async def test_retry_after_cooldown_is_capped_by_max_retry_after_seconds(srv):
    d = await _open(**{**_AT, "max_retry_after_seconds": 0.3})
    try:
        await d.fetch(Request(url=_u(srv, "/rl?ra=3600")))
        t0 = time.monotonic()
        await d.fetch(Request(url=_u(srv, "/ok")))
        waited = time.monotonic() - t0
        assert 0.2 <= waited < 1.0
    finally:
        await d.close()


async def test_retry_after_ignored_when_respect_retry_after_is_off(srv):
    d = await _open(**{**_AT, "respect_retry_after": False})
    try:
        await d.fetch(Request(url=_u(srv, "/rl")))
        assert d.throttle_info()["cooldowns"] == 0
        t0 = time.monotonic()
        await d.fetch(Request(url=_u(srv, "/ok")))
        assert time.monotonic() - t0 < 0.5  # doubled spacing only, no 1s pause
    finally:
        await d.close()


async def test_block_backoff_can_be_turned_off(srv):
    d = await _open(**{**_AT, "autothrottle_block_backoff": False})
    try:
        await d.fetch(Request(url=_u(srv, "/503")))
        assert d.throttle_info()["backoffs"] == 0
    finally:
        await d.close()


async def test_soft_block_flagged_by_detector_slows_the_domain(srv):
    d = await _open(**{**_AT, "blocked_detection_enabled": True, "blocked_max_retries": 0})
    try:
        r = await d.fetch(Request(url=_u(srv, "/403")))
        assert r.headers.get("X-Bitscrape-Blocked") == "true"
        assert d.throttle_info()["backoffs"] == 1  # 403 isn't in backoff_codes; the detector caught it
    finally:
        await d.close()


async def test_status_in_backoff_codes_isnt_double_counted_by_the_detector(srv):
    d = await _open(
        **{**_AT, "blocked_detection_enabled": True, "blocked_max_retries": 0,
           "blocked_http_codes": [503]}
    )
    try:
        await d.fetch(Request(url=_u(srv, "/503")))
        assert d.throttle_info()["backoffs"] == 1
    finally:
        await d.close()


async def test_connection_errors_back_off(srv):
    d = await _open(**_AT)
    try:
        with pytest.raises(DownloadError):
            await d.fetch(Request(url="http://127.0.0.1:1/x", max_retries=0))
        assert d.throttle_info()["backoffs"] == 1
    finally:
        await d.close()


async def test_connection_error_backoff_can_be_disabled():
    d = await _open(**{**_AT, "autothrottle_backoff_on_errors": False})
    try:
        with pytest.raises(DownloadError):
            await d.fetch(Request(url="http://127.0.0.1:1/x", max_retries=0))
        assert d.throttle_info()["backoffs"] == 0
    finally:
        await d.close()


async def test_throttle_latency_excludes_retry_sleeps(srv):
    """Regression: latency used to be measured from the start of the whole fetch,
    so a retry's back-off sleep inflated the delay AutoThrottle learned."""
    d = await _open(**{**_AT, "retry_http_codes": [503]})
    d._backoff_seconds = lambda attempt, ra: 0.3  # type: ignore[method-assign]
    seen: list[float] = []
    orig = d._autothrottle.record

    def spy(domain, latency, **kw):
        seen.append(latency)
        return orig(domain, latency, **kw)

    d._autothrottle.record = spy  # type: ignore[method-assign]
    try:
        r = await d.fetch(Request(url=_u(srv, "/flaky"), max_retries=2))
        assert r.status == 200
        assert len(seen) == 2 and max(seen) < 0.25  # not ~0.3s + latency
    finally:
        await d.close()


async def test_disabled_autothrottle_records_nothing(srv):
    d = await _open(retry_http_codes=[])
    try:
        await d.fetch(Request(url=_u(srv, "/503")))
        info = d.throttle_info()
        assert info["enabled"] is False and info["backoffs"] == 0 and info["domains"] == {}
    finally:
        await d.close()


# ---------------------------------------------------------------------------
# Browser fetches are throttled too
# ---------------------------------------------------------------------------


async def test_browser_session_fetches_are_paced_and_fed_to_autothrottle(srv):
    session, _pw = _browser_session()

    def configure(m: SessionManager) -> None:
        m.add("ui", session)

    d = Downloader(Settings(**{**_AT, "autothrottle_start_delay": 0.0, "download_delay": 0.2}))
    await d.open(configure=configure)
    try:
        t0 = time.monotonic()
        for i in range(3):
            await d.fetch(Request(url=f"http://browser.test/{i}", session="ui"))
        assert time.monotonic() - t0 >= 0.35
        assert "browser.test" in d.throttle_info()["domains"]
    finally:
        await d.close()


# ---------------------------------------------------------------------------
# Engine stats
# ---------------------------------------------------------------------------


class _BlockedSpider(Spider):
    name = "throttle_stats"

    def __init__(self, base: str, **kw: Any) -> None:
        super().__init__(**kw)
        self.base = base

    def start_requests(self):
        return [Request(url=f"{self.base}/503"), Request(url=f"{self.base}/ok")]

    async def parse(self, response):
        yield {"status": response.status}


async def test_engine_reports_throttle_stats(srv):
    spider = _BlockedSpider(_u(srv, "").rstrip("/"))
    settings = spider.settings.model_copy(update=_AT)
    stats = await Engine(spider=spider, settings=settings).run()
    assert stats.throttle_backoffs >= 1
    assert srv.domain in stats.throttle_delays
    assert stats.throttle_wait_seconds >= 0.0
