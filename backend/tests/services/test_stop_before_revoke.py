import asyncio
from contextlib import contextmanager, nullcontext
from datetime import UTC, datetime
from time import monotonic
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from app.db.models import Business, Campaign, ChangeLog, PlatformAccount, User
from app.platforms.base import EntityState
from app.services.changes import ChangeConflict
from tests.integration.harness import make_world


def later() -> float:
    return monotonic() + 60


async def test_stop_platform_leaves_campaigns_of_other_platform_running():
    from app.services.budget_guard import stop_platform

    world = await make_world()
    worker = world.worker()
    worker.step_key = "owner:revoke"
    assert (
        await stop_platform(world.business, worker=worker, platform="vk", reason_text="Отключили VK.", deadline=later())
        is True
    )
    assert world.campaign.status == "active"
    assert not world.changes(reason_code="ACCOUNT_REVOKE")


async def test_stop_platform_reports_change_that_was_not_applied(monkeypatch):
    from app.services.budget_guard import stop_platform

    world = await make_world()
    worker = world.worker()
    worker.step_key = "owner:revoke"
    monkeypatch.setattr(worker, "apply", AsyncMock(return_value=ChangeLog(status="pending")))
    assert (
        await stop_platform(
            world.business, worker=worker, platform="direct", reason_text="Остановили.", deadline=later()
        )
        is False
    )


@pytest.mark.parametrize("trial", ["worker", "business"])
async def test_stop_before_revoke_is_applied_in_trial_mode(trial):
    # Остановка перед отзывом защитная: в пробном режиме она не может остаться предложением, иначе реклама
    # продолжит тратить деньги без управления.
    from app.services.budget_guard import stop_platform

    world = await make_world(business_settings={"dry_run": True} if trial == "business" else None)
    worker = world.worker(dry_run=trial == "worker")
    worker.step_key = "owner:revoke"
    assert (
        await stop_platform(
            world.business, worker=worker, platform="direct", reason_text="Остановили.", deadline=later()
        )
        is True
    )
    (change,) = world.changes(reason_code="ACCOUNT_REVOKE")
    assert change.status == "applied" and not change.evidence["_service"]["dry_run"]
    assert world.campaign.status == "paused_owner"


def busy_lock(repository, times: int):
    """Блокировка бизнеса, которую первые times попыток держит шаг воркера."""
    attempts = []

    @contextmanager
    def lock(business_id):
        attempts.append(business_id)
        if len(attempts) <= times:
            raise ChangeConflict("Этот бизнес уже обрабатывается.")
        yield

    repository.business_lock = lock
    return attempts


async def test_stop_platform_waits_for_worker_step_without_blocking_event_loop():
    from app.services.budget_guard import stop_platform

    world = await make_world()
    worker = world.worker()
    worker.step_key = "owner:revoke"
    attempts = busy_lock(world.repository, times=2)
    ticks = []

    async def ticker():
        for _ in range(5):
            ticks.append(1)
            await asyncio.sleep(0.1)

    stopped, _ = await asyncio.gather(
        stop_platform(world.business, worker=worker, platform="direct", reason_text="Остановили.", deadline=later()),
        ticker(),
    )
    assert stopped is True and len(attempts) >= 3
    assert len(ticks) == 5
    assert world.campaign.status == "paused_owner"


async def test_stop_platform_gives_up_when_business_stays_busy():
    # Занятый бизнес — не «площадка не подтвердила»: отзывать доступ, не попытавшись остановить рекламу, нельзя,
    # и владелец получает «повторите через минуту».
    from app.services.budget_guard import stop_platform

    world = await make_world()
    worker = world.worker()
    worker.step_key = "owner:revoke"
    busy_lock(world.repository, times=1000)
    with pytest.raises(ChangeConflict):
        await stop_platform(
            world.business, worker=worker, platform="direct", reason_text="Стоп.", deadline=later(), wait=0.6
        )
    assert world.campaign.status == "active"


async def test_stop_platform_waits_for_business_only_until_deadline():
    from app.services.budget_guard import stop_platform

    world = await make_world()
    worker = world.worker()
    worker.step_key = "owner:revoke"
    attempts = busy_lock(world.repository, times=1000)
    started = monotonic()
    with pytest.raises(TimeoutError):
        await stop_platform(
            world.business, worker=worker, platform="direct", reason_text="Стоп.", deadline=started + 0.6, wait=15
        )
    # Не ждали все 15 секунд; запас — на нагруженную машину.
    assert monotonic() - started < 5
    with pytest.raises(TimeoutError):
        await stop_platform(
            world.business, worker=worker, platform="direct", reason_text="Стоп.", deadline=monotonic(), wait=15
        )
    assert len(attempts) <= 3
    assert world.campaign.status == "active"


