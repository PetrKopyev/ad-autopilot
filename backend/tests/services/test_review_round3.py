"""Третий круг ревью: блокировки без ожидания в цикле событий, остановка перед отзывом доступа, ролики, тексты."""

import asyncio
from contextlib import contextmanager
from datetime import date, datetime, timedelta
from time import monotonic
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from sqlalchemy.exc import TimeoutError as PoolTimeout

from app.db.models import BudgetLedger, ChangeLog, WorkerRun
from app.platforms.base import Operation
from app.services.changes import ChangeConflict, ChangesRepository, ServerBusy
from app.services.onboarding import NotFound
from tests.integration.harness import BUDGET_KOP, make_world
from tests.services.test_stop_before_revoke import later


class FullPool:
    """Движок, у которого пул блокировок исчерпан: connect сразу отказывает, как QueuePool с pool_timeout=0."""

    def connect(self):
        raise PoolTimeout("pool exhausted")


def test_lock_pool_never_waits_and_is_used_for_application_engine() -> None:
    from app.db import session

    assert session.lock_bind(session.engine) is session.lock_engine
    pool = session.lock_engine.pool
    assert (pool.size(), pool._max_overflow, pool.timeout()) == (5, 20, 0)
    assert session.lock_engine.dialect._on_connect_isolation_level == "AUTOCOMMIT"


def test_full_lock_pool_is_server_busy_not_business_busy() -> None:
    from app.worker.locks import acquire

    engine = FullPool()
    with pytest.raises(ServerBusy, match="Сервер сейчас занят"), acquire(engine, 1):
        pass
    repository = ChangesRepository(SimpleNamespace(get_bind=lambda: engine))
    with pytest.raises(ServerBusy, match="Сервер сейчас занят"), repository.business_lock(1):
        pass


def flaky_acquire(failures: int, attempts: list):
    @contextmanager
    def acquire(bind, business_id):
        attempts.append(business_id)
        if len(attempts) <= failures:
            raise ChangeConflict("Этот бизнес уже обрабатывается.")
        yield

    return acquire


def test_business_lock_waits_until_released(monkeypatch) -> None:
    from app.worker import locks

    attempts, sleeps = [], []
    monkeypatch.setattr(locks, "acquire", flaky_acquire(2, attempts))
    monkeypatch.setattr(locks, "sleep", sleeps.append)
    with locks.business_lock(None, 7, wait=10):
        pass
    assert attempts == [7, 7, 7] and sleeps == [0.5, 0.5]


def test_business_lock_gives_up_at_deadline(monkeypatch) -> None:
    from app.worker import locks

    attempts = []
    monkeypatch.setattr(locks, "acquire", flaky_acquire(1000, attempts))
    monkeypatch.setattr(locks, "sleep", lambda seconds: None)
    clock = iter(range(100))
    monkeypatch.setattr(locks, "monotonic", lambda: next(clock))
    with pytest.raises(ChangeConflict), locks.business_lock(None, 7, wait=3):
        pass
    # Дедлайн 0 + 3: попытки в моменты 1, 2, 3, на третьей — отказ.
    assert len(attempts) == 3
    attempts.clear()
    with pytest.raises(ChangeConflict), locks.business_lock(None, 7):
        pass
    assert len(attempts) == 1


async def test_waiting_for_busy_lock_pool_keeps_event_loop_running(monkeypatch) -> None:
    # Пул блокировок исчерпан перемотками: остановка перед отзывом ждёт, а остальные запросы обслуживаются.
    from app.services.budget_guard import stop_platform

    world = await make_world()
    worker = world.worker()
    worker.step_key = "owner:revoke"
    loop = asyncio.get_running_loop()
    attempts, ran, served = [], [], []

    @contextmanager
    def lock(business_id):
        if 1 <= len(attempts) <= 2:
            # Прошлая попытка получила отказ: её обратный вызов выполнен, если цикл работал, пока остановка ждала
            # (time.sleep держал бы цикл до конца остановки).
            served.append(len(ran) == len(attempts))
        attempts.append(business_id)
        loop.call_soon(ran.append, business_id)
        if len(attempts) <= 2:
            raise ServerBusy
        yield

    world.repository.business_lock = lock
    await stop_platform(world.business, worker=worker, platform="direct", reason_text="Стоп.", deadline=later())
    assert served == [True, True] and world.campaign.status == "paused_owner"


