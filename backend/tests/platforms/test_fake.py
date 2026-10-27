from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest

from app.platforms.base import (
    AdGroupSpec,
    AdSpec,
    AuthExpired,
    CampaignTreeSpec,
    ExternalRefs,
    Operation,
    PlatformError,
    RateLimited,
)
from app.platforms.fake import AdTruth, FailureScenario, FakeAdapter, VirtualClock, salon_scenario
from app.worker.clock import today

MIDNIGHT = datetime(2025, 12, 31, 21, tzinfo=UTC)  # полночь 1 января по Москве


def single_plan(*, daily: int = 1_000_000, period: int = 10_000_000, id: str = "test") -> CampaignTreeSpec:
    return CampaignTreeSpec(
        id, "Тест", daily, period, (AdGroupSpec("group", "Группа", (AdSpec("ad", "Уход", "Запись"),)),)
    )


def make_adapter(**kwargs) -> FakeAdapter:
    return FakeAdapter(
        truths={"ad": AdTruth(ctr=1, cpc_kop=100, conversion_rate=1, impressions_per_hour=600)},
        moderation_delay=timedelta(),
        **kwargs,
    )


@pytest.mark.parametrize("status", ["paused", "stopped", "unknown"])
async def test_fake_rejects_unknown_statuses_on_create_and_apply(status):
    adapter = make_adapter()
    plan = single_plan()
    with pytest.raises(PlatformError, match="статус"):
        await adapter.create_campaign_tree(replace(plan, status=status))
    refs = await adapter.create_campaign_tree(plan)
    with pytest.raises(PlatformError, match="статус"):
        await adapter.apply(Operation("set_status", "campaign", refs.campaign_id, "status", "active", status))
    assert (await adapter.get_state(refs))[refs.campaign_id].status == "active"


async def test_evil_period_overrun_only_multiplies_newly_authorized_spend():
    adapter = make_adapter(evil=True)
    refs = await adapter.create_campaign_tree(single_plan(period=1_000))
    adapter.clock.advance(hours=1)
    await adapter.get_state(refs)
    spent = adapter._spent[refs.campaign_id]
    assert spent == 1_200
    await adapter.apply(Operation("set_budget", "campaign", refs.campaign_id, "period_cap_kop", 1_000, spent + 400))
    adapter.clock.advance(hours=1)
    await adapter.get_state(refs)
    assert adapter._spent[refs.campaign_id] == spent + 500


def test_clock_advances_without_wall_time() -> None:
    clock = VirtualClock(datetime(2026, 1, 31, 23, tzinfo=UTC))
    assert clock.advance(hours=1, days=1) == datetime(2026, 2, 2, tzinfo=UTC)
    assert clock.advance() == clock.now
    with pytest.raises(ValueError):
        VirtualClock(datetime(2026, 1, 1))


@pytest.mark.parametrize("value", [-1, float("nan"), float("inf")])
def test_clock_rejects_invalid_advance(value: float) -> None:
    clock = VirtualClock()
    before = clock.now
    with pytest.raises(ValueError):
        clock.advance(hours=value)
    assert clock.now == before


def test_salon_has_six_ads_with_three_quality_pairs() -> None:
    scenario = salon_scenario()
    assert len(scenario.plan.groups) == 3
    assert [len(group.ads) for group in scenario.plan.groups] == [2, 2, 2]
    assert len(scenario.truths) == 6
    strong, weak, borderline = (scenario.truths[key] for key in ("strong-1", "weak-1", "borderline-1"))
    assert strong.ctr > borderline.ctr > weak.ctr
    assert strong.conversion_rate > borderline.conversion_rate > weak.conversion_rate
    assert strong.cpc_kop < borderline.cpc_kop < weak.cpc_kop
    for group in scenario.plan.groups:
        assert scenario.truths[group.ads[0].id] == scenario.truths[group.ads[1].id]


