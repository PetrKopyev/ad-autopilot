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


@pytest.mark.db
def test_plan_ai_calls_do_not_hold_a_connection(small_pool, monkeypatch, tmp_path) -> None:
    # Идеи, картинки и тексты ИИ готовит десятки секунд: всё это время соединение запроса было занято, и двое
    # пользователей, собирающих план, исчерпывали пул.
    from app.db import session as db_session
    from app.db.models import PlatformAccount
    from app.services.plan import PlanRepository, PlanService
    from tests.services.test_onboarding import (
        LIMITS,
        NOW,
        TODAY,
        ScriptedLLM,
        VariedArt,
        font_path,
        make_world,
        onboard,
    )

    _, engine, _, asset_id = small_pool
    locks = create_engine(engine.url, pool_size=2, max_overflow=0, isolation_level="AUTOCOMMIT")
    monkeypatch.setitem(db_session.LOCK_ENGINES, engine, locks)
    memory = make_world(tmp_path / "memory")
    asyncio.run(onboard(memory))
    held = []

    class WatchingLLM(ScriptedLLM):
        async def complete_json(self, system: str, user: str, temperature: float = 0.2):
            held.append(engine.pool.checkedout())
            return await super().complete_json(system, user, temperature)

    class WatchingArt(VariedArt):
        async def generate(self, prompt: str, *, seed: int, business_id: int):
            held.append(engine.pool.checkedout())
            return await super().generate(prompt, seed=seed, business_id=business_id)

    try:
        with db_session.SessionLocal() as db:
            asset = db.get(Asset, asset_id)
            business = db.get(Business, asset.business_id)
            for column in Business.__table__.columns.keys():
                if column not in {"id", "user_id", "created_at"}:
                    setattr(business, column, getattr(memory.business, column))
            db.delete(asset)
            db.add(PlatformAccount(business_id=business.id, platform="direct", auth_mode="oauth", access_token_enc="t"))
            db.commit()
            service = PlanService(
                PlanRepository(db),
                WatchingLLM(),
                lambda budget, record: WatchingArt(b"", budget, record=record),
                tmp_path / "plan",
                LIMITS,
                font_path=font_path(),
                seeds=iter(range(1, 100)).__next__,
            )
            plan = asyncio.run(service.get_plan(business.id, NOW, TODAY))
            assert plan["groups"] and len(held) > 3 and set(held) == {0}
            # План пересобирается, картинки уже есть: ИИ пишет только тексты.
            business.month_budget_kop += 100_000
            db.commit()
            held.clear()
            asyncio.run(service.get_plan(business.id, NOW, TODAY))
        assert held and set(held) == {0}
        assert engine.pool.checkedout() == 0
    finally:
        locks.dispose()


@pytest.mark.db
def test_full_lock_pool_answers_server_busy_at_once(small_pool, monkeypatch) -> None:
    # Пул блокировок исчерпан (перемотки демо): запрос не ждёт соединение в цикле событий, а сразу отвечает «сервер
    # занят», отличая это от «бизнес занят».
    import time

    from app.db import session as db_session
    from app.services.changes import ChangesRepository, ServerBusy

    _, engine, _, asset_id = small_pool
    locks = create_engine(engine.url, pool_size=1, max_overflow=0, pool_timeout=0, isolation_level="AUTOCOMMIT")
    monkeypatch.setitem(db_session.LOCK_ENGINES, engine, locks)
    try:
        with db_session.SessionLocal() as db, locks.connect():
            business_id = db.get(Asset, asset_id).business_id
            started = time.monotonic()
            with pytest.raises(ServerBusy), ChangesRepository(db).business_lock(business_id):
                pass
            assert time.monotonic() - started < 0.5
        with db_session.SessionLocal() as db, ChangesRepository(db).business_lock(business_id):
            assert locks.pool.checkedout() == 1
    finally:
        locks.dispose()


@pytest.mark.db
def test_platform_checks_before_launch_do_not_hold_a_connection(small_pool, tmp_path) -> None:
    from datetime import date

    from app.db import session as db_session
    from app.domain.types import PlatformLimit
    from app.platforms.fake import FakeAdapter
    from app.services.launch import LaunchRepository, LaunchService

    _, engine, _, asset_id = small_pool
    held = []

    class WatchingAdapter(FakeAdapter):
        async def min_budgets(self):
            held.append(engine.pool.checkedout())
            return await super().min_budgets()

        async def geo_targeting(self, city: str):
            held.append(engine.pool.checkedout())
            return await super().geo_targeting(city)

    with db_session.SessionLocal() as db:
        business = db.get(Business, db.get(Asset, asset_id).business_id)
        service = LaunchService(LaunchRepository(db), lambda business_id, platform: WatchingAdapter(), tmp_path)
        limits = {"direct": PlatformLimit("direct", 10_000_000, 500_000, 3_500_000)}
        asyncio.run(service.check_platforms(business, ["direct"], limits, date(2026, 1, 5)))
    assert held == [0, 0]