@pytest.mark.parametrize(
    "error,status",
    [
        (ServerBusy(), 503),
        (ChangeConflict("Этот бизнес уже обрабатывается."), 409),
        (NotFound("Группа не найдена."), 404),
    ],
)
def test_human_errors_status(error, status) -> None:
    from app.api.business import human_errors

    with pytest.raises(HTTPException) as raised, human_errors():
        raise error
    assert raised.value.status_code == status and raised.value.detail == str(error)


def test_key_error_is_a_bug_not_a_missing_object() -> None:
    # Раньше любой LookupError становился 404 с текстом ошибки: KeyError отдавал наружу имя ключа.
    from app.api.business import human_errors

    with pytest.raises(KeyError), human_errors():
        raise KeyError("access_token_enc")


async def test_stop_does_not_start_next_campaign_after_deadline() -> None:
    from app.services.budget_guard import stop_platform

    world = await make_world()
    worker = world.worker()
    worker.step_key = "owner:revoke"
    with pytest.raises(TimeoutError):
        await stop_platform(
            world.business, worker=worker, platform="direct", reason_text="Стоп.", deadline=monotonic() - 1
        )
    assert world.campaign.status == "active" and not world.changes(reason_code="ACCOUNT_REVOKE")


async def test_slow_stop_is_finished_even_after_deadline(monkeypatch) -> None:
    # Остановку не прерывают посреди: иначе изменение осталось бы pending без ответа площадки, а после отзыва
    # доступа его уже не сверить.
    from app.services.budget_guard import stop_platform

    world = await make_world()
    worker = world.worker()
    worker.step_key = "owner:revoke"
    apply = world.adapter.apply

    async def slow(operation) -> None:
        await asyncio.sleep(0.2)
        await apply(operation)

    monkeypatch.setattr(world.adapter, "apply", slow)
    deadline = monotonic() + 0.05
    assert await stop_platform(world.business, worker=worker, platform="direct", reason_text="Стоп.", deadline=deadline)
    (change,) = world.changes(reason_code="ACCOUNT_REVOKE")
    assert change.status == "applied" and world.campaign.status == "paused_owner"


async def test_protective_stop_replaces_trial_proposal_of_the_same_step() -> None:
    # Пробный режим уже предложил остановку с тем же ключом; защитная остановка выполняется по-настоящему.
    from app.services.budget_guard import stop_platform

    world = await make_world(business_settings={"dry_run": True})
    worker = world.worker()
    worker.step_key = "owner:revoke"
    operation = Operation("set_status", "campaign", world.refs.campaign_id, "status", "active", "paused_owner")
    proposal = await worker.apply(
        world.business,
        world.campaign,
        [operation],
        actor="owner",
        kind="pause",
        reason_code="ACCOUNT_REVOKE",
        reason_text="Стоп.",
        suffix="revoke",
    )
    assert proposal.evidence["_service"]["dry_run"]
    assert await stop_platform(world.business, worker=worker, platform="direct", reason_text="Стоп.", deadline=later())
    (change,) = world.changes(reason_code="ACCOUNT_REVOKE")
    assert change.status == "applied" and not change.evidence["_service"]["dry_run"]
    assert world.campaign.status == "paused_owner"


async def test_budget_stop_notice_is_a_proposal_in_trial_mode(monkeypatch) -> None:
    from unittest.mock import AsyncMock

    world = await make_world(business_settings={"dry_run": True})
    monkeypatch.setattr("app.services.budget_guard.read_spend", AsyncMock(return_value=({"direct": BUDGET_KOP}, {})))
    await world.worker().run("budget_guard", 1)
    (notice,) = world.repository.rows(WorkerRun, 1, kind="notification")
    assert notice.log[0]["reason_code"] == "BUDGET_STOP" and notice.status == "proposed"


async def test_month_notice_names_the_date_and_expires_with_the_month() -> None:
    from app.services.month import month_rollover

    world = await make_world()
    world.clock.advance(days=27)
    await month_rollover(world.worker(), world.business)
    (notice,) = world.repository.rows(WorkerRun, 1, kind="notification")
    entry = notice.log[0]
    assert entry["text"].startswith("1 февраля начнётся новый месяц")
    assert entry["expires_at"] == "2026-02-01T00:00:00+03:00"
    repository = world.repository
    shown = repository.unread_notifications(1, {"MONTH_CONTINUE_NOTICE"}, 10, world.clock.now)
    assert shown == [notice]
    assert not repository.unread_notifications(
        1, {"MONTH_CONTINUE_NOTICE"}, 10, datetime.fromisoformat(entry["expires_at"])
    )


