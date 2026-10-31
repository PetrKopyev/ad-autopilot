from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, timedelta
from types import SimpleNamespace

import pytest
import respx

from app.platforms.base import AdPlatform, CampaignTreeSpec
from app.platforms.direct import DirectAdapter
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


@pytest.fixture(params=["fake", "direct"])
async def platform_errors(request: pytest.FixtureRequest):
    if request.param == "fake":
        harness = fake_harness()
        yield harness.adapter, harness.inject_error
        return
    config = SimpleNamespace(
        direct_api_url="https://api.direct.yandex.com/json/v5",
        direct_api_v501_url="https://api.direct.yandex.com/json/v501",
        direct_sandbox=False,
        direct_amounts_include_vat=False,
        vat_percent=22,
    )
    with respx.mock as router:
        async with DirectAdapter("test-only", config=config) as adapter:

            def inject_error(status: int) -> None:
                router.post("https://api.direct.yandex.com/json/v5/clients").respond(status)

            yield adapter, inject_error