@pytest.mark.db
def test_connection_is_returned_when_handler_fails(small_pool) -> None:
    from fastapi import APIRouter, Depends
    from sqlalchemy import text

    from app.api.routing import ReleasingRoute, request_session

    _, engine, _, _ = small_pool

    def get_db():
        with request_session() as db:
            yield db

    router = APIRouter(route_class=ReleasingRoute)

    @router.get("/fail")
    def fail(db=Depends(get_db)) -> dict:
        db.execute(text("select 1"))
        raise RuntimeError("boom")

    @router.get("/ok")
    def ok(db=Depends(get_db)) -> dict:
        return {"value": db.execute(text("select 1")).scalar()}

    app = FastAPI()
    app.include_router(router)

    async def run() -> list[int]:
        transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
        async with httpx.AsyncClient(transport=transport, base_url="https://testserver") as client:
            return [(await client.get(path)).status_code for path in ("/fail", "/fail", "/ok")]

    assert asyncio.run(run()) == [500, 500, 200]
    assert engine.pool.checkedout() == 0


@pytest.mark.db
def test_launch_returns_connection_while_platform_uploads_and_creates(small_pool, monkeypatch, tmp_path) -> None:
    # Загрузка картинок и создание кампаний на площадке — секунды на каждый вызов: соединение запроса на это время
    # возвращается в пул. Картинки объявлений (Ad.asset_id) сохранены до создания кампании и видны другим сеансам.
    import itertools
    from types import SimpleNamespace

    from app.content.site_reader import SiteReadResult
    from app.db import session as db_session
    from app.db.models import Ad, AdGroup, Campaign, PlatformAccount
    from app.platforms.fake import FakeAdapter
    from app.platforms.metrica import FakeMetrica, MetricaCounter, MetricaGoal
    from app.services.launch import LaunchRepository, LaunchService
    from app.services.onboarding import OnboardingService
    from app.services.plan import PlanService
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

    _, engine, _, asset_id = small_pool
    locks = create_engine(engine.url, pool_size=2, max_overflow=0, isolation_level="AUTOCOMMIT")
    monkeypatch.setitem(db_session.LOCK_ENGINES, engine, locks)
    held, images = [], []

    class WatchingAdapter(FakeAdapter):
        async def upload_image(self, file):
            held.append(engine.pool.checkedout())
            return await super().upload_image(file)

        async def create_campaign_tree(self, plan_part):
            held.append(engine.pool.checkedout())
            with db_session.SessionLocal() as other:
                images.extend(
                    other.scalars(select(Ad.asset_id).join(AdGroup).where(AdGroup.campaign_id == int(plan_part.id)))
                )
            return await super().create_campaign_tree(plan_part)

    async def reader(url: str) -> SiteReadResult:
        return SiteReadResult(page_text())

    metrica = FakeMetrica(
        [MetricaCounter(COUNTER, "Счётчик", "salon.example")], {COUNTER: [MetricaGoal(1, "Заявка", "action")]}
    )
    adapter = WatchingAdapter()

    async def run(db) -> dict:
        repository = LaunchRepository(db)
        llm = ScriptedLLM()
        onboarding = OnboardingService(repository, llm, lambda business_id: metrica, LIMITS, reader=reader)
        # Бизнес пользователя заполняется заново, а его загруженная картинка не дала бы ИИ подготовить свои.
        asset = db.get(Asset, asset_id)
        user_id = db.get(Business, asset.business_id).user_id
        db.delete(asset)
        db.commit()
        created = onboarding.create_business(user_id, SITE, "Самара", BUDGET_KOP, None)
        business = db.get(Business, created["id"])
        repository.add(
            PlatformAccount(business_id=business.id, platform="direct", auth_mode="oauth", access_token_enc="t")
        )
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
        try:
            return await launches.launch(business.id, plan["id"], True, NOW, TODAY)
        finally:
            db.rollback()
            db.execute(delete(Campaign).where(Campaign.business_id == business.id))
            db.commit()

    try:
        with db_session.SessionLocal() as db:
            assert asyncio.run(run(db))["status"] == "launched"
    finally:
        locks.dispose()
    assert held and set(held) == {0}
    assert images and None not in images
    assert engine.pool.checkedout() == 0