async def test_stop_platform_waits_out_busy_server_then_answers_server_busy():
    # Пул блокировок занят (перемотки демо): соединения освобождаются за секунды, поэтому остановка перед отзывом
    # ждёт до wait, как и занятый бизнес. Не дождалась — ServerBusy (503 «сервер занят»), а не 409 про бизнес.
    from app.services.budget_guard import stop_platform
    from app.services.changes import ServerBusy

    world = await make_world()
    worker = world.worker()
    worker.step_key = "owner:revoke"
    attempts = []

    @contextmanager
    def lock(business_id):
        attempts.append(business_id)
        raise ServerBusy
        yield

    world.repository.business_lock = lock
    with pytest.raises(ServerBusy):
        await stop_platform(
            world.business, worker=worker, platform="direct", reason_text="Стоп.", deadline=later(), wait=0.6
        )
    assert len(attempts) >= 2 and world.campaign.status == "active"


class StubAdapter:
    """Площадка, которая помнит статусы кампаний и останавливает их по set_status."""

    def __init__(self, statuses: dict[str, str]) -> None:
        self.statuses = statuses

    async def get_state(self, refs):
        status = self.statuses[refs.campaign_id]
        return {refs.campaign_id: EntityState(refs.campaign_id, "campaign", None, "Автопилот", status)}

    async def apply(self, operation) -> None:
        self.statuses[operation.entity_id] = operation.after


@pytest.fixture
def owner(db_session):
    user = User(yandex_uid="stop-" + uuid4().hex, login="owner", consent_at=datetime.now(UTC))
    db_session.add(user)
    db_session.flush()

    def business(platform: str, status: str, campaign: str | None, campaign_status: str = "active"):
        item = Business(user_id=user.id, site_url="https://example.test", city="Москва", month_budget_kop=100_000)
        db_session.add(item)
        db_session.flush()
        account = PlatformAccount(
            business_id=item.id, platform=platform, auth_mode="oauth", access_token_enc="token", status=status
        )
        db_session.add(account)
        db_session.flush()
        if campaign:
            db_session.add(
                Campaign(
                    business_id=item.id,
                    platform_account_id=account.id,
                    platform=platform,
                    external_id=campaign,
                    type="unified",
                    name="Автопилот",
                    status=campaign_status,
                    daily_limit_kop=1000,
                    period_cap_kop=10_000,
                )
            )
        db_session.commit()
        return account

    return db_session, user, business


@pytest.mark.db
async def test_stop_user_ads_reports_account_that_needs_reauth_with_live_campaigns(owner):
    from app.api.auth import stop_user_ads

    db, user, business = owner
    assert await stop_user_ads(db, None, user.id, None, "Стоп.", later()) == []
    business("direct", "needs_reauth", "c-1")
    business("vk", "needs_reauth", "c-2", campaign_status="paused_owner")
    business("vk", "needs_reauth", None)
    problems = await stop_user_ads(db, None, user.id, None, "Стоп.", later())
    assert problems == ["Яндекс Директ: кабинет требует входа заново, без него мы не можем остановить кампании"]


@pytest.mark.db
async def test_stop_user_ads_stops_only_the_disconnected_account(owner):
    from app.api.auth import stop_user_ads

    db, user, business = owner
    first = business("direct", "ok", "c-1")
    business("direct", "ok", "c-2")
    adapter = StubAdapter({"c-1": "active", "c-2": "active"})
    assert await stop_user_ads(db, lambda *_: adapter, user.id, first.id, "Стоп.", later()) == []
    assert adapter.statuses == {"c-1": "paused_owner", "c-2": "active"}
    adapter.statuses["c-1"] = "active"
    assert await stop_user_ads(db, lambda *_: replace_unconfirmed(adapter), user.id, first.id, "Стоп.", later()) == [
        "Яндекс Директ: площадка не подтвердила остановку"
    ]


def replace_unconfirmed(adapter: StubAdapter) -> StubAdapter:
    """Площадка принимает запрос, но кампания остаётся работать."""
    stub = StubAdapter(adapter.statuses)
    stub.apply = AsyncMock()
    return stub


