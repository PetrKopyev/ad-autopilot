"""В1 круга 8: гонка «Остановить всю рекламу» с шагом воркера на настоящем PostgreSQL.

Память (MemoryRepository) гонку не ловит: там объект business один. Здесь две сессии: шаг воркера держит
advisory-блокировку и переписывает settings из устаревшей копии (remember), а кнопка «Остановить» ждёт
блокировку и останавливает только под ней, на свежих данных.

В1 круга 9 — обратная сторона: тик воркера держит копию business из начала тика (identity map),
и «Остановить», нажатое между загрузкой и шагом, стирался первым remember шага. Теперь run перечитывает
бизнес под блокировкой.
"""

import asyncio
import os
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.db.models import Business, Campaign, PlatformAccount, User
from app.platforms.base import EntityState
from app.platforms.fake import VirtualClock
from app.services.changes import ChangeConflict
from app.services.screens import ScreensService
from app.worker.jobs import Worker, WorkerRepository

pytestmark = pytest.mark.db


@pytest.fixture(autouse=True)
def cleanup_committed_world():
    """committed_world коммитит бизнес настоящим commit (второй сессии его иначе не видно) —
    подчищаем после каждого теста, savepoint-фикстура db_session их не откатит."""
    yield
    from sqlalchemy import create_engine

    url = os.environ["TEST_DATABASE_URL"]
    engine = create_engine(url)
    try:
        with engine.begin() as conn:
            conn.execute(text("DELETE FROM businesses WHERE site_url = 'https://example.test'"))
            conn.execute(text("DELETE FROM users WHERE yandex_uid LIKE 'pause-race-%'"))
    finally:
        engine.dispose()

NOW = datetime(2026, 3, 11, 9, tzinfo=UTC)


class AlreadyPaused:
    """Кампания уже на паузе: stop_all не ходит на площадку, только фиксирует остановку бизнеса."""

    async def get_state(self, refs):
        return {
            key: EntityState(key, "campaign", None, "Кампания", "paused_owner") for key in [refs.campaign_id]
        }

    async def get_stats(self, *args, **kwargs):
        return []

    async def get_spend_today(self, *args, **kwargs):
        return 0

    async def get_balance_hint(self):
        # Меньше дневного лимита кампании (1000 коп): balance_notice обязан сделать remember.
        return 500


def committed_world(engine) -> int:
    """Бизнес с кампанией настоящим commit: второе соединение (шаг воркера) их видит."""
    with Session(engine) as session:
        user = User(yandex_uid="pause-race-" + uuid4().hex, login="test", consent_at=NOW)
        session.add(user)
        session.flush()
        business = Business(
            user_id=user.id,
            site_url="https://example.test",
            city="Самара",
            month_budget_kop=100_000,
            status="running",
            settings={},
        )
        session.add(business)
        session.flush()
        account = PlatformAccount(business_id=business.id, platform="direct", auth_mode="oauth", access_token_enc="t")
        session.add(account)
        session.flush()
        session.add(
            Campaign(
                business_id=business.id,
                platform_account_id=account.id,
                platform="direct",
                external_id="c-1",
                type="unified",
                name="Тест",
                status="active",
                owner_paused=False,
                daily_limit_kop=1_000,
                period_cap_kop=10_000,
            )
        )
        session.commit()
        return business.id


async def hold_lock_and_remember(engine, business_id, hold_seconds):
    """Сценарий шага воркера: держим advisory-блокировку бизнеса и под ней делаем remember
    из устаревшей копии business — ровно то, что перезаписывало settings и стирало «Остановить»."""
    with engine.connect() as conn:
        conn.execute(text("SELECT pg_advisory_lock(:id)"), {"id": business_id})
        session = Session(bind=conn, expire_on_commit=False)
        stale = session.get(Business, business_id)
        await asyncio.sleep(hold_seconds)
        stale.settings = stale.settings | {
            "_worker": stale.settings.get("_worker", {}) | {"step_marker": "worker-step"}
        }
        session.commit()
        conn.commit()  # Session с внешним соединением релизит лишь savepoint: фиксируем саму транзакцию.
        conn.execute(text("SELECT pg_advisory_unlock(:id)"), {"id": business_id})