async def test_month_notice_in_december_names_january_of_next_year() -> None:
    from app.services.month import month_rollover

    world = await make_world()
    world.clock.advance(days=361)
    worker = world.worker()
    # Декабрь уже начат: переход месяца не нужен, проверяется только уведомление о следующем.
    worker.remember(world.business, month="2026-12-01")
    await month_rollover(worker, world.business)
    notices = world.repository.rows(WorkerRun, 1, kind="notification")
    (entry,) = [notice.log[0] for notice in notices if notice.log[0]["reason_code"] == "MONTH_CONTINUE_NOTICE"]
    assert entry["text"].startswith("1 января начнётся новый месяц")
    assert entry["expires_at"] == "2027-01-01T00:00:00+03:00"


def test_budget_counts_fresher_of_ledger_and_reports_and_shows_reports_separately() -> None:
    from app.services.stats import aggregate_budget

    today = date(2026, 11, 26)
    rows = [
        SimpleNamespace(platform="direct", date=date(2026, 11, 25), cost_kop=2_000_000),
        SimpleNamespace(platform="vk", date=date(2026, 11, 25), cost_kop=500_000),
        SimpleNamespace(platform="vk", date=date(2026, 10, 31), cost_kop=900_000),
    ]
    ledger = [
        # Страж бюджета видит и сегодняшний расход, которого ещё нет в отчётах.
        BudgetLedger(platform="direct", month=date(2026, 11, 1), spent_kop=2_015_100, spent_at=None),
        # Снимок старше отчёта: отчёт уже свежее, бюджет не должен показывать меньше статистики.
        BudgetLedger(platform="vk", month=date(2026, 11, 1), spent_kop=400_000, spent_at=None),
    ]
    budget = aggregate_budget(10_000_000, rows, ledger, today)
    assert budget["reported_kop"] == 2_500_000
    assert budget["spent_kop"] == 2_015_100 + 500_000
    assert {item["platform"]: item["spent_kop"] for item in budget["platforms"]} == {
        "direct": 2_015_100,
        "vk": 500_000,
    }


async def test_launch_remembers_the_image_of_every_ad(tmp_path) -> None:
    from tests.services.test_launch import campaigns, launch, ready

    world, plan = await ready(tmp_path)
    await launch(world, plan)
    for campaign in campaigns(world):
        groups, ads, _ = world.repository.tree_rows(campaign.id)
        for group in groups:
            chosen = next(
                item for item in plan["groups"] if item["name"] == group.theme and item["platform"] == campaign.platform
            )
            selected = [asset["id"] for asset in chosen["assets"] if asset["selected"]]
            images = [ad.asset_id for ad in ads if ad.ad_group_id == group.id]
            assert images and None not in images
            # Объявления группы получают выбранные картинки по кругу: у двух объявлений — разные.
            assert images == [selected[position % len(selected)] for position in range(len(images))]
            assert len(set(images)) == min(len(images), len(selected))


async def test_image_experiment_shows_both_pictures_and_says_what_changes() -> None:
    from app.db.models import Ad, Asset, Experiment
    from app.services.screens import ScreensService

    world = await make_world()
    control = next(iter(world.ads.values()))
    world.repository.save(Asset(id=1, business_id=1, kind="image"), Asset(id=2, business_id=1, kind="image"))
    control.asset_id = 1
    variant = Ad(
        id=99, ad_group_id=control.ad_group_id, title=control.title, text=control.text, asset_id=2, status="active"
    )
    world.repository.save(
        variant,
        Experiment(
            id=1,
            business_id=1,
            ad_group_id=control.ad_group_id,
            hypothesis="image",
            control_ad_id=control.id,
            variant_ad_id=variant.id,
            planned_n_per_arm=2000,
            started_at=world.clock.now,
            deadline=world.clock.now + timedelta(days=14),
            status="running",
            stats={},
        ),
    )
    (item,) = ScreensService(world.worker()).experiments(world.business)["items"]
    assert item["hypothesis"] == "image"
    assert item["comparing"].startswith("Меняем только картинку, текст объявления тот же.")
    assert [arm["image"] for arm in item["arms"]] == ["/api/plan/assets/1/file", "/api/plan/assets/2/file"]


