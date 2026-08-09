"""
Tests for scroll_to_bottom() -- the reusable infinite-scroll driver added for
the Playwright path. Uses a fake Playwright ``page`` object so these tests
run without a real browser or the playwright package installed.
"""

from __future__ import annotations

import pytest

from bitscrape.downloader.downloader import scroll_to_bottom


class FakePage:
    """
    Simulates a page whose scrollHeight grows for a fixed number of scrolls
    and then plateaus, like real lazy-loaded content running out.
    """

    def __init__(self, growth_rounds: int, height_step: int = 500, start_height: int = 500):
        self._height = start_height
        self._step = height_step
        self._growth_rounds = growth_rounds
        self._round = 0
        self.scroll_calls = 0
        self.wait_calls = []

    async def evaluate(self, script: str):
        # NOTE: the real scroll script is
        # "window.scrollTo(0, document.body.scrollHeight)" -- it contains
        # BOTH substrings, so "scrollTo" must be checked first or every call
        # gets misread as a pure height read with no scrolling side effect.
        if "scrollTo" in script:
            self.scroll_calls += 1
            if self._round < self._growth_rounds:
                self._height += self._step
                self._round += 1
            return None
        if "scrollHeight" in script:
            return self._height
        raise ValueError(f"Unexpected script: {script}")

    async def wait_for_timeout(self, ms: int):
        self.wait_calls.append(ms)


class FakeLocator:
    def __init__(self, present: bool, clicks: list):
        self._present = present
        self._clicks = clicks

    async def count(self):
        return 1 if self._present else 0

    @property
    def first(self):
        return self

    async def is_visible(self):
        return self._present

    async def click(self, timeout=None):
        self._clicks.append(True)


class FakePageWithButton(FakePage):
    def __init__(self, *a, button_present=True, **kw):
        super().__init__(*a, **kw)
        self.button_present = button_present
        self.clicks: list = []

    def locator(self, selector: str):
        return FakeLocator(self.button_present, self.clicks)


@pytest.mark.asyncio
async def test_stops_after_stable_rounds_when_height_plateaus():
    page = FakePage(growth_rounds=3)
    rounds = await scroll_to_bottom(page, max_scrolls=20, pause_ms=1, stable_rounds=2)
    # Grows for 3 rounds, then needs 2 more stable rounds to confirm plateau = 5 total.
    assert rounds == 5
    assert page.scroll_calls == 5


@pytest.mark.asyncio
async def test_respects_max_scrolls_cap():
    page = FakePage(growth_rounds=100)  # keeps growing "forever"
    rounds = await scroll_to_bottom(page, max_scrolls=10, pause_ms=1, stable_rounds=2)
    assert rounds == 10
    assert page.scroll_calls == 10


@pytest.mark.asyncio
async def test_pause_ms_is_used_for_wait_for_timeout():
    page = FakePage(growth_rounds=1)
    await scroll_to_bottom(page, max_scrolls=5, pause_ms=250, stable_rounds=2)
    assert all(w == 250 for w in page.wait_calls)


@pytest.mark.asyncio
async def test_no_scrollable_content_returns_quickly():
    page = FakePage(growth_rounds=0)  # height never changes
    rounds = await scroll_to_bottom(page, max_scrolls=20, pause_ms=1, stable_rounds=2)
    assert rounds == 2  # confirms plateau in exactly `stable_rounds` rounds


@pytest.mark.asyncio
async def test_click_selector_clicks_load_more_button_each_round():
    page = FakePageWithButton(growth_rounds=3, button_present=True)
    await scroll_to_bottom(
        page, max_scrolls=10, pause_ms=1, stable_rounds=2, click_selector="button.load-more"
    )
    assert len(page.clicks) > 0


@pytest.mark.asyncio
async def test_missing_click_selector_does_not_raise():
    page = FakePageWithButton(growth_rounds=1, button_present=False)
    # Should not raise even though the button never appears.
    rounds = await scroll_to_bottom(
        page, max_scrolls=5, pause_ms=1, stable_rounds=2, click_selector="button.missing"
    )
    assert rounds >= 2