def fresh_meta(db_session, business_id) -> tuple:
    db_session.expire_all()
    stored = db_session.get(Business, business_id)
    return stored.status, stored.settings.get("_worker", {})


async def test_pause_waits_and_survives_worker_remember_race_on_postgres(db_session, monkeypatch):
    """Кнопка ждёт блокировку; шаг воркера под ней переписывает settings из старой копии. Остановка
    всё равно применяется (под блокировкой, на свежих данных), а служебная запись шага не стёрта."""
    from app.services import screens as screens_module

    engine = db_session.get_bind().engine
    business_id = committed_world(engine)
    repository = WorkerRepository(db_session)
    service = ScreensService(Worker(repository, lambda *_: AlreadyPaused(), clock=VirtualClock(NOW), dry_run=False))
    monkeypatch.setattr(screens_module, "PAUSE_LOCK_WAIT", 5.0)

    worker_step = asyncio.create_task(hold_lock_and_remember(engine, business_id, 0.5))
    await asyncio.sleep(0.2)  # шаг воркера первым занял блокировку

    await service.pause(repository.business(business_id))
    await worker_step

    status, meta = fresh_meta(db_session, business_id)
    assert status == "paused"
    assert meta["kill_switch"] is True, "остановка не потерялась"
    assert meta["step_marker"] == "worker-step", "remember шага воркера не стёрт остановкой"
    assert not meta.get("pending_stop")


async def test_pause_reports_conflict_without_trace_when_lock_held_too_long_on_postgres(db_session, monkeypatch):
    """Блокировка не освободилась за время ожидания: честный 409 и никаких записей — повторное нажатие
    не увидит пропавшего намерения (его просто не было)."""
    from app.services import screens as screens_module

    engine = db_session.get_bind().engine
    business_id = committed_world(engine)
    repository = WorkerRepository(db_session)
    service = ScreensService(Worker(repository, lambda *_: AlreadyPaused(), clock=VirtualClock(NOW), dry_run=False))
    monkeypatch.setattr(screens_module, "PAUSE_LOCK_WAIT", 0.3)

    worker_step = asyncio.create_task(hold_lock_and_remember(engine, business_id, 1.0))
    await asyncio.sleep(0.2)

    with pytest.raises(ChangeConflict, match="нажмите ещё раз через минуту"):
        await service.pause(repository.business(business_id))
    await worker_step

    status, meta = fresh_meta(db_session, business_id)
    assert status == "running"
    assert "kill_switch" not in meta
    assert meta["step_marker"] == "worker-step"


async def test_worker_rereads_business_under_lock_and_keeps_owner_stop_on_postgres(db_session, monkeypatch):
    """В1 круга 9: тик загрузил бизнес в identity map, владелец нажал «Остановить» до шага этого бизнеса.
    Шаг воркера не должен стереть остановку старой копией: run перечитывает бизнес под блокировкой.
    budget_guard у работающего бизнеса всегда делает remember (balance_low) — ровно тот вызов, что и стирал."""
    from app.worker.jobs import Worker as WorkerClass

    engine = db_session.get_bind().engine
    business_id = committed_world(engine)
    repository = WorkerRepository(db_session)
    worker = WorkerClass(repository, lambda *_: AlreadyPaused(), clock=VirtualClock(NOW), dry_run=False)
    # Как run_due: бизнесы загружены в identity map сессии в начале тика.
    stale = repository.business(business_id)
    assert stale.status == "running"
    repository.rows(Business)

    # Владелец жмёт «Остановить» из другой сессии между загрузкой тиком и шагом этого бизнеса.
    conn_b = engine.connect()
    session_b = Session(bind=conn_b, expire_on_commit=False)
    worker_b = WorkerClass(
        WorkerRepository(session_b), lambda *_: AlreadyPaused(), clock=VirtualClock(NOW), dry_run=False
    )
    await ScreensService(worker_b).pause(session_b.get(Business, business_id))
    conn_b.commit()
    assert repository.business(business_id) is stale, "identity map держит копию из начала тика"

    await worker.run("budget_guard", business_id)

    status, meta = fresh_meta(db_session, business_id)
    assert status == "paused"
    assert meta["kill_switch"] is True, "остановка владельца не стёрта шагом воркера"
    assert "balance_low" not in meta, "шаг не выполнялся со старой копией: бизнес уже на паузе"
