"""Шаг воркера со старой копией бизнеса не отменяет действия владельца.

Тик грузит бизнесы в начале (identity map держит копию весь тик — по слабым ссылкам,
поэтому список надо держать, как run_due). Пока шли шаги, владелец мог нажать
«Продолжить» (статус и kill_switch уже переписаны) или «Остановить», которую площадка
не подтвердила (kill_switch и pending_stop записаны, статус ещё running). Первый
remember шага с устаревшей копией перезаписывал settings целиком и стирал свежее
действие владельца. run() перечитывает бизнес под блокировкой, когда свежая строка
отличается от копии.
"""

import json
import os

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session

from app.db.models import Ad, AdGroup, Business, Campaign, ChangeLog
from app.platforms.base import EntityState, ModerationInfo
from app.worker.jobs import Worker, WorkerRepository
from tests.db.test_pause_race import NOW, AlreadyPaused, VirtualClock, committed_world, fresh_meta

pytestmark = pytest.mark.db

MONTH = "2026-03-01"


@pytest.fixture(autouse=True)
def cleanup_committed_world():
    """committed_world коммитит бизнес настоящим commit (второй сессии его иначе не видно) —
    подчищаем после каждого теста, savepoint-фикстура db_session их не откатит."""
    yield
    url = os.environ["TEST_DATABASE_URL"]
    engine = create_engine(url)
    try:
        with engine.begin() as conn:
            conn.execute(text("DELETE FROM businesses WHERE site_url = 'https://example.test'"))
            conn.execute(text("DELETE FROM users WHERE yandex_uid LIKE 'pause-race-%'"))
    finally:
        engine.dispose()


class Approves(AlreadyPaused):
    async def get_moderation(self, refs):
        return [ModerationInfo("a-1", "approved", None, NOW)]


class ActiveCabinet(AlreadyPaused):
    """Кабинет, где всё активно; apply запоминает состояние, чтобы сверка шла без расхождений."""

    def __init__(self):
        self._states: dict[str, str] = {}
        self._budgets: dict[str, dict[str, int]] = {}

    async def get_state(self, refs):
        keys = [refs.campaign_id, *refs.groups.values(), *refs.ads.values(), *refs.targets.values()]
        return {
            key: EntityState(
                key,
                "campaign",
                None,
                "Кампания",
                self._states.get(key, "active"),
                daily_limit_kop=self._budgets.get(key, {}).get("daily_limit_kop", 0),
                period_cap_kop=self._budgets.get(key, {}).get("period_cap_kop", 0),
            )
            for key in keys
        }

    async def apply(self, operation):
        if operation.field == "status":
            self._states[operation.entity_id] = operation.after
        else:
            self._budgets.setdefault(operation.entity_id, {})[operation.field] = operation.after

    async def min_budgets(self):
        return {"day": 0, "period": 0}


def with_ad(engine, business_id):
    with Session(engine) as session:
        campaign = session.query(Campaign).filter_by(business_id=business_id).one()
        group = AdGroup(campaign_id=campaign.id, theme="t", geo={}, status="active", external_id="g-1")
        session.add(group)
        session.flush()
        session.add(Ad(ad_group_id=group.id, title="Т", text="т", status="moderation", external_id="a-1"))
        session.commit()


def set_state(engine, business_id, status, worker_meta):
    with engine.begin() as conn:
        conn.execute(
            text("UPDATE businesses SET status=:s, settings=CAST(:j AS jsonb) WHERE id=:id"),
            {"s": status, "j": json.dumps({"_worker": worker_meta}), "id": business_id},
        )


def hold_tick_copy(repository):
    """Как run_due: список бизнесов тика держит объекты в identity map весь цикл. Identity map
    держит их по слабым ссылкам: без живого списка GC соберёт объект, и следующий business()
    прочитает свежую строку — гонка не воспроизведётся."""
    return {business.id: business for business in repository.rows(Business)}


