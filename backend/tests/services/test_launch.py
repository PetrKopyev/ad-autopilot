from datetime import date, timedelta
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from app.db.models import AdGroup, BudgetLedger, Campaign, ChangeLog
from app.platforms.base import ExternalRefs, PlatformError
from app.platforms.direct import DirectAdapter
from app.platforms.fake import FakeAdapter
from app.services.changes import ChangeConflict
from app.services.launch import LaunchBlocked, LaunchFailed
from app.services.plan import budget_limits
from app.services.registry import AccountInfo, AdapterRegistry
from tests.services.test_onboarding import (
    BUDGET_KOP,
    NOW,
    TODAY,
    World,
    make_world,
    onboard,
    page_text,
)


class FailingCreate(FakeAdapter):
    fail = True

    async def create_campaign_tree(self, plan_part):
        if self.fail:
            raise PlatformError("ВКонтакте временно недоступен.", status_code=503)
        return await super().create_campaign_tree(plan_part)


class FailingActivation(FakeAdapter):
    fail = True

    async def apply(self, operation) -> None:
        if self.fail and operation.entity_type == "campaign" and operation.after == "active":
            raise PlatformError("ВКонтакте отклонил запуск.", status_code=500)
        await super().apply(operation)


class StrictMinimum(FakeAdapter):
    async def min_budgets(self) -> dict[str, int]:
        return {"day": 10**9, "period": 0}


async def ready(tmp_path: Path, **kwargs) -> tuple[World, dict]:
    world = make_world(tmp_path, **kwargs)
    await onboard(world)
    return world, await world.plans.get_plan(world.business.id, NOW, TODAY)


def launch(world: World, plan: dict, acknowledged: bool = False):
    return world.launches.launch(world.business.id, plan["id"], acknowledged, NOW, TODAY)


def campaigns(world: World) -> list[Campaign]:
    return world.repository.rows(Campaign, business_id=world.business.id)


def refs_of(world: World, campaign: Campaign) -> ExternalRefs:
    groups, ads, targets = world.repository.tree_rows(campaign.id)
    return ExternalRefs(
        campaign.external_id,
        {str(row.id): row.external_id for row in groups},
        {str(row.id): row.external_id for row in ads},
        {str(row.id): row.external_id for row in targets},
    )


async def states(world: World, campaign: Campaign):
    return await world.adapters[campaign.platform].get_state(refs_of(world, campaign))


async def test_full_path_creates_limited_campaigns_within_budget(tmp_path: Path) -> None:
    world, plan = await ready(tmp_path)
    result = await launch(world, plan)
    assert result["status"] == "launched"
    assert world.business.status == "running" and world.business.settings["_worker"]["started_on"] == "2026-01-01"
    assert world.repository.latest_plan(world.business.id).status == "launched"
    created = campaigns(world)
    assert {item.platform for item in created} == {"direct", "vk"}
    caps = {item.platform: item.period_cap_kop for item in created}
    expected = budget_limits(world.business, ["direct", "vk"], TODAY)
    assert caps == {platform: limit.remaining_cap for platform, limit in expected.items()}
    assert sum(caps.values()) <= BUDGET_KOP * 92 // 100 and all(item.daily_limit_kop > 0 for item in created)
    for item in created:
        campaign_states = await states(world, item)
        state = campaign_states[item.external_id]
        assert state.status == "active"
        assert (state.daily_limit_kop, state.period_cap_kop) == (item.daily_limit_kop, item.period_cap_kop)
        ads = [value for value in campaign_states.values() if value.entity_type == "ad"]
        assert ads and all(ad.status == "active" and ad.image_ref for ad in ads)
    history = world.repository.rows(ChangeLog, business_id=world.business.id)
    assert sorted((change.kind, change.platform, change.status, change.actor) for change in history) == [
        ("launch", "direct", "applied", "owner"),
        ("launch", "vk", "applied", "owner"),
    ]
    assert all("Запустили рекламу" in change.reason_text for change in history)
    ledgers = world.repository.rows(BudgetLedger, business_id=world.business.id)
    assert {row.platform: row.period_cap_kop for row in ledgers} == caps


async def test_simulated_spend_never_exceeds_budget(tmp_path: Path) -> None:
    world, plan = await ready(tmp_path)
    await launch(world, plan)
    spent = 0
    for platform, adapter in world.adapters.items():
        adapter.clock.advance(days=20)
        rows = await adapter.get_stats(date(2026, 1, 1), date(2026, 1, 21), "campaign")
        platform_spent = sum(row.cost_kop for row in rows)
        assert 0 < platform_spent <= next(item.period_cap_kop for item in campaigns(world) if item.platform == platform)
        spent += platform_spent
    assert spent <= BUDGET_KOP