async def test_same_seed_and_different_polling_produce_identical_statistics() -> None:
    first = make_adapter(seed=17)
    second = make_adapter(seed=17)
    refs = await first.create_campaign_tree(single_plan())
    other = await second.create_campaign_tree(single_plan())
    start = first.clock.now.date()
    first.clock.advance(days=1)
    for _ in range(48):
        second.clock.advance(hours=0.5)
        await second.get_state(other)
        await second.get_stats(start, second.clock.now.date())
        await second.get_spend_today()
    assert await first.get_stats(start, first.clock.now.date()) == await second.get_stats(
        start, second.clock.now.date()
    )
    assert await first.get_state(refs) == await second.get_state(other)


async def test_seed_controls_random_traffic() -> None:
    rows = []
    for seed in (1, 2, 1):
        adapter = FakeAdapter(seed=seed, moderation_delay=timedelta())
        await adapter.create_campaign_tree(adapter.scenario.plan)
        day = adapter.clock.now.date()
        adapter.clock.advance(hours=2)
        rows.append(await adapter.get_stats(day, day))
    assert rows[0] == rows[2]
    assert rows[0] != rows[1]


async def test_delayed_conversions_update_click_day_even_after_pause() -> None:
    adapter = make_adapter(seed=42)
    refs = await adapter.create_campaign_tree(single_plan())
    click_day = adapter.clock.now.date()
    adapter.clock.advance(hours=1 / 60)
    (initial,) = await adapter.get_stats(click_day, click_day)
    assert initial.clicks == 10 and initial.conversions == {"lead": 0}
    assert not initial.is_settled
    await adapter.apply(Operation("set_status", "campaign", refs.campaign_id, "status", "active", "paused_owner"))
    adapter.clock.advance(days=2)
    (arrived,) = await adapter.get_stats(click_day, click_day)
    assert arrived.clicks == initial.clicks
    assert arrived.cost_kop == initial.cost_kop
    assert arrived.conversions == {"lead": initial.clicks}
    assert arrived.is_settled
    assert await adapter.get_stats(click_day + timedelta(days=1), adapter.clock.now.date()) == []
    adapter.clock.advance(days=1)
    (settled,) = await adapter.get_stats(click_day, click_day)
    assert settled.is_settled


async def test_zero_conversion_delay_is_immediate() -> None:
    adapter = make_adapter(conversion_delay=timedelta())
    await adapter.create_campaign_tree(single_plan())
    day = adapter.clock.now.date()
    adapter.clock.advance(hours=1)
    (row,) = await adapter.get_stats(day, day)
    assert row.conversions == {"lead": row.clicks}


async def test_settled_waits_for_custom_conversion_delay() -> None:
    adapter = make_adapter(conversion_delay=timedelta(days=5))
    refs = await adapter.create_campaign_tree(single_plan())
    day = adapter.clock.now.date()
    adapter.clock.advance(hours=1)
    await adapter.apply(Operation("set_status", "ad", refs.ads["ad"], "status", "active", "paused_owner"))
    adapter.clock.advance(days=3)
    (row,) = await adapter.get_stats(day, day)
    assert not row.is_settled
    adapter.clock.advance(days=3)
    (row,) = await adapter.get_stats(day, day)
    assert row.is_settled and row.conversions["lead"] == row.clicks


@pytest.mark.parametrize("evil,expected", [(False, 10_000), (True, 12_500)])
async def test_evil_spends_twenty_five_percent_over_both_caps(evil: bool, expected: int) -> None:
    adapter = make_adapter(evil=evil, clock=VirtualClock(MIDNIGHT))
    await adapter.create_campaign_tree(single_plan(daily=10_000, period=10_000))
    day = today(adapter.clock)
    adapter.clock.advance(hours=23)
    assert await adapter.get_spend_today() == expected
    adapter.clock.advance(days=4)
    rows = await adapter.get_stats(day, today(adapter.clock))
    assert sum(row.cost_kop for row in rows) == expected


@pytest.mark.parametrize("daily,period", [(0, 100_000), (100_000, 0), (99, 100_000), (100_000, 99)])
async def test_zero_or_unaffordable_limits_prevent_spend(daily: int, period: int) -> None:
    adapter = make_adapter()
    await adapter.create_campaign_tree(single_plan(daily=daily, period=period))
    day = adapter.clock.now.date()
    adapter.clock.advance(days=2)
    assert sum(row.cost_kop for row in await adapter.get_stats(day, adapter.clock.now.date())) == 0


