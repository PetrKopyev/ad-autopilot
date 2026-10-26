from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, timedelta

import pytest

from app.platforms.base import AdPlatform, CampaignTreeSpec
from app.platforms.fake import FakeAdapter, VirtualClock, salon_scenario


@dataclass
class AdapterHarness:
    adapter: AdPlatform
    plan: CampaignTreeSpec
    today: Callable[[], date]
    advance: Callable[..., object]
    inject_error: Callable[[int], None]


def fake_harness() -> AdapterHarness:
    clock = VirtualClock()
    adapter = FakeAdapter(clock=clock, seed=42, moderation_delay=timedelta())
    return AdapterHarness(adapter, salon_scenario().plan, lambda: clock.now.date(), clock.advance, adapter.inject_error)


@pytest.fixture(params=[pytest.param(fake_harness, id="fake")])
def platform(request: pytest.FixtureRequest) -> AdapterHarness:
    return request.param()
