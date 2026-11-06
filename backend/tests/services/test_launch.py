import asyncio
from contextlib import contextmanager
from dataclasses import replace
from datetime import date, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from cryptography.fernet import InvalidToken

from app.content.images import run_decoding
from app.db.models import Ad, ArtCall, BudgetLedger, Business, Campaign, ChangeLog, LLMCall, Plan, WorkerRun
from app.platforms.base import AwaitingModeration, ExternalRefs, Operation, PlatformError
from app.platforms.direct import DirectAdapter, weekly_limit
from app.platforms.fake import FakeAdapter, VirtualClock
from app.services import launch as launch_module
from app.services import plan as plan_module
from app.services.changes import ChangeConflict, load_operations
from app.services.launch import LaunchBlocked, LaunchFailed
from app.services.onboarding import Blocked, NotFound, ai_budget
from app.services.plan import budget_limits, launch_unsettled
from app.services.registry import AccountInfo, AdapterRegistry
from app.services.rollback import can_revert
from app.worker.jobs import MemoryWorkerRepository, Worker
from tests.services.test_onboarding import (
    BUDGET_KOP,
    COUNTER,
    LIMITS,
    NOW,
    SITE,
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


class MismatchingCreate(FakeAdapter):
    """Как Директ после правки владельца в кабинете: дерево создаётся, но содержимое отличалось от плана."""

    async def create_campaign_tree(self, plan_part):
        refs = await super().create_campaign_tree(plan_part)
        self.content_mismatches = [
            "В кабинете объявление «Маникюр» группы «Маникюр» отличается от плана — создали заново."
        ]
        self.foreign_stops = [
            Operation("set_status", "ad", "991", "status", "active", "archived"),
            Operation("set_status", "target", "992", "status", "active", "paused_owner"),
        ]
        return refs


class FailingActivation(FakeAdapter):
    fail = True

    async def apply(self, operation) -> None:
        if self.fail and operation.entity_type == "campaign" and operation.after == "active":
            raise PlatformError("ВКонтакте отклонил запуск.", status_code=500)
        await super().apply(operation)


class DraftCampaign(FakeAdapter):
    """Кампания-черновик v501: создаётся черновиком, включение отклоняется, пока площадка проверяет
    объявления; остановить черновик нельзя — как живой Директ. После одобрения объявлений повторный
    resume принимается."""

    def __init__(self) -> None:
        super().__init__()
        self.accepted = False

    async def create_campaign_tree(self, plan_part):
        refs = await super().create_campaign_tree(plan_part)
        if not self.accepted:
            state = self._states[refs.campaign_id]
            self._states[refs.campaign_id] = replace(state, status="draft")
        return refs

    async def apply(self, operation) -> None:
        if not self.accepted and operation.entity_type == "campaign" and operation.after == "active":
            raise AwaitingModeration(operation.entity_id)
        if not self.accepted and operation.entity_type == "campaign" and operation.after == "paused_auto":
            raise PlatformError("Кампания является черновиком и не может быть остановлена.")
        await super().apply(operation)


class StrictMinimum(FakeAdapter):
    async def min_budgets(self) -> dict[str, int]:
        return {"day": 10**9, "period": 0}


class SlowModeration(FakeAdapter):
    """Как живой Директ с кампаниями-черновиками v501: включение каждой кампании остаётся ждать проверки
    объявлений, пока площадка не одобрит (accepted); после одобрения повторный resume принимается."""

    def __init__(self) -> None:
        super().__init__()
        self.accepted = False

    async def create_campaign_tree(self, plan_part):
        refs = await super().create_campaign_tree(plan_part)
        if not self.accepted:
            state = self._states[refs.campaign_id]
            self._states[refs.campaign_id] = replace(state, status="draft")
        return refs

    async def apply(self, operation) -> None:
        if not self.accepted and operation.entity_type == "campaign" and operation.after == "active":
            raise AwaitingModeration(operation.entity_id)
        await super().apply(operation)


class FirstCampaignModerates(FakeAdapter):
    """Первая кампания включается с ожиданием проверки, остальные отклоняются: запуск падает после первого
    ожидания — как на проде в ноябре 2026-го."""

    def __init__(self) -> None:
        super().__init__()
        self.accepted = False
        self.awaited = False

    async def apply(self, operation) -> None:
        if not self.accepted and operation.entity_type == "campaign" and operation.after == "active":
            if not self.awaited:
                self.awaited = True
                raise AwaitingModeration(operation.entity_id)
            raise PlatformError("Площадка отклонила запуск кампании.", status_code=500)
        await super().apply(operation)


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


async def test_launch_logs_recreated_cabinet_content(tmp_path: Path) -> None:
    """Содержимое кабинета отличалось от плана (владелец правил текст): запуск пересоздаёт по плану и пишет
    об этом честную запись в журнал — иначе «лишнее» объявление в кабинете было бы владельцу непонятно."""
    world, plan = await ready(tmp_path, adapters={"direct": MismatchingCreate(), "vk": FakeAdapter()})
    assert (await launch(world, plan))["status"] == "launched"
    records = world.repository.rows(ChangeLog, kind="launch", reason_code="LAUNCH_CONTENT_RECREATED")
    assert len(records) == 1
    record = records[0]
    assert record.platform == "direct" and record.status == "applied" and record.actor == "system"
    assert record.reason_text == (
        "В кабинете объявление «Маникюр» группы «Маникюр» отличается от плана — создали заново."
    )
    assert not world.repository.rows(ChangeLog, kind="launch", reason_code="LAUNCH_CONTENT_RECREATED", platform="vk")


async def test_launch_stops_foreign_cabinet_content_with_revertible_record(tmp_path: Path) -> None:
    """Содержимого кабинета, которого нет в плане, запуск лишает активных статусов (иначе оно тратит бюджет
    мимо статистики и недоступно для «остановить всё»), а операции остановок сохраняет единой откатываемой
    записью со ссылками на остановленные сущности."""
    world, plan = await ready(tmp_path, adapters={"direct": MismatchingCreate(), "vk": FakeAdapter()})
    assert (await launch(world, plan))["status"] == "launched"
    (record,) = world.repository.rows(ChangeLog, kind="launch", reason_code="LAUNCH_FOREIGN_STOPPED")
    assert record.platform == "direct" and record.status == "applied" and record.actor == "system"
    assert [load_operations(record)[index].after for index in (0, 1)] == ["archived", "paused_owner"]
    refs = record.evidence["_service"]["refs"]
    assert refs["ads"]["foreign-ad:991"] == "991" and refs["targets"]["foreign-target:992"] == "992"
    assert "незаметно для статистики" in record.reason_text
    # Информационные записи о пересоздании по-прежнему без операций: их откатывать нечего.
    (info,) = world.repository.rows(ChangeLog, kind="launch", reason_code="LAUNCH_CONTENT_RECREATED")
    assert info.operations == []


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


def selected_platforms(plan: dict) -> dict[int, set[str]]:
    """Площадки, на которые уходит каждая выбранная картинка: картинки пары — в группы этой пары на обеих площадках."""
    result: dict[int, set[str]] = {}
    for group in plan["groups"]:
        for asset in group["assets"]:
            if asset["selected"]:
                result.setdefault(asset["id"], set()).add(group["platform"])
    return result


async def test_images_are_cut_uploaded_and_cached_per_platform(tmp_path: Path) -> None:
    world, plan = await ready(tmp_path)
    await launch(world, plan)
    expected = selected_platforms(plan)
    selected = [asset for asset in world.repository.assets(world.business.id) if asset.selected]
    assert {"direct", "vk"} in expected.values()
    assert selected and all(set(asset.platform_refs) == expected[asset.id] for asset in selected)


async def test_images_are_cut_in_decode_queue(tmp_path: Path, monkeypatch) -> None:
    """Нарезка разбирает картинку: она идёт через очередь разбора, а не прямо в пуле потоков, иначе вместе с загрузками
    и сборкой роликов слот ждали бы больше двух потоков."""
    world, plan = await ready(tmp_path)
    calls = []

    async def spy(func, *args, **kwargs):
        calls.append(func)
        return await run_decoding(func, *args, **kwargs)

    monkeypatch.setattr(launch_module, "run_decoding", spy)
    await launch(world, plan)
    assert calls == [world.launches.cut_image] * sum(map(len, selected_platforms(plan).values()))


async def test_image_with_reference_of_other_platform_only_is_uploaded_before_campaign(tmp_path: Path) -> None:
    # spec берёт ссылку картинки площадки из platform_refs без запасного пути: её кладёт upload_images для каждой
    # выбранной картинки, даже если там уже есть ссылка другой площадки.
    world, plan = await ready(tmp_path, platforms=("direct",))
    selected = [asset for asset in world.repository.assets(world.business.id) if asset.selected]
    for asset in selected:
        asset.platform_refs = {"vk": f"vk-{asset.id}"}
    await launch(world, plan)
    assert selected and all(asset.platform_refs["vk"] == f"vk-{asset.id}" for asset in selected)
    assert all(asset.platform_refs["direct"].startswith("fake-image-") for asset in selected)
    (campaign,) = campaigns(world)
    ads = [state for state in (await states(world, campaign)).values() if state.entity_type == "ad"]
    assert ads and all(ad.image_ref.startswith("fake-image-") for ad in ads)


async def test_second_launch_is_noop(tmp_path: Path) -> None:
    world, plan = await ready(tmp_path)
    first = await launch(world, plan)
    history = len(world.repository.rows(ChangeLog))
    assert await launch(world, plan) == first
    assert len(campaigns(world)) == 2 and len(world.repository.rows(ChangeLog)) == history
    assert all(len(adapter._trees) == 1 for adapter in world.adapters.values())


async def test_create_failure_of_one_platform_launches_the_rest(tmp_path: Path) -> None:
    """Сбой создания на одной площадке не роняет запуск целиком: Директ создан, активирован и работает,
    отказавшая площадка зафиксирована честной записью — владельцу не нужно ничего перезапускать вручную."""
    adapters = {"direct": FakeAdapter(), "vk": FailingCreate()}
    world, plan = await ready(tmp_path, adapters=adapters)
    result = await launch(world, plan)
    assert result["status"] == "launched"
    assert result["platform_errors"] == {
        "vk": "Площадка «ВКонтакте» не создала рекламу: ВКонтакте временно недоступен."
    }
    assert world.business.status == "running"
    stored = world.repository.latest_plan(world.business.id)
    assert stored.status == "launched"
    assert stored.payload["launch"]["platform_errors"]["vk"].startswith("Площадка «ВКонтакте» не создала рекламу")
    direct = next(item for item in campaigns(world) if item.platform == "direct")
    state = (await states(world, direct))[direct.external_id]
    assert state.status == "active" and state.daily_limit_kop > 0 and state.period_cap_kop > 0
    vk = next(item for item in campaigns(world) if item.platform == "vk")
    assert vk.external_id is None and vk.status == "paused_auto"
    # Итог честен: одна запись об успешном запуске Директа и одна о неудаче ВКонтакте.
    (record,) = world.repository.rows(ChangeLog, kind="launch", reason_code="LAUNCH_PLATFORM_FAILED")
    assert record.platform == "vk" and record.status == "failed" and record.actor == "system"
    assert "ВКонтакте временно недоступен" in record.reason_text
    launches = [row for row in world.repository.rows(ChangeLog, kind="launch") if row.status == "applied"]
    assert [(row.platform, row.actor) for row in launches] == [("direct", "owner")]
    # Бюджет защищён: лимиты дневные и кэпы у созданного, журнал расхода — только за запущенную площадку.
    ledgers = world.repository.rows(BudgetLedger, business_id=world.business.id)
    assert {row.platform for row in ledgers} == {"direct"}
    adapters["direct"].clock.advance(days=3)
    assert await adapters["direct"].get_spend_today() > 0 and not adapters["vk"]._trees


async def test_failed_platform_can_be_retried_without_touching_launched_one(tmp_path: Path) -> None:
    """Повторный запуск после частичного доводит только отказавшую площадку: Директ не пересоздаётся,
    пометка о неудаче снимается."""
    adapters = {"direct": FakeAdapter(), "vk": FailingCreate()}
    world, plan = await ready(tmp_path, adapters=adapters)
    first = await launch(world, plan)
    assert first["platform_errors"] and world.business.status == "running"
    adapters["vk"].fail = False
    result = await launch(world, plan)
    assert result["status"] == "launched" and result["platform_errors"] == {}
    assert len(campaigns(world)) == 2 and len(adapters["direct"]._trees) == 1 and len(adapters["vk"]._trees) == 1
    assert world.business.status == "running"
    for item in campaigns(world):
        assert (await states(world, item))[item.external_id].status == "active"
    ledgers = world.repository.rows(BudgetLedger, business_id=world.business.id)
    assert {row.platform for row in ledgers} == {"direct", "vk"}
    assert not world.repository.pending(world.business.id)


async def test_activation_failure_of_platform_keeps_launched_ones(tmp_path: Path) -> None:
    """Площадка, которая не включила рекламу, не гасит остальные: её кампания остаётся созданной,
    но выключенной и ничего не тратит, а запущенные площадки работают."""
    adapters = {"direct": FakeAdapter(), "vk": FailingActivation()}
    world, plan = await ready(tmp_path, adapters=adapters)
    result = await launch(world, plan)
    assert result["platform_errors"]["vk"].startswith("Площадка «ВКонтакте» не включила рекламу.")
    assert world.business.status == "running"
    for item in campaigns(world):
        state = (await states(world, item))[item.external_id]
        assert state.status == ("paused_auto" if item.platform == "vk" else "active")
        assert state.period_cap_kop > 0 and item.status == state.status
    (record,) = world.repository.rows(ChangeLog, kind="launch", reason_code="LAUNCH_PLATFORM_FAILED")
    assert record.platform == "vk" and record.status == "failed" and "не включила рекламу" in record.reason_text
    for adapter in adapters.values():
        adapter.clock.advance(days=3)
    assert await adapters["vk"].get_spend_today() == 0
    assert await adapters["direct"].get_spend_today() > 0


async def test_failed_platform_activation_is_retried_after_fix(tmp_path: Path) -> None:
    """После починки площадки повтор запуска включает и её — на кампаниях той же попытки."""
    adapters = {"direct": FakeAdapter(), "vk": FailingActivation()}
    world, plan = await ready(tmp_path, adapters=adapters)
    await launch(world, plan)
    adapters["vk"].fail = False
    result = await launch(world, plan)
    assert result["platform_errors"] == {}
    assert len(campaigns(world)) == 2 and len(adapters["vk"]._trees) == 1
    for item in campaigns(world):
        assert (await states(world, item))[item.external_id].status == "active"


class FailingSecondCreate(FakeAdapter):
    """Сбой создания второй кампании площадки: первая уже создана, остальные — нет."""

    fail = True
    attempts = 0

    async def create_campaign_tree(self, plan_part):
        self.attempts += 1
        if self.fail and self.attempts == 2:
            raise PlatformError("Директ не ответил.", status_code=503)
        return await super().create_campaign_tree(plan_part)


async def test_partially_created_platform_is_not_activated_and_retry_completes(tmp_path: Path) -> None:
    """Транзиентный сбой на второй кампании многоуслугового Директа. Созданная кампания площадки
    не включается (журнал не утверждает «не создала рекламу», пока её кампания стоит), запись честна,
    повтор доводит Директ на сохранённых кампаниях, работающую ВКонтакте не трогает."""
    services = ["Маникюр", "Педикюр", "Наращивание ресниц"]
    adapters = {"direct": FailingSecondCreate(), "vk": FakeAdapter()}
    world, plan = await ready_with_services(tmp_path, services, adapters=adapters)
    result = await launch(world, plan)
    assert result["status"] == "launched"
    assert result["awaiting_moderation"] == []
    assert result["platform_errors"] == {
        "direct": (
            "Площадка «Яндекс Директ» создала рекламу частично и не включила её: Директ не ответил. "
            "Повторный запуск доведёт площадку, работающие не тронет."
        )
    }
    assert world.business.status == "running"
    stored = world.repository.latest_plan(world.business.id)
    assert stored.status == "launched" and set(stored.payload["launch"]["platform_errors"]) == {"direct"}
    # Созданная кампания Директ остановлена и на площадке, и у нас; вторая осталась строкой без external_id.
    direct = sorted((item for item in campaigns(world) if item.platform == "direct"), key=lambda item: item.id)
    assert len(direct) == 2 and direct[0].external_id and direct[1].external_id is None
    assert (await states(world, direct[0]))[direct[0].external_id].status == "paused_auto"
    assert direct[0].status == "paused_auto"
    (record,) = world.repository.rows(ChangeLog, kind="launch", reason_code="LAUNCH_PLATFORM_FAILED")
    assert record.platform == "direct" and record.status == "failed"
    assert "создала рекламу частично" in record.reason_text
    # ВКонтакте работает и тратит, Директ — нет; журнал расхода только за ВКонтакте.
    vk = next(item for item in campaigns(world) if item.platform == "vk")
    assert (await states(world, vk))[vk.external_id].status == "active"
    ledgers = world.repository.rows(BudgetLedger, business_id=world.business.id)
    assert {row.platform for row in ledgers} == {"vk"}
    adapters["direct"].clock.advance(days=2)
    adapters["vk"].clock.advance(days=2)
    assert await adapters["direct"].get_spend_today() == 0 and await adapters["vk"].get_spend_today() > 0

    # Повтор после починки: Директ доводится на кампаниях той же попытки, ВКонтакте не пересоздаётся.
    adapters["direct"].fail = False
    retry = await launch(world, plan)
    assert retry["status"] == "launched" and retry["platform_errors"] == {}
    assert len(adapters["vk"]._trees) == 1
    direct = sorted((item for item in campaigns(world) if item.platform == "direct"), key=lambda item: item.id)
    assert len(direct) == 3 and len(adapters["direct"]._trees) == 3
    for item in direct:
        assert item.status == "active"
        assert (await states(world, item))[item.external_id].status == "active"
    assert world.repository.latest_plan(world.business.id).payload["launch"]["platform_errors"] == {}


async def test_retry_after_owner_stop_requires_resume_first(tmp_path: Path) -> None:
    """Повтор запуска после «Остановить всю рекламу» не снимает паузу сам и не ставит бизнесу
    running с зажатым kill_switch: сначала «Продолжить», потом повтор."""
    adapters = {"direct": FakeAdapter(), "vk": FailingCreate()}
    world, plan = await ready(tmp_path, adapters=adapters)
    assert (await launch(world, plan))["status"] == "launched"
    world.business.settings = world.business.settings | {"_worker": {"kill_switch": True}}
    world.business.status = "paused"
    world.repository.save(world.business)
    with pytest.raises(LaunchBlocked, match="Продолжить"):
        await launch(world, plan)
    assert world.business.status == "paused"
    stored = world.repository.latest_plan(world.business.id)
    assert set(stored.payload["launch"]["platform_errors"]) == {"vk"}


async def test_retry_keeps_started_on_within_month(tmp_path: Path) -> None:
    """Повтор частичного запуска не сдвигает started_on на сегодня. Иначе разрешённый расход месяца
    считается от повтора, остаток против полного расхода почти нулевой, кампании встают без дневного
    бюджета, а страж бюджета после перезапуска ложно останавливает рекламу."""
    adapters = {"direct": FakeAdapter(), "vk": FailingCreate()}
    world, plan = await ready(tmp_path, adapters=adapters)
    assert (await launch(world, plan))["status"] == "launched"
    assert world.business.settings["_worker"]["started_on"] == TODAY.isoformat()
    world.repository.save(
        BudgetLedger(
            business_id=world.business.id,
            month=TODAY,
            platform="direct",
            allocated_kop=3_500_000,
            period_cap_kop=3_500_000,
            spent_kop=1_800_000,
        )
    )
    adapters["direct"].clock.advance(days=10)
    adapters["vk"].clock.advance(days=10)
    adapters["vk"].fail = False
    result = await world.launches.launch(
        world.business.id, plan["id"], False, NOW + timedelta(days=10), TODAY + timedelta(days=10)
    )
    assert result["status"] == "launched"
    assert world.business.settings["_worker"]["started_on"] == TODAY.isoformat()


async def test_retry_limits_follow_plan_shares_and_month_spend(tmp_path: Path) -> None:
    """Повтор считает лимиты от всех площадок плана и остатка месяца, а не как будто площадка одна:
    иначе новая кампания получает дневной лимит всего бюджета, а площадки вместе держат ~170% бюджета."""
    from app.config import get_domain_settings
    from app.domain.budget import campaign_limits, plan_budget

    adapters = {"direct": FakeAdapter(), "vk": FailingCreate()}
    world, plan = await ready(tmp_path, adapters=adapters)
    assert (await launch(world, plan))["status"] == "launched"
    spent = 1_800_000
    world.repository.save(
        BudgetLedger(
            business_id=world.business.id,
            month=TODAY,
            platform="direct",
            allocated_kop=3_500_000,
            period_cap_kop=3_500_000,
            spent_kop=spent,
        )
    )
    adapters["direct"].clock.advance(days=10)
    adapters["vk"].clock.advance(days=10)
    adapters["vk"].fail = False
    retry_day = TODAY + timedelta(days=10)
    result = await world.launches.launch(world.business.id, plan["id"], False, NOW + timedelta(days=10), retry_day)
    assert result["status"] == "launched"
    domain = get_domain_settings()
    budget = plan_budget(BUDGET_KOP, spent, retry_day, ("direct", "vk"), started_on=TODAY, settings=domain)
    vk_plan_limit = next(item for item in budget.limits if item.platform == "vk")
    expected_daily, expected_cap = campaign_limits(vk_plan_limit.daily_limit, vk_plan_limit.remaining_cap, retry_day)
    vk_campaign = next(item for item in campaigns(world) if item.platform == "vk")
    assert vk_campaign.daily_limit_kop == expected_daily
    assert vk_campaign.period_cap_kop == expected_cap
    (vk_ledger,) = [
        row for row in world.repository.rows(BudgetLedger, business_id=world.business.id) if row.platform == "vk"
    ]
    assert vk_ledger.allocated_kop == expected_cap
    # Регрессионная суть: дневной лимит повторной кампании — доля VK, а не весь бюджет площадке-одиночке.
    assert expected_daily < BUDGET_KOP // 31


async def test_all_platforms_failed_launch_stops_everything_and_can_be_repeated(tmp_path: Path) -> None:
    """Если не запустилась ни одна площадка, запуск по-прежнему целиком неудачный: созданное остановлено,
    план пересобирается сразу, а повтор после починки идёт на тех же кампаниях."""
    adapters = {"direct": FailingCreate(), "vk": FailingCreate()}
    world, plan = await ready(tmp_path, adapters=adapters)
    with pytest.raises(LaunchFailed) as error:
        await launch(world, plan)
    assert "повтор безопасен" in str(error.value) and world.business.status == "plan_ready"
    stored = world.repository.latest_plan(world.business.id)
    assert stored.status == "failed" and stored.payload["launch"]["started_at"] is None
    assert stored.payload["launch"]["platform_errors"].keys() == {"direct", "vk"}
    # Упавший запуск остановил созданное: план с новыми данными собирается сразу, а не после воркера.
    world.business.month_budget_kop += 100_000
    rebuilt = await world.plans.get_plan(world.business.id, NOW, TODAY)
    assert rebuilt["id"] != plan["id"] and rebuilt["month_budget_kop"] == world.business.month_budget_kop
    adapters["direct"].fail = False
    adapters["vk"].fail = False
    assert (await launch(world, rebuilt))["status"] == "launched"
    assert len(campaigns(world)) == 4 and len(adapters["direct"]._trees) == 1 and len(adapters["vk"]._trees) == 1
    statuses = {
        item.platform: (await states(world, item))[item.external_id].status
        for item in campaigns(world)
        if item.external_id
    }
    assert statuses == {"direct": "active", "vk": "active"}


async def test_launch_record_is_not_reverted_by_journal(tmp_path: Path) -> None:
    """Запись запуска Директа отдельной отменой не откатывается — бизнес остался бы
    «работает» при остановленных кампаниях; останавливают рекламу на главной. Информационная запись об отказавшей
    площадке тоже не откатывается: операций нет."""
    from app.services.rollback import TURN_ON, RollbackService, can_revert

    adapters = {"direct": FakeAdapter(), "vk": FailingCreate()}
    world, plan = await ready(tmp_path, adapters=adapters)
    result = await launch(world, plan)
    assert result["platform_errors"]
    launches = [row for row in world.repository.rows(ChangeLog, kind="launch") if row.business_id == world.business.id]
    direct_launch = next(row for row in launches if row.platform == "direct" and row.status == "applied")
    failed_record = next(row for row in launches if row.reason_code == "LAUNCH_PLATFORM_FAILED")
    assert not can_revert(direct_launch, world.business)
    assert not can_revert(failed_record, world.business)
    rollback = RollbackService(recovery_worker(world).repository, dry_run=False)
    with pytest.raises(ChangeConflict, match=TURN_ON):
        await rollback.revert(
            direct_launch.id, business_id=world.business.id, adapter=world.adapters["direct"], now=NOW
        )
    direct = next(item for item in campaigns(world) if item.platform == "direct")
    assert (await states(world, direct))[direct.external_id].status == "active"


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
    with pytest.raises(NotFound):
        await world.launches.launch(world.business.id, 999, False, NOW, TODAY)
    with pytest.raises(NotFound, match="План не найден"):
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
        direct_api_v501_url="https://api.direct.yandex.com/json/v501",
        direct_sandbox=True,
        direct_amounts_include_vat=False,
        vat_percent=22,
    )
    registry = AdapterRegistry(lambda *key: None, mode="live", http=http, config=config)
    assert isinstance(registry.build("direct", info()), DirectAdapter)


