import os
from dataclasses import asdict
from datetime import UTC, date, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, delete, func, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session as OrmSession

from app.db.models import (
    Ad,
    AdGroup,
    ArtCall,
    Asset,
    BudgetLedger,
    Business,
    Campaign,
    ChangeLog,
    Experiment,
    LeadsSurvey,
    LLMCall,
    Plan,
    PlatformAccount,
    Session,
    StatsDaily,
    Target,
    User,
    WorkerRun,
)
from app.db.query import Window
from app.platforms.base import Operation
from app.services.onboarding import OnboardingRepository
from app.worker.jobs import WorkerRepository

pytestmark = pytest.mark.db
NOW = datetime(2026, 3, 10, 12, tzinfo=UTC)


def make_tree(db, *, platform: str = "direct", suffix: str = "1") -> SimpleNamespace:
    user = User(yandex_uid="storage-" + uuid4().hex, login="test", consent_at=NOW)
    db.add(user)
    db.flush()
    business = Business(user_id=user.id, site_url="https://example.test", city="Самара", month_budget_kop=100_000)
    db.add(business)
    db.flush()
    account = PlatformAccount(business_id=business.id, platform=platform, auth_mode="oauth", access_token_enc="t")
    db.add(account)
    db.flush()
    campaign = Campaign(
        business_id=business.id,
        platform_account_id=account.id,
        platform=platform,
        external_id=f"c-{suffix}",
        type="unified",
        name="Тест",
        status="active",
        daily_limit_kop=1_000,
        period_cap_kop=10_000,
    )
    db.add(campaign)
    db.flush()
    group = AdGroup(campaign_id=campaign.id, external_id=f"g-{suffix}", theme="Тест", geo={}, status="active")
    db.add(group)
    db.flush()
    ads = [
        Ad(ad_group_id=group.id, external_id=f"a-{suffix}-{index}", title="Т", text="Т", status="active")
        for index in (1, 2)
    ]
    target = Target(ad_group_id=group.id, kind="phrase", value="маникюр", external_id=f"t-{suffix}", status="active")
    db.add_all([*ads, target])
    db.flush()
    return SimpleNamespace(
        user=user, business=business, account=account, campaign=campaign, group=group, ads=ads, target=target
    )


def rejected(db, *items) -> None:
    with pytest.raises(IntegrityError), db.begin_nested():
        db.add_all(items)
        db.flush()


def test_platform_account_is_unique_per_business_and_platform(db_session):
    world = make_tree(db_session)
    rejected(
        db_session,
        PlatformAccount(business_id=world.business.id, platform="direct", auth_mode="oauth", access_token_enc="t"),
    )
    db_session.add(
        PlatformAccount(business_id=world.business.id, platform="vk", auth_mode="oauth", access_token_enc="t")
    )
    db_session.flush()


def test_external_ids_are_unique_within_parent_and_null_is_free(db_session):
    world = make_tree(db_session)
    common = {"type": "unified", "name": "Т", "status": "active", "daily_limit_kop": 0, "period_cap_kop": 0}
    campaign = {"business_id": world.business.id, "platform_account_id": world.account.id, "platform": "direct"}
    rejected(db_session, Campaign(**campaign, **common, external_id="c-1"))
    rejected(db_session, AdGroup(campaign_id=world.campaign.id, external_id="g-1", theme="Т", geo={}, status="active"))
    rejected(db_session, Ad(ad_group_id=world.group.id, external_id="a-1-1", title="Т", text="Т", status="active"))
    rejected(
        db_session,
        Target(ad_group_id=world.group.id, kind="phrase", value="x", external_id="t-1", status="active"),
    )
    # Внешний id ещё не выдан площадкой: таких строк может быть сколько угодно.
    db_session.add_all(
        [
            Campaign(**campaign, **common),
            Campaign(**campaign, **common),
            Ad(ad_group_id=world.group.id, title="Т", text="Т"),
            Ad(ad_group_id=world.group.id, title="Т", text="Т"),
        ]
    )
    db_session.flush()
    other = make_tree(db_session)
    assert other.campaign.external_id == world.campaign.external_id