async def test_caps_and_midnight_are_per_campaign_and_per_day() -> None:
    adapter = make_adapter(clock=VirtualClock(MIDNIGHT))
    await adapter.create_campaign_tree(single_plan(daily=10_000, period=25_000))
    await adapter.create_campaign_tree(single_plan(id="other", daily=15_000, period=35_000))
    start = today(adapter.clock)
    adapter.clock.advance(days=1)
    assert await adapter.get_spend_today() == 0
    rows = await adapter.get_stats(start, start, "campaign")
    assert sorted(row.cost_kop for row in rows) == [10_000, 15_000]
    adapter.clock.advance(days=4)
    rows = await adapter.get_stats(start, today(adapter.clock), "campaign")
    assert sum(row.cost_kop for row in rows) == 60_000


async def test_days_follow_moscow_time_not_utc() -> None:
    adapter = make_adapter(clock=VirtualClock(datetime(2026, 1, 1, 20, 30, tzinfo=UTC)))
    await adapter.create_campaign_tree(single_plan(daily=10_000))
    # 23:30 по Москве: дневной лимит тратится до полуночи и снова — после неё, хотя по UTC это один день.
    adapter.clock.advance(hours=1)
    assert await adapter.get_spend_today() == 10_000
    rows = await adapter.get_stats(date(2026, 1, 1), date(2026, 1, 2))
    assert [(row.date, row.cost_kop) for row in rows] == [(date(2026, 1, 1), 10_000), (date(2026, 1, 2), 10_000)]


@pytest.mark.parametrize("entity", ["campaign", "group", "ad"])
async def test_paused_hierarchy_stops_delivery_and_resume_restores_it(entity: str) -> None:
    adapter = make_adapter()
    refs = await adapter.create_campaign_tree(single_plan())
    state = next(state for state in (await adapter.get_state(refs)).values() if state.entity_type == entity)
    pause = Operation("set_status", entity, state.entity_id, "status", "active", "paused_owner")
    await adapter.apply(pause)
    adapter.clock.advance(hours=1)
    assert await adapter.get_spend_today() == 0
    await adapter.apply(pause.inverse())
    adapter.clock.advance(hours=1)
    assert await adapter.get_spend_today() > 0


async def test_budget_changes_use_spend_already_accumulated() -> None:
    adapter = make_adapter()
    refs = await adapter.create_campaign_tree(single_plan(daily=10_000, period=100_000))
    adapter.clock.advance(hours=1)
    assert await adapter.get_spend_today() == 10_000
    await adapter.apply(Operation("set_budget", "campaign", refs.campaign_id, "daily_limit_kop", 10_000, 20_000))
    adapter.clock.advance(hours=1)
    assert await adapter.get_spend_today() == 20_000
    await adapter.apply(Operation("set_budget", "campaign", refs.campaign_id, "period_cap_kop", 100_000, 15_000))
    adapter.clock.advance(days=2)
    rows = await adapter.get_stats(datetime(2026, 1, 1).date(), adapter.clock.now.date())
    assert sum(row.cost_kop for row in rows) == 20_000


async def test_moderation_delay_blocks_delivery_until_approval() -> None:
    adapter = FakeAdapter(moderation_delay=timedelta(hours=2))
    refs = await adapter.create_campaign_tree(single_plan())
    adapter.clock.advance(hours=1)
    assert {info.status for info in await adapter.get_moderation(refs)} == {"pending"}
    assert await adapter.get_spend_today() == 0
    adapter.clock.advance(hours=1)
    assert {info.status for info in await adapter.get_moderation(refs)} == {"approved"}
    assert await adapter.get_spend_today() == 0
    adapter.clock.advance(hours=1)
    assert await adapter.get_spend_today() > 0