async def test_registry_passes_business_goals_to_direct(http) -> None:
    config = SimpleNamespace(
        direct_api_url="https://api.direct.yandex.com/json/v5",
        direct_api_v501_url="https://api.direct.yandex.com/json/v501",
        direct_sandbox=False,
        direct_amounts_include_vat=True,
        vat_percent=22,
    )
    registry = AdapterRegistry(lambda *key: None, mode="live", http=http, config=config)
    adapter = registry.build("direct", info(goal_ids=(34, 56)))
    assert adapter._report_goals == (34, 56) and not adapter._goals
    adapter = registry.build("direct", info(goal_ids=(34,), goal_value_kop=150000))
    assert "PriorityGoals" not in adapter._campaign_settings(1000)
    adapter._strategy = "WB_MAXIMUM_CONVERSION_RATE"
    assert adapter._campaign_settings(1000)["PriorityGoals"] == {"Items": [{"GoalId": 34, "Value": 1500000000}]}


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


REAL_MINIMUMS = {"direct": {"weekly_limit_kop": 36_600}, "vk": {"daily_limit_kop": 30_000, "period_cap_kop": 30_000}}


@pytest.mark.parametrize(
    "budget,platforms",
    [(1_550_000, {"direct"}), (4_840_000, {"direct"}), (4_850_000, {"direct", "vk"})],
)
@pytest.mark.parametrize(
    "day",
    [TODAY, date(2026, 1, 4), date(2026, 1, 31), date(2026, 2, 28)],
    ids=["first-day", "sunday", "last-day-31", "last-day-28"],
)
async def test_plan_and_launch_agree_on_platforms_with_real_minimums(tmp_path: Path, budget, platforms, day) -> None:
    """План решает, хватает ли доли VK на её минимум; запуск сверяет те же лимиты с минимумами площадок и пропускает
    в любой день, в том числе в воскресенье и в последний день месяца."""
    adapters = {platform: Minimums(minimum) for platform, minimum in REAL_MINIMUMS.items()}
    world = make_world(tmp_path, adapters=adapters)
    world.business.month_budget_kop = budget
    await onboard(world)
    now = datetime.combine(day, NOW.timetz())
    plan = await world.plans.get_plan(world.business.id, now, day)
    assert {group["platform"] for group in plan["groups"]} == platforms
    assert (await world.launches.launch(world.business.id, plan["id"], False, now, day))["status"] == "launched"
    assert {item.platform for item in campaigns(world)} == platforms
    assert all(item.daily_limit_kop >= 30_000 for item in campaigns(world))


@pytest.mark.parametrize("day", [date(2026, 4, 5), date(2026, 3, 31), date(2026, 2, 28), date(2026, 4, 6)])
def test_budget_at_direct_minimum_launches_on_sunday_and_last_day_of_month(day) -> None:
    from app.domain.budget import budget_minimum, launch_limits
    from app.services.launch import below_minimum

    minimum = budget_minimum(("direct",))
    assert minimum == 1_550_000
    limits = launch_limits(minimum, ("direct",), day)["direct"]
    assert below_minimum("direct", limits, REAL_MINIMUMS["direct"], day) is None


def test_live_direct_minimum_above_300_on_sunday_is_advised_to_start_on_monday() -> None:
    from app.domain.budget import launch_limits
    from app.services.launch import below_minimum

    sunday = date(2026, 4, 5)
    limits = launch_limits(2_000_000, ("direct",), sunday)["direct"]
    assert limits.daily_limit < 60_000 <= limits.daily_limit * 7
    text = below_minimum("direct", limits, {"weekly_limit_kop": 60_000}, sunday)
    assert text.startswith("До конца недели осталось мало дней") and "Запустите рекламу с понедельника" in text


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
    direct = {"region_ids": [51], "negative_keywords": plan["negative_phrases"]}
    for platform, geo in (("direct", direct), ("vk", {"regions": [123]})):
        groups = [group for spec in adapters[platform].specs for group in spec.groups]
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


async def test_second_launch_while_first_is_running_creates_nothing(tmp_path: Path, monkeypatch) -> None:
    """Идущий запуск держит блокировку: второй запрос её не получает и узнаёт, что запуск уже идёт."""
    adapters = {"direct": SlowActivation(), "vk": FakeAdapter()}
    world, plan = await ready(tmp_path, adapters=adapters)
    exclusive_lock(world, monkeypatch)
    first = asyncio.create_task(launch(world, plan))
    await adapters["direct"].entered.wait()
    with pytest.raises(LaunchBlocked, match="Запуск уже идёт"):
        await launch(world, plan)
    adapters["direct"].release.set()
    assert (await first)["status"] == "launched" and not launch_module.LAUNCHING
    assert len(campaigns(world)) == 2
    launches = [row for row in world.repository.rows(ChangeLog, business_id=world.business.id) if row.kind == "launch"]
    assert sorted(row.platform for row in launches) == ["direct", "vk"]
    assert (await launch(world, plan))["status"] == "launched"


async def test_retry_of_failed_platform_starts_clean(tmp_path: Path, monkeypatch) -> None:
    """Повтор незапустившейся площадки не получает чужую ошибку и причину прошлой попытки: запуск чистый,
    кампании плана — действующие."""
    adapters = {"direct": FakeAdapter(), "vk": FailingActivation()}
    world, plan = await ready(tmp_path, adapters=adapters)
    await launch(world, plan)
    adapters["vk"].fail = False
    activate, seen = world.launches.activate, []

    async def activate_watched(business, stored, *args) -> None:
        seen.append((stored.payload["launch"]["error"], stored.payload["launch"]["reason"]))
        return await activate(business, stored, *args)

    monkeypatch.setattr(world.launches, "activate", activate_watched)
    assert (await launch(world, plan))["status"] == "launched"
    # Повтор начинается без ошибки и причины прошлой попытки: прерванный повтор не получит чужую причину.
    assert seen == [(None, None)]
    worker = recovery_worker(world)
    assert {item.id for item in worker.current_campaigns(world.business)} == {item.id for item in campaigns(world)}


async def test_stopped_failed_launch_lets_plan_be_rebuilt_at_once(tmp_path: Path) -> None:
    """Упавший запуск (все площадки отказали) остановил всё, что создал: план с новыми данными собирается
    сразу, а не после воркера."""
    adapters = {"direct": FailingCreate(), "vk": FailingCreate()}
    world, plan = await ready(tmp_path, adapters=adapters)
    with pytest.raises(LaunchFailed):
        await launch(world, plan)
    assert world.business.status == "plan_ready"
    assert world.repository.latest_plan(world.business.id).status == "failed"
    world.business.month_budget_kop += 100_000
    rebuilt = await world.plans.get_plan(world.business.id, NOW, TODAY)
    assert rebuilt["id"] != plan["id"] and rebuilt["month_budget_kop"] == world.business.month_budget_kop


async def test_launch_abandoned_while_waiting_for_lock_is_continued(tmp_path: Path, monkeypatch) -> None:
    """Пока запрос ждал блокировку, соседний запуск записал started_at и оборвался: под блокировкой его попытка
    брошена, а не «идёт», и запрос продолжает её, а не отказывает и не ждёт воркера."""
    world, plan = await ready(tmp_path)
    stored = world.repository.get_plan(plan["id"], world.business.id)

    def neighbour_started() -> None:
        if not stored.payload.get("launch"):
            stored.payload = stored.payload | {"launch": {"campaigns": {}, "started_at": NOW.isoformat()}}

    exclusive_lock(world, monkeypatch, neighbour_started)
    assert (await launch(world, plan))["status"] == "launched" and world.business.status == "running"
    assert len(campaigns(world)) == 2 and all(len(adapter._trees) == 1 for adapter in world.adapters.values())


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


def exclusive_lock(world: World, monkeypatch, on_enter=lambda: None) -> set[int]:
    """Блокировка как у PostgreSQL: второй раз её не взять, пока первую не отпустили, даже тем же запуском."""
    held = set()

    @contextmanager
    def lock(business_id: int):
        if business_id in held:
            raise ChangeConflict("Изменение этого бизнеса уже выполняется. Попробуйте позже.")
        held.add(business_id)
        try:
            on_enter()
            yield
        finally:
            held.discard(business_id)

    monkeypatch.setattr(world.repository, "business_lock", lock)
    return held


async def test_launch_holds_lock_until_business_is_running(tmp_path: Path, monkeypatch) -> None:
    """Кампании включаются, а бизнес ещё черновик: блокировка держится до конца запуска, и шаги мастера получают 409,
    а не меняют бюджет и Метрику посреди запуска. Включение идёт под той же блокировкой, а не берёт её второй раз."""
    world, plan = await ready(tmp_path)
    held = exclusive_lock(world, monkeypatch)
    # Метрика ждёт блокировку, как описание: запуск держит её дольше ожидания, и запись отвечает 409.
    monkeypatch.setattr("app.services.onboarding.LOCK_WAIT_SECONDS", 0)
    activate, finish = world.launches.activate, world.launches.finish
    seen = []

    async def activate_watched(*args) -> None:
        seen.append(("activate", set(held), world.business.status))
        try:
            world.onboarding.create_business(world.business.user_id, SITE, "Самара", BUDGET_KOP * 2, None)
            seen.append("budget saved")
        except ChangeConflict:
            seen.append("budget 409")
        try:
            await world.onboarding.save_metrica(world.business.id, COUNTER, [1])
            seen.append("metrica saved")
        except ChangeConflict:
            seen.append("metrica 409")
        return await activate(*args)

    def finish_watched(*args) -> None:
        seen.append(("finish", set(held), world.business.status))
        finish(*args)

    monkeypatch.setattr(world.launches, "activate", activate_watched)
    monkeypatch.setattr(world.launches, "finish", finish_watched)
    assert (await launch(world, plan))["status"] == "launched" and not held
    business_id = world.business.id
    assert seen == [
        ("activate", {business_id}, "plan_ready"),
        "budget 409",
        "metrica 409",
        ("finish", {business_id}, "plan_ready"),
    ]
    assert world.business.status == "running" and world.business.month_budget_kop == BUDGET_KOP
    caps = {item.platform: item.period_cap_kop for item in campaigns(world)}
    limits = budget_limits(world.business, ["direct", "vk"], TODAY)
    assert caps == {platform: limit.remaining_cap for platform, limit in limits.items()}


async def test_budget_changed_while_launch_waited_for_lock_is_not_launched(tmp_path: Path, monkeypatch) -> None:
    """Мастер сохранил бюджет, пока запуск ждал блокировку: лимиты по старому бюджету не уходят на площадки."""
    world, plan = await ready(tmp_path)

    def wizard_saved_budget() -> None:
        world.business.month_budget_kop = BUDGET_KOP * 2

    exclusive_lock(world, monkeypatch, wizard_saved_budget)
    with pytest.raises(LaunchBlocked, match="изменились"):
        await launch(world, plan)
    assert not campaigns(world) and all(not adapter._trees for adapter in world.adapters.values())
    assert world.repository.latest_plan(world.business.id).status == "draft"


async def test_business_deleted_while_launch_waited_for_lock_is_not_found(tmp_path: Path, monkeypatch) -> None:
    world, plan = await ready(tmp_path)
    exclusive_lock(world, monkeypatch, lambda: world.repository.tables[Business].clear())
    with pytest.raises(NotFound, match="Бизнес не найден"):
        await launch(world, plan)
    assert not campaigns(world) and all(not adapter._trees for adapter in world.adapters.values())


class Crash(BaseException):
    """Процесс API упал: обработчики ошибок запуска не выполняются, блокировку снимает обрыв соединения."""


class CrashAfterActivation(FakeAdapter):
    async def apply(self, operation) -> None:
        await super().apply(operation)
        if operation.entity_type == "campaign" and operation.after == "active":
            raise Crash


class StuckPause(FakeAdapter):
    stuck = True

    async def apply(self, operation) -> None:
        if self.stuck and operation.entity_type == "campaign" and operation.after == "paused_auto":
            raise PlatformError("Директ не ответил.", status_code=503)
        await super().apply(operation)


class CrashOnCreate(FakeAdapter):
    async def create_campaign_tree(self, plan_part):
        raise Crash


async def crash_before_finish(world: World, plan: dict, monkeypatch, now=NOW) -> None:
    """Кампании созданы и включены, а до finish процесс не дожил: бизнес остался plan_ready."""

    def crash(*args) -> None:
        raise Crash

    monkeypatch.setattr(world.launches, "finish", crash)
    with pytest.raises(Crash):
        await world.launches.launch(world.business.id, plan["id"], False, now, TODAY)
    monkeypatch.undo()


def recovery_worker(world: World, *, after=timedelta(minutes=11), resolve=None, dry_run=True) -> Worker:
    # Пробный режим не мешает: остановка брошенного запуска защитная, включал кампании настоящий запуск.
    repository = MemoryWorkerRepository()
    repository.tables = world.repository.tables
    resolve = resolve or (lambda business_id, platform: world.adapters[platform])
    return Worker(repository, resolve, clock=VirtualClock(NOW + after), dry_run=dry_run)


def interrupted_stops(world: World) -> list[ChangeLog]:
    return [row for row in world.repository.rows(ChangeLog) if row.reason_code == "LAUNCH_INTERRUPTED"]


def notices(world: World) -> list[WorkerRun]:
    return [row for row in world.repository.tables.get(WorkerRun, []) if row.kind == "notification"]


async def platform_statuses(world: World) -> dict[str, str]:
    return {item.platform: (await states(world, item))[item.external_id].status for item in campaigns(world)}


async def test_worker_stops_abandoned_launch_and_launch_can_be_repeated(tmp_path: Path, monkeypatch) -> None:
    world, plan = await ready(tmp_path)
    await crash_before_finish(world, plan, monkeypatch)
    stored = world.repository.latest_plan(world.business.id)
    assert world.business.status == "plan_ready" and stored.status == "draft" and stored.payload["launch"]["started_at"]
    assert await platform_statuses(world) == {"direct": "active", "vk": "active"}

    worker = recovery_worker(world)
    await worker.run_business_due(world.business.id)
    assert await platform_statuses(world) == {"direct": "paused_auto", "vk": "paused_auto"}
    assert all(item.status == "paused_auto" for item in campaigns(world))
    assert stored.status == "failed" and stored.payload["launch"]["started_at"] is None
    assert stored.payload["launch"]["error"] == launch_module.INTERRUPTED
    stops = [row for row in world.repository.rows(ChangeLog) if row.reason_code == "LAUNCH_INTERRUPTED"]
    assert sorted((row.platform, row.actor, row.kind, row.status) for row in stops) == [
        ("direct", "system", "pause", "applied"),
        ("vk", "system", "pause", "applied"),
    ]
    assert all(row.reason_text == launch_module.INTERRUPTED_STOP for row in stops)
    # Вернуть остановку из истории нельзя: бизнес не запущен, и за включённой рекламой никто бы не следил.
    assert not any(can_revert(row, world.business) for row in stops)
    (notice,) = notices(world)
    assert notice.log[0]["reason_code"] == "LAUNCH_INTERRUPTED" and notice.log[0]["text"] == launch_module.INTERRUPTED
    view = await world.plans.get_plan(world.business.id, NOW, TODAY)
    assert view["id"] == plan["id"] and view["launch_error"] == launch_module.INTERRUPTED

    history = len(world.repository.rows(ChangeLog))
    worker.clock.advance(hours=0.5)
    await worker.run_business_due(world.business.id)
    assert len(world.repository.rows(ChangeLog)) == history and len(notices(world)) == 1

    assert (await launch(world, plan))["status"] == "launched" and world.business.status == "running"
    assert len(campaigns(world)) == 2 and all(len(adapter._trees) == 1 for adapter in world.adapters.values())
    assert await platform_statuses(world) == {"direct": "active", "vk": "active"}


async def test_running_launch_holds_lock_and_is_not_touched(tmp_path: Path, monkeypatch, caplog) -> None:
    """Запуск держит блокировку от записи started_at до finish: пока он идёт, воркер её не получает, кампании не
    трогает и тихо ждёт следующего хода."""
    world, plan = await ready(tmp_path)
    exclusive_lock(world, monkeypatch)
    worker = recovery_worker(world, after=timedelta(minutes=1))
    monkeypatch.setattr(worker.repository, "business_lock", world.repository.business_lock)
    activate, seen = world.launches.activate, []

    async def worker_during_activation(*args) -> None:
        seen.append(world.repository.latest_plan(world.business.id).payload["launch"]["started_at"])
        await worker.recover_launch(world.business.id)
        await worker.run_business_due(world.business.id)
        return await activate(*args)

    monkeypatch.setattr(world.launches, "activate", worker_during_activation)
    assert (await launch(world, plan))["status"] == "launched"
    assert seen == [NOW.isoformat()]
    assert await platform_statuses(world) == {"direct": "active", "vk": "active"}
    assert not interrupted_stops(world) and not notices(world)
    assert "Launch recovery failed" not in caplog.text


async def test_abandoned_launch_is_stopped_without_waiting_for_stale(tmp_path: Path, monkeypatch) -> None:
    """Под свободной блокировкой незавершённый запуск брошен, даже если начат минуту назад: каждая минута ожидания —
    реклама без присмотра."""
    world, plan = await ready(tmp_path)
    await crash_before_finish(world, plan, monkeypatch)
    await recovery_worker(world, after=timedelta(minutes=1)).run_business_due(world.business.id)
    assert await platform_statuses(world) == {"direct": "paused_auto", "vk": "paused_auto"}
    assert world.repository.latest_plan(world.business.id).payload["launch"]["started_at"] is None


async def test_repeated_interruption_of_same_plan_is_stopped_again(tmp_path: Path, monkeypatch) -> None:
    """Повтор запуска того же плана снова оборвался: ход восстановления новой попытки не находит остановку прошлой
    под тем же ключом, а останавливает включённое повтором и присылает новое уведомление."""
    world, plan = await ready(tmp_path)
    await crash_before_finish(world, plan, monkeypatch)
    await recovery_worker(world).run_business_due(world.business.id)
    assert await platform_statuses(world) == {"direct": "paused_auto", "vk": "paused_auto"}

    await crash_before_finish(world, plan, monkeypatch, now=NOW + timedelta(hours=1))
    assert await platform_statuses(world) == {"direct": "active", "vk": "active"}
    worker = recovery_worker(world, after=timedelta(hours=2))
    await worker.run_business_due(world.business.id)
    assert await platform_statuses(world) == {"direct": "paused_auto", "vk": "paused_auto"}
    stored = world.repository.latest_plan(world.business.id)
    assert stored.status == "failed" and stored.payload["launch"]["started_at"] is None
    assert sorted((row.platform, row.status) for row in interrupted_stops(world)) == [
        ("direct", "applied"),
        ("direct", "applied"),
        ("vk", "applied"),
        ("vk", "applied"),
    ]
    assert len(notices(world)) == 2
    (latest,) = worker.repository.unread_notifications(world.business.id, {"LAUNCH_INTERRUPTED"}, 5, worker.clock.now)
    assert latest.started_at == worker.clock.now and latest.log[0]["text"] == launch_module.INTERRUPTED


async def test_activation_cut_midway_is_reconciled_and_stopped(tmp_path: Path) -> None:
    adapters = {"direct": FakeAdapter(), "vk": CrashAfterActivation()}
    world, plan = await ready(tmp_path, adapters=adapters)
    with pytest.raises(Crash):
        await launch(world, plan)
    (pending,) = world.repository.pending(world.business.id)
    assert pending.platform == "vk" and pending.kind == "launch"
    await recovery_worker(world).run_business_due(world.business.id)
    assert pending.status == "applied" and not world.repository.pending(world.business.id)
    assert await platform_statuses(world) == {"direct": "paused_auto", "vk": "paused_auto"}
    assert world.repository.latest_plan(world.business.id).payload["launch"]["started_at"] is None