async def test_images_are_cut_uploaded_and_cached_per_platform(tmp_path: Path) -> None:
    world, plan = await ready(tmp_path)
    await launch(world, plan)
    selected = [asset for asset in world.repository.assets(world.business.id) if asset.selected]
    assert selected and all(set(asset.platform_refs) == {"direct", "vk"} for asset in selected)


async def test_second_launch_is_noop(tmp_path: Path) -> None:
    world, plan = await ready(tmp_path)
    first = await launch(world, plan)
    history = len(world.repository.rows(ChangeLog))
    assert await launch(world, plan) == first
    assert len(campaigns(world)) == 2 and len(world.repository.rows(ChangeLog)) == history
    assert all(len(adapter._trees) == 1 for adapter in world.adapters.values())


async def test_failure_midway_leaves_no_unlimited_or_active_campaign(tmp_path: Path) -> None:
    adapters = {"direct": FakeAdapter(), "vk": FailingCreate()}
    world, plan = await ready(tmp_path, adapters=adapters)
    with pytest.raises(LaunchFailed) as error:
        await launch(world, plan)
    assert "ВКонтакте временно недоступен" in str(error.value) and "повтор безопасен" in str(error.value)
    assert world.repository.latest_plan(world.business.id).status == "failed"
    assert world.business.status == "plan_ready" and not world.repository.rows(ChangeLog)
    direct = next(item for item in campaigns(world) if item.platform == "direct")
    state = (await states(world, direct))[direct.external_id]
    assert state.status == "paused_auto" and state.daily_limit_kop > 0 and state.period_cap_kop > 0
    assert (
        direct.status == "paused_auto"
        and not next(item for item in campaigns(world) if item.platform == "vk").external_id
    )
    adapters["direct"].clock.advance(days=3)
    assert await adapters["direct"].get_spend_today() == 0


async def test_retry_after_failure_is_safe_and_idempotent(tmp_path: Path) -> None:
    adapters = {"direct": FakeAdapter(), "vk": FailingCreate()}
    world, plan = await ready(tmp_path, adapters=adapters)
    with pytest.raises(LaunchFailed):
        await launch(world, plan)
    adapters["vk"].fail = False
    await launch(world, plan)
    assert len(campaigns(world)) == 2 and len(world.repository.rows(AdGroup)) == 6
    assert len(adapters["direct"]._trees) == 1 and len(adapters["vk"]._trees) == 1
    assert world.business.status == "running"
    for item in campaigns(world):
        assert (await states(world, item))[item.external_id].status == "active"


async def test_activation_failure_pauses_already_started_platform(tmp_path: Path) -> None:
    adapters = {"direct": FakeAdapter(), "vk": FailingActivation()}
    world, plan = await ready(tmp_path, adapters=adapters)
    with pytest.raises(LaunchFailed, match="не включила рекламу"):
        await launch(world, plan)
    for item in campaigns(world):
        state = (await states(world, item))[item.external_id]
        assert state.status == "paused_auto" and state.period_cap_kop > 0 and item.status == "paused_auto"
    assert world.business.status == "plan_ready"
    for adapter in adapters.values():
        adapter.clock.advance(days=3)
        assert await adapter.get_spend_today() == 0


async def test_retry_after_activation_failure_starts_everything(tmp_path: Path) -> None:
    adapters = {"direct": FakeAdapter(), "vk": FailingActivation()}
    world, plan = await ready(tmp_path, adapters=adapters)
    with pytest.raises(LaunchFailed):
        await launch(world, plan)
    adapters["vk"].fail = False
    await launch(world, plan)
    assert len(campaigns(world)) == 2
    for item in campaigns(world):
        assert (await states(world, item))[item.external_id].status == "active"


@pytest.mark.parametrize(
    ("prepare", "reason"),
    [
        ("no_marking", "маркировку"),
        ("vk_unconfirmed", "маркировку"),
        ("no_counter", "счётчика"),
        ("no_goal", "счётчика"),
        ("no_approval", "описание"),
        ("no_images", "картинку"),
        ("budget_changed", "изменились"),
    ],
)
async def test_launch_is_forbidden_until_requirements_are_met(tmp_path: Path, prepare: str, reason: str) -> None:
    world, plan = await ready(tmp_path)
    business = world.business
    if prepare == "no_marking":
        world.repository.tables[type(world.repository.marking(business.id))].clear()
    elif prepare == "vk_unconfirmed":
        world.repository.marking(business.id).confirmed_in_platform = {"direct": True, "vk": False}
    elif prepare == "no_counter":
        business.metrica_counter_id = None
    elif prepare == "no_goal":
        business.lead_goal_ids = []
    elif prepare == "no_approval":
        business.description_approved_at = None
    elif prepare == "no_images":
        for asset in world.repository.assets(business.id):
            asset.selected = False
    else:
        business.month_budget_kop += 100
    with pytest.raises(LaunchBlocked) as error:
        await launch(world, plan)
    assert reason in " ".join(error.value.reasons)
    assert not campaigns(world) and all(not adapter._trees for adapter in world.adapters.values())
    assert world.repository.latest_plan(business.id).status == "draft" and business.status == "plan_ready"


