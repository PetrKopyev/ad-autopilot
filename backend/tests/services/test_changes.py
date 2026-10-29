import os
from contextlib import contextmanager
from copy import deepcopy
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.db.models import Ad, AdGroup, Business, Campaign, ChangeLog, Plan, User
from app.platforms.base import AdGroupSpec, AdSpec, CampaignTreeSpec, Operation, PlatformError
from app.platforms.fake import FakeAdapter
from app.services.changes import (
    ChangeConflict,
    ChangesRepository,
    ChangesService,
    differences,
    load_operations,
    replaced_campaigns,
)


class MemoryRepository(ChangesRepository):
    def __init__(self):
        self.items = {}
        self.snapshots = []
        self.current_business = Business(id=1, settings={})
        self.entities = {}
        self.tests = []

    @contextmanager
    def business_lock(self, business_id):
        yield

    def business(self, business_id):
        return self.current_business

    def save(self, change):
        if change.id is None:
            change.id = len(self.items) + 1
        self.items[change.id] = change
        self.snapshots.append((change.id, change.status, deepcopy(change.operations)))

    def get(self, change_id, business_id):
        item = self.items.get(change_id)
        return item if item and item.business_id == business_id else None

    def pending(self, business_id=None):
        return [
            item
            for item in self.items.values()
            if item.status == "pending"
            and not item.evidence.get("_service", {}).get("dry_run")
            and (business_id is None or item.business_id == business_id)
        ]

    def reverts(self, change_id):
        return [item for item in self.items.values() if item.reverts_id == change_id]

    def local_entities(self, business_id, platform):
        return self.entities

    def experiments(self, change):
        return self.tests

    def recent_owner_changes(self, business_id, now):
        return [
            item
            for item in self.items.values()
            if item.actor == "owner"
            and item.status in {"applied", "reverted"}
            and item.created_at > now - timedelta(hours=24)
        ]


@pytest.fixture
def repository():
    return MemoryRepository()


@pytest.fixture
async def tree():
    adapter = FakeAdapter(moderation_delay=timedelta())
    plan = CampaignTreeSpec(
        "history",
        "История",
        10_000,
        300_000,
        (AdGroupSpec("group", "Группа", (AdSpec("ad", "Заголовок", "Текст", "old-image"),)),),
    )
    return adapter, await adapter.create_campaign_tree(plan)


async def apply_budget(repository, adapter, refs, **kwargs):
    return await ChangesService(repository, dry_run=False).apply_change(
        business_id=repository.current_business.id,
        adapter=adapter,
        refs=refs,
        operations=[Operation("set_budget", "campaign", refs.campaign_id, "daily_limit_kop", 10_000, 12_000)],
        actor="autopilot",
        platform="fake",
        kind="budget",
        reason_code="SCALE_UP",
        reason_params={"old_limit": 10_000, "new_limit": 12_000, "group": "Маникюр", "campaign": "Автопилот"},
        **kwargs,
    )


async def test_pending_is_saved_before_apply_and_verified(repository, tree, monkeypatch):
    adapter, refs = tree
    apply = adapter.apply

    async def observed(operation):
        assert repository.snapshots[-1][1] == "pending"
        assert repository.snapshots[-1][2][0]["before"] == 10_000
        await apply(operation)

    monkeypatch.setattr(adapter, "apply", observed)
    change = await apply_budget(repository, adapter, refs)
    assert [status for _, status, _ in repository.snapshots] == ["pending", "applied"]
    assert change.reason_text.endswith("с 100 до 120 ₽.")
    assert (await adapter.get_state(refs))[refs.campaign_id].daily_limit_kop == 12_000


async def test_records_actual_before_instead_of_stale_snapshot(repository, tree):
    adapter, refs = tree
    await adapter.apply(Operation("set_budget", "campaign", refs.campaign_id, "daily_limit_kop", 10_000, 11_000))
    change = await apply_budget(repository, adapter, refs)
    assert load_operations(change)[0].before == 11_000


async def test_apply_error_with_unchanged_state_is_failed(repository, tree, monkeypatch):
    adapter, refs = tree
    monkeypatch.setattr(adapter, "apply", AsyncMock(side_effect=PlatformError("private error")))
    change = await apply_budget(repository, adapter, refs)
    assert change.status == "failed"
    assert change.evidence["_service"]["differences"][0]["current"] == 10_000
    assert "private error" not in str(change.evidence)