async def test_closed_account_keeps_plan_unsettled_until_campaign_is_stopped(tmp_path: Path, monkeypatch) -> None:
    world, plan = await ready(tmp_path)
    await crash_before_finish(world, plan, monkeypatch)
    closed = {"vk"}

    def resolve(business_id: int, platform: str):
        if platform in closed:
            raise LookupError("Кабинет требует входа заново.")
        return world.adapters[platform]

    worker = recovery_worker(world, resolve=resolve)
    await worker.run_business_due(world.business.id)
    stored = world.repository.latest_plan(world.business.id)
    assert await platform_statuses(world) == {"direct": "paused_auto", "vk": "active"}
    assert stored.status == "failed" and stored.payload["launch"]["started_at"]
    error = stored.payload["launch"]["error"]
    assert "«ВКонтакте»" in error and "войти в кабинет заново" in error
    # Повторный ход, пока кабинет закрыт: попытка повторяется, но ни истории, ни уведомлений не прибавляется.
    history = len(world.repository.rows(ChangeLog))
    worker.clock.advance(hours=0.5)
    await worker.run_business_due(world.business.id)
    assert len(world.repository.rows(ChangeLog)) == history and len(notices(world)) == 1
    # Новый план — новые кампании: пока старая работает, план не пересобирается, иначе она осталась бы сиротой.
    world.business.month_budget_kop += 100_000
    with pytest.raises(Blocked, match="Прошлый запуск рекламы не завершился"):
        await world.plans.get_plan(world.business.id, NOW, TODAY)
    assert len(world.repository.rows(Plan)) == 1

    closed.clear()
    worker.clock.advance(hours=0.5)
    await worker.run_business_due(world.business.id)
    assert await platform_statuses(world) == {"direct": "paused_auto", "vk": "paused_auto"}
    assert stored.payload["launch"]["started_at"] is None
    assert stored.payload["launch"]["error"] == launch_module.INTERRUPTED
    latest = worker.repository.unread_notifications(world.business.id, {"LAUNCH_INTERRUPTED"}, 5, NOW)
    assert [row.log[0]["text"] for row in latest] == [launch_module.INTERRUPTED]
    rebuilt = await world.plans.get_plan(world.business.id, NOW, TODAY)
    assert rebuilt["id"] != plan["id"] and rebuilt["month_budget_kop"] == world.business.month_budget_kop


async def test_partial_launch_reads_notices_that_are_no_longer_true(tmp_path: Path) -> None:
    """Запуск, завершившийся частично (площадка не создалась), — тоже запуск: уведомления прошлых прерванных
    попыток отмечает прочитанными, как успешный, — их тексты больше неправда."""
    adapters = {"direct": FakeAdapter(), "vk": FailingCreate()}
    world, plan = await ready(tmp_path, adapters=adapters)
    worker = recovery_worker(world)
    for target in (plan["id"], plan["id"] + 100):
        worker.notice(
            world.business, "LAUNCH_INTERRUPTED", "Не удалось остановить.", key=f"k{target}", target_id=target
        )
    result = await launch(world, plan)
    assert result["platform_errors"] and world.business.status == "running"
    unread = worker.repository.unread_notifications(world.business.id, {"LAUNCH_INTERRUPTED"}, 5, NOW)
    assert not unread and len(notices(world)) == 2


async def test_campaign_missing_on_platform_does_not_hold_plan(tmp_path: Path, monkeypatch) -> None:
    """Владелец удалил кампанию в кабинете: её нет в ответе площадки, тратить ей нечего, и план не ждёт её остановки."""
    world, plan = await ready(tmp_path)
    await crash_before_finish(world, plan, monkeypatch)
    vk = next(item for item in campaigns(world) if item.platform == "vk")
    get_state = world.adapters["vk"].get_state

    async def deleted_in_cabinet(refs: ExternalRefs) -> dict:
        return {key: state for key, state in (await get_state(refs)).items() if key != vk.external_id}

    monkeypatch.setattr(world.adapters["vk"], "get_state", deleted_in_cabinet)
    await recovery_worker(world).run_business_due(world.business.id)
    stored = world.repository.latest_plan(world.business.id)
    assert stored.payload["launch"]["started_at"] is None
    assert stored.payload["launch"]["error"] == launch_module.INTERRUPTED
    assert [(row.platform, row.status) for row in interrupted_stops(world)] == [("direct", "applied")]


async def test_disconnected_account_does_not_hold_plan_forever(tmp_path: Path, monkeypatch) -> None:
    """Кабинет требует входа заново, и владелец отключил его: план не ждёт кабинета, которого уже нет, и собирается
    заново без этой площадки."""
    world, plan = await ready(tmp_path)
    await crash_before_finish(world, plan, monkeypatch)

    def resolve(business_id: int, platform: str):
        if platform == "vk":
            raise PlatformError("Кабинет площадки не подключён или требует входа заново.")
        return world.adapters[platform]

    worker = recovery_worker(world, resolve=resolve)
    await worker.run_business_due(world.business.id)
    stored = world.repository.latest_plan(world.business.id)
    assert stored.payload["launch"]["started_at"] and "отключите кабинет" in stored.payload["launch"]["error"]
    account = next(item for item in world.repository.accounts(world.business.id) if item.platform == "vk")
    account.status = "revoked"
    worker.clock.advance(hours=0.5)
    await worker.run_business_due(world.business.id)
    assert stored.payload["launch"]["started_at"] is None
    # Управлять кампанией отключённого кабинета нечем: «деньги не тратятся» было бы неправдой, её проверяет владелец.
    error = stored.payload["launch"]["error"]
    assert error == launch_module.interrupted_revoked(["vk"]) and "не тратятся" not in error
    assert error.startswith("Запуск рекламы не завершился. Кабинет") and "None" not in error
    assert "Кабинет «ВКонтакте» отключён" in error and "проверьте в самом кабинете" in error
    assert notices(world)[-1].log[0]["text"] == error
    rebuilt = await world.plans.get_plan(world.business.id, NOW, TODAY)
    assert rebuilt["id"] != plan["id"] and world.repository.latest_plan(world.business.id).payload["platforms"] == [
        "direct"
    ]


@pytest.mark.parametrize("row_deleted", [False, True])
async def test_campaign_not_created_on_platform_does_not_hold_plan(tmp_path: Path, row_deleted: bool) -> None:
    """Процесс упал, пока создавалась кампания ВКонтакте: на площадке её нет (external_id не записан), а строки может
    не быть вовсе. Остановить нечего — план не ждёт."""
    adapters = {"direct": FakeAdapter(), "vk": CrashOnCreate()}
    world, plan = await ready(tmp_path, adapters=adapters)
    with pytest.raises(Crash):
        await launch(world, plan)
    vk = next(item for item in campaigns(world) if item.platform == "vk")
    assert vk.external_id is None
    if row_deleted:
        world.repository.tables[Campaign].remove(vk)
    await recovery_worker(world).run_business_due(world.business.id)
    stored = world.repository.latest_plan(world.business.id)
    assert stored.status == "failed" and stored.payload["launch"]["started_at"] is None
    direct = next(item for item in campaigns(world) if item.platform == "direct")
    assert (await states(world, direct))[direct.external_id].status == "paused_auto" and not interrupted_stops(world)


async def test_running_business_is_not_locked_for_recovery(tmp_path: Path, monkeypatch) -> None:
    """У запущенного бизнеса брошенного запуска нет: ход воркера не берёт ради него блокировку, которую ждали бы
    настройки и запросы владельца."""
    world, plan = await ready(tmp_path)
    await launch(world, plan)
    worker = recovery_worker(world)
    taken = []

    @contextmanager
    def lock(business_id: int):
        taken.append(business_id)
        yield

    monkeypatch.setattr(worker.repository, "business_lock", lock)
    await worker.recover_launch(world.business.id)
    assert taken == []


async def test_launch_finished_while_worker_waited_for_lock_is_left_alone(tmp_path: Path, monkeypatch) -> None:
    """Воркер прочитал черновик, а запуск, который держал блокировку, тем временем завершился: под блокировкой бизнес
    перечитывается, и кампании только что запущенного бизнеса не останавливаются."""
    from copy import copy

    world, plan = await ready(tmp_path)
    await crash_before_finish(world, plan, monkeypatch)
    worker = recovery_worker(world)
    stale, recovered = copy(world.business), []

    @contextmanager
    def launch_finished_meanwhile(business_id: int):
        world.business.status = "running"
        yield

    async def recover(*args) -> None:
        recovered.append(args)

    monkeypatch.setattr(worker.repository, "business", lambda business_id: stale)
    monkeypatch.setattr(
        worker.repository,
        "refresh",
        lambda item: item.__dict__.update(
            {key: value for key, value in world.business.__dict__.items() if not key.startswith("_")}
        ),
    )
    monkeypatch.setattr(worker.repository, "business_lock", launch_finished_meanwhile)
    monkeypatch.setattr(launch_module, "recover_launch", recover)
    await worker.recover_launch(world.business.id)
    assert stale.status == "running" and recovered == []


async def test_lost_demo_state_does_not_settle_abandoned_launch(tmp_path: Path, monkeypatch) -> None:
    """Демо-данные пропали после перезапуска сервера: состояния кампаний нет, но это не остановка, и план не
    объявляется остановленным."""
    world, plan = await ready(tmp_path)
    await crash_before_finish(world, plan, monkeypatch)
    lost = {platform: FakeAdapter() for platform in world.adapters}
    worker = recovery_worker(world, resolve=lambda business_id, platform: lost[platform])
    await worker.run_business_due(world.business.id)
    stored = world.repository.latest_plan(world.business.id)
    assert stored.status == "draft" and stored.payload["launch"]["started_at"]
    assert [row.log[0]["reason_code"] for row in notices(world)] == ["DEMO_STATE_LOST"]


async def test_recovery_failure_does_not_stop_business_steps(tmp_path: Path, monkeypatch, caplog) -> None:
    world, plan = await ready(tmp_path)
    await crash_before_finish(world, plan, monkeypatch)
    worker = recovery_worker(world)
    ran = []

    async def broken(*args) -> None:
        raise RuntimeError

    async def run(kind: str, business_id: int | None = None, **options) -> None:
        ran.append(kind)

    monkeypatch.setattr(launch_module, "recover_launch", broken)
    monkeypatch.setattr(worker, "run", run)
    await worker.run_business_due(world.business.id)
    assert "budget_guard" in ran and f"Launch recovery failed for business {world.business.id}" in caplog.text


async def test_launched_plan_with_leftover_launch_marks_is_settled(tmp_path: Path) -> None:
    """Запущенный план не считается незавершённым и не показывает ошибку, даже если в нём остались отметки попытки."""
    world, plan = await ready(tmp_path)
    await launch(world, plan)
    stored = world.repository.latest_plan(world.business.id)
    stored.payload = stored.payload | {
        "launch": stored.payload["launch"] | {"started_at": NOW.isoformat(), "error": "Старая ошибка."}
    }
    assert not launch_unsettled(stored)
    view = await world.plans.get_plan(world.business.id, NOW, TODAY)
    assert view["status"] == "launched" and view["launch_error"] is None


async def replaced_plan(tmp_path: Path) -> tuple[World, Campaign, set[int], Worker]:
    """Запуск первого плана упал целиком (Директ не включил рекламу, ВКонтакте не создалась) и остановил
    созданное, а бизнес запущен по новому плану: кампания Директа первого плана остановлена и показывала бы
    устаревшие объявления."""
    adapters = {"direct": FailingActivation(), "vk": FailingCreate()}
    world, plan = await ready(tmp_path, adapters=adapters)
    with pytest.raises(LaunchFailed):
        await launch(world, plan)
    (old,) = [item for item in campaigns(world) if item.platform == "direct" and item.external_id]
    world.business.month_budget_kop += 100_000
    replacement = await world.plans.get_plan(world.business.id, NOW, TODAY)
    adapters["direct"].fail = False
    adapters["vk"].fail = False
    await world.launches.launch(world.business.id, replacement["id"], False, NOW, TODAY)
    current = set(world.repository.latest_plan(world.business.id).payload["launch"]["campaigns"].values())
    return world, old, current, recovery_worker(world, after=timedelta(days=1), dry_run=False)


async def statuses_by_id(world: World) -> dict[int, str]:
    return {
        item.id: (await states(world, item))[item.external_id].status for item in campaigns(world) if item.external_id
    }


async def turn_on_in_cabinet(world: World, campaign: Campaign) -> None:
    turn_on = Operation("set_status", "campaign", campaign.external_id, "status", "paused_auto", "active")
    await world.adapters[campaign.platform].apply(turn_on)


async def test_campaigns_of_replaced_plan_are_neither_resumed_nor_rebalanced(tmp_path: Path) -> None:
    """Шаги воркера не дают кампаниям прежнего плана долю бюджета и не включают их, а расход и остановка по-прежнему
    их видят."""
    from app.services.budget_guard import read_spend, stop_all
    from app.services.month import month_rollover
    from app.services.optimize import rebalance_budget, snapshot

    world, old, current, worker = await replaced_plan(tmp_path)
    business, adapters = world.business, world.adapters
    old_limits = (old.daily_limit_kop, old.period_cap_kop)
    assert {item.id for item in worker.current_campaigns(business)} == current and old.id not in current
    assert old in worker.campaigns(business)
    groups, ads, _ = world.repository.tree_rows(old.id)
    seen_ads, seen_groups = await snapshot(worker, business, include_paused_campaigns=True)
    assert seen_groups and not {group.external_id for group in groups} & {item.id for item in seen_groups}
    assert seen_ads and not {ad.external_id for ad in ads} & {item.id for item in seen_ads}

    def touched(reason_code: str) -> set[str]:
        return {
            op["entity_id"]
            for row in world.repository.rows(ChangeLog)
            if row.reason_code == reason_code
            for op in row.operations
        }

    await rebalance_budget(worker, business)
    rebalanced = touched("BUDGET_REBALANCE")
    assert rebalanced and old.external_id not in rebalanced
    assert (old.daily_limit_kop, old.period_cap_kop) == old_limits

    worker.step_key = "budget"
    stop = {"spent": 0, "budget": business.month_budget_kop, "margin": 0}
    await stop_all(business, worker=worker, reason_code="BUDGET_STOP", reason_params=stop, budget_stop=True)
    worker.remember(business, month="2025-12-01")
    await month_rollover(worker, business)
    assert business.status == "running" and old.external_id not in touched("BUDGET_RESUME")
    assert await statuses_by_id(world) == {campaign_id: "active" for campaign_id in current} | {old.id: "paused_auto"}

    # Владелец сам включил старую кампанию в кабинете: её расход учитывается, и «Остановить всё» её останавливает.
    await turn_on_in_cabinet(world, old)
    adapters["direct"].clock.advance(hours=12)
    _, lifetime = await read_spend(worker, business)
    assert lifetime.get(old.external_id, 0) > 0
    worker.step_key = "owner"
    await stop_all(business, worker=worker, actor="owner")
    assert (await states(world, old))[old.external_id].status == "paused_owner"


async def test_demo_rewind_does_not_look_for_abandoned_launches(tmp_path: Path, monkeypatch) -> None:
    world, plan = await ready(tmp_path)
    await crash_before_finish(world, plan, monkeypatch)
    worker = recovery_worker(world)
    with worker.rewinding():
        await worker.run_business_due(world.business.id)
    assert await platform_statuses(world) == {"direct": "active", "vk": "active"}


async def test_worker_startup_stops_abandoned_launch(tmp_path: Path, monkeypatch) -> None:
    """Выкатка перезапускает API и воркер вместе: брошенный запуск останавливается сразу, а не через полчаса."""
    world, plan = await ready(tmp_path)
    await crash_before_finish(world, plan, monkeypatch)
    await recovery_worker(world).startup()
    assert await platform_statuses(world) == {"direct": "paused_auto", "vk": "paused_auto"}
    assert world.repository.latest_plan(world.business.id).payload["launch"]["started_at"] is None


async def test_owner_resume_does_not_turn_on_campaign_of_replaced_plan(tmp_path: Path) -> None:
    """Владелец включил старую кампанию в кабинете, страж бюджета остановил всё, и владелец возобновил рекламу:
    возобновляются только кампании действующего плана."""
    from app.services.budget_guard import stop_all
    from app.services.screens import ScreensService

    world, old, current, worker = await replaced_plan(tmp_path)
    await turn_on_in_cabinet(world, old)
    worker.step_key = "budget"
    stop = {"spent": 0, "budget": world.business.month_budget_kop, "margin": 0}
    await stop_all(world.business, worker=worker, reason_code="BUDGET_STOP", reason_params=stop, budget_stop=True)
    assert world.business.status == "budget_stop" and old.status == "paused_auto"
    await ScreensService(worker).resume(world.business)
    assert world.business.status == "running"
    assert await statuses_by_id(world) == {campaign_id: "active" for campaign_id in current} | {old.id: "paused_auto"}


async def test_limits_of_replaced_plan_do_not_count_in_budget_margin(tmp_path: Path) -> None:
    """Запас стража бюджета и проверки остатка считается по лимитам действующего плана: устаревший большой лимит
    остановленной кампании не останавливает рекламу раньше времени и не мешает её возобновить."""
    from app.services.budget_guard import budget_guard
    from app.services.screens import ScreensService

    world, old, _, worker = await replaced_plan(tmp_path)
    # Запас — доля дневного лимита за пропущенные проверки: такой лимит один съел бы весь бюджет месяца.
    old.daily_limit_kop = 100 * world.business.month_budget_kop
    await ScreensService(worker).check_remaining(world.business)
    await budget_guard(worker, world.business)
    assert world.business.status == "running"
    assert not [row for row in world.repository.rows(ChangeLog) if row.reason_code == "BUDGET_STOP"]


async def test_moderation_of_replaced_plan_is_not_polled(tmp_path: Path) -> None:
    """Объявления прежнего плана не показываются: их отказы не исправляются и не приходят владельцу уведомлениями."""
    from app.platforms.base import ModerationInfo
    from app.services.sync import sync_moderation

    world, old, _, worker = await replaced_plan(tmp_path)
    _, ads, _ = world.repository.tree_rows(old.id)
    reviews = world.adapters["direct"]._reviews
    for ad in ads:
        reviews[ad.external_id].info = ModerationInfo(ad.external_id, "rejected", "Ошибка в тексте.", NOW)
    await sync_moderation(worker, world.business)
    assert ads and all(ad.status == "draft" and ad.moderation_status is None for ad in ads)
    rejected = {row.log[0]["target_id"] for row in notices(world) if row.log[0]["reason_code"] == "MODERATION_REJECTED"}
    assert not rejected & {f"direct:{ad.external_id}" for ad in ads}


async def test_only_latest_plan_of_draft_is_launched(tmp_path: Path) -> None:
    """Запуск v1 упал и остановил созданное; бюджет меняли туда и обратно — v2, затем v3 с тем же хэшем, что у v1.
    Запуск v1 или v2 из старой вкладки не создаёт вторые кампании: действующий план у бизнеса один, а кэпы не
    удваиваются."""
    adapters = {"direct": FailingActivation(), "vk": FailingActivation()}
    world, v1 = await ready(tmp_path, adapters=adapters)
    with pytest.raises(LaunchFailed):
        await launch(world, v1)
    base = world.business.month_budget_kop
    world.business.month_budget_kop = base + 100_000
    v2 = await world.plans.get_plan(world.business.id, NOW, TODAY)
    world.business.month_budget_kop = base
    v3 = await world.plans.get_plan(world.business.id, NOW, TODAY)
    assert len({v1["id"], v2["id"], v3["id"]}) == 3
    adapters["direct"].fail = False
    adapters["vk"].fail = False
    created = len(campaigns(world))
    for outdated in (v1, v2):
        with pytest.raises(LaunchBlocked) as blocked:
            await launch(world, outdated)
        assert blocked.value.reasons == [launch_module.OUTDATED]
    assert len(campaigns(world)) == created and world.business.status == "plan_ready"
    assert (await launch(world, v3))["status"] == "launched"
    with pytest.raises(LaunchBlocked) as blocked:
        await launch(world, v1)
    assert blocked.value.reasons == [launch_module.LAUNCHED_OTHER]
    launched = [
        plan for plan in world.repository.rows(Plan, business_id=world.business.id) if plan.status == "launched"
    ]
    assert [plan.id for plan in launched] == [v3["id"]]
    current = set(launched[0].payload["launch"]["campaigns"].values())
    worker = recovery_worker(world, after=timedelta(days=1), dry_run=False)
    assert {item.id for item in worker.current_campaigns(world.business)} == current and len(current) == 2
    # Даже если данные прошлых версий оставили два запущенных плана, действующий — последний из них.
    stale = world.repository.get_plan(v1["id"], world.business.id)
    stale.status = "launched"
    assert {item.id for item in worker.current_campaigns(world.business)} == current


async def test_launch_of_new_plan_reads_notices_of_old_plans(tmp_path: Path, monkeypatch) -> None:
    """Прерванный запуск v1 остановлен воркером, и реклама запущена по v2: уведомление о v1 уже неправда."""
    world, plan = await ready(tmp_path)
    await crash_before_finish(world, plan, monkeypatch)
    worker = recovery_worker(world)
    await worker.run_business_due(world.business.id)
    unread = worker.repository.unread_notifications(world.business.id, {"LAUNCH_INTERRUPTED"}, 5, worker.clock.now)
    assert [row.log[0]["target_id"] for row in unread] == [plan["id"]]
    world.business.month_budget_kop += 100_000
    replacement = await world.plans.get_plan(world.business.id, NOW, TODAY)
    assert replacement["id"] != plan["id"]
    assert (await launch(world, replacement))["status"] == "launched"
    assert not worker.repository.unread_notifications(world.business.id, {"LAUNCH_INTERRUPTED"}, 5, worker.clock.now)


async def test_ads_screen_shows_only_current_plan_and_counts_old_spend_in_totals(tmp_path: Path) -> None:
    """Объявления прежнего плана навсегда остались со статусами упавшего запуска: экран «Реклама» их не показывает и
    не включает, а их расход входит в итоги бюджета."""
    from app.db.models import StatsDaily
    from app.services.screens import ScreensService

    world, old, current, worker = await replaced_plan(tmp_path)
    screens = ScreensService(worker)
    items = screens.ads(world.business, TODAY, TODAY + timedelta(days=1))["items"]
    _, old_ads, _ = world.repository.tree_rows(old.id)
    assert items and {item["campaign_id"] for item in items} == current
    assert not {item["id"] for item in items} & {ad.id for ad in old_ads}
    with pytest.raises(NotFound, match="Объявление не найдено"):
        await screens.ad_status(world.business, old_ads[0].id, True)
    assert not [row for row in world.repository.rows(ChangeLog) if row.reason_code == "OWNER_AD_STATUS"]
    worker.repository.save(
        StatsDaily(
            business_id=world.business.id,
            platform="direct",
            campaign_id=old.id,
            ad_id=old_ads[0].id,
            date=TODAY,
            impressions=100,
            clicks=3,
            cost_kop=12_300,
            conversions={},
        )
    )
    assert screens.budget(world.business)["spent_kop"] == 12_300