def test_plan_version_operation_id_and_idempotency_keys_are_unique(db_session):
    world = make_tree(db_session)
    business_id = world.business.id
    db_session.add(Plan(business_id=business_id, version=1, payload={}))
    db_session.flush()
    rejected(db_session, Plan(business_id=business_id, version=1, payload={}))
    db_session.add(ArtCall(business_id=business_id, cost_kop=0, status="succeeded", operation_id="op-1"))
    db_session.add_all([ArtCall(business_id=business_id, cost_kop=0, status="pending") for _ in range(2)])
    db_session.flush()
    rejected(db_session, ArtCall(business_id=business_id, cost_kop=0, status="succeeded", operation_id="op-1"))
    change = {
        "business_id": business_id,
        "actor": "autopilot",
        "kind": "pause",
        "entity_type": "ad",
        "entity_id": "a-1-1",
        "operations": [],
        "reason_code": "X",
        "reason_text": "Т",
    }
    db_session.add_all([ChangeLog(**change, idempotency_key="step:1"), ChangeLog(**change), ChangeLog(**change)])
    db_session.flush()
    rejected(db_session, ChangeLog(**change, idempotency_key="step:1"))
    run = {"business_id": business_id, "kind": "optimize", "status": "ok"}
    db_session.add(WorkerRun(**run, slot_key="optimize:2026-03-10"))
    db_session.add_all([WorkerRun(kind="heartbeat", status="ok") for _ in range(2)])
    db_session.flush()
    rejected(db_session, WorkerRun(**run, slot_key="optimize:2026-03-10"))


@pytest.mark.parametrize(
    "statement",
    [
        "UPDATE businesses SET month_budget_kop = -1 WHERE id = :business",
        "UPDATE businesses SET avg_check_kop = -1 WHERE id = :business",
        "UPDATE businesses SET lead_to_client_rate = 1.5 WHERE id = :business",
        "UPDATE campaigns SET daily_limit_kop = -1 WHERE id = :campaign",
        "UPDATE campaigns SET period_cap_kop = -1 WHERE id = :campaign",
        "UPDATE campaigns SET status = 'running' WHERE id = :campaign",
        "UPDATE ad_groups SET status = 'paused' WHERE id = :group",
        "UPDATE targets SET status = 'deleted' WHERE id = :target",
        "UPDATE ads SET moderation_status = 'accepted' WHERE id = :ad",
        "INSERT INTO stats_daily (business_id, platform, campaign_id, ad_id, date, clicks) "
        "VALUES (:business, 'direct', :campaign, :ad, '2026-03-01', -1)",
        "INSERT INTO budget_ledger (business_id, month, platform, allocated_kop, period_cap_kop) "
        "VALUES (:business, '2026-03-02', 'direct', 0, 0)",
        "INSERT INTO budget_ledger (business_id, month, platform, allocated_kop, period_cap_kop, spent_kop) "
        "VALUES (:business, '2026-03-01', 'direct', 0, 0, -5)",
        "INSERT INTO leads_survey (business_id, week_start, leads_count) VALUES (:business, '2026-03-02', -1)",
        "INSERT INTO experiments (business_id, ad_group_id, hypothesis, control_ad_id, variant_ad_id, metric, "
        "planned_n_per_arm, min_effect, deadline) "
        "VALUES (:business, :group, 'text', :ad, :ad, 'ctr', 100, 0.2, now())",
        "INSERT INTO worker_runs (business_id, kind, status) VALUES (:business, 'cleanup', 'ok')",
        "INSERT INTO worker_runs (business_id, kind, status) VALUES (:business, 'optimize', 'done')",
        "INSERT INTO art_calls (business_id, cost_kop, status) VALUES (:business, 0, 'done')",
        "INSERT INTO llm_calls (business_id, purpose, model, tokens_in, tokens_out, cost_kop, status) "
        "VALUES (:business, 'x', 'm', 1, 1, -1, 'DONE')",
        "INSERT INTO assets (business_id, kind, file_path, cost_kop) VALUES (:business, 'image', 'a.jpg', -1)",
    ],
)
def test_checks_reject_negative_money_and_unknown_statuses(db_session, statement):
    world = make_tree(db_session)
    params = {
        "business": world.business.id,
        "campaign": world.campaign.id,
        "group": world.group.id,
        "target": world.target.id,
        "ad": world.ads[0].id,
    }
    with pytest.raises(IntegrityError, match="CheckViolation"), db_session.begin_nested():
        db_session.execute(text(statement), {key: value for key, value in params.items() if f":{key}" in statement})