@pytest.mark.parametrize("medical", [True, False])
async def test_rejected_ads_never_spend(medical: bool) -> None:
    plan = single_plan()
    group = plan.groups[0]
    plan = replace(plan, groups=(replace(group, ads=(replace(group.ads[0], medical=medical),)),))
    adapter = FakeAdapter(rejection_probability=0 if medical else 1)
    refs = await adapter.create_campaign_tree(plan)
    adapter.clock.advance(days=1)
    (review,) = await adapter.get_moderation(refs)
    assert review.status == "rejected" and review.reason
    if medical:
        assert "Медицинская" in review.reason
    assert await adapter.get_spend_today() == 0
    assert await adapter.get_stats(adapter.clock.now.date() - timedelta(days=1), adapter.clock.now.date()) == []


async def test_text_edit_resubmits_moderation_and_inverse_recovers() -> None:
    adapter = FakeAdapter(moderation_delay=timedelta(hours=1))
    refs = await adapter.create_campaign_tree(single_plan())
    adapter.clock.advance(hours=2)
    operation = Operation("update_ad_text", "ad", refs.ads["ad"], "text", "Запись", "Медицинское лечение")
    await adapter.apply(operation)
    assert (await adapter.get_moderation(refs))[0].status == "pending"
    adapter.clock.advance(hours=1)
    assert (await adapter.get_moderation(refs))[0].status == "rejected"
    await adapter.apply(operation.inverse())
    adapter.clock.advance(hours=1)
    assert (await adapter.get_moderation(refs))[0].status == "approved"


@pytest.mark.parametrize(
    "failures,error,status",
    [
        (FailureScenario(rate_limited=1), RateLimited, 429),
        (FailureScenario(server_error=1), PlatformError, 503),
        (FailureScenario(auth_expired=1), AuthExpired, 401),
    ],
)
async def test_probability_failures(failures: FailureScenario, error: type[PlatformError], status: int) -> None:
    adapter = FakeAdapter(failures=failures)
    with pytest.raises(error) as caught:
        await adapter.whoami()
    assert caught.value.status_code == status


async def test_probabilistic_failures_are_reproducible() -> None:
    results = []
    for _ in range(2):
        adapter = FakeAdapter(seed=13, failures=FailureScenario(rate_limited=0.3, server_error=0.2))
        statuses = []
        for _ in range(30):
            try:
                await adapter.whoami()
                statuses.append(200)
            except PlatformError as error:
                statuses.append(error.status_code)
        results.append(statuses)
    assert results[0] == results[1]
    assert set(results[0]) == {200, 429, 503}


async def test_failed_apply_can_be_retried_without_partial_change() -> None:
    adapter = make_adapter()
    refs = await adapter.create_campaign_tree(single_plan())
    original = await adapter.get_state(refs)
    operation = Operation("set_budget", "campaign", refs.campaign_id, "daily_limit_kop", 1_000_000, 50_000)
    adapter.inject_error(503)
    with pytest.raises(PlatformError):
        await adapter.apply(operation)
    assert await adapter.get_state(refs) == original
    await adapter.apply(operation)
    assert (await adapter.get_state(refs))[refs.campaign_id].daily_limit_kop == 50_000


async def test_api_outage_does_not_stop_spend_or_change_random_stream() -> None:
    first, second = make_adapter(seed=5), make_adapter(seed=5)
    for adapter in (first, second):
        await adapter.create_campaign_tree(single_plan())
        adapter.clock.advance(hours=2)
    first.inject_error(429)
    with pytest.raises(RateLimited):
        await first.get_spend_today()
    start, end = datetime(2026, 1, 1).date(), first.clock.now.date()
    assert await first.get_stats(start, end) == await second.get_stats(start, end)


async def test_expired_auth_stays_expired_until_refresh() -> None:
    adapter = FakeAdapter(token_lifetime=timedelta(hours=1))
    session = await adapter.exchange_code("demo-code")
    adapter.clock.advance(hours=1)
    for _ in range(2):
        with pytest.raises(AuthExpired):
            await adapter.whoami()
    refreshed = await adapter.refresh(session)
    assert refreshed.expires_at == adapter.clock.now + timedelta(hours=1)
    assert (await adapter.whoami())["id"] == session.account_id
    adapter.inject_error(401)
    with pytest.raises(AuthExpired):
        await adapter.whoami()
    with pytest.raises(AuthExpired):
        await adapter.whoami()
    await adapter.refresh(refreshed)
    assert await adapter.whoami()