async def test_campaign_turned_on_between_recovery_passes_is_stopped_again(tmp_path: Path, monkeypatch) -> None:
    """Ход восстановления остановил ВКонтакте, а Директ не ответил; до следующего хода той же попытки владелец включил
    кампанию ВКонтакте в кабинете. Следующий ход останавливает её снова, а не находит прошлую остановку под ключом."""
    adapters = {"direct": StuckPause(), "vk": FakeAdapter()}
    world, plan = await ready(tmp_path, adapters=adapters)
    await crash_before_finish(world, plan, monkeypatch)
    worker = recovery_worker(world)
    await worker.run_business_due(world.business.id)
    assert await platform_statuses(world) == {"direct": "active", "vk": "paused_auto"}
    vk = next(item for item in campaigns(world) if item.platform == "vk")
    await turn_on_in_cabinet(world, vk)
    adapters["direct"].stuck = False
    worker.clock.advance(hours=0.5)
    await worker.run_business_due(world.business.id)
    assert await platform_statuses(world) == {"direct": "paused_auto", "vk": "paused_auto"}
    stored = world.repository.latest_plan(world.business.id)
    assert (
        stored.payload["launch"]["started_at"] is None
        and stored.payload["launch"]["error"] == launch_module.INTERRUPTED
    )
    assert sorted((row.platform, row.status) for row in interrupted_stops(world)) == [
        ("direct", "applied"),
        ("direct", "failed"),
        ("vk", "applied"),
        ("vk", "applied"),
    ]


async def test_draft_without_abandoned_launch_is_not_locked_for_recovery(tmp_path: Path, monkeypatch) -> None:
    """Блокировку черновика ждут шаги мастера: ход воркера без брошенного запуска её не берёт."""
    world, plan = await ready(tmp_path)
    worker = recovery_worker(world)
    taken = []

    @contextmanager
    def lock(business_id: int):
        taken.append(business_id)
        yield

    monkeypatch.setattr(worker.repository, "business_lock", lock)
    await worker.recover_launch(world.business.id)
    assert world.business.status == "plan_ready" and taken == []
    await crash_before_finish(world, plan, monkeypatch)
    monkeypatch.setattr(worker.repository, "business_lock", lock)
    await worker.recover_launch(world.business.id)
    assert taken and set(taken) == {world.business.id}
    assert world.repository.latest_plan(world.business.id).payload["launch"]["started_at"] is None


async def test_unexpected_error_text_is_not_shown_but_platform_conflict_is(tmp_path: Path, monkeypatch) -> None:
    """Текст чужого исключения может содержать закрытые данные: владелец видит «Непредвиденная ошибка». Отказ площадки
    объясняют причиной своими словами, а конфликт очереди платформы доходит до владельца своим текстом: записка
    проваленной попытки пишется, как при любом другом сбое, но ответ — 409, а не 502."""

    class Leaking(FakeAdapter):
        async def create_campaign_tree(self, plan_part):
            raise RuntimeError("секрет: token=abc")

    world, plan = await ready(tmp_path, adapters={"direct": FakeAdapter(), "vk": Leaking()})
    with pytest.raises(LaunchFailed) as failed:
        await launch(world, plan)
    stored = world.repository.latest_plan(world.business.id)
    assert "секрет" not in str(failed.value) and "секрет" not in stored.payload["launch"]["error"]
    assert stored.payload["launch"]["reason"] == "Непредвиденная ошибка."
    assert str(failed.value) == launch_module.failed_launch("Непредвиденная ошибка.")

    async def conflict(*args) -> None:
        raise ChangeConflict("Площадка ещё не подтвердила предыдущее изменение.")

    world.adapters["vk"] = FakeAdapter()
    monkeypatch.setattr(world.launches, "activate", conflict)
    with pytest.raises(ChangeConflict, match="Площадка ещё не подтвердила предыдущее изменение"):
        await launch(world, plan)
    assert stored.status == "failed" and stored.payload["launch"]["started_at"] is None
    assert all(campaign.status == "paused_auto" for campaign in campaigns(world))
    assert stored.payload["launch"]["reason"] == "Площадка ещё не подтвердила предыдущее изменение."


async def test_interrupted_launch_without_reason_names_no_reason(tmp_path: Path, monkeypatch) -> None:
    """Упал процесс, а площадка не отвечает на остановку: в объяснении нет причины неудачи, и «None» не появляется."""
    world, plan = await ready(tmp_path, adapters={"direct": StuckPause(), "vk": FakeAdapter()})
    await crash_before_finish(world, plan, monkeypatch)
    await recovery_worker(world).run_business_due(world.business.id)
    error = world.repository.latest_plan(world.business.id).payload["launch"]["error"]
    assert error == launch_module.interrupted_unstopped(["direct"])
    assert error.startswith("Запуск рекламы не завершился, а остановить") and "None" not in error


async def test_launch_payload_without_reason_is_recovered(tmp_path: Path, monkeypatch) -> None:
    """План, начатый до обновления сервера, не хранит launch.reason: брошенный запуск всё равно останавливается."""
    world, plan = await ready(tmp_path)
    await crash_before_finish(world, plan, monkeypatch)
    stored = world.repository.latest_plan(world.business.id)
    stored.payload = stored.payload | {
        "launch": {key: value for key, value in stored.payload["launch"].items() if key != "reason"}
    }
    await recovery_worker(world).run_business_due(world.business.id)
    assert await platform_statuses(world) == {"direct": "paused_auto", "vk": "paused_auto"}
    assert stored.payload["launch"]["error"] == launch_module.INTERRUPTED


async def test_foreign_campaign_in_launch_payload_is_not_stopped(tmp_path: Path, monkeypatch) -> None:
    """Кампания ищется только среди кампаний бизнеса: чужой id в плане не останавливает чужую рекламу."""
    world, plan = await ready(tmp_path)
    await crash_before_finish(world, plan, monkeypatch)
    vk = next(item for item in campaigns(world) if item.platform == "vk")
    vk.business_id = world.business.id + 1
    await recovery_worker(world).run_business_due(world.business.id)
    assert (await states(world, vk))[vk.external_id].status == "active"
    assert [row.platform for row in interrupted_stops(world)] == ["direct"]


async def test_account_that_needs_login_does_not_count_as_stopped(tmp_path: Path, monkeypatch) -> None:
    """Отключён только кабинет со статусом revoked: кабинет, который ждёт входа, — наш, и его кампанию нужно
    остановить, а не объявить остановленной."""
    world, plan = await ready(tmp_path)
    await crash_before_finish(world, plan, monkeypatch)
    account = next(item for item in world.repository.accounts(world.business.id) if item.platform == "vk")
    account.status = "needs_reauth"

    def resolve(business_id: int, platform: str):
        if platform == "vk":
            raise LookupError("Кабинет требует входа заново.")
        return world.adapters[platform]

    await recovery_worker(world, resolve=resolve).run_business_due(world.business.id)
    stored = world.repository.latest_plan(world.business.id)
    assert await platform_statuses(world) == {"direct": "paused_auto", "vk": "active"}
    assert stored.payload["launch"]["started_at"]
    assert stored.payload["launch"]["error"] == launch_module.interrupted_unstopped(["vk"])


async def test_repeated_unstopped_interruption_of_same_plan_notifies_again(tmp_path: Path, monkeypatch) -> None:
    """Попытка прервалась, остановить не удалось; повтор того же плана прервался снова: уведомление новой попытки
    приходит, а не теряется под ключом прошлой."""
    adapters = {"direct": StuckPause(), "vk": FakeAdapter()}
    world, plan = await ready(tmp_path, adapters=adapters)
    await crash_before_finish(world, plan, monkeypatch)
    await recovery_worker(world).run_business_due(world.business.id)
    stored = world.repository.latest_plan(world.business.id)
    assert stored.status == "failed" and stored.payload["launch"]["started_at"]
    await crash_before_finish(world, plan, monkeypatch, now=NOW + timedelta(hours=1))
    worker = recovery_worker(world, after=timedelta(hours=2))
    await worker.run_business_due(world.business.id)
    assert len(notices(world)) == 2
    (latest,) = worker.repository.unread_notifications(world.business.id, {"LAUNCH_INTERRUPTED"}, 5, worker.clock.now)
    assert latest.started_at == worker.clock.now
    assert latest.log[0]["text"] == launch_module.interrupted_unstopped(["direct"])


async def test_abandoned_launch_campaigns_are_not_current(tmp_path: Path, monkeypatch) -> None:
    """Пока брошенный запуск не остановлен, его план ещё черновик: его кампании не действующие, и шаги воркера их не
    включают."""
    world, plan = await ready(tmp_path)
    await crash_before_finish(world, plan, monkeypatch)
    worker = recovery_worker(world)
    assert world.repository.latest_plan(world.business.id).status == "draft"
    assert len(worker.campaigns(world.business)) == 2 and worker.current_campaigns(world.business) == []


async def test_retry_does_not_depend_on_current_campaigns(tmp_path: Path, monkeypatch) -> None:
    """Повтор незапустившейся площадки идёт по плану: запуск берёт кампании из плана, а не спрашивает
    действующие."""
    adapters = {"direct": FakeAdapter(), "vk": FailingActivation()}
    world, plan = await ready(tmp_path, adapters=adapters)
    await launch(world, plan)
    adapters["vk"].fail = False

    def forbidden(self, business) -> None:
        raise AssertionError("Запуск не спрашивает действующие кампании.")

    monkeypatch.setattr(Worker, "current_campaigns", forbidden)
    assert (await launch(world, plan))["status"] == "launched"
    monkeypatch.undo()
    worker = recovery_worker(world)
    assert {item.id for item in worker.current_campaigns(world.business)} == {item.id for item in campaigns(world)}


async def test_budget_guard_counts_spend_of_old_plan_campaign_turned_on_in_cabinet(tmp_path: Path) -> None:
    """Запас стража считается по лимитам действующего плана, а расход — по всем кампаниям: кампания прежнего плана,
    которую владелец включил в кабинете, тратит бюджет месяца."""
    from app.db.models import BudgetLedger
    from app.services.budget_guard import budget_guard, read_spend

    world, old, current, worker = await replaced_plan(tmp_path)
    await turn_on_in_cabinet(world, old)
    # Расход и вчера: сегодняшний излишек быстрого отчёта площадки и так относится к бизнесу целиком.
    world.adapters["direct"].clock.advance(hours=30)
    worker.clock.advance(hours=12)
    _, lifetime = await read_spend(worker, world.business)
    assert lifetime[old.external_id] > 0
    await budget_guard(worker, world.business)
    (ledger,) = [row for row in world.repository.rows(BudgetLedger) if row.platform == "direct"]
    direct = [item for item in campaigns(world) if item.platform == "direct" and item.external_id]
    assert ledger.spent_kop == sum(lifetime.get(item.external_id, 0) for item in direct)
    assert ledger.spent_kop > sum(lifetime.get(item.external_id, 0) for item in direct if item.id in current)


async def test_disconnecting_account_stops_old_plan_campaign_turned_on_in_cabinet(tmp_path: Path) -> None:
    """Перед отзывом доступа останавливаются все кампании кабинета: без токена включённую владельцем кампанию прежнего
    плана уже никто не остановит."""
    from time import monotonic

    from app.services.budget_guard import stop_platform

    world, old, _, worker = await replaced_plan(tmp_path)
    await turn_on_in_cabinet(world, old)
    stopped = await stop_platform(
        world.business, worker=worker, platform="direct", reason_text="Отключили кабинет.", deadline=monotonic() + 60
    )
    assert stopped is True
    statuses = await statuses_by_id(world)
    assert statuses[old.id] == "paused_owner"
    assert all(statuses[item.id] == "paused_owner" for item in campaigns(world) if item.platform == "direct")


async def replaced_vk_plan(tmp_path: Path) -> tuple[World, Campaign, Worker]:
    """Первый план упал целиком (Директ не создался, ВКонтакте не включила рекламу) и остановил созданное; новый
    план собрали, пока кабинет ВКонтакте ждал входа, — только для Директа. Потом владелец вошёл снова:
    кампания ВКонтакте есть только у прежнего плана."""
    adapters = {"direct": FailingCreate(), "vk": FailingActivation()}
    world, plan = await ready(tmp_path, adapters=adapters)
    with pytest.raises(LaunchFailed):
        await launch(world, plan)
    (old,) = [item for item in campaigns(world) if item.platform == "vk"]
    vk = next(item for item in world.repository.accounts(world.business.id) if item.platform == "vk")
    vk.status = "needs_reauth"
    adapters["direct"].fail = False
    replacement = await world.plans.get_plan(world.business.id, NOW, TODAY)
    assert (await launch(world, replacement))["status"] == "launched"
    vk.status = "ok"
    adapters["vk"].fail = False
    return world, old, recovery_worker(world, after=timedelta(days=1), dry_run=False)


async def test_stats_of_old_plan_platform_are_still_synced(tmp_path: Path) -> None:
    """Площадка есть только у прежнего плана: его кампанию, включённую владельцем в кабинете, статистика всё равно
    читает, и расход попадает в отчёты."""
    from app.db.models import StatsDaily
    from app.services.sync import shared_ids, sync_stats

    world, old, worker = await replaced_vk_plan(tmp_path)
    assert {item.platform for item in worker.current_campaigns(world.business)} == {"direct"}
    await turn_on_in_cabinet(world, old)
    world.adapters["vk"].clock.advance(hours=12)
    await sync_stats(worker, world.business)
    rows = [row for row in world.repository.rows(StatsDaily) if row.campaign_id == old.id]
    assert rows and sum(row.cost_kop for row in rows) > 0
    # Внешние id уникальны только внутри площадки: совпадение с объявлением прежнего плана на другой площадке видно.
    _, old_ads, _ = world.repository.tree_rows(old.id)
    direct_ad = next(
        ad for item in worker.current_campaigns(world.business) for ad in world.repository.tree_rows(item.id)[1]
    )
    old_ads[0].external_id = direct_ad.external_id
    assert shared_ids(worker, world.business) == {direct_ad.external_id}


async def test_owner_stop_of_old_plan_campaign_is_not_reverted(tmp_path: Path) -> None:
    """«Остановить всё» остановило и кампанию прежнего плана, которую владелец включал в кабинете. Откат этой остановки
    включил бы рекламу с устаревшими объявлениями, без доли бюджета и без присмотра: он запрещён и в истории, и при
    запросе. Остановку кампании действующего плана вернуть можно."""
    from app.services.budget_guard import stop_all
    from app.services.rollback import REPLACED, RollbackService

    world, old, current, worker = await replaced_plan(tmp_path)
    await turn_on_in_cabinet(world, old)
    worker.step_key = "owner"
    await stop_all(world.business, worker=worker, actor="owner")
    stops = {row.entity_id: row for row in world.repository.rows(ChangeLog) if row.reason_code == "OWNER_STOP"}
    (current_direct,) = [item for item in campaigns(world) if item.id in current and item.platform == "direct"]
    assert (await statuses_by_id(world))[old.id] == "paused_owner"
    replaced = worker.repository.replaced_entities(world.business.id)
    assert replaced == {("direct", old.external_id)}
    assert not can_revert(stops[old.external_id], world.business, replaced=replaced)
    assert can_revert(stops[current_direct.external_id], world.business, replaced=replaced)
    rollback = RollbackService(worker.repository, dry_run=False)
    with pytest.raises(ChangeConflict) as refused:
        await rollback.revert(
            stops[old.external_id].id,
            business_id=world.business.id,
            adapter=world.adapters["direct"],
            now=worker.clock.now,
        )
    assert str(refused.value) == REPLACED
    assert stops[old.external_id].status == "applied" and (await statuses_by_id(world))[old.id] == "paused_owner"
    revert = await rollback.revert(
        stops[current_direct.external_id].id,
        business_id=world.business.id,
        adapter=world.adapters["direct"],
        now=worker.clock.now,
    )
    assert revert.status == "applied" and (await statuses_by_id(world))[current_direct.id] == "active"


async def test_demo_rewind_with_old_plan_campaign(tmp_path: Path) -> None:
    """Перемотка демо у бизнеса с кампанией прежнего плана: она не включается и не получает бюджет, а если владелец
    включил её в кабинете, её расход учитывается и страж бюджета её видит."""
    from app.db.models import BudgetLedger
    from app.demo_flags import AdvanceProgress
    from app.services.demo import DemoService
    from app.services.screens import ScreensService

    world, old, current, worker = await replaced_plan(tmp_path)
    limits = (old.daily_limit_kop, old.period_cap_kop)
    progress = AdvanceProgress(days=1)
    await DemoService(worker).advance(world.business.id, progress)
    assert progress.done == 1 and (await statuses_by_id(world))[old.id] == "paused_auto"
    assert (old.daily_limit_kop, old.period_cap_kop) == limits
    await turn_on_in_cabinet(world, old)
    await DemoService(worker).advance(world.business.id, AdvanceProgress(days=1))
    direct = sum(row.spent_kop for row in world.repository.rows(BudgetLedger) if row.platform == "direct")
    spent = world.adapters["direct"]._spent
    assert spent[old.external_id] > 0 and direct >= spent[old.external_id]
    items = ScreensService(worker).ads(world.business, TODAY, TODAY + timedelta(days=3))["items"]
    assert {item["campaign_id"] for item in items} == current


async def test_business_deleted_before_recovery_is_skipped(tmp_path: Path, monkeypatch, caplog) -> None:
    """Владелец удалил аккаунт, пока ход воркера шёл к его бизнесу: восстановление и шаги пропускают его без падения
    хода остальных бизнесов."""
    world, plan = await ready(tmp_path)
    await crash_before_finish(world, plan, monkeypatch)
    worker = recovery_worker(world)
    world.repository.tables[Business].remove(world.business)
    await worker.run_business_due(world.business.id)
    assert f"Launch recovery failed for business {world.business.id}" in caplog.text
    assert await platform_statuses(world) == {"direct": "active", "vk": "active"} and not interrupted_stops(world)


class ReconcileBlind(FakeAdapter):
    """Включение кампании прошло, а чтения после него не отвечают silent раз: исход неизвестен, изменение pending."""

    silent = 1
    armed = False

    async def apply(self, operation) -> None:
        await super().apply(operation)
        if operation.entity_type == "campaign" and operation.after == "active":
            self.armed = True

    async def get_state(self, refs):
        if self.armed and self.silent:
            self.silent -= 1
            raise PlatformError("Таймаут.", status_code=503)
        return await super().get_state(refs)


def pending_launches(world: World) -> list[ChangeLog]:
    return world.repository.pending(world.business.id)


async def unknown_activation(tmp_path: Path, silent: int = 1) -> tuple[World, dict]:
    """Запуск упал на включении ВКонтакте с неизвестным исходом и сам остановил созданное: план failed без
    started_at, а включение ВКонтакте осталось pending."""
    adapters = {"direct": FakeAdapter(), "vk": ReconcileBlind()}
    adapters["vk"].silent = silent
    world, plan = await ready(tmp_path, adapters=adapters)
    with pytest.raises(LaunchFailed) as failed:
        await launch(world, plan)
    assert "Площадка «ВКонтакте» не ответила, включилась ли реклама." in str(failed.value)
    stored = world.repository.latest_plan(world.business.id)
    assert stored.status == "failed" and stored.payload["launch"]["started_at"] is None
    (pending,) = pending_launches(world)
    assert pending.platform == "vk" and pending.kind == "launch"
    assert "свер" not in pending.evidence["_service"]["error"] and "свер" not in str(failed.value)
    return world, plan


async def test_unknown_activation_of_stopped_launch_does_not_block_retry(tmp_path: Path) -> None:
    """Неизвестный исход включения после неудачи, которая всё остановила, раньше навсегда запрещал повтор: изменение
    никто не проверял. Повтор под блокировкой проверяет его сам и запускает рекламу на тех же кампаниях."""
    world, plan = await unknown_activation(tmp_path)
    created = {item.id for item in campaigns(world)}
    assert (await launch(world, plan))["status"] == "launched" and world.business.status == "running"
    assert not pending_launches(world) and {item.id for item in campaigns(world)} == created
    assert all(len(adapter._trees) == 1 for adapter in world.adapters.values())
    assert await platform_statuses(world) == {"direct": "active", "vk": "active"}


async def test_worker_settles_unknown_activation_of_stopped_launch(tmp_path: Path) -> None:
    """Плана с незавершённым запуском нет, а изменение pending осталось: ход воркера всё равно его проверяет."""
    world, _ = await unknown_activation(tmp_path)
    (change,) = pending_launches(world)
    await recovery_worker(world).run_business_due(world.business.id)
    assert change.status == "failed" and not pending_launches(world)
    assert await platform_statuses(world) == {"direct": "paused_auto", "vk": "paused_auto"}


async def test_retry_waits_while_platform_still_does_not_answer(tmp_path: Path) -> None:
    """Площадка не отвечает и при повторе: исход включения неизвестен, запуск откладывается понятным текстом, ничего
    нового не создаётся. Когда площадка отвечает, повтор проходит."""
    world, plan = await unknown_activation(tmp_path, silent=2)
    stored = world.repository.latest_plan(world.business.id)
    with pytest.raises(LaunchBlocked) as blocked:
        await launch(world, plan)
    assert blocked.value.reasons == [launch_module.SILENT.format("ВКонтакте")]
    assert "свер" not in blocked.value.reasons[0] and len(pending_launches(world)) == 1
    assert stored.status == "failed" and stored.payload["launch"]["started_at"] is None
    assert len(campaigns(world)) == 2 and all(len(adapter._trees) == 1 for adapter in world.adapters.values())
    assert (await launch(world, plan))["status"] == "launched" and not pending_launches(world)


async def test_pending_of_platform_outside_plan_does_not_block_launch(tmp_path: Path, monkeypatch) -> None:
    """Исход включения ВКонтакте неизвестен, а кабинет с тех пор требует входа, и новый план — только Директ: запуск
    не сверяет площадку, которую не включает, её изменение остаётся воркеру."""
    world, _ = await unknown_activation(tmp_path, silent=100)
    account = next(item for item in world.repository.accounts(world.business.id) if item.platform == "vk")
    account.status = "expired"
    plan = await world.plans.get_plan(world.business.id, NOW, TODAY)
    resolve = world.launches.resolve_adapter

    def closed(business_id: int, platform: str):
        if platform == "vk":
            raise PlatformError("Кабинет площадки не подключён или требует входа заново.")
        return resolve(business_id, platform)

    monkeypatch.setattr(world.launches, "resolve_adapter", closed)
    assert (await launch(world, plan))["status"] == "launched"
    launched = world.repository.latest_plan(world.business.id)
    assert launched.payload["platforms"] == ["direct"] and list(launched.payload["launch"]["campaigns"]) == ["direct:1"]
    assert [change.platform for change in pending_launches(world)] == ["vk"]