BUSINESS_TABLES = (
    PlatformAccount,
    Campaign,
    ChangeLog,
    Experiment,
    StatsDaily,
    BudgetLedger,
    LeadsSurvey,
    WorkerRun,
    LLMCall,
    ArtCall,
    Plan,
    Asset,
)


def full_business(db) -> SimpleNamespace:
    world = make_tree(db)
    business_id = world.business.id
    asset = Asset(business_id=business_id, kind="image", file_path=f"{business_id}/a.jpg")
    db.add(asset)
    db.flush()
    world.ads[0].asset_id = asset.id
    change = ChangeLog(
        business_id=business_id,
        actor="autopilot",
        kind="launch",
        platform="direct",
        entity_type="ad",
        entity_id=world.ads[1].external_id,
        operations=[],
        reason_code="X",
        reason_text="Т",
        status="applied",
    )
    db.add(change)
    db.flush()
    experiment = Experiment(
        business_id=business_id,
        ad_group_id=world.group.id,
        hypothesis="text",
        control_ad_id=world.ads[0].id,
        variant_ad_id=world.ads[1].id,
        metric="ctr",
        planned_n_per_arm=100,
        min_effect=0.2,
        deadline=NOW + timedelta(days=14),
        change_id=change.id,
    )
    db.add(experiment)
    db.flush()
    world.ads[0].experiment_id = world.ads[1].experiment_id = experiment.id
    db.add_all(
        [
            StatsDaily(
                business_id=business_id,
                platform="direct",
                campaign_id=world.campaign.id,
                ad_id=world.ads[0].id,
                date=date(2026, 3, 1),
            ),
            BudgetLedger(
                business_id=business_id,
                month=date(2026, 3, 1),
                platform="direct",
                allocated_kop=0,
                period_cap_kop=0,
            ),
            LeadsSurvey(business_id=business_id, week_start=date(2026, 3, 2), leads_count=1),
            WorkerRun(business_id=business_id, kind="optimize", status="ok", slot_key="optimize:2026-03-10"),
            LLMCall(
                business_id=business_id, purpose="x", model="m", tokens_in=1, tokens_out=1, cost_kop=1, status="DONE"
            ),
            ArtCall(business_id=business_id, cost_kop=1, status="succeeded"),
            Plan(business_id=business_id, version=1, payload={}),
            Session(id=uuid4().hex, user_id=world.user.id, expires_at=NOW, csrf_secret="s"),
        ]
    )
    db.flush()
    return world


def test_account_with_campaigns_cannot_be_deleted(db_session):
    world = full_business(db_session)
    with pytest.raises(IntegrityError, match="ForeignKeyViolation"), db_session.begin_nested():
        db_session.delete(world.account)
        db_session.flush()
    db_session.delete(make_tree(db_session).target)
    db_session.flush()


def test_deleting_user_removes_every_business_row_despite_restricted_account_key():
    """Данные создаются и удаляются разными транзакциями, как в работе: DELETE /api/me идёт каскадом от users."""
    from app.auth.sessions import AuthRepository

    engine = create_engine(os.environ["TEST_DATABASE_URL"])
    users = []
    try:
        with OrmSession(engine, expire_on_commit=False) as db:
            world, kept = full_business(db), make_tree(db)
            users = [world.user.id, kept.user.id]
            db.commit()
        with OrmSession(engine) as db:
            assert AuthRepository(db).delete_user(world.user.id) == [world.business.id]
        with OrmSession(engine) as db:
            for model in BUSINESS_TABLES:
                count = select(func.count()).select_from(model).where(model.business_id == world.business.id)
                assert not db.scalar(count), model.__tablename__
            for model, column in ((Ad, Ad.id), (AdGroup, AdGroup.id), (Target, Target.id)):
                ids = {item.id for item in (*world.ads, world.group, world.target)}
                assert not db.scalar(select(func.count()).select_from(model).where(column.in_(ids)))
            assert db.get(User, world.user.id) is None and db.get(Business, world.business.id) is None
            assert not db.scalar(select(func.count()).select_from(Session).where(Session.user_id == world.user.id))
            assert db.get(Campaign, kept.campaign.id) is not None
    finally:
        with engine.begin() as connection:
            connection.execute(delete(User).where(User.id.in_(users)))
        engine.dispose()