@pytest.mark.db
async def test_stop_user_ads_after_deadline_reports_timeout_and_leaves_campaigns(owner):
    from app.api.auth import stop_user_ads

    db, user, business = owner
    business("direct", "ok", "c-1")
    business("vk", "ok", "c-2")
    adapter = StubAdapter({"c-1": "active", "c-2": "active"})
    problems = await stop_user_ads(db, lambda *_: adapter, user.id, None, "Стоп.", monotonic() - 1)
    assert problems == [
        "Яндекс Директ: остановка не закончилась за минуту",
        "ВКонтакте: остановка не закончилась за минуту",
    ]
    assert adapter.statuses == {"c-1": "active", "c-2": "active"}


@pytest.mark.db
async def test_stop_user_ads_does_not_wait_for_busy_business_after_deadline(owner):
    from sqlalchemy import text

    from app.api.auth import stop_user_ads

    class SlowAdapter(StubAdapter):
        async def get_state(self, refs):
            # Срок выходит, пока останавливается первый кабинет: и под нагрузкой первый успевает начаться.
            await asyncio.sleep(max(0.0, deadline - monotonic()) + 0.1)
            return await super().get_state(refs)

    db, user, business = owner
    business("direct", "ok", "c-1")
    busy = business("vk", "ok", "c-2")
    adapter = SlowAdapter({"c-1": "active", "c-2": "active"})
    with db.get_bind().engine.connect() as step:
        step.execute(text("SELECT pg_advisory_lock(:id)"), {"id": busy.business_id})
        deadline = monotonic() + 2
        problems = await stop_user_ads(db, lambda *_: adapter, user.id, None, "Стоп.", deadline)
        step.execute(text("SELECT pg_advisory_unlock(:id)"), {"id": busy.business_id})
    # Блокировку занятого бизнеса после срока не ждут: иначе ещё LOCK_WAIT_SECONDS (15 с).
    assert monotonic() - deadline < 5
    assert problems == ["ВКонтакте: остановка не закончилась за минуту"]
    assert adapter.statuses == {"c-1": "paused_owner", "c-2": "active"}


@pytest.mark.db
async def test_stop_user_ads_after_deadline_does_not_blame_account_without_campaigns(owner):
    # Срок вышел на первом кабинете; у второго кампаний его площадки нет — «не закончилась за минуту» было бы неправдой.
    from app.api.auth import stop_user_ads

    db, user, business = owner
    business("direct", "ok", "c-1")
    business("vk", "ok", None)
    adapter = StubAdapter({"c-1": "active"})
    problems = await stop_user_ads(db, lambda *_: adapter, user.id, None, "Стоп.", monotonic() - 1)
    assert problems == ["Яндекс Директ: остановка не закончилась за минуту"]
    assert adapter.statuses == {"c-1": "active"}


async def test_ad_stopper_gives_the_whole_stop_one_minute(monkeypatch):
    from app.api import auth

    calls = []

    async def stop_user_ads(db, resolve, user_id, account_id, reason_text, deadline):
        calls.append(deadline - monotonic())
        return []

    monkeypatch.setattr(auth, "stop_user_ads", stop_user_ads)
    monkeypatch.setattr("app.db.session.SessionLocal", lambda: nullcontext())
    request = SimpleNamespace(
        app=SimpleNamespace(state=SimpleNamespace(platform_adapters=SimpleNamespace(resolve=None)))
    )
    assert await auth.get_ad_stopper(request)(1, None, "Стоп.") == []
    (left,) = calls
    assert auth.STOP_TIMEOUT == 60 and 59 < left <= 60


async def test_ad_stopper_does_not_cancel_stop_that_outlives_the_minute(monkeypatch):
    # Минута — срок начала остановок, а не обрыв: прерванная посреди остановка оставила бы изменение без ответа.
    from app.api import auth

    async def stop_user_ads(db, resolve, user_id, account_id, reason_text, deadline):
        await asyncio.sleep(0.3)
        return ["ВКонтакте: остановка не закончилась за минуту"]

    monkeypatch.setattr(auth, "STOP_TIMEOUT", 0.1)
    monkeypatch.setattr(auth, "stop_user_ads", stop_user_ads)
    monkeypatch.setattr("app.db.session.SessionLocal", lambda: nullcontext())
    request = SimpleNamespace(
        app=SimpleNamespace(state=SimpleNamespace(platform_adapters=SimpleNamespace(resolve=None)))
    )
    assert await auth.get_ad_stopper(request)(1, None, "Стоп.") == ["ВКонтакте: остановка не закончилась за минуту"]