async def test_medical_launch_needs_acknowledgement(tmp_path: Path) -> None:
    world, plan = await ready(tmp_path, pages=page_text("Услуги косметолога и ботокс."))
    with pytest.raises(LaunchBlocked, match="лицензию"):
        await launch(world, plan, acknowledged=False)
    assert not campaigns(world)
    await launch(world, plan, acknowledged=True)
    assert world.business.status == "running"


async def test_direct_account_is_required(tmp_path: Path) -> None:
    world, plan = await ready(tmp_path, platforms=("vk",))
    with pytest.raises(LaunchBlocked, match="Директ"):
        await launch(world, plan)
    assert not campaigns(world)


async def test_budget_below_platform_minimum_is_blocked_before_creation(tmp_path: Path) -> None:
    adapters = {"direct": StrictMinimum(), "vk": FakeAdapter()}
    world, plan = await ready(tmp_path, adapters=adapters)
    with pytest.raises(LaunchBlocked, match="не хватает"):
        await launch(world, plan)
    assert not campaigns(world) and not adapters["vk"]._trees


async def test_unknown_plan_and_foreign_business(tmp_path: Path) -> None:
    world, plan = await ready(tmp_path)
    with pytest.raises(LookupError):
        await world.launches.launch(world.business.id, 999, False, NOW, TODAY)
    with pytest.raises(LookupError):
        await world.launches.launch(world.business.id + 1, plan["id"], False, NOW, TODAY)


async def test_concurrent_launch_is_rejected_without_marking_plan_failed(tmp_path: Path) -> None:
    world, plan = await ready(tmp_path)
    world.repository.locked = True
    with pytest.raises(ChangeConflict):
        await launch(world, plan)
    assert world.repository.latest_plan(world.business.id).status == "draft" and not campaigns(world)


async def test_launch_after_first_week_resets_ai_week_snapshot(tmp_path: Path) -> None:
    world, plan = await ready(tmp_path)
    await launch(world, plan)
    assert world.business.settings["_ai"]["base"] == world.repository.ai_spent(world.business.id)
    assert (TODAY - timedelta(days=TODAY.weekday())).isoformat() == world.business.settings["_ai"]["week"]


@pytest.fixture
async def http():
    async with httpx.AsyncClient() as client:
        yield client


def info(**changes) -> AccountInfo:
    values = {
        "status": "ok",
        "city": "Самара",
        "href": "https://salon.example/",
        "counter_id": 12345,
        "marking_confirmed": True,
        "token": "t",
    }
    return AccountInfo(**(values | changes))


async def test_registry_in_fake_mode_keeps_one_simulator_per_platform(http) -> None:
    registry = AdapterRegistry(lambda *key: None, mode="fake", http=http)
    direct = registry.resolve(1, "direct")
    assert isinstance(direct, FakeAdapter) and registry.get((1, "direct")) is direct
    assert registry.resolve(1, "vk") is not direct and registry.resolve(2, "direct") is not direct
    counters = await registry.metrica(1).list_counters()
    assert counters and (await registry.metrica(1).list_goals(counters[0].id))


async def test_registry_requires_connected_account_in_live_mode(http) -> None:
    accounts = {(1, "direct"): info(status="needs_reauth")}
    registry = AdapterRegistry(lambda *key: accounts.get(key), mode="live", http=http)
    for key in ((1, "direct"), (1, "vk")):
        with pytest.raises(PlatformError):
            registry.resolve(*key)
        assert registry.get(key) is None
    with pytest.raises(PlatformError):
        registry.metrica(1)


async def test_registry_rebuilds_adapter_only_when_account_data_changes(http) -> None:
    accounts = {(1, "direct"): info()}
    built = []

    class Counting(AdapterRegistry):
        def build(self, platform, account):
            built.append(account)
            return FakeAdapter()

    registry = Counting(lambda *key: accounts.get(key), mode="live", http=http)
    first = registry.resolve(1, "direct")
    assert registry.resolve(1, "direct") is first and len(built) == 1
    accounts[(1, "direct")] = info(counter_id=777)
    assert registry.resolve(1, "direct") is not first and len(built) == 2


async def test_registry_builds_direct_for_known_city_and_refuses_unknown(http) -> None:
    config = SimpleNamespace(
        direct_api_url="https://api.direct.yandex.com/json/v5",
        direct_api_v501_url="https://api.direct.yandex.com/v501",
        direct_sandbox=True,
    )
    registry = AdapterRegistry(lambda *key: None, mode="live", http=http, config=config)
    assert isinstance(registry.build("direct", info()), DirectAdapter)
    with pytest.raises(PlatformError, match="Урюпинск"):
        registry.build("direct", info(city="Урюпинск"))