def test_media_root_itself_is_never_removed(monkeypatch, tmp_path) -> None:
    from app.api.auth import remove_business_media
    from app.config import settings

    monkeypatch.setattr(settings, "media_dir", tmp_path)
    (tmp_path / "7").mkdir()
    (tmp_path / "keep.jpg").write_bytes(b"x")
    remove_business_media(".")
    assert (tmp_path / "keep.jpg").exists() and (tmp_path / "7").is_dir()
    remove_business_media(7)
    assert not (tmp_path / "7").exists() and tmp_path.is_dir()


def test_demo_advances_are_limited_by_lock_pool() -> None:
    from app.demo_flags import MAX_ADVANCES, AdvanceJobs

    jobs = AdvanceJobs()
    for business_id in range(MAX_ADVANCES):
        assert jobs.start(business_id, 1) is not None
    assert jobs.start(0, 1) is None
    with pytest.raises(ServerBusy):
        jobs.start(MAX_ADVANCES, 1)
    jobs.get(0).state = "done"
    assert jobs.start(MAX_ADVANCES, 1) is not None
    assert jobs.businesses() == set(range(MAX_ADVANCES + 1))


def test_demo_advances_leave_lock_pool_room_for_requests_and_worker() -> None:
    # Каждая перемотка держит соединение пула блокировок минутами: при MAX_ADVANCES, равном пулу, остановка рекламы и
    # шаги воркера получали бы только «сервер занят».
    from app.db.session import lock_engine
    from app.demo_flags import MAX_ADVANCES

    assert MAX_ADVANCES < lock_engine.pool.size() + lock_engine.pool._max_overflow


def test_forgotten_advance_still_counts_until_its_thread_ends() -> None:
    from app.demo_flags import MAX_ADVANCES, AdvanceJobs

    jobs = AdvanceJobs()
    started = [jobs.start(business_id, 1) for business_id in range(MAX_ADVANCES)]
    for business_id in range(MAX_ADVANCES):
        jobs.forget(business_id)
    assert not jobs.businesses()
    with pytest.raises(ServerBusy):
        jobs.start(MAX_ADVANCES, 1)
    started[0].state = "error"
    assert jobs.start(MAX_ADVANCES, 1) is not None


async def test_video_queue_keeps_built_videos_between_retries() -> None:
    # Бизнес занят: сохранить нельзя, но собранное ffmpeg не теряется — повтор берёт его, а не собирает заново.
    from app.services.plan import VideoQueue

    builds, seen = [], []

    def build(business_id, built):
        seen.append(dict(built))
        if "segment" not in built:
            builds.append(business_id)
            built["segment"] = ([1, 2, 3], b"video")
        return len(seen) == 3

    await VideoQueue(retries=12, delay=0).request(5, build)
    assert builds == [5] and len(seen) == 3 and seen[1] == seen[2] == {"segment": ([1, 2, 3], b"video")}


async def test_video_queue_gives_up_after_twelve_retries() -> None:
    from app.services.plan import VideoQueue

    attempts = []
    await VideoQueue(delay=0).request(5, lambda business_id, built: attempts.append(business_id) or False)
    assert len(attempts) == 13


async def test_video_built_once_is_reused_when_business_was_busy(tmp_path, monkeypatch) -> None:
    from app.services import plan as plan_module
    from tests.services.test_plan import build as build_plan
    from tests.services.test_plan import make_world as make_plan_world

    world = make_plan_world(tmp_path)
    draft = await build_plan(world)
    ids = [asset["id"] for asset in draft["groups"][0]["assets"]]
    assert len(ids) >= 3
    world.plans.select_assets(world.business.id, ids)
    monkeypatch.setattr(plan_module.shutil, "which", lambda name: "/usr/bin/ffmpeg")
    calls = []
    monkeypatch.setattr(
        plan_module, "build_video", lambda images, title, font_path: calls.append(images) or SimpleNamespace(data=b"v")
    )
    world.repository.locked = True
    built = {}
    assert world.plans.build_videos(world.business.id, built) is False
    world.repository.locked = False
    assert world.plans.build_videos(world.business.id, built) is True
    assert len(calls) == 1
    assert [asset for asset in world.repository.assets(world.business.id) if asset.kind == "video" and asset.selected]


