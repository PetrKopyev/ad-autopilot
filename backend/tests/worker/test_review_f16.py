from integration.harness import make_world

from app.db.models import WorkerRun
from tests.worker.test_guard_edges import with_vk, worker_for


async def test_scale_works_while_other_platform_account_is_disconnected() -> None:
    """Кабинет ВКонтакте отключён: рост бюджета хорошей группы Директа не спрашивает его расход и не падает."""
    world = await make_world()
    vk, _ = await with_vk(world)
    worker = worker_for(world, vk, vk_connected=[False])
    await world.advance(worker, days=20)
    assert world.changes(reason_code="SCALE_UP", status="applied")
    assert not [run for run in world.repository.rows(WorkerRun) if run.kind == "optimize" and run.status == "failed"]


async def test_budget_change_works_while_vk_account_is_disconnected(tmp_path) -> None:
    from app.services.screens import ScreensService
    from tests.services.test_launch import vk_disconnected_world

    world, worker = await vk_disconnected_world(tmp_path)
    amount = world.business.month_budget_kop + 1_000_000
    saved = await ScreensService(worker).save_budget(world.business, amount)
    assert saved["month_budget_kop"] == amount


async def test_month_spent_takes_larger_of_report_and_ledger_on_each_platform(tmp_path, monkeypatch) -> None:
    """Директ: свежий отчёт больше учёта; ВКонтакте отключён — только учёт. Расход — сумма больших по площадкам."""
    from unittest.mock import AsyncMock

    from app.db.models import BudgetLedger
    from app.services.screens import ScreensService
    from app.worker.clock import today
    from tests.services.test_launch import vk_disconnected_world

    world, worker = await vk_disconnected_world(tmp_path)
    month = today(worker.clock).replace(day=1)
    for platform, spent in (("direct", 100_000), ("vk", 50_000)):
        ledger = worker.repository.find(BudgetLedger, business_id=world.business.id, platform=platform, month=month)
        ledger = ledger or BudgetLedger(business_id=world.business.id, platform=platform, month=month)
        ledger.allocated_kop, ledger.period_cap_kop, ledger.spent_kop = 0, 0, spent
        worker.repository.save(ledger)
    monkeypatch.setattr("app.services.screens.read_spend", AsyncMock(return_value=({"direct": 120_000}, {})))
    assert await ScreensService(worker).month_spent(world.business) == 170_000


async def test_campaign_without_known_groups_still_resumes_when_service_returns(tmp_path) -> None:
    """Группы кампании в плане без услуги (ids пуст): «убрали все услуги» не срабатывает, и кампания, остановленная
    из-за убранной услуги, продолжает работу при её возврате."""
    from app.db.models import ChangeLog
    from tests.services.test_launch import (
        campaigns,
        describe_services,
        launch,
        ready_with_services,
        recovery_worker,
    )

    world, plan = await ready_with_services(tmp_path, ["Маникюр", "Педикюр"])
    await launch(world, plan)
    worker = recovery_worker(world, dry_run=True)
    await describe_services(world, worker, "Маникюр")
    stopped = next(item for item in campaigns(world) if "Педикюр" in item.name)
    assert stopped.status == "paused_owner"
    launched = world.repository.latest_plan(world.business.id)
    for group in launched.payload["groups"]:
        group.pop("service", None)
    await describe_services(world, worker, "Маникюр\nПедикюр")
    assert stopped.status == "active"
    assert [row for row in world.repository.rows(ChangeLog) if row.reason_code == "SERVICE_RETURNED"]


async def test_reconnect_does_not_resume_campaign_paused_by_platform(tmp_path) -> None:
    """Отключение кабинета ставит paused_owner. Если после этого площадка остановила кампанию сама (paused_auto),
    подключение кабинета её не включает."""
    from dataclasses import replace

    from app.services.budget_guard import resume_reconnected
    from tests.services.test_launch import campaigns, vk_disconnected_world

    world, worker = await vk_disconnected_world(tmp_path)
    direct = next(item for item in campaigns(world) if item.platform == "direct")
    states = world.adapters["direct"]._states
    states[direct.external_id] = replace(states[direct.external_id], status="paused_auto")
    worker.step_key = "owner:reconnect"
    await resume_reconnected(worker, world.business, "direct")
    assert states[direct.external_id].status == "paused_auto"
