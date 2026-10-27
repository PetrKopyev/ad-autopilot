import asyncio
from contextlib import contextmanager
from datetime import UTC, datetime
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from app.db.models import Business, Campaign, ChangeLog, PlatformAccount, User
from app.platforms.base import EntityState
from app.services.changes import ChangeConflict
from tests.integration.harness import make_world


async def test_stop_platform_leaves_campaigns_of_other_platform_running():
    from app.services.budget_guard import stop_platform

    world = await make_world()
    worker = world.worker()
    worker.step_key = "owner:revoke"
    assert await stop_platform(world.business, worker=worker, platform="vk", reason_text="Отключили VK.") is True
    assert world.campaign.status == "active"
    assert not world.changes(reason_code="ACCOUNT_REVOKE")


async def test_stop_platform_reports_change_that_was_not_applied(monkeypatch):
    from app.services.budget_guard import stop_platform

    world = await make_world()
    worker = world.worker()
    worker.step_key = "owner:revoke"
    monkeypatch.setattr(worker, "apply", AsyncMock(return_value=ChangeLog(status="pending")))
    assert await stop_platform(world.business, worker=worker, platform="direct", reason_text="Остановили.") is False


@pytest.mark.parametrize("trial", ["worker", "business"])
async def test_stop_before_revoke_is_applied_in_trial_mode(trial):
    # Остановка перед отзывом защитная: в пробном режиме она не может остаться предложением, иначе реклама
    # продолжит тратить деньги без управления.
    from app.services.budget_guard import stop_platform

    world = await make_world(business_settings={"dry_run": True} if trial == "business" else None)
    worker = world.worker(dry_run=trial == "worker")
    worker.step_key = "owner:revoke"
    assert await stop_platform(world.business, worker=worker, platform="direct", reason_text="Остановили.") is True
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
        stop_platform(world.business, worker=worker, platform="direct", reason_text="Остановили."), ticker()
    )
    assert stopped is True and len(attempts) >= 3
    assert len(ticks) == 5
    assert world.campaign.status == "paused_owner"


async def test_stop_platform_gives_up_when_business_stays_busy():
    from app.services.budget_guard import stop_platform

    world = await make_world()
    worker = world.worker()
    worker.step_key = "owner:revoke"
    busy_lock(world.repository, times=1000)
    assert await stop_platform(world.business, worker=worker, platform="direct", reason_text="Стоп.", wait=0.6) is False
    assert world.campaign.status == "active"


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
    assert await stop_user_ads(db, None, user.id, None, "Стоп.") == []
    business("direct", "needs_reauth", "c-1")
    business("vk", "needs_reauth", "c-2", campaign_status="paused_owner")
    business("vk", "needs_reauth", None)
    problems = await stop_user_ads(db, None, user.id, None, "Стоп.")
    assert problems == ["Яндекс Директ: кабинет требует входа заново, без него мы не можем остановить кампании"]


@pytest.mark.db
async def test_stop_user_ads_stops_only_the_disconnected_account(owner):
    from app.api.auth import stop_user_ads

    db, user, business = owner
    first = business("direct", "ok", "c-1")
    business("direct", "ok", "c-2")
    adapter = StubAdapter({"c-1": "active", "c-2": "active"})
    assert await stop_user_ads(db, lambda *_: adapter, user.id, first.id, "Стоп.") == []
    assert adapter.statuses == {"c-1": "paused_owner", "c-2": "active"}
    adapter.statuses["c-1"] = "active"
    assert await stop_user_ads(db, lambda *_: replace_unconfirmed(adapter), user.id, first.id, "Стоп.") == [
        "Яндекс Директ: площадка не подтвердила остановку"
    ]


def replace_unconfirmed(adapter: StubAdapter) -> StubAdapter:
    """Площадка принимает запрос, но кампания остаётся работать."""
    stub = StubAdapter(adapter.statuses)
    stub.apply = AsyncMock()
    return stub
