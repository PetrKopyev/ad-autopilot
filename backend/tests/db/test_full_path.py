import itertools
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import func, select

from app.content.art import ArtBudget
from app.content.site_reader import SiteReadResult
from app.db.models import Ad, AdGroup, Business, Campaign, ChangeLog, Plan, PlatformAccount, User, WorkerRun
from app.platforms.base import Operation
from app.platforms.fake import FakeAdapter, VirtualClock
from app.platforms.metrica import FakeMetrica, MetricaCounter, MetricaGoal
from app.services.changes import ChangeConflict
from app.services.launch import LaunchRepository, LaunchService
from app.services.onboarding import Blocked, OnboardingService
from app.services.plan import PlanService
from app.services.rollback import RollbackService
from app.worker.jobs import Worker, WorkerRepository
from tests.services.test_onboarding import (
    BUDGET_KOP,
    COUNTER,
    LIMITS,
    NOW,
    SITE,
    TODAY,
    ScriptedLLM,
    VariedArt,
    font_path,
    onboard,
    page_text,
)

pytestmark = pytest.mark.db
STEPS = ("month_rollover", "budget_guard", "sync_moderation", "sync_stats", "optimize", "rebalance_budget")


def count(db, model, **filters) -> int:
    return db.scalar(select(func.count()).select_from(model).filter_by(**filters))


async def test_business_plan_launch_worker_and_rollback_on_postgres(db_session, tmp_path: Path) -> None:
    db = db_session
    clock = VirtualClock(NOW)
    adapter = FakeAdapter(clock=clock)
    repository = LaunchRepository(db)
    llm = ScriptedLLM()
    user = User(yandex_uid="full-path-" + uuid4().hex, login="test", consent_at=NOW)
    repository.add(user)

    async def reader(url: str) -> SiteReadResult:
        return SiteReadResult(page_text())

    metrica = FakeMetrica(
        [MetricaCounter(COUNTER, "Счётчик", "salon.example")], {COUNTER: [MetricaGoal(1, "Заявка", "action")]}
    )
    onboarding = OnboardingService(repository, llm, lambda business_id: metrica, LIMITS, reader=reader)
    created = onboarding.create_business(user.id, SITE, "Самара", BUDGET_KOP, None)
    business = db.get(Business, created["id"])
    repository.add(PlatformAccount(business_id=business.id, platform="direct", auth_mode="oauth", access_token_enc="t"))
    await onboard(SimpleNamespace(onboarding=onboarding, business=business), confirmed={"direct": True})

    def art_factory(budget: ArtBudget, record) -> VariedArt:
        return VariedArt(b"", budget, record=record)

    plans = PlanService(
        repository, llm, art_factory, tmp_path, LIMITS, font_path=font_path(), seeds=itertools.count(1).__next__
    )
    plan = await plans.get_plan(business.id, NOW, TODAY)
    assert (await plans.get_plan(business.id, NOW, TODAY))["id"] == plan["id"]

    launches = LaunchService(repository, lambda business_id, platform: adapter, tmp_path, font_path=font_path())
    result = await launches.launch(business.id, plan["id"], True, NOW, TODAY)
    assert result["status"] == "launched" and business.status == "running"
    assert await launches.launch(business.id, plan["id"], True, NOW, TODAY) == result
    assert len(adapter._trees) == 1 and count(db, ChangeLog, business_id=business.id) == 1
    campaign = db.scalar(select(Campaign).where(Campaign.business_id == business.id))
    assert campaign.platform == "direct" and campaign.status == "active"

    worker = Worker(WorkerRepository(db), lambda business_id, platform: adapter, clock=clock, dry_run=False)
    clock.advance(days=2)
    for _ in range(2):
        for kind in STEPS:
            await worker.run(kind, business.id)
    runs = db.scalars(select(WorkerRun).where(WorkerRun.business_id == business.id)).all()
    assert sorted(run.kind for run in runs if run.slot_key) == sorted(STEPS)
    assert all(run.status == "ok" for run in runs if run.kind in STEPS)

    ad = db.scalar(select(Ad).join(AdGroup).where(AdGroup.campaign_id == campaign.id).order_by(Ad.id).limit(1))
    worker.step_key = "optimize:full-path"
    pause = [Operation("set_status", "ad", ad.external_id, "status", "active", "paused_auto")]
    options = {"reason_code": "CUSTOM", "reason_text": "Тест.", "kind": "pause", "suffix": str(ad.id)}
    change = await worker.apply(business, campaign, pause, entity_type="ad", entity_id=ad.external_id, **options)
    assert change.status == "applied" and change.idempotency_key == f"optimize:full-path:{campaign.id}:{ad.id}"
    assert await worker.apply(business, campaign, pause, **options) is change
    assert (await adapter.get_state(worker.refs(campaign)))[ad.external_id].status == "paused_auto"

    revert = await RollbackService(repository, dry_run=False).revert(
        change.id, business_id=business.id, adapter=adapter, now=clock.now + timedelta(minutes=1)
    )
    db.refresh(change)
    assert (revert.status, change.status) == ("applied", "reverted")
    assert (await adapter.get_state(worker.refs(campaign)))[ad.external_id].status == "active"

    # Неудачная попытка слота освобождает ключ: повтор пишет новую строку без нарушения UNIQUE.
    second = db.scalars(select(Ad).join(AdGroup).where(AdGroup.campaign_id == campaign.id).order_by(Ad.id)).all()[1]
    worker.step_key = "budget_guard:full-path"
    key = f"budget_guard:full-path:{campaign.id}:{second.id}"
    failed = ChangeLog(
        business_id=business.id,
        actor="system",
        kind="pause",
        platform="direct",
        entity_type="ad",
        entity_id=second.external_id,
        operations=[],
        reason_code="CUSTOM",
        reason_text="Тест.",
        evidence={"worker_key": key},
        status="failed",
        idempotency_key=key,
    )
    repository.add(failed)
    stop = [Operation("set_status", "ad", second.external_id, "status", "active", "paused_auto")]
    options = {"reason_code": "CUSTOM", "reason_text": "Тест.", "kind": "pause", "suffix": str(second.id)}
    retried = await worker.apply(business, campaign, stop, actor="system", **options)
    db.refresh(failed)
    assert (failed.idempotency_key, retried.idempotency_key, retried.status) == (None, key, "applied")
    retried.status = "pending"
    repository.commit()
    with pytest.raises(ChangeConflict):
        await worker.apply(business, campaign, stop, actor="system", **options)