@pytest.mark.parametrize("error", [InvalidToken(), LookupError("vk")])
async def test_closed_account_with_unknown_activation_blocks_launch_with_text(tmp_path: Path, monkeypatch, error):
    """Сверить включение нечем: токен кабинета не расшифровать (сменили ключ) или кабинет удалён. Владелец получает
    понятный отказ (409), а не ошибку сервера."""
    world, plan = await unknown_activation(tmp_path)
    resolve = world.launches.resolve_adapter

    def closed(business_id: int, platform: str):
        if platform == "vk":
            raise error
        return resolve(business_id, platform)

    monkeypatch.setattr(world.launches, "resolve_adapter", closed)
    with pytest.raises(LaunchBlocked) as blocked:
        await launch(world, plan)
    assert blocked.value.reasons == [f"ВКонтакте: {launch_module.CLOSED}"]
    assert len(pending_launches(world)) == 1


@pytest.mark.parametrize("error", [InvalidToken(), LookupError("vk")])
async def test_closed_account_before_launch_blocks_it_with_text(tmp_path: Path, monkeypatch, error) -> None:
    world, plan = await ready(tmp_path)
    resolve = world.launches.resolve_adapter

    def closed(business_id: int, platform: str):
        if platform == "vk":
            raise error
        return resolve(business_id, platform)

    monkeypatch.setattr(world.launches, "resolve_adapter", closed)
    with pytest.raises(LaunchBlocked) as blocked:
        await launch(world, plan)
    assert blocked.value.reasons == [f"ВКонтакте: {launch_module.CLOSED}"]
    assert not campaigns(world) and world.repository.latest_plan(world.business.id).status == "draft"


async def test_abandoned_launch_with_silent_platform_is_left_to_worker(tmp_path: Path, monkeypatch) -> None:
    """Процесс упал посреди включения, и площадка не отвечает и при повторе: брошенный запуск остаётся незавершённым,
    его кампании остановит воркер."""
    adapters = {"direct": FakeAdapter(), "vk": CrashAfterActivation()}
    world, plan = await ready(tmp_path, adapters=adapters)
    with pytest.raises(Crash):
        await launch(world, plan)

    async def silent(refs):
        raise PlatformError("Таймаут.", status_code=503)

    monkeypatch.setattr(adapters["vk"], "get_state", silent)
    with pytest.raises(LaunchBlocked, match="«ВКонтакте» не отвечает"):
        await launch(world, plan)
    stored = world.repository.latest_plan(world.business.id)
    assert stored.status == "draft" and stored.payload["launch"]["started_at"] == NOW.isoformat()
    assert len(pending_launches(world)) == 1


async def test_abandoned_launch_is_continued_by_owner_at_once(tmp_path: Path, monkeypatch) -> None:
    """Процесс упал перед finish, кампании включены. Владелец видит, что запуск не завершился, и сразу жмёт
    «Запустить»: запуск продолжается на тех же кампаниях, без дублей и без ожидания воркера."""
    world, plan = await ready(tmp_path)
    await crash_before_finish(world, plan, monkeypatch)
    view = await world.plans.get_plan(world.business.id, NOW, TODAY)
    assert view["status"] == "draft" and view["launch_error"] == plan_module.UNFINISHED
    created = {item.id for item in campaigns(world)}
    result = await world.launches.launch(world.business.id, plan["id"], False, NOW + timedelta(minutes=2), TODAY)
    assert result["status"] == "launched" and world.business.status == "running"
    assert {item.id for item in campaigns(world)} == created and len(created) == 2
    assert all(len(adapter._trees) == 1 for adapter in world.adapters.values())
    assert await platform_statuses(world) == {"direct": "active", "vk": "active"}
    launches = [row for row in world.repository.rows(ChangeLog) if row.kind == "launch"]
    assert sorted((row.platform, row.status) for row in launches) == [
        ("direct", "applied"),
        ("direct", "applied"),
        ("vk", "applied"),
        ("vk", "applied"),
    ]
    stored = world.repository.latest_plan(world.business.id)
    assert stored.payload["launch"]["started_at"] is None and stored.payload["launch"]["error"] is None
    assert not interrupted_stops(world) and not notices(world)
    worker = recovery_worker(world)
    assert {item.id for item in worker.current_campaigns(world.business)} == created


async def test_abandoned_launch_with_changed_budget_is_stopped_and_refused(tmp_path: Path, monkeypatch) -> None:
    """Брошенный запуск, а бюджет с тех пор изменили: продолжать его нельзя, и созданное останавливается сразу, а не
    ждёт воркера. План записан упавшим, и новый план собирается по новым данным."""
    world, plan = await ready(tmp_path)
    await crash_before_finish(world, plan, monkeypatch)
    world.business.month_budget_kop += 100_000
    with pytest.raises(LaunchBlocked, match="изменились"):
        await launch(world, plan)
    assert await platform_statuses(world) == {"direct": "paused_auto", "vk": "paused_auto"}
    assert all(item.status == "paused_auto" for item in campaigns(world))
    stored = world.repository.latest_plan(world.business.id)
    assert stored.status == "failed" and stored.payload["launch"]["started_at"] is None
    assert stored.payload["launch"]["error"] == launch_module.INTERRUPTED
    assert world.business.status == "plan_ready"
    rebuilt = await world.plans.get_plan(world.business.id, NOW, TODAY)
    assert rebuilt["id"] != plan["id"] and rebuilt["month_budget_kop"] == world.business.month_budget_kop


async def test_abandoned_launch_in_closed_account_is_left_to_worker(tmp_path: Path, monkeypatch) -> None:
    """Повтор брошенного запуска отказан, а кабинет ВКонтакте требует входа: остановить там нечем, план ждёт воркера,
    и текст не обещает, что всё остановлено."""
    world, plan = await ready(tmp_path)
    await crash_before_finish(world, plan, monkeypatch)
    world.business.month_budget_kop += 100_000
    resolve = world.launches.resolve_adapter

    def closed(business_id: int, platform: str):
        if platform == "vk":
            raise LookupError("Кабинет требует входа заново.")
        return resolve(business_id, platform)

    monkeypatch.setattr(world.launches, "resolve_adapter", closed)
    with pytest.raises(LaunchBlocked, match="изменились"):
        await launch(world, plan)
    assert await platform_statuses(world) == {"direct": "paused_auto", "vk": "active"}
    stored = world.repository.latest_plan(world.business.id)
    error = stored.payload["launch"]["error"]
    assert stored.status == "failed" and stored.payload["launch"]["started_at"]
    assert error == launch_module.failed_launch(None, stopped=False) and "None" not in error
    assert error.startswith("Запуск рекламы не завершился. Не все созданные кампании")


async def test_busy_lock_without_running_launch_keeps_its_own_text(tmp_path: Path, monkeypatch) -> None:
    """Блокировку держит не запуск (шаг мастера, перемотка демо): «Запуск уже идёт» было бы неправдой."""
    world, plan = await ready(tmp_path)
    held = exclusive_lock(world, monkeypatch)
    held.add(world.business.id)
    with pytest.raises(ChangeConflict, match="уже выполняется") as busy:
        await launch(world, plan)
    assert not isinstance(busy.value, LaunchBlocked) and not campaigns(world)


async def test_busy_lock_next_to_abandoned_launch_is_not_reported_as_running(tmp_path: Path, monkeypatch) -> None:
    """Брошенный запуск оставил started_at, а блокировку держит воркер, который его останавливает, или шаг мастера:
    запуск не идёт, и «Запуск уже идёт» было бы неправдой."""
    world, plan = await ready(tmp_path)
    await crash_before_finish(world, plan, monkeypatch)
    assert launch_unsettled(world.repository.latest_plan(world.business.id)) and not launch_module.LAUNCHING
    held = exclusive_lock(world, monkeypatch)
    held.add(world.business.id)
    with pytest.raises(ChangeConflict, match="уже выполняется") as busy:
        await launch(world, plan)
    assert not isinstance(busy.value, LaunchBlocked)


async def test_busy_lock_pool_is_not_reported_as_running_launch(tmp_path: Path, monkeypatch) -> None:
    from app.services.changes import ServerBusy

    world, plan = await ready(tmp_path)
    await crash_before_finish(world, plan, monkeypatch)

    @contextmanager
    def busy(business_id: int):
        raise ServerBusy
        yield

    monkeypatch.setattr(world.repository, "business_lock", busy)
    with pytest.raises(ServerBusy):
        await launch(world, plan)


async def test_worker_stops_abandoned_new_plan_next_to_failed_old_one(tmp_path: Path, monkeypatch) -> None:
    """v1 упал целиком и остановил созданное, v2 брошен посреди запуска: предпроверка воркера находит брошенный
    запуск среди нескольких планов и останавливает кампании v2."""
    adapters = {"direct": FailingActivation(), "vk": FailingActivation()}
    world, v1 = await ready(tmp_path, adapters=adapters)
    with pytest.raises(LaunchFailed):
        await launch(world, v1)
    world.business.month_budget_kop += 100_000
    v2 = await world.plans.get_plan(world.business.id, NOW, TODAY)
    adapters["direct"].fail = False
    adapters["vk"].fail = False
    await crash_before_finish(world, v2, monkeypatch)
    plans = world.repository.rows(Plan, business_id=world.business.id)
    assert [launch_unsettled(item) for item in sorted(plans, key=lambda item: item.version)] == [False, True]
    fresh = set(world.repository.latest_plan(world.business.id).payload["launch"]["campaigns"].values())
    await recovery_worker(world).run_business_due(world.business.id)
    statuses = await statuses_by_id(world)
    assert fresh and all(statuses[campaign_id] == "paused_auto" for campaign_id in fresh)
    assert not launch_unsettled(world.repository.latest_plan(world.business.id))


async def test_stopped_launch_reads_notices_of_its_plan_only(tmp_path: Path) -> None:
    """Неудача, которая всё остановила, отмечает прочитанным «не удалось остановить» своего плана, а не чужого."""
    adapters = {"direct": FailingActivation(), "vk": FailingActivation()}
    world, plan = await ready(tmp_path, adapters=adapters)
    worker = recovery_worker(world)
    for target in (plan["id"], plan["id"] + 100):
        worker.notice(
            world.business, "LAUNCH_INTERRUPTED", "Не удалось остановить.", key=f"k{target}", target_id=target
        )
    with pytest.raises(LaunchFailed, match="остановлено и ничего не тратит"):
        await launch(world, plan)
    unread = worker.repository.unread_notifications(world.business.id, {"LAUNCH_INTERRUPTED"}, 5, worker.clock.now)
    assert [row.log[0]["target_id"] for row in unread] == [plan["id"] + 100]


async def test_revoked_account_with_stopped_campaign_is_stopped(tmp_path: Path, monkeypatch) -> None:
    """Кабинет отключён, но его кампанию остановили перед отключением (строка остановлена): владельцу не нужно
    проверять кабинет, план записан остановленным."""
    world, plan = await ready(tmp_path)
    await crash_before_finish(world, plan, monkeypatch)
    vk = next(item for item in campaigns(world) if item.platform == "vk")
    vk.status = "paused_owner"
    account = next(item for item in world.repository.accounts(world.business.id) if item.platform == "vk")
    account.status = "revoked"
    await recovery_worker(world).run_business_due(world.business.id)
    stored = world.repository.latest_plan(world.business.id)
    assert stored.payload["launch"]["started_at"] is None
    assert stored.payload["launch"]["error"] == launch_module.INTERRUPTED


def test_unstopped_text_names_one_or_several_platforms() -> None:
    one = launch_module.interrupted_unstopped(["direct"])
    several = launch_module.interrupted_unstopped(["direct", "vk"], "Площадка не ответила.")
    assert "рекламу на площадке «Яндекс Директ» пока" in one
    assert "рекламу на площадках «Яндекс Директ», «ВКонтакте» пока" in several
    assert several.startswith("Запуск не удался: Площадка не ответила. Остановить")


async def test_campaign_of_launched_plan_without_platform_id_is_not_current(tmp_path: Path) -> None:
    world, plan = await ready(tmp_path)
    await launch(world, plan)
    direct, vk = sorted(campaigns(world), key=lambda item: item.platform)
    vk.external_id = None
    assert [item.id for item in recovery_worker(world).current_campaigns(world.business)] == [direct.id]


async def test_only_campaigns_of_last_launched_of_three_plans_are_current(tmp_path: Path) -> None:
    """v1 и v2 упали целиком и остановили созданное, v3 запущен: действующие — только кампании v3, остальные —
    прежних планов, и их остановку откат не отменяет."""
    adapters = {"direct": FailingActivation(), "vk": FailingActivation()}
    world, plan = await ready(tmp_path, adapters=adapters)
    for _ in range(2):
        with pytest.raises(LaunchFailed):
            await launch(world, plan)
        world.business.month_budget_kop += 100_000
        plan = await world.plans.get_plan(world.business.id, NOW, TODAY)
    adapters["direct"].fail = False
    adapters["vk"].fail = False
    assert (await launch(world, plan))["status"] == "launched"
    latest = set(world.repository.latest_plan(world.business.id).payload["launch"]["campaigns"].values())
    worker = recovery_worker(world)
    assert len(campaigns(world)) == 6 and len(latest) == 2
    assert {item.id for item in worker.current_campaigns(world.business)} == latest
    old = {(item.platform, item.external_id) for item in campaigns(world) if item.id not in latest and item.external_id}
    assert len(old) == 4 and worker.repository.replaced_entities(world.business.id) == old


async def test_interrupted_retry_of_failed_plan_is_explained_and_continued(tmp_path: Path, monkeypatch) -> None:
    """Повтор упавшего запуска оборвался: план ещё failed, но без объяснения прошлой неудачи. Экран плана говорит, что
    запуск не завершился, а следующий повтор его продолжает."""
    adapters = {"direct": FailingActivation(), "vk": FailingActivation()}
    world, plan = await ready(tmp_path, adapters=adapters)
    with pytest.raises(LaunchFailed):
        await launch(world, plan)
    adapters["direct"].fail = False
    adapters["vk"].fail = False
    await crash_before_finish(world, plan, monkeypatch)
    stored = world.repository.latest_plan(world.business.id)
    assert stored.status == "failed" and stored.payload["launch"]["error"] is None
    view = await world.plans.get_plan(world.business.id, NOW, TODAY)
    assert view["launch_error"] == plan_module.UNFINISHED
    assert (await launch(world, plan))["status"] == "launched" and len(campaigns(world)) == 2
    view = await world.plans.get_plan(world.business.id, NOW, TODAY)
    assert view["status"] == "launched" and view["launch_error"] is None


class WeeklyMinimum(FakeAdapter):
    def __init__(self, weekly: int) -> None:
        super().__init__()
        self.weekly = weekly

    async def min_budgets(self) -> dict[str, int]:
        return {"weekly_limit_kop": self.weekly}


async def ready_with_services(tmp_path: Path, services: list[str], **kwargs) -> tuple[World, dict]:
    world = make_world(tmp_path, **kwargs)
    await onboard(world)
    fields = {key: {"value": item["value"]} for key, item in world.business.description["fields"].items()}
    fields["services"] = {"value": "\n".join(services)}
    fields["prices"] = {"value": "\n".join(f"{name} — 1 500 ₽" for name in services)}
    world.onboarding.save_description(world.business.id, fields, True, NOW)
    return world, await world.plans.get_plan(world.business.id, NOW, TODAY)


async def test_direct_gets_campaign_per_service_with_share_of_platform_limit(tmp_path: Path) -> None:
    services = ["Маникюр", "Педикюр", "Наращивание ресниц"]
    world, plan = await ready_with_services(tmp_path, services)
    assert (await launch(world, plan))["status"] == "launched"
    direct = sorted((item for item in campaigns(world) if item.platform == "direct"), key=lambda item: item.id)
    assert [item.name for item in direct] == [f"Автопилот: {name} — Самара" for name in services]
    expected = budget_limits(world.business, ["direct", "vk"], TODAY)["direct"]
    assert sum(item.daily_limit_kop for item in direct) <= expected.daily_limit
    assert sum(item.period_cap_kop for item in direct) <= expected.remaining_cap
    assert all(item.status == "active" for item in direct)
    for item in direct:
        assert len(world.repository.tree_rows(item.id)[0]) == 1
    launched = world.repository.latest_plan(world.business.id).payload["launch"]
    assert set(launched["campaigns"]) == {"direct:1", "direct:2", "direct:3", "vk"}
    history = world.repository.rows(ChangeLog, business_id=world.business.id)
    assert sorted(change.platform for change in history) == ["direct", "direct", "direct", "vk"]


async def test_platform_share_floor_counts_every_direct_campaign(tmp_path: Path, monkeypatch) -> None:
    from app.domain.budget import share_floors
    from app.services import optimize

    world, plan = await ready_with_services(tmp_path, ["Маникюр", "Педикюр", "Наращивание ресниц"])
    await launch(world, plan)
    counted = []

    def floors(total_daily, minimums, campaigns):
        counted.append(campaigns)
        return share_floors(total_daily, minimums, campaigns)

    monkeypatch.setattr(optimize, "share_floors", floors)
    await optimize.rebalance_budget(recovery_worker(world, dry_run=False), world.business, force=True)
    assert counted == [{"direct": 3, "vk": 1}]


async def test_scale_leaves_donor_campaign_its_platform_minimum(tmp_path: Path, monkeypatch) -> None:
    from math import ceil

    from app.domain.budget import MINIMUM_MARGIN, daily_floor
    from app.domain.types import Action, Decision, GroupSnapshot
    from app.services import optimize

    adapters = {"direct": WeeklyMinimum(14_000), "vk": FakeAdapter()}
    world, plan = await ready_with_services(tmp_path, ["Маникюр", "Педикюр", "Наращивание ресниц"], adapters=adapters)
    await launch(world, plan)
    direct = sorted((item for item in campaigns(world) if item.platform == "direct"), key=lambda item: item.id)
    target, donor, other = (world.repository.tree_rows(item.id)[0][0].external_id for item in direct)
    groups = [
        GroupSnapshot(target, "direct", 50_000),
        GroupSnapshot(donor, "direct", 50_000, active=False),
        GroupSnapshot(other, "direct", 50_000),
    ]
    numbers = {"old_limit": 50_000, "new_limit": 100_000}
    scale = Decision(target, Action.SCALE, "SCALE_UP", entity="group", new_daily_limit=100_000, numbers=numbers)
    monkeypatch.setattr(optimize, "snapshot", AsyncMock(return_value=([], groups)))
    monkeypatch.setattr(optimize, "decide", lambda *args, **kwargs: [scale])
    monkeypatch.setattr(launch_module, "sync_removed_services", AsyncMock())
    worker = recovery_worker(world, after=timedelta(days=2), dry_run=False)
    await optimize.optimize(worker, world.business)
    floor = ceil(daily_floor(await world.adapters["direct"].min_budgets(), MINIMUM_MARGIN))
    assert floor == 2_000
    limits = worker.metadata(world.business)["group_limits"]
    assert limits == {target: 100_000 - floor, donor: floor, other: 50_000}