async def test_apply_error_note_has_no_internal_jargon(repository, tree, monkeypatch):
    """Пометка об ошибке площадки лежит в изменении, пока его не проверили: без слов «сверка» и «применение»."""
    adapter, refs = tree
    monkeypatch.setattr(adapter, "apply", AsyncMock(side_effect=PlatformError("private error")))
    monkeypatch.setattr(ChangesService, "_reconcile", AsyncMock())
    error = (await apply_budget(repository, adapter, refs)).evidence["_service"]["error"]
    assert error and "свер" not in error.lower() and "примен" not in error.lower()


async def test_lost_apply_response_is_reconciled_as_applied(repository, tree, monkeypatch):
    adapter, refs = tree
    apply = adapter.apply

    async def lost_response(operation):
        await apply(operation)
        raise PlatformError("response lost")

    monkeypatch.setattr(adapter, "apply", lost_response)
    assert (await apply_budget(repository, adapter, refs)).status == "applied"


@pytest.mark.parametrize("applied", [True, False])
async def test_restart_reconciles_pending_without_writing_platform(repository, tree, monkeypatch, applied):
    adapter, refs = tree
    apply = adapter.apply

    async def crashed(operation):
        if applied:
            await apply(operation)
        raise RuntimeError("process stopped")

    monkeypatch.setattr(adapter, "apply", crashed)
    with pytest.raises(RuntimeError):
        await apply_budget(repository, adapter, refs)
    assert repository.items[1].status == "pending"
    monkeypatch.setattr(adapter, "apply", AsyncMock(side_effect=AssertionError("must not repeat apply")))
    recovered = await ChangesService(repository, dry_run=False).recover_pending(lambda *_: adapter)
    assert recovered[0].status == ("applied" if applied else "failed")
    adapter.apply.assert_not_called()


async def test_read_outage_keeps_pending_until_recovery(repository, tree, monkeypatch):
    adapter, refs = tree
    get_state = adapter.get_state
    calls = 0

    async def unavailable_after_apply(tree_refs):
        nonlocal calls
        calls += 1
        if calls > 1:
            raise PlatformError("unavailable")
        return await get_state(tree_refs)

    monkeypatch.setattr(adapter, "get_state", unavailable_after_apply)
    change = await apply_budget(repository, adapter, refs)
    assert change.status == "pending"
    assert await ChangesService(repository).recover_pending(lambda *_: adapter) == [change]
    assert change.status == "pending"
    monkeypatch.setattr(adapter, "get_state", get_state)
    await ChangesService(repository).recover_pending(lambda *_: adapter)
    assert change.status == "applied"


async def test_partial_batch_fails_without_claiming_all_applied(repository, tree, monkeypatch):
    adapter, refs = tree
    apply = adapter.apply
    calls = 0

    async def fail_second(operation):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise PlatformError("second operation failed")
        await apply(operation)

    monkeypatch.setattr(adapter, "apply", fail_second)
    change = await ChangesService(repository, dry_run=False).apply_change(
        business_id=1,
        adapter=adapter,
        refs=refs,
        operations=[
            Operation("set_budget", "campaign", refs.campaign_id, "daily_limit_kop", 10_000, 12_000),
            Operation("set_status", "ad", refs.ads["ad"], "status", "active", "paused_auto"),
        ],
        actor="autopilot",
        kind="budget",
        platform="fake",
        reason_code="CUSTOM",
        reason_text="Готовая причина.",
    )
    assert change.status == "failed"
    assert (await adapter.get_state(refs))[refs.campaign_id].daily_limit_kop == 12_000


async def test_dry_run_does_not_call_platform_and_is_not_recovered(repository, tree, monkeypatch):
    adapter, refs = tree
    repository.current_business.settings = {"dry_run": True}
    monkeypatch.setattr(adapter, "apply", AsyncMock(side_effect=AssertionError("dry run")))
    monkeypatch.setattr(adapter, "get_state", AsyncMock(side_effect=AssertionError("dry run")))
    change = await apply_budget(repository, adapter, refs)
    assert change.status == "pending" and change.evidence["_service"]["dry_run"] is True
    assert await ChangesService(repository).recover_pending(lambda *_: adapter) == []


async def test_autopilot_cannot_change_frozen_ad_through_its_campaign(repository, tree):
    adapter, refs = tree
    now = datetime(2026, 10, 26, tzinfo=UTC)
    repository.entities = {
        ("campaign", refs.campaign_id): Campaign(id=1, external_id=refs.campaign_id),
        ("group", refs.groups["group"]): AdGroup(id=2, campaign_id=1, external_id=refs.groups["group"]),
        ("ad", refs.ads["ad"]): Ad(
            id=3, ad_group_id=2, external_id=refs.ads["ad"], frozen_until=now + timedelta(days=14)
        ),
    }
    with pytest.raises(ChangeConflict, match="защищён"):
        await apply_budget(repository, adapter, refs, now=now)
    assert repository.items == {}
    assert (await apply_budget(repository, adapter, refs, now=now + timedelta(days=14))).status == "applied"