@pytest.mark.db
def test_site_check_rereads_business_and_returns_connection_while_ai_answers(small_pool, monkeypatch) -> None:
    # Сайт читается и ИИ отвечает без соединения запроса, а под блокировкой бизнес перечитывается из базы: проверку,
    # которую за это время закончил другой запрос, второй раз не оплачиваем, а правка описания, сделанная, пока ИИ
    # отвечал, не теряется.
    from app.content.site_reader import SiteReadResult
    from app.db import session as db_session
    from app.services.onboarding import OnboardingRepository, OnboardingService
    from tests.services.test_onboarding import LIMITS, NOW, TODAY, SlowLLM, page_text

    _, engine, _, asset_id = small_pool
    locks = create_engine(engine.url, pool_size=2, max_overflow=0, isolation_level="AUTOCOMMIT")
    monkeypatch.setitem(db_session.LOCK_ENGINES, engine, locks)
    with db_session.SessionLocal() as db:
        business_id = db.get(Asset, asset_id).business_id
    held = []

    def elsewhere(description: dict, record: bool = True) -> None:
        if record:
            held.append(engine.pool.checkedout())
        with db_session.SessionLocal() as db:
            db.get(Business, business_id).description = description
            db.commit()

    def check(url: str) -> dict:
        return {
            "site_url": url,
            "checked_at": NOW.isoformat(),
            "pages_read": 1,
            "services_found": 1,
            "counter_ids": [],
            "block_reason": None,
            "fresh": {},
        }

    async def finished_elsewhere(url: str) -> SiteReadResult:
        elsewhere({"check": check(url)})
        return SiteReadResult(page_text())

    async def reader(url: str) -> SiteReadResult:
        return SiteReadResult(page_text())

    edited = {"services": {"value": "Правка владельца", "quote": None, "verified": False, "question": ""}}
    try:
        for read, during, calls in (
            (finished_elsewhere, lambda: None, []),
            (reader, lambda: elsewhere({"fields": edited}), ["description"]),
        ):
            elsewhere({}, record=False)
            llm = SlowLLM(during)
            with db_session.SessionLocal() as db:
                repository = OnboardingRepository(db)
                service = OnboardingService(repository, llm, lambda business_id: None, LIMITS, reader=read)
                asyncio.run(service.site_check(business_id, NOW, TODAY))
            assert llm.calls == calls
        with db_session.SessionLocal() as db:
            description = db.get(Business, business_id).description
        assert description["fields"] == edited and description["check"]["services_found"] == 1
        assert held == [0, 0]
    finally:
        locks.dispose()
    assert engine.pool.checkedout() == 0


@pytest.mark.db
def test_saves_waiting_for_business_lock_do_not_hold_a_connection(small_pool, monkeypatch) -> None:
    # Сохранение ждёт блокировку бизнеса до 15 секунд. Соединение запроса, открытое до ожидания, на это время
    # возвращается в пул: ждущие сохранения не занимают его у остальных запросов.
    from types import SimpleNamespace

    from app.api import business as business_api
    from app.auth.sessions import CSRF_COOKIE, SESSION_COOKIE
    from app.db import session as db_session
    from app.services.changes import ChangesRepository
    from app.services.onboarding import LOCK_WAITERS

    app, engine, token, asset_id = small_pool
    locks = create_engine(engine.url, pool_size=2, max_overflow=0, isolation_level="AUTOCOMMIT")
    monkeypatch.setitem(db_session.LOCK_ENGINES, engine, locks)
    app.include_router(business_api.router)
    app.state.platform_adapters = SimpleNamespace(metrica=lambda business_id: None)
    with db_session.SessionLocal() as db:
        business_id = db.get(Asset, asset_id).business_id
    marking = {"subject_type": "ip", "inn": "526317984689", "name": "ИП Иванов", "phone": "89001234567"}
    saves = [
        ("POST", "/api/business", {"site_url": "example.test", "city": "Москва", "month_budget_kop": 5_000_000}),
        ("PUT", "/api/business/description", {"description": {"services": {"value": "Стрижка"}}, "approved": False}),
        ("PUT", "/api/business/marking", marking),
        ("PUT", "/api/plan/assets", {"asset_ids": [asset_id]}),
    ]

    async def run() -> list[tuple]:
        result = []
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="https://testserver") as client:
            client.cookies.set(SESSION_COOKIE, token)
            client.cookies.set(CSRF_COOKIE, "csrf")
            for method, path, body in saves:
                with db_session.SessionLocal() as db, ChangesRepository(db).business_lock(business_id):
                    save = asyncio.create_task(
                        client.request(method, path, json=body, headers={"X-CSRF-Token": "csrf"})
                    )
                    deadline = asyncio.get_running_loop().time() + 3
                    while asyncio.get_running_loop().time() < deadline and not (
                        business_id in LOCK_WAITERS and engine.pool.checkedout() == 0
                    ):
                        await asyncio.sleep(0.02)
                    waiting = business_id in LOCK_WAITERS and not save.done()
                    held = engine.pool.checkedout()
                    other = (await client.get("/api/notifications")).status_code
                result.append((path, waiting, held, other, (await save).status_code))
        return result

    try:
        assert asyncio.run(run()) == [(path, True, 0, 200, 200) for _, path, _ in saves]
    finally:
        locks.dispose()
    assert engine.pool.checkedout() == 0
