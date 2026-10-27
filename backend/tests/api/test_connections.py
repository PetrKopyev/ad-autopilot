import asyncio
import os
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import create_engine, delete, select
from sqlalchemy.orm import sessionmaker

from app.db.models import Asset, Business, Session, User
from tests.services.test_onboarding import picture


@pytest.fixture
def small_pool(monkeypatch, tmp_path):
    """Приложение с пулом на одно соединение и бизнес с картинкой, сохранённые в PostgreSQL по-настоящему."""
    url = os.environ.get("TEST_DATABASE_URL")
    if not url:
        pytest.skip("TEST_DATABASE_URL is required")
    from app.api import notifications, plan
    from app.auth.sessions import hash_session_id
    from app.config import settings
    from app.db import session as db_session

    engine = create_engine(url, pool_size=1, max_overflow=0, pool_timeout=3)
    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    monkeypatch.setattr(db_session, "SessionLocal", factory)
    monkeypatch.setattr(settings, "media_dir", tmp_path)
    token = uuid4().hex
    with factory() as db:
        user = User(yandex_uid="pool-" + token, login="pool", consent_at=datetime.now(UTC))
        db.add(user)
        db.flush()
        business = Business(user_id=user.id, site_url="https://example.test", city="Москва", month_budget_kop=0)
        db.add(business)
        db.flush()
        (tmp_path / str(business.id)).mkdir()
        (tmp_path / str(business.id) / "a.jpg").write_bytes(picture(5))
        asset = Asset(business_id=business.id, kind="upload", file_path=f"{business.id}/a.jpg", selected=True)
        db.add_all(
            [
                asset,
                Session(
                    id=hash_session_id(token),
                    user_id=user.id,
                    csrf_secret="csrf",
                    expires_at=datetime.now(UTC) + timedelta(hours=1),
                ),
            ]
        )
        db.commit()
    app = FastAPI()
    app.include_router(plan.router)
    app.include_router(notifications.router)
    app.state.http = None
    try:
        yield app, engine, token, asset.id
    finally:
        with factory() as db:
            db.execute(delete(User).where(User.id == user.id))
            db.commit()
        engine.dispose()


async def fetch(app: FastAPI, token: str, paths: list[str], threads: int = 40) -> list[int]:
    import anyio.to_thread

    from app.auth.sessions import SESSION_COOKIE

    anyio.to_thread.current_default_thread_limiter().total_tokens = threads
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="https://testserver") as client:
        client.cookies.set(SESSION_COOKIE, token)
        responses = await asyncio.gather(*(client.get(path) for path in paths))
    return [response.status_code for response in responses]


async def fetch_files(app: FastAPI, token: str, asset_id: int, count: int) -> list[int]:
    return await fetch(app, token, [f"/api/plan/assets/{asset_id}/file"] * count)


@pytest.mark.db
def test_asset_file_needs_one_connection_at_a_time(small_pool) -> None:
    # Проверка входа и поиск картинки раньше держали по соединению до конца отдачи файла: пулу из одного
    # соединения не хватало даже на один запрос, а двое пользователей с десятком картинок исчерпывали 15.
    app, engine, token, asset_id = small_pool
    assert asyncio.run(fetch_files(app, token, asset_id, 1)) == [200]
    assert asyncio.run(fetch_files(app, token, asset_id, 20)) == [200] * 20
    assert engine.pool.checkedout() == 0


@pytest.mark.db
def test_connection_is_returned_without_waiting_for_a_free_thread(small_pool) -> None:
    # Сессия, открытая синхронной зависимостью, закрывалась в пуле потоков. Когда все потоки заняты запросами, которые
    # ждут соединение, закрытию не доставалось потока, соединение не возвращалось, и через 30 секунд — ошибка 500.
    app, engine, token, asset_id = small_pool
    paths = ["/api/notifications", f"/api/plan/assets/{asset_id}/file", "/api/plan"] * 8
    statuses = asyncio.run(fetch(app, token, paths, threads=2))
    assert 500 not in statuses and set(statuses) <= {200, 409}
    assert engine.pool.checkedout() == 0


@pytest.mark.db
def test_business_lock_does_not_take_connection_from_request_pool(small_pool, monkeypatch) -> None:
    # Блокировку держит отдельное соединение: из общего пула оно отнимало место у запросов (перемотка демо — минуты),
    # а запрос, уже державший соединение сессии, ждал второе и при исчерпании пула ждал сам себя.
    from app.db import session as db_session
    from app.services.changes import ChangesRepository
    from app.worker.jobs import WorkerRepository

    _, engine, _, _ = small_pool
    locks = create_engine(engine.url, pool_size=2, max_overflow=0, isolation_level="AUTOCOMMIT")
    monkeypatch.setitem(db_session.LOCK_ENGINES, engine, locks)
    try:
        with db_session.SessionLocal() as db:
            business = db.scalar(select(Business).order_by(Business.id.desc()).limit(1))
            for repository in (ChangesRepository(db), WorkerRepository(db)):
                with repository.business_lock(business.id):
                    db.get(Business, business.id)
                    assert engine.pool.checkedout() == 1 and locks.pool.checkedout() == 1
    finally:
        locks.dispose()