class Crash(BaseException):
    """Процесс API упал между включением кампании и finish."""


async def test_abandoned_launch_is_stopped_by_worker_on_postgres(db_session, tmp_path: Path, monkeypatch) -> None:
    db = db_session
    clock = VirtualClock(NOW)
    adapter = FakeAdapter(clock=clock)
    repository = LaunchRepository(db)
    llm = ScriptedLLM()
    user = User(yandex_uid="abandoned-" + uuid4().hex, login="test", consent_at=NOW)
    repository.add(user)

    async def reader(url: str) -> SiteReadResult:
        return SiteReadResult(page_text())

    metrica = FakeMetrica(
        [MetricaCounter(COUNTER, "Счётчик", "salon.example")], {COUNTER: [MetricaGoal(1, "Заявка", "action")]}
    )
    onboarding = OnboardingService(repository, llm, lambda business_id: metrica, LIMITS, reader=reader)
    business = db.get(Business, onboarding.create_business(user.id, SITE, "Самара", BUDGET_KOP, None)["id"])
    repository.add(PlatformAccount(business_id=business.id, platform="direct", auth_mode="oauth", access_token_enc="t"))
    await onboard(SimpleNamespace(onboarding=onboarding, business=business), confirmed={"direct": True})
    plans = PlanService(
        repository,
        llm,
        lambda budget, record: VariedArt(b"", budget, record=record),
        tmp_path,
        LIMITS,
        font_path=font_path(),
        seeds=itertools.count(1).__next__,
    )
    plan = await plans.get_plan(business.id, NOW, TODAY)
    launches = LaunchService(repository, lambda business_id, platform: adapter, tmp_path, font_path=font_path())

    def crash(*args) -> None:
        raise Crash

    monkeypatch.setattr(launches, "finish", crash)
    with pytest.raises(Crash):
        await launches.launch(business.id, plan["id"], True, NOW, TODAY)
    monkeypatch.undo()
    campaign = db.scalar(select(Campaign).where(Campaign.business_id == business.id))
    worker = Worker(WorkerRepository(db), lambda business_id, platform: adapter, clock=clock, dry_run=True)
    assert (await adapter.get_state(worker.refs(campaign)))[campaign.external_id].status == "active"
    assert business.status == "plan_ready"

    # Новый бюджет — новый план и новая кампания: пока старая включена, план не пересобирается.
    business.month_budget_kop += 100_000
    repository.commit()
    with pytest.raises(Blocked, match="Прошлый запуск рекламы не завершился"):
        await plans.get_plan(business.id, NOW, TODAY)
    business.month_budget_kop -= 100_000
    repository.commit()

    clock.advance(hours=0.25)
    # Блокировку держит другое соединение — запуск ещё идёт: его кампанию не трогаем.
    with repository.business_lock(business.id):
        await worker.run_business_due(business.id)
    assert count(db, ChangeLog, business_id=business.id, reason_code="LAUNCH_INTERRUPTED") == 0

    for _ in range(2):
        await worker.run_business_due(business.id)
        clock.advance(hours=0.5)
    stored = db.scalar(select(Plan).where(Plan.id == plan["id"]).execution_options(populate_existing=True))
    assert stored.status == "failed" and stored.payload["launch"]["started_at"] is None
    assert (await adapter.get_state(worker.refs(campaign)))[campaign.external_id].status == "paused_auto"
    assert count(db, ChangeLog, business_id=business.id, reason_code="LAUNCH_INTERRUPTED", status="applied") == 1
    (notice,) = worker.repository.unread_notifications(business.id, {"LAUNCH_INTERRUPTED"}, 5, clock.now)
    assert notice.log[0]["text"] == stored.payload["launch"]["error"]
    assert count(db, WorkerRun, business_id=business.id, kind="notification") == 1

    result = await launches.launch(business.id, plan["id"], True, clock.now, TODAY)
    assert result["status"] == "launched" and business.status == "running" and len(adapter._trees) == 1
    assert (await adapter.get_state(worker.refs(campaign)))[campaign.external_id].status == "active"
    # Запуск состоялся: уведомление о прерванной попытке уже неправда, а кампания — кампания действующего плана.
    assert not worker.repository.unread_notifications(business.id, {"LAUNCH_INTERRUPTED"}, 5, clock.now)
    assert worker.current_campaigns(business) == [campaign]