async def test_assets_auth_marking_and_service_methods(tmp_path: Path) -> None:
    adapter = FakeAdapter()
    query = parse_qs(urlparse(adapter.auth_url("state & value")).query)
    assert query == {"state": ["state & value"]}
    file = tmp_path / "asset.bin"
    file.write_bytes(b"test-asset")
    assert await adapter.upload_image(file) == await adapter.upload_image(file)
    assert await adapter.upload_video(file) != await adapter.upload_image(file)
    assert await adapter.get_balance_hint() is None
    await adapter.set_marking({"subject_type": "ip", "name": "Демо"})
    assert adapter.marking_instruction() is None
    assert (await adapter.limits()).remaining_units is None
    assert await adapter.min_budgets() == {"day": 0, "period": 0}


async def test_state_and_refs_are_detached_snapshots() -> None:
    adapter = make_adapter()
    refs = await adapter.create_campaign_tree(single_plan())
    state = await adapter.get_state(refs)
    group_id = refs.groups["group"]
    state[group_id].targeting["city"] = "Изменение снимка"
    assert (await adapter.get_state(refs))[group_id].targeting == {}
    with pytest.raises(PlatformError) as caught:
        await adapter.get_state(ExternalRefs("unknown", {}, {}, {}))
    assert caught.value.status_code == 404
    with pytest.raises(PlatformError):
        await adapter.apply(Operation("set_status", "ad", "unknown", "status", "active", "paused_owner"))


@pytest.mark.parametrize("value", [1.5, True, -1, "100"])
def test_money_rejects_non_integer_or_negative_values(value) -> None:
    with pytest.raises(ValueError):
        single_plan(daily=value)
    with pytest.raises(ValueError):
        AdTruth(cpc_kop=value)
    with pytest.raises(ValueError):
        Operation("set_budget", "campaign", "id", "daily_limit_kop", 1, value)


def test_operation_validates_fields_and_noninvertible_actions() -> None:
    with pytest.raises(ValueError):
        Operation("set_status", "ad", "id", "text", "before", "after")
    with pytest.raises(ValueError):
        Operation("set_budget", "ad", "id", "daily_limit_kop", 100, 200)
    with pytest.raises(ValueError):
        Operation("set_targeting", "group", "id", "targeting", {}, "city")
    operation = Operation("set_status", "ad", "id", "status", "active", "archived", invertible=False)
    with pytest.raises(ValueError, match="необратима"):
        operation.inverse()


def test_inverse_copies_nested_values() -> None:
    after = {"interests": ["уход"]}
    operation = Operation("set_targeting", "group", "id", "targeting", {}, after)
    inverse = operation.inverse()
    after["interests"].append("спорт")
    operation.after["interests"].append("волосы")
    assert inverse.before == {"interests": ["уход"]}


def test_duplicate_ids_and_invalid_simulation_parameters_are_rejected() -> None:
    plan = single_plan()
    with pytest.raises(ValueError):
        replace(plan, groups=(plan.groups[0], plan.groups[0]))
    with pytest.raises(ValueError):
        FailureScenario(rate_limited=0.6, server_error=0.6)
    with pytest.raises(ValueError):
        AdTruth(ctr=float("nan"))
    with pytest.raises(ValueError):
        FakeAdapter(conversion_delay=timedelta(days=-1))
    with pytest.raises(ValueError):
        FakeAdapter(scenario="unknown")


async def test_group_and_campaign_statistics_sum_all_ads() -> None:
    adapter = FakeAdapter(moderation_delay=timedelta(), seed=10)
    await adapter.create_campaign_tree(adapter.scenario.plan)
    day = adapter.clock.now.date()
    adapter.clock.advance(hours=2)
    totals = []
    for level in ("ad", "group", "campaign"):
        rows = await adapter.get_stats(day, day, level)
        totals.append(
            tuple(sum(getattr(row, field) for row in rows) for field in ("impressions", "clicks", "cost_kop"))
        )
    assert totals[0] == totals[1] == totals[2]