async def test_owner_edit_has_24_hour_cooldown(repository, tree):
    adapter, refs = tree
    now = datetime(2026, 10, 26, tzinfo=UTC)
    await ChangesService(repository, dry_run=False).apply_change(
        business_id=1,
        adapter=adapter,
        refs=refs,
        operations=[Operation("set_budget", "campaign", refs.campaign_id, "daily_limit_kop", 10_000, 11_000)],
        actor="owner",
        kind="budget",
        platform="fake",
        reason_code="OWNER_EDIT",
        reason_params={"object": "бюджет"},
        now=now,
    )
    with pytest.raises(ChangeConflict, match="24 часа"):
        await apply_budget(repository, adapter, refs, now=now + timedelta(hours=23))
    assert (await apply_budget(repository, adapter, refs, now=now + timedelta(hours=24))).status == "applied"


async def test_external_change_is_recorded_without_platform_write(repository, tree, monkeypatch):
    adapter, refs = tree
    operation = Operation("update_ad_text", "ad", refs.ads["ad"], "text", "Текст", "Правка в кабинете")
    await adapter.apply(operation)
    monkeypatch.setattr(adapter, "apply", AsyncMock(side_effect=AssertionError("observation must not write")))
    change = await ChangesService(repository, dry_run=False).apply_change(
        business_id=1,
        adapter=adapter,
        refs=refs,
        operations=[operation],
        actor="external",
        kind="text",
        platform="fake",
        reason_code="EXTERNAL_CHANGE",
        reason_params={"object": "объявления"},
    )
    assert change.status == "applied" and load_operations(change)[0].before == "Текст"
    adapter.apply.assert_not_called()


async def test_known_reason_uses_template_even_when_text_was_supplied(repository, tree):
    adapter, refs = tree
    change = await apply_budget(repository, adapter, refs, reason_text="Не использовать.")
    assert change.reason_text.endswith("с 100 до 120 ₽.")


@pytest.mark.parametrize("amount", [True, 1.5, "100", -1])
def test_money_requires_integer_kopecks(amount):
    with pytest.raises(ValueError):
        Operation("set_budget", "campaign", "campaign", "daily_limit_kop", 100, amount)


async def test_sequential_operations_verify_final_value_and_show_initial_restore(tree):
    adapter, refs = tree
    ad_id = refs.ads["ad"]
    operations = [
        Operation("set_status", "ad", ad_id, "status", "active", "paused_owner"),
        Operation("set_status", "ad", ad_id, "status", "paused_owner", "archived"),
    ]
    assert differences(operations, await adapter.get_state(refs))[0]["restore"] == "active"
    for operation in operations:
        await adapter.apply(operation)
    assert differences(operations, await adapter.get_state(refs)) == []


@pytest.fixture
def database():
    database_url = os.environ.get("TEST_DATABASE_URL")
    if not database_url:
        pytest.skip("TEST_DATABASE_URL is required")
    engine = create_engine(database_url)
    try:
        if engine.dialect.name != "postgresql":
            pytest.skip("TEST_DATABASE_URL must point to PostgreSQL")
        with engine.connect() as connection:
            transaction = connection.begin()
            try:
                with Session(bind=connection, join_transaction_mode="create_savepoint", expire_on_commit=False) as db:
                    user = User(yandex_uid="changes-" + uuid4().hex, login="test", consent_at=datetime.now(UTC))
                    db.add(user)
                    db.flush()
                    business = Business(
                        user_id=user.id, site_url="https://example.test", city="Test", month_budget_kop=0
                    )
                    db.add(business)
                    db.flush()
                    yield db, business, user
            finally:
                transaction.rollback()
    finally:
        engine.dispose()


@pytest.mark.db
async def test_postgresql_persists_operations_and_recovers_pending(database, tree):
    db, business, _ = database
    adapter, refs = tree
    repository = ChangesRepository(db)
    change = ChangeLog(
        business_id=business.id,
        actor="autopilot",
        kind="budget",
        platform="fake",
        entity_type="campaign",
        entity_id=refs.campaign_id,
        operations=[asdict(Operation("set_budget", "campaign", refs.campaign_id, "daily_limit_kop", 10_000, 12_000))],
        reason_code="SCALE_UP",
        reason_text="Проверка.",
        status="pending",
        evidence={"_service": {"refs": asdict(refs)}},
    )
    repository.save(change)
    await adapter.apply(load_operations(change)[0])
    await ChangesService(repository, dry_run=False).recover_pending(lambda *_: adapter, business_id=business.id)
    db.expire_all()
    stored = db.get(ChangeLog, change.id)
    assert stored.status == "applied"
    assert stored.operations[0]["before"] == 10_000
    assert stored.operations[0]["after"] == 12_000