def test_worker_slot_is_taken_once_and_failed_slot_is_retaken(db_session):
    world = make_tree(db_session)
    repository = WorkerRepository(db_session)
    business_id = world.business.id
    run = repository.start_run(business_id, "optimize", "optimize:2026-03-10", NOW)
    assert (run.status, run.slot_key, run.log) == ("running", "optimize:2026-03-10", [{"key": "optimize:2026-03-10"}])
    run.status = "failed"
    repository.save(run)
    again = repository.start_run(business_id, "optimize", "optimize:2026-03-10", NOW + timedelta(minutes=30))
    assert again.id == run.id and again.status == "running" and again.started_at == NOW + timedelta(minutes=30)
    again.status = "ok"
    repository.save(again)
    assert repository.start_run(business_id, "optimize", "optimize:2026-03-10", NOW + timedelta(hours=1)) is None
    assert repository.start_run(business_id, "sync_stats", "optimize:2026-03-10", NOW).id != run.id
    assert repository.start_run(business_id, "optimize", "optimize:2026-03-11", NOW).id != run.id
    rows = db_session.scalars(select(WorkerRun).where(WorkerRun.business_id == business_id)).all()
    assert sorted((row.kind, row.slot_key) for row in rows) == [
        ("optimize", "optimize:2026-03-10"),
        ("optimize", "optimize:2026-03-11"),
        ("sync_stats", "optimize:2026-03-10"),
    ]


def test_notifications_are_written_once_per_key_and_status_reads_latest_runs(db_session):
    world = make_tree(db_session)
    repository = WorkerRepository(db_session)
    business_id = world.business.id
    for text_ in ("первое", "повтор"):
        repository.notify(
            WorkerRun(
                kind="notification",
                business_id=business_id,
                slot_key="month_notice:2026-03-01",
                started_at=NOW,
                finished_at=NOW,
                status="ok",
                units_spent=0,
                log=[{"key": "month_notice:2026-03-01", "text": text_}],
            )
        )
    (notice,) = repository.rows(WorkerRun, business_id, kind="notification")
    assert notice.log[0]["text"] == "первое"
    db_session.add_all(
        [
            WorkerRun(kind="heartbeat", status="ok", started_at=NOW, finished_at=NOW),
            WorkerRun(
                business_id=business_id,
                kind="budget_guard",
                status="ok",
                slot_key="a",
                started_at=NOW + timedelta(minutes=1),
                finished_at=NOW + timedelta(minutes=2),
            ),
            WorkerRun(
                business_id=business_id,
                kind="budget_guard",
                status="failed",
                slot_key="b",
                started_at=NOW + timedelta(minutes=30),
                finished_at=NOW + timedelta(minutes=31),
            ),
        ]
    )
    db_session.flush()
    assert repository.last_check(business_id) >= NOW + timedelta(minutes=2)
    assert repository.latest_run(business_id, "budget_guard").status == "failed"


def test_prune_keeps_two_days_of_heartbeats_ninety_days_of_runs_and_all_notifications(db_session):
    world = make_tree(db_session)
    repository = WorkerRepository(db_session)
    marker = "prune-" + uuid4().hex
    ages = {"heartbeat": (1, 3), "optimize": (89, 91), "notification": (89, 91)}
    for kind, (fresh, old) in ages.items():
        for days in (fresh, old):
            db_session.add(
                WorkerRun(
                    kind=kind,
                    business_id=None if kind == "heartbeat" else world.business.id,
                    slot_key=None if kind == "heartbeat" else f"{kind}:{days}",
                    status="ok",
                    started_at=NOW - timedelta(days=days),
                    log=[{"marker": marker}],
                )
            )
    db_session.flush()
    repository.prune_runs(NOW)
    left = db_session.scalars(select(WorkerRun).where(WorkerRun.log.contains([{"marker": marker}]))).all()
    assert sorted((row.kind, (NOW - row.started_at).days) for row in left) == [
        ("heartbeat", 1),
        ("notification", 89),
        ("notification", 91),
        ("optimize", 89),
    ]