async def test_reselected_set_brings_back_its_video_without_rebuilding(tmp_path, monkeypatch) -> None:
    from app.services import plan as plan_module
    from tests.services.test_plan import build as build_plan
    from tests.services.test_plan import make_world as make_plan_world

    world = make_plan_world(tmp_path)
    draft = await build_plan(world)
    ids = [asset["id"] for asset in draft["groups"][0]["assets"]]
    assert len(ids) >= 3
    monkeypatch.setattr(plan_module.shutil, "which", lambda name: "/usr/bin/ffmpeg")
    calls = []
    monkeypatch.setattr(
        plan_module, "build_video", lambda images, title, font_path: calls.append(images) or SimpleNamespace(data=b"v")
    )
    world.plans.select_assets(world.business.id, ids)
    world.plans.build_videos(world.business.id)
    (video,) = [asset for asset in world.repository.assets(world.business.id) if asset.kind == "video"]
    world.plans.select_assets(world.business.id, ids[:1])
    world.plans.build_videos(world.business.id)
    assert video.selected is False
    world.plans.select_assets(world.business.id, ids)
    world.plans.build_videos(world.business.id)
    assert video.selected is True and len(calls) == 1


async def test_plan_is_not_generated_for_business_deleted_while_waiting_for_lock(tmp_path) -> None:
    # Удаление аккаунта идёт под блокировкой бизнеса: дождавшись её, генерация видит, что бизнеса уже нет, и не тратит
    # деньги на ИИ и не создаёт каталог media/<id>/ заново.
    from app.db.models import Business
    from tests.services.test_onboarding import NOW, TODAY, onboard
    from tests.services.test_onboarding import make_world as make_plan_world

    world = make_plan_world(tmp_path)
    await onboard(world)
    repository = world.repository
    lock = repository.business_lock

    @contextmanager
    def deleting(business_id):
        with lock(business_id):
            repository.tables[Business].remove(world.business)
            yield

    repository.business_lock = deleting
    with pytest.raises(NotFound):
        await world.plans.get_plan(world.business.id, NOW, TODAY)
    assert world.llm.calls.count("ad") == 0 and not world.arts
    assert not (world.media / str(world.business.id)).exists()


async def test_regenerate_rereads_business_under_lock(tmp_path) -> None:
    from app.db.models import Business
    from tests.services.test_onboarding import NOW, TODAY, onboard
    from tests.services.test_onboarding import make_world as make_plan_world

    world = make_plan_world(tmp_path)
    await onboard(world)
    await world.plans.get_plan(world.business.id, NOW, TODAY)
    images = len(world.repository.assets(world.business.id))
    arts = len(world.arts)
    repository = world.repository
    lock = repository.business_lock

    @contextmanager
    def deleting(business_id):
        with lock(business_id):
            repository.tables[Business].remove(world.business)
            yield

    repository.business_lock = deleting
    with pytest.raises(NotFound):
        await world.plans.regenerate_images(world.business.id, 1, NOW, TODAY)
    assert len(world.arts) == arts and len(repository.assets(world.business.id)) == images


async def test_image_file_is_removed_when_its_row_is_not_saved(tmp_path, monkeypatch) -> None:
    from tests.services.test_onboarding import NOW, TODAY, onboard
    from tests.services.test_onboarding import make_world as make_plan_world

    world = make_plan_world(tmp_path)
    await onboard(world)
    add = world.repository.add

    def failing(item):
        if type(item).__name__ == "Asset":
            raise RuntimeError("business deleted")
        add(item)

    monkeypatch.setattr(world.repository, "add", failing)
    with pytest.raises(RuntimeError):
        await world.plans.get_plan(world.business.id, NOW, TODAY)
    assert not list((world.media / str(world.business.id)).glob("*.jpg"))


def test_budget_over_limit_message_uses_rubles() -> None:
    from app.services.onboarding import MAX_BUDGET_KOP, OnboardingError
    from app.services.screens import ScreensService

    world = asyncio.run(make_world())
    with pytest.raises(OnboardingError, match="не превышать 10 000 000 ₽"):
        asyncio.run(ScreensService(world.worker()).save_budget(world.business, MAX_BUDGET_KOP + 1))