@pytest.mark.db
async def test_postgresql_filters_history_by_owner_platform_actor_and_page(database, tree):
    db, business, user = database
    adapter, refs = tree
    repository = ChangesRepository(db)
    for index, (actor, platform) in enumerate((("owner", "fake"), ("autopilot", "fake"), ("owner", "direct"))):
        await ChangesService(repository, dry_run=False).apply_change(
            business_id=business.id,
            adapter=adapter,
            refs=refs,
            operations=[Operation("set_status", "ad", refs.ads["ad"], "status", "active", "paused_owner")],
            actor=actor,
            platform=platform,
            kind="pause",
            reason_code="CUSTOM",
            reason_text="Тест.",
            now=datetime(2026, 10, 20, tzinfo=UTC) + timedelta(days=index * 2),
        )
    rows, total = repository.history(user.id, platform="fake", actor="owner", page_size=1)
    assert total == 1 and len(rows) == 1 and rows[0].actor == "owner"
    assert repository.history(user.id + 1) == ([], 0)
    assert repository.history(user.id, page=2, page_size=2)[1] == 3
    assert len(repository.history(user.id, page=2, page_size=2)[0]) == 1


@pytest.mark.db
async def test_postgresql_history_hides_routine_limit_recalculation_unless_system_is_asked(database, tree):
    db, business, user = database
    adapter, refs = tree
    repository = ChangesRepository(db)
    for index, (kind, operation) in enumerate(
        (
            ("budget", Operation("set_budget", "campaign", refs.campaign_id, "daily_limit_kop", 10_000, 12_000)),
            ("pause", Operation("set_status", "campaign", refs.campaign_id, "status", "active", "paused_auto")),
        )
    ):
        await ChangesService(repository, dry_run=False).apply_change(
            business_id=business.id,
            adapter=adapter,
            refs=refs,
            operations=[operation],
            actor="system",
            platform="fake",
            kind=kind,
            reason_code="BUDGET_REBALANCE",
            reason_text="Пересчитали лимиты.",
            now=datetime(2026, 10, 20, tzinfo=UTC) + timedelta(days=index),
        )
    rows, total = repository.history(user.id)
    assert total == 1 and [row.kind for row in rows] == ["pause"]
    rows, total = repository.history(user.id, actor="system")
    assert total == 2


@pytest.mark.db
def test_postgresql_business_lock_survives_commit_and_rejects_parallel_writer(database):
    db, business, _ = database
    first = ChangesRepository(db)
    with Session(bind=db.get_bind().engine) as other_db:
        second = ChangesRepository(other_db)
        with first.business_lock(business.id):
            db.commit()
            with pytest.raises(ChangeConflict):
                with second.business_lock(business.id):
                    pytest.fail("Parallel writer acquired a held business lock")
        with second.business_lock(business.id):
            pass


def test_campaigns_of_earlier_launched_and_failed_plans_are_replaced():
    """v1 запущен, v2 упал, v3 запущен: действующий — последний запущенный, а кампании v1 и v2 прежние, хотя v1 тоже
    запущен. Порядок строк и id не важны: берётся версия."""

    def plan(id: int, version: int, status: str, campaigns: dict) -> Plan:
        return Plan(id=id, version=version, status=status, payload={"launch": {"campaigns": campaigns}})

    plans = [
        plan(1, 3, "launched", {"direct": 5, "vk": 6}),
        plan(3, 1, "launched", {"direct": 1, "vk": 2}),
        plan(2, 2, "failed", {"direct": 3, "vk": 4}),
        plan(4, 4, "draft", {}),
    ]
    assert replaced_campaigns(plans) == {1, 2, 3, 4}
    assert replaced_campaigns(plans[1:]) == {3, 4}


def test_conflict_texts_shown_to_owner_have_no_internal_jargon():
    import inspect

    from app.services import changes, experiments
    from app.worker import jobs

    sources = "".join(inspect.getsource(module) for module in (changes, experiments, jobs))
    for text in (changes.PENDING, experiments.VARIANT_UNCONFIRMED):
        assert "свер" not in text.lower()
    assert "шаг будет повторён" not in sources and "требуется сверить" not in sources