async def test_direct_budget_below_campaign_minimums_shares_campaigns_between_services(tmp_path: Path) -> None:
    services = ["Маникюр", "Педикюр", "Наращивание ресниц", "Дизайн ногтей"]
    limit = budget_limits(make_world(tmp_path / "probe").business, ["direct", "vk"], TODAY)["direct"]
    whole = weekly_limit(limit.daily_limit, limit.remaining_cap, TODAY)
    adapters = {"direct": WeeklyMinimum(whole * 3 // 8), "vk": FakeAdapter()}
    world, plan = await ready_with_services(tmp_path, services, adapters=adapters)
    assert (await launch(world, plan))["status"] == "launched"
    names = sorted(item.name for item in campaigns(world) if item.platform == "direct")
    assert names == ["Автопилот: Маникюр, Наращивание ресниц — Самара", "Автопилот: Педикюр, Дизайн ногтей — Самара"]


def test_launch_started_before_campaigns_per_service_keeps_one_direct_campaign() -> None:
    groups = [{"id": index, "platform": "direct", "service": f"Услуга {index}"} for index in (1, 2)]
    plan = Plan(business_id=1, status="ready", payload={"groups": groups, "launch": {"campaigns": {"direct": 7}}})
    limit = budget_limits(Business(city="Самара", month_budget_kop=BUDGET_KOP), ["direct"], TODAY)
    units = plan_module.campaign_units(plan, ["direct"], limit, {"direct": {}}, TODAY)
    assert list(units) == ["direct"] and [group["id"] for group in units["direct"][0]] == [1, 2]


THURSDAY, SUNDAY = date(2026, 1, 1), date(2026, 1, 4)
DIRECT_MINIMUM = {"weekly_limit_kop": 36_600}


def thursday_layout_plan(campaigns: dict[str, int] | None = None) -> tuple[Plan, dict]:
    groups = [{"id": index, "platform": "direct", "service": f"Услуга {index}"} for index in (1, 2, 3)]
    batches = {f"direct:{index}": [index] for index in (1, 2, 3)}
    plan = Plan(
        business_id=1,
        status="failed",
        payload={"groups": groups, "launch": {"campaigns": campaigns or {}, "batches": batches}},
    )
    return plan, budget_limits(Business(city="Самара", month_budget_kop=4_000_000), ["direct"], SUNDAY)


@pytest.mark.parametrize("campaigns", [{}, {"direct:1": 7}], ids=["nothing_staged", "staged_not_created"])
def test_retry_on_sunday_lays_out_campaigns_again_instead_of_thursday_layout(campaigns) -> None:
    """40 000 ₽, три услуги — в четверг три кампании, запуск упал до создания на площадке (строка
    кампании могла остаться заведённой). Повтор в воскресенье не берёт раскладку четверга (по 316 ₽ на кампанию —
    ниже минимума Директа 366 ₽), а раскладывает заново."""
    plan, limits = thursday_layout_plan(campaigns)
    units = plan_module.campaign_units(plan, ["direct"], limits, {"direct": DIRECT_MINIMUM}, SUNDAY)
    assert {key: [group["id"] for group in groups] for key, (groups, _) in units.items()} == {"direct:1": [1, 2, 3]}
    _, part = units["direct:1"]
    assert weekly_limit(part.daily_limit, part.remaining_cap, SUNDAY) >= DIRECT_MINIMUM["weekly_limit_kop"]
    assert launch_module.units_below_minimum(units, {"direct": DIRECT_MINIMUM}, SUNDAY) is None


def test_retry_keeps_created_campaign_and_names_minimum_of_each_campaign() -> None:
    """Кампания, созданная на площадке в четверг, сохраняет свою услугу; остальные раскладываются заново, а если
    лимит какой-то кампании ниже минимума Директа — понятная причина до создания кампаний."""
    plan, limits = thursday_layout_plan({"direct:1": 7})
    units = plan_module.campaign_units(plan, ["direct"], limits, {"direct": DIRECT_MINIMUM}, SUNDAY, {"direct:1"})
    assert {key: [group["id"] for group in groups] for key, (groups, _) in units.items()} == {
        "direct:1": [1],
        "direct:2": [2, 3],
    }
    reason = launch_module.units_below_minimum(units, {"direct": DIRECT_MINIMUM}, SUNDAY)
    assert "366 ₽" in reason and "с понедельника" in reason


class FailingWeeklyMinimum(WeeklyMinimum):
    """Директ с минимумом 366 ₽ в неделю, который создаёт первые created кампаний и отказывает на следующих."""

    fail = True
    created = 0
    trees = 0

    async def create_campaign_tree(self, plan_part):
        self.trees += 1
        if self.fail and self.trees > self.created:
            raise PlatformError("Директ временно недоступен.", status_code=503)
        return await super().create_campaign_tree(plan_part)


async def test_retry_of_failed_direct_on_sunday_lays_out_campaigns_again(tmp_path: Path) -> None:
    """Через запуск: при 40 000 ₽ VK выключен, в четверг Директ отказал на первой из трёх кампаний
    (её строка заведена, на площадке её нет). Повтор в воскресенье раскладывает услуги заново: одна кампания
    с лимитом не ниже минимума Директа."""
    adapters = {"direct": FailingWeeklyMinimum(DIRECT_MINIMUM["weekly_limit_kop"]), "vk": FakeAdapter()}
    world, plan = await ready_with_services(tmp_path, ["Маникюр", "Педикюр", "Наращивание ресниц"], adapters=adapters)
    world.business.month_budget_kop = 4_000_000
    plan = await world.plans.get_plan(world.business.id, NOW, THURSDAY)
    with pytest.raises(LaunchFailed):
        await world.launches.launch(world.business.id, plan["id"], False, NOW, THURSDAY)
    stored = world.repository.latest_plan(world.business.id).payload["launch"]
    assert sorted(key for key in stored["batches"] if key.startswith("direct:")) == ["direct:1", "direct:2", "direct:3"]
    assert list(stored["campaigns"]) == ["direct:1"]
    adapters["direct"].fail = False
    result = await world.launches.launch(world.business.id, plan["id"], False, NOW + timedelta(days=3), SUNDAY)
    assert result["status"] == "launched"
    (direct,) = [item for item in campaigns(world) if item.external_id]
    assert len(world.repository.tree_rows(direct.id)[0]) == 3
    assert weekly_limit(direct.daily_limit_kop, direct.period_cap_kop, SUNDAY) >= DIRECT_MINIMUM["weekly_limit_kop"]


async def test_retry_of_partial_launch_on_sunday_checks_minimum_of_each_campaign(tmp_path: Path) -> None:
    """То же в повторе площадки частичного запуска (retry_platforms): VK работает, Директ в четверг создал первую
    кампанию и отказал на второй. В воскресенье созданная кампания сохраняется, и лимиты не набирают минимума
    Директа — понятный отказ до создания кампаний, VK не тронут."""
    direct = FailingWeeklyMinimum(DIRECT_MINIMUM["weekly_limit_kop"])
    direct.created = 1
    adapters = {"direct": direct, "vk": FakeAdapter()}
    world, plan = await ready_with_services(tmp_path, ["Маникюр", "Педикюр", "Наращивание ресниц"], adapters=adapters)
    plan = await world.plans.get_plan(world.business.id, NOW, THURSDAY)
    result = await world.launches.launch(world.business.id, plan["id"], False, NOW, THURSDAY)
    assert set(result["platform_errors"]) == {"direct"}
    direct.fail = False
    trees = direct.trees
    with pytest.raises(LaunchBlocked, match="366 ₽"):
        await world.launches.launch(world.business.id, plan["id"], False, NOW + timedelta(days=3), SUNDAY)
    assert direct.trees == trees
    vk = next(item for item in campaigns(world) if item.platform == "vk")
    assert (await states(world, vk))[vk.external_id].status == "active"


async def test_direct_groups_get_negative_phrases_without_words_of_their_own_service(tmp_path: Path) -> None:
    adapters = {"direct": Regions({"region_ids": [51]}), "vk": FakeAdapter()}
    world, plan = await ready_with_services(tmp_path, ["Маникюр", "Курсы маникюра"], adapters=adapters)
    assert {"курсы", "вакансия", "бесплатно", "купить"} <= set(plan["negative_phrases"])
    await launch(world, plan)
    groups = [group for spec in adapters["direct"].specs for group in spec.groups]
    courses = [group.targeting["negative_keywords"] for group in groups if "Курсы" in group.name]
    other = [group.targeting["negative_keywords"] for group in groups if "Курсы" not in group.name]
    assert other == [plan["negative_phrases"]]
    assert courses == [[phrase for phrase in plan["negative_phrases"] if phrase != "курсы"]]


async def test_service_removed_from_shared_campaign_stops_only_its_ads(tmp_path: Path) -> None:
    from app.services.launch import sync_removed_services

    services = ["Маникюр", "Педикюр", "Наращивание ресниц", "Дизайн ногтей"]
    limit = budget_limits(make_world(tmp_path / "probe").business, ["direct", "vk"], TODAY)["direct"]
    whole = weekly_limit(limit.daily_limit, limit.remaining_cap, TODAY)
    adapters = {"direct": WeeklyMinimum(whole * 3 // 8), "vk": FakeAdapter()}
    world, plan = await ready_with_services(tmp_path, services, adapters=adapters)
    await launch(world, plan)
    fields = {key: {"value": item["value"]} for key, item in world.business.description["fields"].items()}
    fields["services"] = {"value": "Маникюр\nПедикюр\nДизайн ногтей"}
    world.onboarding.save_description(world.business.id, fields, True, NOW)
    worker = recovery_worker(world, dry_run=True)
    worker.step_key = "optimize:services"
    await sync_removed_services(worker, world.business)
    direct = [item for item in campaigns(world) if item.platform == "direct"]
    assert all(item.status == "active" for item in direct)
    shared = next(item for item in direct if "Наращивание ресниц" in item.name)
    rows, ads, _ = world.repository.tree_rows(shared.id)
    themes = {row.id: row.theme for row in rows}
    stopped = {themes[ad.ad_group_id] for ad in ads if ad.status == "paused_owner"}
    assert stopped and all("Наращивание ресниц" in theme for theme in stopped)
    assert any(ad.status != "paused_owner" for ad in ads)
    change = next(row for row in world.repository.rows(ChangeLog) if row.reason_code == "SERVICE_REMOVED")
    assert (
        change.status == "applied" and "Наращивание ресниц" in change.reason_text and shared.name in change.reason_text
    )
    # Услугу убрал владелец («Вы»), а объявления по его правке остановил автопилот — так и написано.
    assert change.reason_text.startswith("Вы убрали из описания") and "Автопилот остановил" in change.reason_text


async def shared_campaign_world(tmp_path: Path) -> tuple[World, Worker, Campaign]:
    services = ["Маникюр", "Педикюр", "Наращивание ресниц", "Дизайн ногтей"]
    limit = budget_limits(make_world(tmp_path / "probe").business, ["direct", "vk"], TODAY)["direct"]
    whole = weekly_limit(limit.daily_limit, limit.remaining_cap, TODAY)
    adapters = {"direct": WeeklyMinimum(whole * 3 // 8), "vk": FakeAdapter()}
    world, plan = await ready_with_services(tmp_path, services, adapters=adapters)
    await launch(world, plan)
    shared = next(item for item in campaigns(world) if "Наращивание ресниц" in item.name)
    return world, recovery_worker(world, dry_run=True), shared


async def describe_services(world: World, worker: Worker, value: str) -> None:
    from app.services.launch import sync_removed_services

    fields = {key: {"value": item["value"]} for key, item in world.business.description["fields"].items()}
    fields["services"] = {"value": value}
    world.onboarding.save_description(world.business.id, fields, True, NOW)
    worker.step_key = f"optimize:{value}"
    await sync_removed_services(worker, world.business)


async def test_service_spelled_differently_is_not_removed(tmp_path: Path) -> None:
    world, worker, _ = await shared_campaign_world(tmp_path)
    await describe_services(world, worker, "маникюр\n  Педикюр \nнаращивание  РЕСНИЦ\nДизайн ногтей")
    assert not [row for row in world.repository.rows(ChangeLog) if row.reason_code == "SERVICE_REMOVED"]
    assert "paused_owner" not in {ad.status for ad in world.repository.rows(Ad)}


async def test_service_returned_to_shared_campaign_resumes_only_ads_stopped_for_it(tmp_path: Path) -> None:
    world, worker, shared = await shared_campaign_world(tmp_path)
    rows, ads, _ = world.repository.tree_rows(shared.id)
    themes = {row.id: row.theme for row in rows}
    own = [ad for ad in ads if "Наращивание ресниц" in themes[ad.ad_group_id]]
    other = next(ad for ad in ads if "Наращивание ресниц" not in themes[ad.ad_group_id])
    # Объявление, которое владелец остановил сам, возврат услуги не включает.
    states = world.adapters["direct"]._states
    states[other.external_id] = replace(states[other.external_id], status="paused_owner")
    other.status = "paused_owner"
    await describe_services(world, worker, "Маникюр\nПедикюр\nДизайн ногтей")
    assert {ad.status for ad in own} == {"paused_owner"}
    await describe_services(world, worker, "Маникюр\nПедикюр\nНаращивание ресниц\nДизайн ногтей")
    assert {ad.status for ad in own} == {"active"} and other.status == "paused_owner"
    assert states[other.external_id].status == "paused_owner"
    (returned,) = [row for row in world.repository.rows(ChangeLog) if row.reason_code == "SERVICE_RETURNED"]
    assert returned.status == "applied" and "Наращивание ресниц" in returned.reason_text
    assert (
        returned.reason_text.startswith("Вы вернули в описание") and "Автопилот снова включил" in returned.reason_text
    )
    assert {op["entity_id"] for op in returned.operations} == {ad.external_id for ad in own}


async def test_removed_service_skips_ads_not_created_on_platform(tmp_path: Path) -> None:
    world, worker, shared = await shared_campaign_world(tmp_path)
    rows, ads, _ = world.repository.tree_rows(shared.id)
    themes = {row.id: row.theme for row in rows}
    own = [ad for ad in ads if "Наращивание ресниц" in themes[ad.ad_group_id]]
    own[0].external_id = None
    before = own[0].status
    await describe_services(world, worker, "Маникюр\nПедикюр\nДизайн ногтей")
    assert own[0].status == before and {ad.status for ad in own[1:]} == {"paused_owner"}


async def vk_disconnected_world(tmp_path: Path) -> tuple[World, Worker]:
    from time import monotonic

    from app.db.models import PlatformAccount
    from app.services.budget_guard import stop_platform

    world, plan = await ready_with_services(tmp_path, ["Маникюр"])
    await launch(world, plan)
    worker = recovery_worker(world, after=timedelta(days=2), dry_run=False)
    for platform in ("direct", "vk"):
        worker.step_key = f"owner:revoke-{platform}"
        await stop_platform(
            world.business, worker=worker, platform=platform, reason_text="Отключили.", deadline=monotonic() + 60
        )
    assert {item.status for item in campaigns(world)} == {"paused_owner"}
    vk = next(item for item in world.repository.rows(PlatformAccount) if item.platform == "vk")
    vk.status = "revoked"

    def resolve(business_id: int, platform: str):
        if platform == "vk":
            raise PlatformError("Кабинет площадки не подключён или требует входа заново.")
        return world.adapters[platform]

    return world, recovery_worker(world, after=timedelta(days=2), resolve=resolve, dry_run=False)


async def test_reconnected_direct_resumes_while_vk_account_stays_disconnected(tmp_path: Path) -> None:
    from app.services.budget_guard import resume_reconnected

    world, worker = await vk_disconnected_world(tmp_path)
    worker.step_key = "owner:reconnect"
    await resume_reconnected(worker, world.business, "direct")
    assert {item.platform: item.status for item in campaigns(world)} == {"direct": "active", "vk": "paused_owner"}


async def test_continue_works_while_vk_account_stays_disconnected(tmp_path: Path) -> None:
    from app.services.screens import ScreensService

    world, worker = await vk_disconnected_world(tmp_path)
    world.business.status = "paused"
    await ScreensService(worker).resume(world.business)
    assert world.business.status == "running"
    assert {item.platform: item.status for item in campaigns(world)} == {"direct": "active", "vk": "paused_owner"}


class CrashOnSecondActivation(SlowModeration):
    """Процесс упал посреди включения: первая кампания ушла ждать проверки, исход второй неизвестен."""

    def __init__(self) -> None:
        super().__init__()
        self.calls = 0

    async def apply(self, operation) -> None:
        if not self.accepted and operation.entity_type == "campaign" and operation.after == "active":
            self.calls += 1
            if self.calls == 2:
                raise asyncio.CancelledError()
        await super().apply(operation)


class DraftRefusesSuspend(SlowModeration):
    """Директ не даёт остановить кампанию-черновик, пока проверяются её объявления."""

    async def apply(self, operation) -> None:
        paused = operation.after in {"paused_owner", "paused_auto"}
        if not self.accepted and operation.entity_type == "campaign" and paused:
            raise PlatformError("Кампания-черновик не может быть остановлена.")
        await super().apply(operation)


class AcceptedOff(SlowModeration):
    """После одобрения живой Директ показывает кампанию выключенной (Status=ACCEPTED, State=OFF), а не «на
    проверке»; одно объявление площадка может отклонить."""

    def accept(self, rejected: str | None = None) -> None:
        self.accepted = True
        for key, state in self._states.items():
            if state.status == "draft":
                self._states[key] = replace(state, status="paused_auto")
            if key == rejected:
                self._states[key] = replace(state, status="rejected")

    async def apply(self, operation) -> None:
        state = self._states.get(operation.entity_id)
        if state is not None and state.status == "rejected":
            return
        await super().apply(operation)


def load_refs_of(change: ChangeLog) -> ExternalRefs:
    from app.services.changes import load_refs

    return load_refs(change)


async def accepted_off_launch(tmp_path: Path, adapter: AcceptedOff) -> tuple[World, ChangeLog]:
    world, plan = await ready_with_services(tmp_path, ["Маникюр"], platforms=("direct",), adapters={"direct": adapter})
    await launch(world, plan)
    (change,) = pending_launches(world)
    return world, change


async def reconcile(world: World, hours: float = 2) -> Worker:
    worker = recovery_worker(world, after=timedelta(hours=hours), dry_run=False)
    await worker.changes.recover_pending(worker.resolve_adapter, business_id=world.business.id, now=worker.clock.now)
    return worker


class CrashOnSecondAcceptedOff(AcceptedOff):
    """Процесс упал посреди включения второй кампании; после одобрения Директ показывает кампании выключенными."""

    def __init__(self) -> None:
        super().__init__()
        self.calls = 0

    async def apply(self, operation) -> None:
        if not self.accepted and operation.entity_type == "campaign" and operation.after == "active":
            self.calls += 1
            if self.calls == 2:
                raise asyncio.CancelledError()
        await super().apply(operation)


def launch_records(world: World) -> list[ChangeLog]:
    return [row for row in world.repository.rows(ChangeLog) if row.reason_code == "LAUNCH"]


class PartlyAccepted(AcceptedOff):
    """Одно объявление ещё на проверке: Директ не включает ни его, ни кампанию; fail_once — временный сбой
    включения кампании."""

    def __init__(self) -> None:
        super().__init__()
        self.waiting: set[str] = set()
        self.fail_once = False

    async def apply(self, operation) -> None:
        if operation.after == "active" and (operation.entity_id in self.waiting or operation.entity_type == "campaign"):
            if self.fail_once and operation.entity_type == "campaign":
                self.fail_once = False
                raise PlatformError("Директ временно недоступен.", status_code=503)
            if self.waiting:
                raise AwaitingModeration(operation.entity_id)
        await super().apply(operation)


class Extending(SlowModeration):
    def __init__(self) -> None:
        super().__init__()
        self.extended: list[str] = []

    async def extend_end_date(self, campaign_id: str) -> None:
        self.extended.append(campaign_id)


class AcceptedOffRefusesSuspend(DraftRefusesSuspend, AcceptedOff):
    pass


async def test_interrupted_new_month_pass_still_resumes_budget_stopped_campaign(tmp_path: Path, monkeypatch) -> None:
    """Проход нового месяца прервался на возобновлении — повтор помнит остановку по бюджету и
    возобновляет кампанию, хотя статус бизнеса уже running."""
    from app.services.budget_guard import stop_all

    world, plan = await ready_with_services(tmp_path, ["Маникюр"], platforms=("direct",))
    await launch(world, plan)
    worker = recovery_worker(world, after=timedelta(hours=7), dry_run=False)
    await stop_all(
        world.business,
        worker=worker,
        budget_stop=True,
        reason_code="BUDGET_STOP",
        reason_params={"spent": 0, "budget": world.business.month_budget_kop, "margin": 0},
    )
    worker.remember(world.business, month=(TODAY.replace(day=1) - timedelta(days=1)).replace(day=1).isoformat())
    apply = worker.apply

    async def refuse(*args, **kwargs):
        if kwargs.get("reason_code") == "BUDGET_RESUME":
            raise ChangeConflict("Площадка не подтвердила изменение.")
        return await apply(*args, **kwargs)

    monkeypatch.setattr(worker, "apply", refuse)
    with pytest.raises(ChangeConflict):
        await worker.run("month_rollover", world.business.id, key="interrupted-month-1")
    assert world.business.status == "running"
    monkeypatch.setattr(worker, "apply", apply)
    await worker.run("month_rollover", world.business.id, key="interrupted-month-2")
    (campaign,) = campaigns(world)
    assert (await states(world, campaign))[campaign.external_id].status == "active"


class ExtendingFake(FakeAdapter):
    def __init__(self) -> None:
        super().__init__()
        self.extended: list[str] = []

    async def extend_end_date(self, campaign_id: str) -> None:
        self.extended.append(campaign_id)


async def test_rebalance_conflict_of_one_campaign_does_not_stop_the_rest(tmp_path: Path, monkeypatch) -> None:
    """Площадка не подтвердила изменение одной кампании — пересчёт остальных и продление дат
    идут, а шаг всё равно провалится и повторится."""
    from app.services.optimize import rebalance_budget

    adapter = ExtendingFake()
    world, plan = await ready_with_services(
        tmp_path, ["Маникюр", "Педикюр"], platforms=("direct",), adapters={"direct": adapter}
    )
    await launch(world, plan)
    first, second = sorted(campaigns(world), key=lambda item: item.id)
    worker = recovery_worker(world, after=timedelta(hours=7), dry_run=False)
    apply = worker.apply

    async def refuse_first(business, campaign, *args, **kwargs):
        if campaign.id == first.id:
            raise ChangeConflict("Площадка не подтвердила изменение.")
        return await apply(business, campaign, *args, **kwargs)

    monkeypatch.setattr(worker, "apply", refuse_first)
    with pytest.raises(ChangeConflict):
        await rebalance_budget(worker, world.business)
    assert set(adapter.extended) == {first.external_id, second.external_id}


# Ожидание проверки площадкой — состояние кампании (review_since), а не pending-запись: сценарии запуска, остановки,
# продолжения, бюджета и брошенного запуска.


def ads_of(world: World) -> list[Ad]:
    return [row for row in world.repository.rows(Ad, world.business.id) if row.external_id]


def approve(world: World, *, rejected: str | None = None) -> None:
    """Сверка модерации прочитала решение площадки (sync_moderation): одобренные объявления активны."""
    for ad in ads_of(world):
        if ad.external_id == rejected:
            ad.moderation_status, ad.status = "rejected", "rejected"
        else:
            ad.moderation_status = "approved"
            ad.status = "active" if ad.status == "moderation" else ad.status


async def settle(world: World, hours: float = 2) -> Worker:
    from app.services.sync import settle_review

    worker = recovery_worker(world, after=timedelta(hours=hours), dry_run=False)
    await settle_review(worker, world.business)
    return worker


def settle_records(world: World) -> list[ChangeLog]:
    return [row for row in world.repository.rows(ChangeLog) if row.reason_code == "MODERATION_SETTLE"]


async def platform_status(world: World, campaign: Campaign) -> str:
    return (await states(world, campaign))[campaign.external_id].status


async def test_launch_waiting_for_review_is_applied_and_enabled_after_approval(tmp_path: Path) -> None:
    """(а) Запуск упёрся в проверку объявлений: запись LAUNCH применена сразу (не висит pending), кампания «ждёт
    одобрения» (review_since), бизнес работает. После одобрения шаг модерации сам включает каждую кампанию."""
    adapter = SlowModeration()
    adapter.moderation_delay = timedelta()
    world, plan = await ready_with_services(
        tmp_path, ["Маникюр", "Педикюр"], platforms=("direct",), adapters={"direct": adapter}
    )
    result = await launch(world, plan)
    assert result["awaiting_moderation"] == ["direct"] and result["platform_errors"] == {}
    assert not pending_launches(world) and world.business.status == "running"
    assert {row.status for row in launch_records(world)} == {"applied"}
    assert all(row.evidence["_service"]["review"] for row in launch_records(world))
    direct = campaigns(world)
    assert len(direct) == 2
    for campaign in direct:
        assert campaign.status == "active" and campaign.review_since is not None
        assert await platform_status(world, campaign) == "draft"
    adapter.accepted = True
    worker = recovery_worker(world, after=timedelta(hours=1), dry_run=False)
    await worker.run("sync_moderation", world.business.id, key="review-a")
    for campaign in direct:
        assert await platform_status(world, campaign) == "active"
        assert campaign.status == "active" and campaign.review_since is None
    assert {row.status for row in settle_records(world)} == {"applied"}


async def test_review_lasting_over_five_days_notifies_owner_without_failing(tmp_path: Path) -> None:
    """Проверка дольше 5 дней: владелец получает одно уведомление, запись не проваливается, ожидание остаётся."""
    world, plan = await ready_with_services(
        tmp_path, ["Маникюр"], platforms=("direct",), adapters={"direct": SlowModeration()}
    )
    await launch(world, plan)
    (campaign,) = campaigns(world)
    await settle(world, hours=24 * 4)
    assert not notices(world)
    await settle(world, hours=24 * 6)
    await settle(world, hours=24 * 6 + 1)
    assert [row.log[0]["reason_code"] for row in notices(world)] == ["MODERATION_LONG"]
    assert campaign.review_since is not None and {row.status for row in launch_records(world)} == {"applied"}


async def test_one_ad_approved_enables_campaign_while_second_is_reviewed(tmp_path: Path) -> None:
    """(б) Одно объявление одобрено, второе ещё на проверке: одобренная, но выключенная (ACCEPTED/OFF) кампания
    включается, ожидание держится до решения по второму; после одобрения второе включается тоже."""
    adapter = AcceptedOff()
    world, plan = await ready_with_services(tmp_path, ["Маникюр"], platforms=("direct",), adapters={"direct": adapter})
    await launch(world, plan)
    (campaign,) = campaigns(world)
    first, second = ads_of(world)
    adapter.accept()
    adapter._states[second.external_id] = replace(adapter._states[second.external_id], status="moderation")
    approve(world)
    second.moderation_status, second.status = "pending", "moderation"
    await settle(world)
    platform = await states(world, campaign)
    assert platform[campaign.external_id].status == "active" and platform[first.external_id].status == "active"
    assert campaign.review_since is not None
    adapter._states[second.external_id] = replace(adapter._states[second.external_id], status="paused_auto")
    approve(world)
    await settle(world, hours=3)
    assert (await states(world, campaign))[second.external_id].status == "active"
    assert campaign.review_since is None


async def test_rejected_ad_does_not_hold_campaign_waiting(tmp_path: Path) -> None:
    """(в) Отклонённое объявление не держит ни кампанию, ни запись: одобренные включаются, ожидание снимается, а
    отклонённое остаётся отклонённым (его переписывает сверка модерации)."""
    adapter = AcceptedOff()
    world, plan = await ready_with_services(tmp_path, ["Маникюр"], platforms=("direct",), adapters={"direct": adapter})
    await launch(world, plan)
    (campaign,) = campaigns(world)
    first, second = ads_of(world)
    adapter.accept(rejected=second.external_id)
    approve(world, rejected=second.external_id)
    await settle(world)
    platform = await states(world, campaign)
    assert platform[campaign.external_id].status == "active" and platform[first.external_id].status == "active"
    assert second.status == "rejected" and campaign.review_since is None


async def test_all_ads_rejected_notifies_owner_instead_of_long_review(tmp_path: Path) -> None:
    """Все объявления кампании отклонены — черновик не примут никогда: владелец сразу
    получает уведомление об этом, а не «долгая проверка» через 5 дней. Одобренную, но выключенную кампанию не
    включаем: показывать нечего, и запись «включили рекламу» ввела бы владельца в заблуждение."""
    adapter = AcceptedOff()
    world, plan = await ready_with_services(tmp_path, ["Маникюр"], platforms=("direct",), adapters={"direct": adapter})
    await launch(world, plan)
    (campaign,) = campaigns(world)
    adapter.accept()
    for ad in ads_of(world):
        ad.moderation_status, ad.status = "rejected", "rejected"
    await settle(world)
    await settle(world, hours=24 * 6)
    assert [row.log[0]["reason_code"] for row in notices(world)] == ["MODERATION_ALL_REJECTED"]
    assert await platform_status(world, campaign) == "paused_auto"
    assert not [row for row in settle_records(world) if row.kind == "resume"]


async def test_all_ads_rejected_in_draft_campaign_ends_review(tmp_path: Path) -> None:
    """Площадка отклонила все объявления кампании-черновика — черновик так и останется на проверке,
    но ждать нечего: отметка проверки снимается (иначе кампанию не сверял бы sync_campaign_status), владелец получает
    одно уведомление об отказе, кампания остаётся как есть."""
    world, plan = await ready_with_services(
        tmp_path, ["Маникюр"], platforms=("direct",), adapters={"direct": SlowModeration()}
    )
    await launch(world, plan)
    (campaign,) = campaigns(world)
    for ad in ads_of(world):
        ad.moderation_status, ad.status = "rejected", "rejected"
    await settle(world)
    assert campaign.review_since is None and campaign.status == "active"
    assert await platform_status(world, campaign) == "draft"
    assert [row.log[0]["reason_code"] for row in notices(world)] == ["MODERATION_ALL_REJECTED"]


async def test_rebalance_pause_of_rejected_draft_does_not_restart_review(tmp_path: Path) -> None:
    """Черновик, все объявления которого отклонены, снят с проверки; пересчёт лимитов ставит его на
    паузу «показывать нечего», которую площадка черновику не применяет. Отметка проверки не возвращается: пересчёт не
    держит под кампанию бюджет, а уведомление об отказе не повторяется."""
    from app.services.optimize import NO_ACTIVE_ADS

    world, plan = await ready_with_services(
        tmp_path, ["Маникюр"], platforms=("direct",), adapters={"direct": DraftRefusesSuspend()}
    )
    await launch(world, plan)
    (campaign,) = campaigns(world)
    for ad in ads_of(world):
        ad.moderation_status, ad.status = "rejected", "rejected"
    await settle(world)
    assert campaign.review_since is None
    for hours in (3, 5):
        await recovery_worker(world, after=timedelta(hours=hours), dry_run=False).run(
            "rebalance_budget", world.business.id, key=f"rejected-rebalance-{hours}"
        )
        await settle(world, hours=hours + 1)
    (pause,) = [row for row in world.repository.rows(ChangeLog) if row.reason_code == "BUDGET_REBALANCE"]
    assert pause.reason_text == NO_ACTIVE_ADS and campaign.status == "paused_auto"
    assert campaign.review_since is None and campaign.daily_limit_kop == 0
    assert [row.log[0]["reason_code"] for row in notices(world)] == ["MODERATION_ALL_REJECTED"]


async def test_stop_during_review_keeps_campaign_off_after_approval_and_continue_enables(tmp_path: Path) -> None:
    """(г) «Остановить» во время проверки: черновик Директ остановить не даёт — это не сбой остановки, цель —
    пауза. После одобрения кампания не включается (а если площадка включила её сама — останавливается), и
    «Продолжить» включает её обычным путём."""
    from app.services.budget_guard import stop_all
    from app.services.screens import ScreensService

    adapter = AcceptedOffRefusesSuspend()
    world, plan = await ready_with_services(
        tmp_path, ["Маникюр", "Педикюр"], platforms=("direct",), adapters={"direct": adapter}
    )
    await launch(world, plan)
    worker = recovery_worker(world, after=timedelta(minutes=5), dry_run=False)
    await stop_all(world.business, worker=worker, actor="owner")
    assert world.business.status == "paused"
    first, second = sorted(campaigns(world), key=lambda item: item.id)
    assert first.status == second.status == "paused_owner" and first.review_since is not None
    adapter.accept()
    adapter._states[second.external_id] = replace(adapter._states[second.external_id], status="active")
    await settle(world)
    assert await platform_status(world, first) == "paused_auto"
    assert await platform_status(world, second) != "active"
    assert first.review_since is None and second.review_since is None
    await ScreensService(recovery_worker(world, after=timedelta(hours=3), dry_run=False)).resume(world.business)
    assert world.business.status == "running"
    assert await platform_status(world, first) == await platform_status(world, second) == "active"


async def test_continue_does_not_undo_settled_pause_of_removed_service(tmp_path: Path) -> None:
    """Паузу, доведённую шагом модерации, «Продолжить» снимает по причине исходной паузы —
    кампанию услуги, убранной во время проверки, не включает."""
    from app.services.budget_guard import stop_all
    from app.services.screens import ScreensService

    adapter = AcceptedOffRefusesSuspend()
    world, plan = await ready_with_services(
        tmp_path, ["Маникюр", "Педикюр"], platforms=("direct",), adapters={"direct": adapter}
    )
    await launch(world, plan)
    first, second = sorted(campaigns(world), key=lambda item: item.id)
    worker = recovery_worker(world, after=timedelta(minutes=5), dry_run=False)
    await worker.apply(
        world.business,
        first,
        [Operation("set_status", "campaign", first.external_id, "status", "draft", "paused_auto")],
        actor="system",
        kind="pause",
        reason_code="SERVICE_REMOVED",
        reason_text="Услугу убрали — кампанию остановили.",
        suffix="service",
    )
    adapter.accept()
    adapter._states[first.external_id] = replace(adapter._states[first.external_id], status="active")
    await settle(world)
    assert [row for row in settle_records(world) if row.entity_id == first.external_id]
    last = world.repository.last_status_change(world.business.id, "direct", "campaign", first.external_id)
    assert last.reason_code == "SERVICE_REMOVED"
    await stop_all(world.business, worker=recovery_worker(world, after=timedelta(hours=2, minutes=30)), actor="owner")
    await ScreensService(recovery_worker(world, after=timedelta(hours=3), dry_run=False)).resume(world.business)
    assert world.business.status == "running"
    assert await platform_status(world, first) == "paused_auto" and await platform_status(world, second) == "active"


async def test_returned_service_resumes_campaign_whose_pause_was_settled_after_review(tmp_path: Path) -> None:
    """Услугу убрали, пока площадка проверяла её кампанию: паузу черновика после одобрения довёл шаг модерации. Вернули
    услугу — кампания включается, как после обычной остановки за убранную услугу."""
    adapter = AcceptedOffRefusesSuspend()
    world, plan = await ready_with_services(
        tmp_path, ["Маникюр", "Педикюр"], platforms=("direct",), adapters={"direct": adapter}
    )
    await launch(world, plan)
    removed = next(item for item in campaigns(world) if "Педикюр" in item.name)
    worker = recovery_worker(world, after=timedelta(minutes=5), dry_run=False)
    await describe_services(world, worker, "Маникюр")
    assert removed.status == "paused_owner"
    adapter.accept()
    adapter._states[removed.external_id] = replace(adapter._states[removed.external_id], status="active")
    approve(world)
    await settle(world)
    assert [row for row in settle_records(world) if row.entity_id == removed.external_id]
    assert await platform_status(world, removed) == "paused_owner"
    await describe_services(world, recovery_worker(world, after=timedelta(hours=3), dry_run=False), "Маникюр\nПедикюр")
    assert await platform_status(world, removed) == "active"
    assert any(row.reason_code == "SERVICE_RETURNED" for row in world.repository.rows(ChangeLog))


async def test_stop_after_approval_before_enable_changes_target_to_pause(tmp_path: Path) -> None:
    """(г) Площадка уже одобрила и выключила кампанию (ACCEPTED/OFF), а шаг модерации её ещё не включил: «Остановить»
    меняет и эту цель — кампания на паузе владельца, и шаг модерации её не включает."""
    from app.services.budget_guard import stop_all

    adapter = AcceptedOff()
    world, plan = await ready_with_services(tmp_path, ["Маникюр"], platforms=("direct",), adapters={"direct": adapter})
    await launch(world, plan)
    adapter.accept()
    approve(world)
    await stop_all(world.business, worker=recovery_worker(world, dry_run=False), actor="owner")
    (campaign,) = campaigns(world)
    assert campaign.status == "paused_owner"
    await settle(world)
    assert await platform_status(world, campaign) != "active" and campaign.review_since is None


async def test_service_removed_after_approval_before_enable_keeps_campaign_off(tmp_path: Path) -> None:
    """Услугу убрали, когда площадка уже одобрила и выключила её кампанию, а шаг модерации ещё не
    включил: цель — пауза владельца, и шаг модерации кампанию не включает."""
    adapter = AcceptedOff()
    world, plan = await ready_with_services(
        tmp_path, ["Маникюр", "Педикюр"], platforms=("direct",), adapters={"direct": adapter}
    )
    await launch(world, plan)
    removed = next(item for item in campaigns(world) if "Педикюр" in item.name)
    adapter.accept()
    approve(world)
    await describe_services(world, recovery_worker(world, after=timedelta(minutes=5), dry_run=False), "Маникюр")
    assert removed.status == "paused_owner"
    await settle(world)
    assert await platform_status(world, removed) != "active" and removed.review_since is None


async def test_continue_during_review_waits_for_approval_again(tmp_path: Path) -> None:
    """(г) «Продолжить», пока площадка ещё проверяет: включение применено и снова ждёт одобрения, бизнес работает,
    повторное «Продолжить» не нужно; после одобрения кампания включается."""
    from app.services.budget_guard import stop_all
    from app.services.screens import ScreensService

    adapter = DraftRefusesSuspend()
    world, plan = await ready_with_services(tmp_path, ["Маникюр"], platforms=("direct",), adapters={"direct": adapter})
    await launch(world, plan)
    worker = recovery_worker(world, after=timedelta(minutes=5), dry_run=False)
    await stop_all(world.business, worker=worker, actor="owner")
    await ScreensService(worker).resume(world.business)
    (campaign,) = campaigns(world)
    assert world.business.status == "running" and not pending_launches(world)
    assert campaign.status == "active" and campaign.review_since is not None
    adapter.accepted = True
    approve(world)
    await settle(world)
    assert await platform_status(world, campaign) == "active" and campaign.review_since is None


async def test_stop_in_cabinet_during_review_is_external_pause(tmp_path: Path) -> None:
    """(д) Владелец остановил кампанию или объявление в кабинете Директа (SUSPENDED), пока шла проверка: после неё
    это пауза владельца (EXTERNAL_PAUSE), а не «одобрено, но выключено» — ни шаг модерации, ни «Продолжить» её не
    включают."""
    adapter = AcceptedOff()
    world, plan = await ready_with_services(
        tmp_path, ["Маникюр", "Педикюр"], platforms=("direct",), adapters={"direct": adapter}
    )
    await launch(world, plan)
    first, second = sorted(campaigns(world), key=lambda item: item.id)
    ad = ads_of(world)[-1]
    adapter.accept()
    approve(world)
    adapter._states[first.external_id] = replace(adapter._states[first.external_id], status="paused_owner")
    adapter._states[ad.external_id] = replace(adapter._states[ad.external_id], status="paused_owner")
    await settle(world)
    assert await platform_status(world, first) == "paused_owner"
    assert first.status == "paused_owner" and first.owner_paused and first.review_since is None
    assert ad.status == "paused_owner"
    outside = [row for row in world.repository.rows(ChangeLog) if row.reason_code.startswith("EXTERNAL_")]
    assert sorted((row.reason_code, row.actor) for row in outside) == [
        ("EXTERNAL_AD_PAUSE", "external"),
        ("EXTERNAL_PAUSE", "external"),
    ]
    await settle(world, hours=3)
    assert await platform_status(world, first) == "paused_owner"
    assert await platform_status(world, second) == "active"


async def test_budget_stop_during_review_and_new_month_resume(tmp_path: Path) -> None:
    """(е) Остановка по бюджету во время проверки применяется (черновик остановить нельзя — не сбой). Новый месяц
    включает кампанию обычным путём: включение снова ждёт одобрения, а после него кампания работает."""
    from app.services.budget_guard import stop_all

    adapter = DraftRefusesSuspend()
    world, plan = await ready_with_services(tmp_path, ["Маникюр"], platforms=("direct",), adapters={"direct": adapter})
    await launch(world, plan)
    worker = recovery_worker(world, after=timedelta(hours=7), dry_run=False)
    await stop_all(
        world.business,
        worker=worker,
        budget_stop=True,
        reason_code="BUDGET_STOP",
        reason_params={"spent": 0, "budget": world.business.month_budget_kop, "margin": 0},
    )
    (campaign,) = campaigns(world)
    assert world.business.status == "budget_stop" and campaign.status == "paused_auto"
    worker.remember(world.business, month=(TODAY.replace(day=1) - timedelta(days=1)).replace(day=1).isoformat())
    await worker.run("month_rollover", world.business.id, key="review-month")
    assert world.business.status == "running" and not pending_launches(world)
    (resume,) = [row for row in world.repository.rows(ChangeLog) if row.reason_code == "BUDGET_RESUME"]
    assert resume.status == "applied" and campaign.status == "active" and campaign.review_since is not None
    adapter.accepted = True
    approve(world)
    await settle(world, hours=8)
    assert await platform_status(world, campaign) == "active"


async def test_month_stop_during_review_is_not_undone_by_approval(tmp_path: Path) -> None:
    """(е) Остановка 1-го числа (выключено «продолжать») во время проверки: после одобрения кампания остаётся
    выключенной, «Продолжить» включает её."""
    from app.services.screens import ScreensService

    adapter = AcceptedOffRefusesSuspend()
    world, plan = await ready_with_services(tmp_path, ["Маникюр"], platforms=("direct",), adapters={"direct": adapter})
    await launch(world, plan)
    world.business.auto_continue = False
    worker = recovery_worker(world, after=timedelta(hours=7), dry_run=False)
    worker.remember(world.business, month=(TODAY.replace(day=1) - timedelta(days=1)).replace(day=1).isoformat())
    await worker.run("month_rollover", world.business.id, key="review-month-stop")
    (campaign,) = campaigns(world)
    assert world.business.status == "paused" and campaign.status == "paused_auto"
    adapter.accept()
    approve(world)
    await settle(world, hours=8)
    assert await platform_status(world, campaign) == "paused_auto"
    await ScreensService(recovery_worker(world, after=timedelta(hours=9), dry_run=False)).resume(world.business)
    assert await platform_status(world, campaign) == "active"


class ReviewMinimum(AcceptedOffRefusesSuspend, Extending):
    """Минимум площадки можно поднять после запуска: пересчёт лимитов тогда останавливает кампанию."""

    strict = False

    async def min_budgets(self) -> dict[str, int]:
        return {"day": 10**9, "period": 0} if self.strict else await super().min_budgets()


async def test_rebalance_counts_reviewed_campaign_as_working(tmp_path: Path) -> None:
    """(ж) Пересчёт лимитов во время проверки: кампания «ждёт одобрения» получает лимиты по плану и продление даты
    окончания, как работающая."""
    adapter = ReviewMinimum()
    world, plan = await ready_with_services(tmp_path, ["Маникюр"], platforms=("direct",), adapters={"direct": adapter})
    await launch(world, plan)
    (campaign,) = campaigns(world)
    adapter._states[campaign.external_id] = replace(adapter._states[campaign.external_id], daily_limit_kop=1)
    worker = recovery_worker(world, after=timedelta(hours=7), dry_run=False)
    await worker.run("rebalance_budget", world.business.id, key="review-rebalance")
    assert (await states(world, campaign))[campaign.external_id].daily_limit_kop > 1
    assert campaign.daily_limit_kop > 1 and adapter.extended == [campaign.external_id]


async def test_rebalance_pause_during_review_is_not_undone_by_approval(tmp_path: Path) -> None:
    """(ж) Пересчёт остановил кампанию на проверке (остатка меньше минимума площадки): цель — пауза, и после
    одобрения кампания не включается."""
    adapter = ReviewMinimum()
    world, plan = await ready_with_services(tmp_path, ["Маникюр"], platforms=("direct",), adapters={"direct": adapter})
    await launch(world, plan)
    (campaign,) = campaigns(world)
    adapter.strict = True
    worker = recovery_worker(world, after=timedelta(hours=7), dry_run=False)
    await worker.run("rebalance_budget", world.business.id, key="review-rebalance")
    assert campaign.status == "paused_auto"
    adapter.accept()
    approve(world)
    await settle(world, hours=8)
    assert await platform_status(world, campaign) == "paused_auto"


async def test_rebalance_pause_after_approval_before_enable_keeps_campaign_off(tmp_path: Path) -> None:
    """Площадка уже одобрила и выключила кампанию (ACCEPTED/OFF), шаг модерации её ещё не включил,
    а пересчёт лимитов останавливает её (остатка меньше минимума площадки): цель становится паузой, и шаг модерации
    кампанию не включает."""
    adapter = ReviewMinimum()
    world, plan = await ready_with_services(tmp_path, ["Маникюр"], platforms=("direct",), adapters={"direct": adapter})
    await launch(world, plan)
    (campaign,) = campaigns(world)
    adapter.accept()
    approve(world)
    adapter.strict = True
    await recovery_worker(world, after=timedelta(hours=1), dry_run=False).run(
        "rebalance_budget", world.business.id, key="approved-rebalance"
    )
    assert campaign.status == "paused_auto"
    await settle(world)
    assert await platform_status(world, campaign) == "paused_auto" and campaign.review_since is None
    assert not [row for row in settle_records(world) if row.kind == "resume"]


async def test_rebalance_does_not_resume_settled_pause_of_campaign_with_nothing_to_show(tmp_path: Path) -> None:
    """Паузу «показывать нечего» (NO_ACTIVE_ADS) черновику после проверки довёл шаг модерации: пересчёт лимитов судит
    по исходной паузе и кампанию не включает — объявления включает владелец."""
    from app.services.optimize import NO_ACTIVE_ADS

    adapter = ReviewMinimum()
    world, plan = await ready_with_services(tmp_path, ["Маникюр"], platforms=("direct",), adapters={"direct": adapter})
    await launch(world, plan)
    (campaign,) = campaigns(world)
    worker = recovery_worker(world, after=timedelta(hours=1), dry_run=False)
    await worker.apply(
        world.business,
        campaign,
        [Operation("set_status", "campaign", campaign.external_id, "status", "draft", "paused_auto")],
        actor="system",
        kind="pause",
        reason_code="BUDGET_REBALANCE",
        reason_text=NO_ACTIVE_ADS,
        suffix="nothing",
    )
    campaign.daily_limit_kop = 0
    adapter.accept()
    adapter._states[campaign.external_id] = replace(adapter._states[campaign.external_id], status="active")
    approve(world)
    await settle(world)
    assert settle_records(world) and await platform_status(world, campaign) == "paused_auto"
    await recovery_worker(world, after=timedelta(hours=3), dry_run=False).run(
        "rebalance_budget", world.business.id, key="settled-rebalance"
    )
    assert await platform_status(world, campaign) == "paused_auto"


class ReviewMinimumAdsOff(ReviewMinimum):
    """После одобрения Директ показывает выключенными (OFF) и объявления остановленной кампании."""

    def accept(self, rejected: str | None = None) -> None:
        super().accept(rejected)
        for key, state in self._states.items():
            if state.entity_type == "ad":
                self._states[key] = replace(state, status="paused_auto")


async def test_approved_ads_of_paused_campaign_stay_off_and_review_ends(tmp_path: Path) -> None:
    """У кампании, чья цель — пауза, выключенные после проверки объявления — норма: шаг
    модерации их не включает и не ждёт, отметка проверки снимается."""
    adapter = ReviewMinimumAdsOff()
    world, plan = await ready_with_services(tmp_path, ["Маникюр"], platforms=("direct",), adapters={"direct": adapter})
    await launch(world, plan)
    (campaign,) = campaigns(world)
    adapter.strict = True
    await recovery_worker(world, after=timedelta(hours=7), dry_run=False).run(
        "rebalance_budget", world.business.id, key="review-rebalance"
    )
    assert campaign.status == "paused_auto" and world.business.status == "running"
    adapter.accept()
    approve(world)
    await settle(world, hours=8)
    found = await states(world, campaign)
    assert {state.status for state in found.values() if state.entity_type in {"campaign", "ad"}} == {"paused_auto"}
    assert campaign.review_since is None and not settle_records(world)


class AbandonedRefusesSuspend(CrashOnSecondAcceptedOff):
    """Брошенный посреди включения запуск; черновик Директ остановить не даёт, а после одобрения может включить."""

    async def apply(self, operation) -> None:
        paused = operation.after in {"paused_owner", "paused_auto"}
        if not self.accepted and operation.entity_type == "campaign" and paused:
            raise PlatformError("Кампания-черновик не может быть остановлена.")
        await super().apply(operation)


async def test_abandoned_launch_during_review_is_stopped_and_not_enabled(tmp_path: Path) -> None:
    """(з) Запуск брошен посреди включения, первая кампания уже «ждёт одобрения»: воркер останавливает кампании
    плана, в том числе те, что на проверке (отказ остановить черновик — не сбой); после одобрения они не
    включаются, а включённую площадкой — останавливает."""
    adapter = AbandonedRefusesSuspend()
    world, plan = await ready_with_services(
        tmp_path, ["Маникюр", "Педикюр"], platforms=("direct",), adapters={"direct": adapter}
    )
    with pytest.raises(asyncio.CancelledError):
        await launch(world, plan)
    first, second = sorted(campaigns(world), key=lambda item: item.id)
    assert next(row for row in launch_records(world) if row.status == "applied").entity_id == first.external_id
    world.business.status = "draft"
    await recovery_worker(world, after=timedelta(hours=1)).run_business_due(world.business.id)
    assert world.repository.latest_plan(world.business.id).status == "failed"
    assert first.status == second.status == "paused_auto" and first.review_since is not None
    adapter.accept()
    adapter._states[first.external_id] = replace(adapter._states[first.external_id], status="active")
    await recovery_worker(world, after=timedelta(hours=2)).run_business_due(world.business.id)
    assert await platform_status(world, first) == "paused_auto"
    assert await platform_status(world, second) == "paused_auto"
    assert first.review_since is None


class SecondResumeFails(FakeAdapter):
    fail: str | None = None

    async def apply(self, operation) -> None:
        if operation.entity_id == self.fail and operation.after == "active":
            raise PlatformError("Директ временно недоступен.", status_code=503)
        await super().apply(operation)


async def test_continue_failed_on_second_campaign_resumes_the_rest_on_next_press(tmp_path: Path) -> None:
    """«Продолжить» после защиты бюджета, площадка не включила вторую кампанию: первая работает, бизнес running,
    вторая остаётся на паузе защиты со своей причиной; повторное нажатие включает её, третье — «уже работает»."""
    from app.services.budget_guard import stop_all
    from app.services.screens import ScreensService

    adapter = SecondResumeFails()
    world, plan = await ready_with_services(
        tmp_path, ["Маникюр", "Педикюр"], platforms=("direct",), adapters={"direct": adapter}
    )
    await launch(world, plan)
    worker = recovery_worker(world, after=timedelta(minutes=5), dry_run=False)
    await stop_all(
        world.business,
        worker=worker,
        reason_code="BUDGET_STOP",
        reason_params={"spent": 0, "budget": world.business.month_budget_kop, "margin": 0},
        budget_stop=True,
    )
    assert world.business.status == "budget_stop"
    first, second = sorted(campaigns(world), key=lambda item: item.id)
    adapter.fail = second.external_id
    result = await ScreensService(worker).resume(world.business)
    assert result["warning"] == "Часть кампаний не включилась. Нажмите «Продолжить рекламу» ещё раз."
    assert world.business.status == "running" and not worker.metadata(world.business).get("kill_switch")
    assert await platform_status(world, first) == "active" and await platform_status(world, second) == "paused_auto"
    last = world.repository.last_status_change(world.business.id, "direct", "campaign", second.external_id)
    assert second.status == "paused_auto" and last.reason_code == "BUDGET_STOP"
    assert result["status"] == "running" and result["resume_pending"] is True
    adapter.fail = None
    result = await ScreensService(recovery_worker(world, after=timedelta(minutes=10), dry_run=False)).resume(
        world.business
    )
    assert result["warning"] is None and world.business.status == "running" and result["resume_pending"] is False
    assert await platform_status(world, first) == await platform_status(world, second) == "active"
    with pytest.raises(ChangeConflict, match="Реклама уже работает"):
        await ScreensService(recovery_worker(world, after=timedelta(minutes=15), dry_run=False)).resume(world.business)


class ResumeUnanswered(FakeAdapter):
    """Площадка включила кампанию, но следующее чтение не ответило: исход «Продолжить» неизвестен."""

    blind = False
    silent = False

    async def apply(self, operation) -> None:
        await super().apply(operation)
        if self.blind and operation.entity_type == "campaign" and operation.after == "active":
            self.blind, self.silent = False, True

    async def get_state(self, refs):
        if self.silent:
            self.silent = False
            raise PlatformError("Таймаут.", status_code=503)
        return await super().get_state(refs)


async def test_continue_with_unconfirmed_enable_is_not_an_error(tmp_path: Path) -> None:
    """Включение после «Продолжить» осталось pending: это не сбой — бизнес работает, остановка снята, а включение
    доводит сверка; повторное нажатие отвечает «уже работает»."""
    from app.services.budget_guard import stop_all
    from app.services.screens import ScreensService

    adapter = ResumeUnanswered()
    world, plan = await ready_with_services(tmp_path, ["Маникюр"], platforms=("direct",), adapters={"direct": adapter})
    await launch(world, plan)
    worker = recovery_worker(world, after=timedelta(minutes=5), dry_run=False)
    await stop_all(world.business, worker=worker, actor="owner")
    adapter.blind = True
    result = await ScreensService(worker).resume(world.business)
    assert result["warning"] is None and result["resume_pending"] is False
    assert world.business.status == "running" and not worker.metadata(world.business).get("kill_switch")
    (record,) = pending_launches(world)
    assert record.reason_code == "OWNER_RESUME"
    with pytest.raises(ChangeConflict, match="Реклама уже работает"):
        await ScreensService(recovery_worker(world, after=timedelta(minutes=7), dry_run=False)).resume(world.business)
    await recovery_worker(world, after=timedelta(minutes=10)).run_business_due(world.business.id)
    (campaign,) = campaigns(world)
    assert record.status == "applied" and campaign.status == "active"


class Deleted(FakeAdapter):
    """Владелец удалил кампанию в кабинете площадки: её состояния площадка больше не отдаёт."""

    gone: str | None = None

    async def get_state(self, refs):
        found = await super().get_state(refs)
        return {key: state for key, state in found.items() if key != self.gone}


def not_found(world: World) -> list[WorkerRun]:
    return [row for row in notices(world) if row.log[0]["reason_code"] == "CAMPAIGN_NOT_FOUND"]


async def test_continue_skips_campaign_deleted_in_cabinet(tmp_path: Path) -> None:
    """Кампанию удалили в кабинете, пока реклама стояла: «Продолжить» не падает, включает остальные,
    владелец получает одно уведомление, а удалённая кампания не держит кнопку «Продолжить» у работающей рекламы."""
    from app.services.budget_guard import stop_all
    from app.services.screens import ScreensService

    adapter = Deleted()
    world, plan = await ready_with_services(
        tmp_path, ["Маникюр", "Педикюр"], platforms=("direct",), adapters={"direct": adapter}
    )
    await launch(world, plan)
    first, second = sorted(campaigns(world), key=lambda item: item.id)
    await stop_all(world.business, worker=recovery_worker(world, dry_run=False), actor="owner")
    adapter.gone = second.external_id
    worker = recovery_worker(world, after=timedelta(hours=1), dry_run=False)
    result = await ScreensService(worker).resume(world.business)
    assert world.business.status == "running" and result["warning"] is None and not result["resume_pending"]
    assert await platform_status(world, first) == "active"
    with pytest.raises(ChangeConflict, match="Реклама уже работает"):
        await ScreensService(recovery_worker(world, after=timedelta(hours=2), dry_run=False)).resume(world.business)
    assert len(not_found(world)) == 1


async def test_stop_platform_counts_campaign_deleted_in_cabinet_as_stopped(tmp_path: Path) -> None:
    """Удалённая в кабинете кампания не делает отзыв кабинета неудачным: тратить ей нечем."""
    from time import monotonic

    from app.services.budget_guard import stop_platform

    adapter = Deleted()
    world, plan = await ready_with_services(
        tmp_path, ["Маникюр", "Педикюр"], platforms=("direct",), adapters={"direct": adapter}
    )
    await launch(world, plan)
    first, second = sorted(campaigns(world), key=lambda item: item.id)
    adapter.gone = second.external_id
    worker = recovery_worker(world, after=timedelta(hours=1), dry_run=False)
    worker.step_key = "owner:revoke"
    deadline = monotonic() + 60
    stopped = await stop_platform(
        world.business, worker=worker, platform="direct", reason_text="Остановили.", deadline=deadline
    )
    assert stopped is True and await platform_status(world, first) == "paused_owner"


async def test_campaign_returned_to_cabinet_can_be_continued_again(tmp_path: Path) -> None:
    """Кампания пропала в кабинете, затем вернулась и стоит остановленной нами: она выходит из
    missing_campaigns, кнопка «Продолжить» снова есть и включает её."""
    from app.services.budget_guard import campaign_state, stop_all
    from app.services.screens import ScreensService

    adapter = Deleted()
    world, plan = await ready_with_services(
        tmp_path, ["Маникюр", "Педикюр"], platforms=("direct",), adapters={"direct": adapter}
    )
    await launch(world, plan)
    first, second = sorted(campaigns(world), key=lambda item: item.id)
    await stop_all(world.business, worker=recovery_worker(world, dry_run=False), actor="owner")
    adapter.gone = second.external_id
    worker = recovery_worker(world, after=timedelta(hours=1), dry_run=False)
    await ScreensService(worker).resume(world.business)
    assert worker.metadata(world.business)["missing_campaigns"] == [second.id]
    adapter.gone = None
    worker = recovery_worker(world, after=timedelta(hours=2), dry_run=False)
    state = await campaign_state(worker, world.business, second)
    assert state is not None and state.status in {"paused_owner", "paused_auto"}
    assert worker.metadata(world.business)["missing_campaigns"] == []
    assert ScreensService(worker).resume_left(world.business, set()) is True
    await ScreensService(worker).resume(world.business)
    assert await platform_status(world, second) == "active"


async def test_month_rollover_and_rebalance_skip_campaign_deleted_in_cabinet(tmp_path: Path) -> None:
    """Удалённая в кабинете кампания не роняет шаги воркера каждый тик: новый месяц возобновляет
    остальные, пересчёт лимитов и сверка статусов идут дальше, уведомление одно."""
    from app.services.budget_guard import stop_all
    from app.services.month import month_rollover
    from app.services.optimize import rebalance_budget
    from app.services.sync import sync_campaign_status

    adapter = Deleted()
    world, plan = await ready_with_services(
        tmp_path, ["Маникюр", "Педикюр"], platforms=("direct",), adapters={"direct": adapter}
    )
    await launch(world, plan)
    first, second = sorted(campaigns(world), key=lambda item: item.id)
    worker = recovery_worker(world, after=timedelta(hours=1), dry_run=False)
    await stop_all(
        world.business,
        worker=worker,
        budget_stop=True,
        reason_code="BUDGET_STOP",
        reason_params={"spent": 0, "budget": world.business.month_budget_kop, "margin": 0},
    )
    adapter.gone = second.external_id
    worker.remember(world.business, month=(TODAY.replace(day=1) - timedelta(days=1)).replace(day=1).isoformat())
    await month_rollover(worker, world.business)
    assert world.business.status == "running" and await platform_status(world, first) == "active"
    worker = recovery_worker(world, after=timedelta(hours=2), dry_run=False)
    await rebalance_budget(worker, world.business)
    await sync_campaign_status(worker, world.business)
    assert len(not_found(world)) == 1


class DraftAd(SlowModeration):
    """Черновик объявления: включение только отправляет его на проверку (Директ — moderate)."""

    async def apply(self, operation) -> None:
        state = self._states.get(operation.entity_id)
        if operation.entity_type == "ad" and operation.after == "active" and state and state.status == "draft":
            raise AwaitingModeration(operation.entity_id)
        await super().apply(operation)


async def test_settle_that_only_sends_draft_to_review_says_so_and_is_written_once(tmp_path: Path) -> None:
    """Шаг модерации отправил черновик объявления на проверку — запись не «включили рекламу», а честно
    «отправили на проверку»; повтор шага в том же окне проверки вторую запись не пишет."""
    from app.services.sync import REVIEW_SENT

    adapter = DraftAd()
    world, plan = await ready_with_services(tmp_path, ["Маникюр"], platforms=("direct",), adapters={"direct": adapter})
    await launch(world, plan)
    ad = ads_of(world)[0]
    ad.status = "moderation"
    adapter._states[ad.external_id] = replace(adapter._states[ad.external_id], status="draft")
    await settle(world)
    await settle(world, hours=2.5)
    (record,) = settle_records(world)
    assert record.kind == "resume" and record.reason_text == REVIEW_SENT
    assert record.evidence["_service"]["review"] == [ad.external_id]


async def test_owner_pause_from_cabinet_survives_conflict_of_next_settle_record(tmp_path: Path, monkeypatch) -> None:
    """Пауза в кабинете записана (EXTERNAL_PAUSE), а следующая запись шага модерации упала в конфликт:
    отметка «остановил владелец» уже сохранена."""
    adapter = AcceptedOff()
    world, plan = await ready_with_services(tmp_path, ["Маникюр"], platforms=("direct",), adapters={"direct": adapter})
    await launch(world, plan)
    (campaign,) = campaigns(world)
    first, second = ads_of(world)
    adapter.accept()
    approve(world)
    adapter._states[campaign.external_id] = replace(adapter._states[campaign.external_id], status="paused_owner")
    adapter._states[second.external_id] = replace(adapter._states[second.external_id], status="active")
    second.status = "paused_auto"
    from app.services.sync import settle_review

    worker = recovery_worker(world, after=timedelta(hours=2), dry_run=False)
    saved: list[bool] = []
    save = worker.repository.save

    def remember_owner_paused(*items):
        saved.extend(item.owner_paused for item in items if item is campaign)
        save(*items)

    monkeypatch.setattr(worker.repository, "save", remember_owner_paused)
    apply = worker.apply

    async def conflict_after_first(*args, **kwargs):
        if kwargs.get("reason_code") == "MODERATION_SETTLE":
            raise ChangeConflict("Площадка занята.")
        return await apply(*args, **kwargs)

    monkeypatch.setattr(worker, "apply", conflict_after_first)
    await settle_review(worker, world.business)
    assert True in saved


async def test_continue_with_campaigns_stopped_in_cabinet_explains_why(tmp_path: Path) -> None:
    """«Продолжить» не включило ни одной кампании — все остановлены в кабинете площадки: владелец видит,
    почему, а не молчаливый ответ."""
    from app.services.budget_guard import stop_all
    from app.services.screens import ScreensService

    world, plan = await ready_with_services(tmp_path, ["Маникюр"], platforms=("direct",))
    await launch(world, plan)
    (campaign,) = campaigns(world)
    await stop_all(world.business, worker=recovery_worker(world, dry_run=False), actor="owner")
    campaign.owner_paused = True
    result = await ScreensService(recovery_worker(world, after=timedelta(hours=1), dry_run=False)).resume(
        world.business
    )
    assert world.business.status == "paused"
    assert result["warning"] == (
        "Реклама не включилась: кампании остановлены или удалены в кабинете площадки. "
        "Включите их там и нажмите «Продолжить рекламу» ещё раз."
    )


class AcceptedOffWithoutMoney(AcceptedOff):
    """Проверка закончена, но на счёте нет денег: Директ принимает resume, а кампания остаётся выключенной (OFF)."""

    async def apply(self, operation) -> None:
        if self.accepted and operation.entity_type == "campaign" and operation.after == "active":
            return
        await super().apply(operation)


async def test_approved_campaign_left_off_by_platform_leaves_review(tmp_path: Path) -> None:
    """Кампания одобрена, но площадка оставила её выключенной (нет денег на счёте): включение после проверки не
    повторяется каждый час, отметка проверки снимается — кампанию сверяет sync_campaign_status, а в журнале не остаётся
    «включили рекламу» у неудавшегося включения."""
    from app.services.sync import REVIEW_RESUMED, sync_campaign_status

    adapter = AcceptedOffWithoutMoney()
    world, plan = await ready_with_services(tmp_path, ["Маникюр"], platforms=("direct",), adapters={"direct": adapter})
    await launch(world, plan)
    (campaign,) = campaigns(world)
    adapter.accept()
    approve(world)
    await settle(world)
    await settle(world, hours=3)
    (resume,) = [row for row in settle_records(world) if row.kind == "resume"]
    assert resume.status == "failed" and resume.reason_text != REVIEW_RESUMED
    assert campaign.review_since is None
    await sync_campaign_status(recovery_worker(world, after=timedelta(hours=4), dry_run=False), world.business)
    assert [row.reason_code for row in world.repository.rows(ChangeLog) if row.actor == "external"] == [
        "EXTERNAL_PAUSE"
    ]


async def test_revert_of_pause_made_during_review_enables_after_approval(tmp_path: Path) -> None:
    """Откат паузы, сделанной, пока площадка проверяла кампанию-черновик: вернуть «черновик» нельзя, цель отката —
    работающая кампания, и включение ждёт одобрения."""
    from app.services.rollback import RollbackService

    adapter = AcceptedOffRefusesSuspend()
    world, plan = await ready_with_services(tmp_path, ["Маникюр"], platforms=("direct",), adapters={"direct": adapter})
    await launch(world, plan)
    (campaign,) = campaigns(world)
    worker = recovery_worker(world, after=timedelta(minutes=5), dry_run=False)
    worker.step_key = "owner"
    pause = await worker.apply(
        world.business,
        campaign,
        [Operation("set_status", "campaign", campaign.external_id, "status", "active", "paused_owner")],
        actor="owner",
        kind="pause",
        reason_code="OWNER_PAUSE",
        reason_text="Вы остановили кампанию.",
    )
    assert pause.status == "applied" and campaign.status == "paused_owner"
    revert = await RollbackService(worker.repository, dry_run=False).revert(
        pause.id, business_id=world.business.id, adapter=adapter, now=worker.clock.now
    )
    assert revert.status == "applied"
    assert campaign.status == "active" and campaign.review_since is not None
    adapter.accept()
    approve(world)
    await settle(world)
    assert await platform_status(world, campaign) == "active" and campaign.review_since is None


async def test_pause_of_draft_enabled_by_platform_is_settled_by_budget_guard(tmp_path: Path) -> None:
    """Остановка черновика на проверке записана целью; если площадка после одобрения включила кампанию сама, страж
    бюджета (раз в 30 минут) доводит паузу, не дожидаясь часового шага модерации, и запись не дублируется."""
    from app.services.budget_guard import stop_all

    adapter = AcceptedOffRefusesSuspend()
    world, plan = await ready_with_services(tmp_path, ["Маникюр"], platforms=("direct",), adapters={"direct": adapter})
    await launch(world, plan)
    (campaign,) = campaigns(world)
    await stop_all(
        world.business, worker=recovery_worker(world, after=timedelta(minutes=5), dry_run=False), actor="owner"
    )
    assert campaign.status == "paused_owner" and campaign.review_since is not None
    adapter.accept()
    adapter._states[campaign.external_id] = replace(adapter._states[campaign.external_id], status="active")
    await recovery_worker(world, after=timedelta(minutes=40), dry_run=False).run(
        "budget_guard", world.business.id, key="draft-pause-guard"
    )
    assert await platform_status(world, campaign) != "active" and campaign.review_since is None
    await settle(world)
    assert [row.kind for row in settle_records(world)] == ["pause"]


class DraftFailsSecondCreate(DraftCampaign):
    """Первая кампания создана черновиком, создание второй упало; остановить черновик Директ не даёт."""

    attempts = 0

    async def create_campaign_tree(self, plan_part):
        self.attempts += 1
        if self.attempts == 2:
            raise PlatformError("Директ не ответил.", status_code=503)
        return await super().create_campaign_tree(plan_part)


async def test_failed_launch_counts_draft_campaign_as_stopped(tmp_path: Path) -> None:
    """Запуск не удался, а созданная кампания осталась черновиком: остановить его площадка не даёт, но он и так не
    показывается — владелец не видит ложного «не удалось остановить»."""
    world, plan = await ready_with_services(
        tmp_path, ["Маникюр", "Педикюр"], platforms=("direct",), adapters={"direct": DraftFailsSecondCreate()}
    )
    with pytest.raises(LaunchFailed) as error:
        await launch(world, plan)
    assert launch_module.NOT_STOPPED not in str(error.value)
    (campaign,) = [item for item in campaigns(world) if item.external_id]
    assert await platform_status(world, campaign) == "draft" and campaign.status == "paused_auto"


async def test_rebalance_restores_limits_of_campaign_back_from_zero_daily_limit(tmp_path: Path) -> None:
    """Площадка выключила кампанию (на счёте нет денег): это пауза вне автопилота, пересчёт обнуляет её дневной лимит.
    Счёт пополнили — кампания снова работает, и следующий пересчёт возвращает лимиты одной применённой записью, не выше
    запусковых."""
    adapter = FakeAdapter()
    world, plan = await ready_with_services(tmp_path, ["Маникюр"], platforms=("direct",), adapters={"direct": adapter})
    await launch(world, plan)
    (campaign,) = campaigns(world)
    launched = campaign.daily_limit_kop, campaign.period_cap_kop
    adapter._states[campaign.external_id] = replace(adapter._states[campaign.external_id], status="paused_auto")
    await recovery_worker(world, after=timedelta(hours=7), dry_run=False).run(
        "rebalance_budget", world.business.id, key="off-by-platform"
    )
    assert campaign.daily_limit_kop == 0
    adapter._states[campaign.external_id] = replace(adapter._states[campaign.external_id], status="active")
    await recovery_worker(world, after=timedelta(hours=31), dry_run=False).run(
        "rebalance_budget", world.business.id, key="on-again"
    )
    state = (await states(world, campaign))[campaign.external_id]
    assert campaign.status == "active" and 0 < campaign.daily_limit_kop == state.daily_limit_kop
    assert campaign.period_cap_kop == state.period_cap_kop <= launched[1]
    (restored,) = [row for row in world.repository.rows(ChangeLog) if row.reason_code == "BUDGET_REBALANCE"]
    assert restored.status == "applied"
    assert {op["field"] for op in restored.operations} == {"daily_limit_kop", "period_cap_kop"}
