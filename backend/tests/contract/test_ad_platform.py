from dataclasses import replace
from datetime import timedelta

import pytest

from app.platforms.base import AdPlatform, AuthExpired, ModerationRejected, Operation, PlatformError, RateLimited

from .conftest import AdapterHarness


async def test_creates_complete_tree_once(platform: AdapterHarness) -> None:
    adapter, plan = platform.adapter, platform.plan
    assert isinstance(adapter, AdPlatform)
    first = await adapter.create_campaign_tree(plan)
    original = await adapter.get_state(first)
    second = await adapter.create_campaign_tree(replace(plan, name="Повтор", daily_limit_kop=1))

    assert second == first
    assert await adapter.get_state(second) == original
    assert set(first.groups) == {group.id for group in plan.groups}
    assert set(first.ads) == {ad.id for group in plan.groups for ad in group.ads}
    assert set(first.targets) == {target.id for group in plan.groups for target in group.targets}
    assert len(first.entity_ids()) == len(set(first.entity_ids()))
    assert original[first.campaign_id].name.endswith(plan.idempotency_key)

    first.ads.clear()
    assert await adapter.create_campaign_tree(plan) == second
    other = await adapter.create_campaign_tree(replace(plan, id=plan.id + "-other"))
    assert not set(other.entity_ids()) & set(second.entity_ids())


@pytest.mark.parametrize(
    "kind,entity,field,after",
    [
        ("set_status", "campaign", "status", "paused_owner"),
        ("set_status", "group", "status", "paused_auto"),
        ("set_status", "ad", "status", "paused_owner"),
        ("set_status", "target", "status", "archived"),
        ("set_budget", "campaign", "daily_limit_kop", 12345),
        ("set_budget", "campaign", "period_cap_kop", 67890),
        ("update_ad_text", "ad", "title", "Новое название"),
        ("update_ad_text", "ad", "text", "Новый текст"),
        ("swap_ad_image", "ad", "image_ref", "uploaded-image-ref"),
        ("set_targeting", "group", "targeting", {"city": "Казань", "interests": ["уход"]}),
    ],
)
async def test_apply_and_inverse_restore_state(platform: AdapterHarness, kind, entity, field, after) -> None:
    adapter = platform.adapter
    refs = await adapter.create_campaign_tree(platform.plan)
    before = await adapter.get_state(refs)
    state = next(value for value in before.values() if value.entity_type == entity)
    operation = Operation(kind, entity, state.entity_id, field, getattr(state, field), after)

    await adapter.apply(operation)
    assert getattr((await adapter.get_state(refs))[state.entity_id], field) == after
    await adapter.apply(operation)
    assert getattr((await adapter.get_state(refs))[state.entity_id], field) == after
    await adapter.apply(operation.inverse())
    await adapter.apply(operation.inverse())

    assert await adapter.get_state(refs) == before
    assert operation.inverse().inverse() == operation


@pytest.mark.parametrize("level", ["ad", "group", "campaign"])
async def test_period_statistics_equal_daily_statistics(platform: AdapterHarness, level) -> None:
    adapter = platform.adapter
    await adapter.create_campaign_tree(platform.plan)
    start = platform.today()
    platform.advance(days=4)
    end = platform.today()
    period = await adapter.get_stats(start, end, level)
    daily = []
    for offset in range((end - start).days + 1):
        day = start + timedelta(days=offset)
        daily.extend(await adapter.get_stats(day, day, level))

    assert period
    assert sorted(period, key=lambda row: (row.entity_id, row.date)) == sorted(
        daily, key=lambda row: (row.entity_id, row.date)
    )
    for row in period:
        assert row.fetched_at.date() == end
        assert type(row.cost_kop) is int
        assert 0 <= row.clicks <= row.impressions
        assert all(0 <= conversions <= row.clicks for conversions in row.conversions.values())
        if row.date > end - timedelta(days=2):
            assert not row.is_settled


async def test_daily_and_period_limits_are_respected(platform: AdapterHarness) -> None:
    adapter = platform.adapter
    plan = replace(platform.plan, daily_limit_kop=17_123, period_cap_kop=45_678)
    await adapter.create_campaign_tree(plan)
    start = platform.today()
    for _ in range(8):
        platform.advance(hours=12)
        today = platform.today()
        assert 0 <= await adapter.get_spend_today() <= plan.daily_limit_kop
        today_rows = await adapter.get_stats(today, today, "campaign")
        assert await adapter.get_spend_today() == sum(row.cost_kop for row in today_rows)
        platform.advance(hours=12)
    rows = await adapter.get_stats(start, platform.today(), "campaign")
    assert rows and sum(row.cost_kop for row in rows) > 0
    assert all(row.cost_kop <= plan.daily_limit_kop for row in rows)
    assert sum(row.cost_kop for row in rows) <= plan.period_cap_kop


async def test_get_spend_today_counts_only_requested_campaigns(platform: AdapterHarness) -> None:
    adapter = platform.adapter
    refs = await adapter.create_campaign_tree(platform.plan)
    other = await adapter.create_campaign_tree(replace(platform.plan, id=platform.plan.id + "-other"))
    platform.advance(hours=2)
    first = await adapter.get_spend_today(campaign_ids={refs.campaign_id})
    second = await adapter.get_spend_today(campaign_ids={other.campaign_id})
    assert first > 0 and second > 0
    assert await adapter.get_spend_today(campaign_ids={"someone-else"}) == 0
    both = await adapter.get_spend_today(campaign_ids={refs.campaign_id, other.campaign_id})
    assert both == first + second


@pytest.mark.parametrize(
    "status,error", [(401, AuthExpired), (429, RateLimited), (500, PlatformError), (503, PlatformError)]
)
async def test_errors_use_common_types(platform_errors, status: int, error: type[PlatformError]) -> None:
    adapter, inject_error = platform_errors
    assert isinstance(adapter, AdPlatform)
    inject_error(status)
    with pytest.raises(error) as caught:
        await adapter.whoami()
    assert caught.value.status_code == status
    if isinstance(caught.value, RateLimited):
        assert caught.value.retry_after > 0


async def test_medical_rejection_uses_common_error(platform: AdapterHarness) -> None:
    group = platform.plan.groups[0]
    ad = replace(group.ads[0], medical=True, status="paused_owner")
    plan = replace(platform.plan, groups=(replace(group, ads=(ad,)),))
    refs = await platform.adapter.create_campaign_tree(plan)
    platform.advance(days=1)
    (review,) = await platform.adapter.get_moderation(refs)
    assert review.status == "rejected" and review.reason
    with pytest.raises(ModerationRejected) as caught:
        await platform.adapter.apply(
            Operation("set_status", "ad", review.entity_id, "status", "paused_owner", "active")
        )
    assert caught.value.entity_id == review.entity_id
    assert caught.value.reason == review.reason