@pytest.mark.db
def test_disconnect_fails_pending_changes_of_that_platform_only(db_session) -> None:
    from uuid import uuid4

    from app.auth.sessions import DISCONNECTED_PENDING, AuthRepository
    from app.db.models import Business, PlatformAccount, User

    user = User(yandex_uid="disconnect-" + uuid4().hex, login="owner", consent_at=datetime.now().astimezone())
    db_session.add(user)
    db_session.flush()
    business = Business(user_id=user.id, site_url="https://example.test", city="Москва", month_budget_kop=100_000)
    db_session.add(business)
    db_session.flush()
    account = PlatformAccount(business_id=business.id, platform="direct", auth_mode="oauth", access_token_enc="t")
    db_session.add(account)

    def change(platform: str, dry_run: bool = False) -> ChangeLog:
        return ChangeLog(
            business_id=business.id,
            actor="owner",
            kind="pause",
            platform=platform,
            entity_type="campaign",
            entity_id="c-1",
            operations=[],
            reason_code="ACCOUNT_REVOKE",
            reason_params={},
            reason_text="Стоп.",
            evidence={"_service": {"refs": {}, "dry_run": dry_run}},
            status="pending",
            created_at=datetime.now().astimezone(),
        )

    direct, vk, proposal = change("direct"), change("vk"), change("direct", dry_run=True)
    db_session.add_all([direct, vk, proposal])
    db_session.commit()
    assert AuthRepository(db_session).disconnect_account(user.id, account.id)
    db_session.expire_all()
    assert direct.status == "failed" and direct.evidence["_service"]["error"] == DISCONNECTED_PENDING
    assert vk.status == "pending" and proposal.status == "pending"
    assert ChangesRepository(db_session).pending(business.id) == [vk]


@pytest.mark.db
def test_disconnect_frees_worker_blocked_by_unconfirmed_stop(db_session) -> None:
    # Остановка перед отключением не дождалась сверки: без доступа к кабинету pending навсегда остановил бы все шаги
    # воркера, кроме стража бюджета, и для второй площадки.
    from uuid import uuid4

    from app.auth.sessions import AuthRepository
    from app.db.models import Business, PlatformAccount, User
    from app.worker.jobs import WorkerRepository

    user = User(yandex_uid="frees-" + uuid4().hex, login="owner", consent_at=datetime.now().astimezone())
    db_session.add(user)
    db_session.flush()
    business = Business(user_id=user.id, site_url="https://example.test", city="Москва", month_budget_kop=100_000)
    db_session.add(business)
    db_session.flush()
    account = PlatformAccount(business_id=business.id, platform="vk", auth_mode="oauth", access_token_enc="t")
    db_session.add(account)
    db_session.flush()
    db_session.add(
        ChangeLog(
            business_id=business.id,
            actor="owner",
            kind="pause",
            platform="vk",
            entity_type="campaign",
            entity_id="c-1",
            operations=[],
            reason_code="ACCOUNT_REVOKE",
            reason_params={},
            reason_text="Стоп.",
            evidence={"_service": {"refs": {}, "dry_run": False}},
            status="pending",
            created_at=datetime.now().astimezone(),
        )
    )
    db_session.commit()
    repository = WorkerRepository(db_session)
    assert repository.pending(business.id)
    AuthRepository(db_session).disconnect_account(user.id, account.id)
    assert not repository.pending(business.id)


async def selected_video_world(tmp_path, monkeypatch):
    from app.services import plan as plan_module
    from tests.services.test_plan import build as build_plan
    from tests.services.test_plan import make_world as make_plan_world

    world = make_plan_world(tmp_path)
    draft = await build_plan(world)
    world.plans.select_assets(world.business.id, [asset["id"] for asset in draft["groups"][0]["assets"]])
    monkeypatch.setattr(plan_module.shutil, "which", lambda name: "/usr/bin/ffmpeg")
    return world


async def test_video_is_not_stored_for_business_deleted_during_build(tmp_path, monkeypatch) -> None:
    from app.db.models import Asset, Plan
    from app.services import plan as plan_module

    world = await selected_video_world(tmp_path, monkeypatch)
    tables = world.repository.tables

    def build_video(images, title, font_path):
        tables[Asset].clear()
        tables[Plan].clear()
        return SimpleNamespace(data=b"v")

    monkeypatch.setattr(plan_module, "build_video", build_video)
    assert world.plans.build_videos(world.business.id) is True
    assert not tables[Asset] and not list(world.media.glob("*/video-*"))


async def test_video_file_is_removed_when_its_row_is_not_saved(tmp_path, monkeypatch) -> None:
    from app.services import plan as plan_module

    world = await selected_video_world(tmp_path, monkeypatch)
    monkeypatch.setattr(plan_module, "build_video", lambda images, title, font_path: SimpleNamespace(data=b"v"))
    commit = world.repository.commit

    @contextmanager
    def lock(business_id):
        world.repository.commit = lambda: (_ for _ in ()).throw(RuntimeError("business deleted"))
        try:
            yield
        finally:
            world.repository.commit = commit

    world.repository.business_lock = lock
    with pytest.raises(RuntimeError):
        world.plans.build_videos(world.business.id)
    assert not list(world.media.glob("*/video-*"))