def test_change_lookups_use_keys_status_and_operations(db_session):
    world = make_tree(db_session)
    repository = WorkerRepository(db_session)
    business_id = world.business.id
    ad = world.ads[0].external_id

    def change(**values) -> ChangeLog:
        operation = Operation("set_status", "ad", ad, "status", "active", "paused_auto")
        item = ChangeLog(
            business_id=business_id,
            actor="autopilot",
            kind="pause",
            platform="direct",
            entity_type="ad",
            entity_id=ad,
            operations=[asdict(operation)],
            reason_code="X",
            reason_text="Т",
            created_at=NOW,
            **values,
        )
        db_session.add(item)
        db_session.flush()
        return item

    applied = change(status="applied", idempotency_key="optimize:1")
    later = change(status="applied")
    change(status="failed")
    dry = change(status="pending", evidence={"_service": {"dry_run": True}})
    pending = change(status="pending", evidence={"_service": {"dry_run": False}})
    assert repository.change_by_key(business_id, "optimize:1") is applied
    assert repository.change_by_key(business_id, "optimize:2") is None
    assert repository.pending(business_id) == [pending] and dry not in repository.pending()
    assert repository.last_status_change(business_id, "direct", "ad", ad) is later
    assert repository.last_status_change(business_id, "vk", "ad", ad) is None
    assert repository.last_status_change(business_id, "direct", "ad", world.ads[1].external_id) is None


def test_rows_filters_run_in_sql_for_business_scope(db_session):
    world = make_tree(db_session)
    other = make_tree(db_session)
    repository = WorkerRepository(db_session)
    for tree in (world, other):
        for offset in range(5):
            db_session.add(
                StatsDaily(
                    business_id=tree.business.id,
                    platform=tree.campaign.platform,
                    campaign_id=tree.campaign.id,
                    ad_id=tree.ads[0].id,
                    date=date(2026, 3, 1) + timedelta(days=offset),
                    is_settled=offset < 3,
                )
            )
    db_session.flush()
    window = repository.rows(StatsDaily, world.business.id, date=Window(date(2026, 3, 2), date(2026, 3, 4)))
    assert sorted(row.date.day for row in window) == [2, 3, 4]
    assert len(repository.rows(StatsDaily, world.business.id, is_settled=True, date=Window(date(2026, 3, 2)))) == 2
    assert repository.rows(StatsDaily, world.business.id, platform="vk") == []
    ids = {ad.id for ad in world.ads}
    assert {ad.id for ad in repository.rows(Ad, world.business.id, id=ids | {other.ads[0].id})} == ids
    assert repository.find(AdGroup, business_id=other.business.id, id=world.group.id) is None


def test_same_external_ids_on_two_platforms_of_one_business_stay_apart(db_session):
    world = make_tree(db_session)
    account = PlatformAccount(business_id=world.business.id, platform="vk", auth_mode="oauth", access_token_enc="t")
    db_session.add(account)
    db_session.flush()
    campaign = Campaign(
        business_id=world.business.id,
        platform_account_id=account.id,
        platform="vk",
        external_id="c-1",
        type="vk_plan",
        name="Т",
        status="active",
        daily_limit_kop=0,
        period_cap_kop=0,
    )
    db_session.add(campaign)
    db_session.flush()
    group = AdGroup(campaign_id=campaign.id, external_id="g-1", theme="Т", geo={}, status="active")
    db_session.add(group)
    db_session.flush()
    ad = Ad(ad_group_id=group.id, external_id="a-1-1", title="Т", text="Т", status="active")
    db_session.add(ad)
    db_session.flush()
    repository = WorkerRepository(db_session)
    assert repository.local_entities(world.business.id, "direct")[("ad", "a-1-1")] is world.ads[0]
    assert repository.local_entities(world.business.id, "vk")[("ad", "a-1-1")] is ad


