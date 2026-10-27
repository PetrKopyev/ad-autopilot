import asyncio
from datetime import date, timedelta
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from app.db.models import AdGroup, ArtCall, BudgetLedger, Campaign, ChangeLog, LLMCall
from app.platforms.base import ExternalRefs, PlatformError
from app.platforms.direct import DirectAdapter, weekly_limit
from app.platforms.fake import FakeAdapter
from app.services.changes import ChangeConflict
from app.services.launch import LaunchBlocked, LaunchFailed
from app.services.onboarding import ai_budget
from app.services.plan import budget_limits
from app.services.registry import AccountInfo, AdapterRegistry
from tests.services.test_onboarding import (
    BUDGET_KOP,
    LIMITS,
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


async def test_weekly_ai_limit_starts_at_launch_without_onboarding_spend(tmp_path: Path) -> None:
    world, plan = await ready(tmp_path)
    assert world.repository.ai_spent(world.business.id) > 0
    for call in world.repository.rows(LLMCall) + world.repository.rows(ArtCall):
        call.created_at = NOW - timedelta(minutes=5)
    await launch(world, plan)
    assert world.repository.launched_at(world.business.id) == NOW
    assert ai_budget(world.repository, world.business, LIMITS, TODAY).spent_kop == 0
    assert "_ai" not in world.business.settings


@pytest.fixture
async def http():
    async with httpx.AsyncClient() as client:
        yield client


def info(**changes) -> AccountInfo:
    values = {
        "status": "ok",
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


async def test_registry_builds_direct_for_any_city_region_is_resolved_at_launch(http) -> None:
    config = SimpleNamespace(
        direct_api_url="https://api.direct.yandex.com/json/v5",
        direct_api_v501_url="https://api.direct.yandex.com/v501",
        direct_sandbox=True,
    )
    registry = AdapterRegistry(lambda *key: None, mode="live", http=http, config=config)
    assert isinstance(registry.build("direct", info()), DirectAdapter)


class Minimums(FakeAdapter):
    """Минимумы под ключами настоящих площадок: Директ — недельный, VK — дневной и на кампанию."""

    def __init__(self, minimum: dict[str, int]) -> None:
        super().__init__()
        self.minimum = minimum

    async def min_budgets(self) -> dict[str, int]:
        return self.minimum


@pytest.mark.parametrize(
    "platform,minimum",
    [
        ("direct", {"weekly_limit_kop": 10**9}),
        ("direct", {"weekly_limit_kop": 30_000, "daily_limit_kop": 10**8}),
        ("vk", {"daily_limit_kop": 10**8, "period_cap_kop": 10_000}),
        ("vk", {"daily_limit_kop": 10_000, "period_cap_kop": 10**9}),
    ],
)
async def test_budget_below_real_platform_minimum_is_blocked(tmp_path: Path, platform, minimum) -> None:
    adapters = {"direct": FakeAdapter(), "vk": FakeAdapter()} | {platform: Minimums(minimum)}
    world, plan = await ready(tmp_path, adapters=adapters)
    with pytest.raises(LaunchBlocked, match="не хватает"):
        await launch(world, plan)
    assert not campaigns(world) and all(not adapter._trees for adapter in adapters.values())


async def test_budget_at_platform_minimums_launches(tmp_path: Path) -> None:
    world, plan = await ready(tmp_path)
    limits = budget_limits(world.business, ["direct", "vk"], TODAY)
    world.adapters["vk"] = Minimums(
        {"daily_limit_kop": limits["vk"].daily_limit, "period_cap_kop": limits["vk"].remaining_cap}
    )
    # Четверг: Директ получит недельный лимит на четыре дня до воскресенья, его и сравниваем с минимумом.
    weekly = weekly_limit(limits["direct"].daily_limit, limits["direct"].remaining_cap, TODAY)
    assert weekly == limits["direct"].daily_limit * 4
    world.adapters["direct"] = Minimums({"weekly_limit_kop": weekly})
    assert (await launch(world, plan))["status"] == "launched"
    assert world.adapters["direct"]._trees


async def test_direct_minimum_is_checked_against_the_weekly_limit_actually_sent(tmp_path: Path) -> None:
    """В четверг уходит лимит на 4 дня: минимум между 4 и 7 дневными раньше пропускался, а Директ его отклонял."""
    world, plan = await ready(tmp_path)
    limits = budget_limits(world.business, ["direct", "vk"], TODAY)
    world.adapters["direct"] = Minimums({"weekly_limit_kop": limits["direct"].daily_limit * 4 + 1})
    with pytest.raises(LaunchBlocked) as error:
        await launch(world, plan)
    assert error.value.reasons[0].startswith("До конца недели осталось мало дней: «Яндекс Директ» не примет")
    assert not campaigns(world)


async def test_exhausted_day_is_explained_before_platform_minimums(tmp_path: Path, monkeypatch) -> None:
    from app.domain.types import PlatformLimit

    world, plan = await ready(tmp_path)
    world.adapters["direct"] = Minimums({"weekly_limit_kop": 10**9})
    monkeypatch.setattr(
        "app.services.launch.budget_limits",
        lambda *args: {platform: PlatformLimit(platform, 100_000, 0, 100_000) for platform in ("direct", "vk")},
    )
    with pytest.raises(LaunchBlocked, match="почти исчерпан"):
        await launch(world, plan)


class Regions(FakeAdapter):
    def __init__(self, geo: dict | None) -> None:
        super().__init__()
        self.geo = geo
        self.specs = []

    async def geo_targeting(self, city: str) -> dict:
        if self.geo is None:
            raise PlatformError(f"Город «{city}» не найден в справочнике регионов площадки.")
        return self.geo

    async def create_campaign_tree(self, plan_part):
        self.specs.append(plan_part)
        return await super().create_campaign_tree(plan_part)


async def test_groups_get_region_of_each_platform(tmp_path: Path) -> None:
    adapters = {"direct": Regions({"region_ids": [51]}), "vk": Regions({"regions": [123]})}
    world, plan = await ready(tmp_path, adapters=adapters)
    await launch(world, plan)
    for platform, geo in (("direct", {"region_ids": [51]}), ("vk", {"regions": [123]})):
        groups = adapters[platform].specs[0].groups
        assert groups and all(group.targeting == geo for group in groups)


async def test_unknown_city_blocks_launch_before_anything_is_created(tmp_path: Path) -> None:
    adapters = {"direct": Regions({"region_ids": [51]}), "vk": Regions(None)}
    world, plan = await ready(tmp_path, adapters=adapters)
    with pytest.raises(LaunchBlocked) as error:
        await launch(world, plan)
    assert error.value.reasons == ["ВКонтакте: Город «Самара» не найден в справочнике регионов площадки."]
    assert not campaigns(world) and not adapters["direct"].specs
    assert world.repository.latest_plan(world.business.id).status == "draft"


async def test_zero_budget_cannot_launch(tmp_path: Path) -> None:
    world, plan = await ready(tmp_path)
    world.business.month_budget_kop = 0
    plan = await world.plans.get_plan(world.business.id, NOW, TODAY)
    with pytest.raises(LaunchBlocked):
        await launch(world, plan)
    assert not campaigns(world)


class SlowActivation(FakeAdapter):
    """Первое включение кампании ждёт сигнала: второй запрос «Запустить» приходит, пока первый ещё идёт."""

    def __init__(self) -> None:
        super().__init__()
        self.entered = asyncio.Event()
        self.release = asyncio.Event()

    async def apply(self, operation) -> None:
        if operation.entity_type == "campaign" and operation.after == "active" and not self.release.is_set():
            self.entered.set()
            await self.release.wait()
        await super().apply(operation)


async def test_second_launch_while_first_is_running_creates_nothing(tmp_path: Path) -> None:
    adapters = {"direct": SlowActivation(), "vk": FakeAdapter()}
    world, plan = await ready(tmp_path, adapters=adapters)
    first = asyncio.create_task(launch(world, plan))
    await adapters["direct"].entered.wait()
    with pytest.raises(LaunchBlocked, match="Запуск уже идёт"):
        await launch(world, plan)
    adapters["direct"].release.set()
    assert (await first)["status"] == "launched"
    assert len(campaigns(world)) == 2
    launches = [row for row in world.repository.rows(ChangeLog, business_id=world.business.id) if row.kind == "launch"]
    assert sorted(row.platform for row in launches) == ["direct", "vk"]
    assert (await launch(world, plan))["status"] == "launched"


async def test_failed_launch_can_be_retried_at_once(tmp_path: Path) -> None:
    adapters = {"direct": FakeAdapter(), "vk": FailingActivation()}
    world, plan = await ready(tmp_path, adapters=adapters)
    with pytest.raises(LaunchFailed):
        await launch(world, plan)
    adapters["vk"].fail = False
    assert (await launch(world, plan))["status"] == "launched"


@pytest.mark.parametrize(
    "today,advice",
    [
        (date(2026, 1, 8), "До конца недели осталось мало дней"),  # четверг: неделю обрезает воскресенье
        (date(2026, 4, 29), "До конца месяца осталось мало дней"),  # среда, а 30 апреля — конец месяца
        (date(2026, 5, 31), "До конца недели осталось мало дней"),  # воскресенье и последний день месяца
    ],
)
def test_direct_weekly_minimum_advice_names_what_cuts_the_week(today, advice) -> None:
    from app.domain.types import PlatformLimit
    from app.services.launch import below_minimum

    limit = PlatformLimit("direct", 10_000_000, 100_000, 10_000_000)
    text = below_minimum("direct", limit, {"weekly_limit_kop": 600_000}, today)
    assert text.startswith(advice)
    start = "с 1-го числа" if "месяца" in advice else "с понедельника"
    assert f"Запустите рекламу {start}" in text
    assert below_minimum("direct", limit, {"weekly_limit_kop": 100_000}, today) is None


async def test_second_launch_rereads_plan_under_lock_and_does_not_create_twice(tmp_path: Path, monkeypatch) -> None:
    """Второй запрос прочитал план до блокировки, пока первый создавал кампании: под блокировкой план перечитывается."""
    from copy import copy

    world, draft = await ready(tmp_path)
    stored = world.repository.get_plan(draft["id"], world.business.id)
    stale = copy(stored)
    monkeypatch.setattr(world.repository, "get_plan", lambda plan_id, business_id: stale)
    monkeypatch.setattr(
        world.repository,
        "refresh",
        lambda item: item.__dict__.update(
            {key: value for key, value in stored.__dict__.items() if not key.startswith("_")}
        ),
    )
    stored.status = "launched"
    stored.payload = stored.payload | {"launch": {"campaigns": {"direct": 1}, "launched_at": NOW.isoformat()}}
    result = await launch(world, draft)
    assert result["status"] == "launched" and not campaigns(world)