async def test_resume_between_load_and_step_is_not_undone(db_session):
    """Тик загрузил бизнес на паузе владельца, владелец нажал «Продолжить», шаг идёт со старой копией."""
    engine = db_session.get_bind().engine
    business_id = committed_world(engine)
    with_ad(engine, business_id)
    set_state(engine, business_id, "paused", {"kill_switch": True, "kill_switch_campaigns": [1], "month": MONTH})
    repository = WorkerRepository(db_session)
    worker = Worker(repository, lambda *_: Approves(), clock=VirtualClock(NOW), dry_run=False)
    held = hold_tick_copy(repository)
    assert repository.business(business_id) is held[business_id], "копия из начала тика"
    # Итог screens.resume: running, kill_switch=False.
    set_state(engine, business_id, "running", {"kill_switch": False, "kill_switch_campaigns": [], "month": MONTH})
    await worker.run("sync_moderation", business_id)
    status, meta = fresh_meta(db_session, business_id)
    assert status == "running"
    assert not meta.get("kill_switch"), f"«Продолжить» стёрто старой копией: {status} {meta}"
    # Свежая строка отличалась от копии: run() обязан был перечитать объект под блокировкой.
    assert held[business_id].status == "running"


async def test_unconfirmed_stop_between_load_and_step_is_not_undone(db_session):
    """«Остановить» записал kill_switch и pending_stop, но площадка не подтвердила: статус ещё running."""
    engine = db_session.get_bind().engine
    business_id = committed_world(engine)
    with_ad(engine, business_id)
    set_state(engine, business_id, "running", {"month": MONTH})
    repository = WorkerRepository(db_session)
    worker = Worker(repository, lambda *_: Approves(), clock=VirtualClock(NOW), dry_run=False)
    held = hold_tick_copy(repository)
    assert repository.business(business_id) is held[business_id], "копия из начала тика"
    pending = {"reason_code": "OWNER_STOP", "reason_params": None, "budget_stop": False, "actor": "owner"}
    set_state(engine, business_id, "running", {"kill_switch": True, "pending_stop": pending, "month": MONTH})
    await worker.run("sync_moderation", business_id)
    status, meta = fresh_meta(db_session, business_id)
    assert meta.get("kill_switch") and meta.get("pending_stop"), f"незавершённая остановка стёрта: {status} {meta}"
    # Обе отметки записаны заново в свежей строке: run() обязан был перечитать объект под блокировкой.
    assert held[business_id].status == "running"


async def test_campaign_rows_fresh_after_owner_resume(db_session):
    """После «Продолжить» кампании тика не остаются старыми копиями —
    шаг перерасчёта видит свежий статус и не пишет ложный EXTERNAL_RESUME с frozen_until."""
    engine = db_session.get_bind().engine
    business_id = committed_world(engine)
    with_ad(engine, business_id)
    set_state(engine, business_id, "paused", {"kill_switch": True, "kill_switch_campaigns": [1], "month": MONTH})
    with engine.begin() as conn:
        conn.execute(text("UPDATE campaigns SET status='paused_owner' WHERE business_id=:id"), {"id": business_id})
    repository = WorkerRepository(db_session)
    worker = Worker(repository, lambda *_: ActiveCabinet(), clock=VirtualClock(NOW), dry_run=False)
    held = hold_tick_copy(repository)
    assert repository.business(business_id) is held[business_id], "копия из начала тика"
    # Шаг раньше в том же тике уже грузил кампанию: она в identity map со статусом остановки.
    held_campaigns = repository.rows(Campaign)
    assert held_campaigns[0].status == "paused_owner"
    # Итог screens.resume: бизнес снова работает, кампания включена записью OWNER_RESUME.
    set_state(engine, business_id, "running", {"kill_switch": False, "kill_switch_campaigns": [], "month": MONTH})
    with Session(engine) as session:
        session.query(Campaign).filter_by(business_id=business_id).update({"status": "active"})
        session.commit()
    await worker.run("rebalance_budget", business_id)
    status, meta = fresh_meta(db_session, business_id)
    assert status == "running"
    assert not meta.get("kill_switch"), f"«Продолжить» стёрто старой копией: {status} {meta}"
    with Session(engine) as session:
        campaign = session.query(Campaign).filter_by(business_id=business_id).one()
        assert campaign.status == "active", "кампания переписана со старой копии"
        assert not campaign.owner_paused
        assert campaign.frozen_until is None, "защита после отката не из того сценария"
        assert not session.query(ChangeLog).filter_by(business_id=business_id, reason_code="EXTERNAL_RESUME").count()