def test_weekly_ai_spend_is_counted_by_created_at_from_launch(db_session):
    world = make_tree(db_session)
    repository = OnboardingRepository(db_session)
    business_id = world.business.id
    assert repository.launched_at(business_id) is None
    launched = datetime(2026, 3, 10, 9, tzinfo=UTC)
    db_session.add_all(
        [
            Plan(business_id=business_id, version=1, status="failed", payload={}),
            Plan(
                business_id=business_id,
                version=2,
                status="launched",
                payload={"launch": {"launched_at": launched.isoformat()}},
            ),
            LLMCall(
                business_id=business_id,
                purpose="x",
                model="m",
                tokens_in=1,
                tokens_out=1,
                cost_kop=500,
                status="DONE",
                created_at=launched - timedelta(minutes=1),
            ),
            LLMCall(
                business_id=business_id,
                purpose="x",
                model="m",
                tokens_in=1,
                tokens_out=1,
                cost_kop=300,
                status="DONE",
                created_at=launched,
            ),
            ArtCall(
                business_id=business_id, cost_kop=200, status="succeeded", created_at=launched + timedelta(hours=1)
            ),
        ]
    )
    db_session.flush()
    assert repository.launched_at(business_id) == launched
    assert repository.ai_spent(business_id) == 1000
    assert repository.ai_spent(business_id, launched) == 500
    assert repository.ai_spent(business_id, launched + timedelta(seconds=1)) == 200


def test_naive_launch_time_is_read_as_utc(db_session):
    world = make_tree(db_session)
    naive = datetime(2026, 3, 10, 9)
    db_session.add(
        Plan(
            business_id=world.business.id,
            version=1,
            status="launched",
            payload={"launch": {"launched_at": naive.isoformat()}},
        )
    )
    db_session.flush()
    assert OnboardingRepository(db_session).launched_at(world.business.id) == naive.replace(tzinfo=UTC)


def test_revocations_skip_vk_account_connected_in_another_business(db_session):
    from app.auth.sessions import AuthRepository

    mine, theirs = make_tree(db_session, suffix="mine"), make_tree(db_session, suffix="theirs")
    second = Business(user_id=mine.user.id, site_url="https://b.example.test", city="Самара", month_budget_kop=100_000)
    db_session.add(second)
    db_session.flush()
    vk = PlatformAccount(
        business_id=second.id, platform="vk", auth_mode="oauth", access_token_enc="v", external_login="777"
    )
    other = PlatformAccount(
        business_id=theirs.business.id, platform="vk", auth_mode="oauth", access_token_enc="o", external_login="777"
    )
    db_session.add_all([vk, other])
    db_session.flush()
    repository = AuthRepository(db_session)
    assert [item.platform for item in repository.revocations(mine.user.id)] == ["direct"]
    assert repository.revocations(mine.user.id, vk.id) == []
    other.status = "revoked"
    db_session.flush()
    revocations = sorted(repository.revocations(mine.user.id), key=lambda item: item.platform)
    assert [(item.platform, item.user_id) for item in revocations] == [("direct", None), ("vk", "777")]
    assert repository.revocations(theirs.user.id, theirs.account.id)[0].access_token == "t"


async def test_site_check_reads_site_before_lock_and_saves_under_it_on_postgres(db_session):
    from app.content.site_reader import SiteReadResult
    from app.platforms.metrica import FakeMetrica
    from app.services.onboarding import OnboardingService
    from tests.services.test_onboarding import LIMITS, NOW, SITE, TODAY, ScriptedLLM, page_text

    user = User(yandex_uid="site-" + uuid4().hex, login="owner", consent_at=NOW)
    db_session.add(user)
    db_session.commit()
    llm = ScriptedLLM()
    free = []

    async def reader(url: str) -> SiteReadResult:
        # Пока сайт читается, блокировку бизнеса свободно берёт другой процесс.
        with db_session.get_bind().engine.connect() as other:
            free.append(other.scalar(text("SELECT pg_try_advisory_lock(:id)"), {"id": business_id}))
            other.execute(text("SELECT pg_advisory_unlock(:id)"), {"id": business_id})
        return SiteReadResult(page_text())

    repository = OnboardingRepository(db_session)
    service = OnboardingService(repository, llm, lambda _: FakeMetrica([], {}), LIMITS, reader=reader)
    business_id = service.create_business(user.id, SITE, "Самара", 100_000, None)["id"]
    result = await service.site_check(business_id, NOW, TODAY)
    assert result["status"] == "ready" and free == [True] and llm.calls == ["description"]
    assert (await service.site_check(business_id, NOW, TODAY)) == result and llm.calls == ["description"]
    business = db_session.get(Business, business_id)
    db_session.expire(business)
    assert business.description["check"]["site_url"] == SITE
