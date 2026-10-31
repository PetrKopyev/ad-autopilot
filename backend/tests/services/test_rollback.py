import os
from dataclasses import asdict, replace
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.dialects.postgresql import dialect
from sqlalchemy.orm import Session

from app.db.models import Ad, AdGroup, Business, Campaign, ChangeLog, Experiment, PlatformAccount, User
from app.platforms.base import AdGroupSpec, AdSpec, CampaignTreeSpec, Operation, PlatformError
from app.platforms.direct import DirectAdapter
from app.platforms.fake import FakeAdapter
from app.services.changes import ChangeConflict, ChangesRepository, ChangesService, ServerBusy, load_operations
from app.services.rollback import (
    NOTHING_TO_REVERT,
    ConfirmationRequired,
    RollbackService,
    can_revert,
    inverse_operations,
    reverts_by_experiment,
    superseded,
    turns_on_replaced,
)
from tests.platforms.test_direct import DirectServer, configuration


@pytest.fixture
def repository():
    repository = MagicMock(spec=ChangesRepository)
    items = {}

    def save(change):
        if change.id is None:
            change.id = len(items) + 1
        items[change.id] = change

    repository.save.side_effect = save
    repository.get.side_effect = lambda id, business_id: (
        items.get(id) if id in items and items[id].business_id == business_id else None
    )
    repository.pending.side_effect = lambda business_id=None: [
        item
        for item in items.values()
        if item.status == "pending" and not item.evidence.get("_service", {}).get("dry_run")
    ]
    repository.reverts.side_effect = lambda id: [item for item in items.values() if item.reverts_id == id]
    repository.business.return_value = Business(id=1, settings={})
    repository.experiments.return_value = []
    repository.local_entities.return_value = {}
    repository.campaign_names.return_value = {}
    repository.sync_local.side_effect = lambda change, operations: ChangesRepository.sync_local(
        repository, change, operations
    )
    return repository


@pytest.fixture
async def tree():
    adapter = FakeAdapter(moderation_delay=timedelta())
    plan = CampaignTreeSpec(
        "rollback",
        "Откат",
        10_000,
        300_000,
        (
            AdGroupSpec(
                "group",
                "Группа",
                (
                    AdSpec("control", "Контроль", "Прежний текст", "old-image"),
                    AdSpec("variant", "Вариант", "Новый текст", "new-image"),
                ),
            ),
        ),
    )
    return adapter, await adapter.create_campaign_tree(plan)


async def applied(repository, adapter, refs, *, operations=None, **kwargs):
    return await ChangesService(repository, dry_run=False).apply_change(
        business_id=1,
        adapter=adapter,
        refs=refs,
        operations=operations
        or [Operation("set_status", "ad", refs.ads["control"], "status", "active", "paused_auto")],
        actor="autopilot",
        kind="pause",
        platform="fake",
        reason_code="CUSTOM",
        reason_text="Остановили объявление.",
        **kwargs,
    )


async def test_rollback_appends_inverse_and_freezes_owner_ad(repository, tree):
    adapter, refs = tree
    now = datetime(2026, 10, 26, tzinfo=UTC)
    ad = Ad(id=1, ad_group_id=1, external_id=refs.ads["control"], frozen_until=None)
    repository.local_entities.return_value = {("ad", ad.external_id): ad}
    original = await applied(repository, adapter, refs)
    before = original.operations.copy()
    revert = await RollbackService(repository, dry_run=False).revert(
        original.id, business_id=1, adapter=adapter, now=now
    )
    assert revert.kind == "revert" and revert.reverts_id == original.id and revert.status == "applied"
    assert original.status == "reverted" and original.operations == before
    assert load_operations(revert)[0].after == "active"
    assert ad.status == "active" and ad.frozen_until == now + timedelta(days=14)
    assert (await adapter.get_state(refs))[ad.external_id].status == "active"
    with pytest.raises(ChangeConflict):
        await RollbackService(repository, dry_run=False).revert(original.id, business_id=1, adapter=adapter)
    with pytest.raises(ChangeConflict):
        await RollbackService(repository, dry_run=False).revert(revert.id, business_id=1, adapter=adapter)


@pytest.mark.parametrize(
    "reason", ["BUDGET_REBALANCE", "BUDGET_STOP", "SPEND_MISMATCH", "MONTH_STOP", "BUDGET_UPDATE_FAILED"]
)
async def test_budget_protection_cannot_be_rolled_back_even_with_confirmation(repository, tree, reason):
    adapter, refs = tree
    original = await applied(repository, adapter, refs)
    original.actor, original.reason_code = "system", reason
    before = await adapter.get_state(refs)
    with pytest.raises(ChangeConflict, match="защита бюджета, отменить нельзя"):
        await RollbackService(repository, dry_run=False).revert(
            original.id,
            business_id=1,
            adapter=adapter,
            confirm=True,
        )
    assert original.status == "applied" and await adapter.get_state(refs) == before


@pytest.mark.parametrize("stopped", ["budget_stop", "kill_switch"])
async def test_any_system_pause_is_protected_while_business_stopped(repository, tree, stopped):
    adapter, refs = tree
    original = await applied(repository, adapter, refs)
    original.actor = "system"
    business = repository.business(1)
    business.status = "budget_stop" if stopped == "budget_stop" else "paused"
    business.settings = {"_worker": {"kill_switch": stopped == "kill_switch"}}
    with pytest.raises(ChangeConflict, match="защита бюджета"):
        await RollbackService(repository, dry_run=False).revert(
            original.id,
            business_id=1,
            adapter=adapter,
            confirm=True,
        )


async def test_campaign_budget_rollback_freezes_campaign_without_freezing_its_ads(repository, tree):
    adapter, refs = tree
    campaign = Campaign(id=1, external_id=refs.campaign_id, daily_limit_kop=10_000, period_cap_kop=300_000)
    ads = [
        Ad(id=index, ad_group_id=1, external_id=external_id) for index, external_id in enumerate(refs.ads.values(), 1)
    ]
    repository.local_entities.return_value = {
        ("campaign", refs.campaign_id): campaign,
        **{("ad", ad.external_id): ad for ad in ads},
    }
    original = await applied(
        repository,
        adapter,
        refs,
        operations=[
            Operation("set_budget", "campaign", refs.campaign_id, "daily_limit_kop", 10_000, 12_000),
        ],
    )
    revert = await RollbackService(repository, dry_run=False).revert(original.id, business_id=1, adapter=adapter)
    assert revert.status == "applied"
    assert all(ad.frozen_until is None for ad in ads)
    frozen = repository.business(1).settings["_worker"]["frozen_entities"]
    assert set(frozen) == {f"campaign:{refs.campaign_id}"}


async def test_conflict_requires_confirmation_and_records_real_before(repository, tree):
    adapter, refs = tree
    original = await applied(repository, adapter, refs)
    await adapter.apply(Operation("set_status", "ad", refs.ads["control"], "status", "paused_auto", "paused_owner"))
    with pytest.raises(ConfirmationRequired) as caught:
        await RollbackService(repository, dry_run=False).revert(original.id, business_id=1, adapter=adapter)
    assert caught.value.changes[0] == {
        "entity_type": "ad",
        "entity_id": refs.ads["control"],
        "field": "status",
        "expected": "paused_auto",
        "current": "paused_owner",
        "restore": "active",
    }
    assert original.status == "applied" and repository.save.call_count == 2
    revert = await RollbackService(repository, dry_run=False).revert(
        original.id, business_id=1, adapter=adapter, confirm=True
    )
    assert load_operations(revert)[0].before == "paused_owner"
    assert load_operations(revert)[0].after == "active"


async def test_irreversible_ad_creation_pauses_then_archives(repository, tree, monkeypatch):
    adapter, refs = tree
    original = await applied(
        repository,
        adapter,
        refs,
        operations=[
            Operation("set_status", "ad", refs.ads["variant"], "status", "draft", "active", invertible=False),
        ],
    )
    apply = adapter.apply
    statuses = []

    async def observed(operation):
        statuses.append(operation.after)
        await apply(operation)

    monkeypatch.setattr(adapter, "apply", observed)
    revert = await RollbackService(repository, dry_run=False).revert(original.id, business_id=1, adapter=adapter)
    assert statuses == ["paused_owner", "archived"]
    assert (await adapter.get_state(refs))[refs.ads["variant"]].status == "archived"
    assert revert.status == "applied" and "архив" in revert.evidence["warning"]


async def test_image_rollback_restores_previous_reference(repository, tree):
    adapter, refs = tree
    original = await applied(
        repository,
        adapter,
        refs,
        operations=[
            Operation("swap_ad_image", "ad", refs.ads["control"], "image_ref", "old-image", "new-image"),
        ],
    )
    await RollbackService(repository, dry_run=False).revert(original.id, business_id=1, adapter=adapter)
    assert (await adapter.get_state(refs))[refs.ads["control"]].image_ref == "old-image"


async def test_external_change_can_only_be_reverted_by_owner(repository, tree):
    adapter, refs = tree
    original = await applied(repository, adapter, refs)
    original.actor = "external"
    with pytest.raises(ChangeConflict, match="только владелец"):
        await RollbackService(repository, dry_run=False).revert(
            original.id, business_id=1, adapter=adapter, actor="system"
        )
    assert original.status == "applied"
    revert = await RollbackService(repository, dry_run=False).revert(original.id, business_id=1, adapter=adapter)
    assert revert.status == "applied" and original.status == "reverted"


@pytest.mark.parametrize("status", ["pending", "failed", "reverted"])
async def test_unapplied_or_already_reverted_changes_are_rejected(repository, tree, status):
    adapter, refs = tree
    original = await applied(repository, adapter, refs)
    original.status = status
    with pytest.raises(ChangeConflict):
        await RollbackService(repository, dry_run=False).revert(original.id, business_id=1, adapter=adapter)


async def test_failed_rollback_keeps_original_applied_and_does_not_freeze(repository, tree, monkeypatch):
    adapter, refs = tree
    original = await applied(repository, adapter, refs)
    ad = Ad(id=1, ad_group_id=1, external_id=refs.ads["control"], frozen_until=None)
    repository.local_entities.return_value = {("ad", ad.external_id): ad}
    monkeypatch.setattr(adapter, "apply", AsyncMock(side_effect=PlatformError("outage")))
    revert = await RollbackService(repository, dry_run=False).revert(original.id, business_id=1, adapter=adapter)
    assert revert.status == "failed" and original.status == "applied" and ad.frozen_until is None


async def test_pending_rollback_is_finished_after_restart(repository, tree, monkeypatch):
    adapter, refs = tree
    original = await applied(repository, adapter, refs)
    apply = adapter.apply

    async def crashed(operation):
        await apply(operation)
        raise RuntimeError("restart")

    monkeypatch.setattr(adapter, "apply", crashed)
    with pytest.raises(RuntimeError):
        await RollbackService(repository, dry_run=False).revert(original.id, business_id=1, adapter=adapter)
    assert original.status == "applied"
    with pytest.raises(ChangeConflict):
        await RollbackService(repository, dry_run=False).revert(original.id, business_id=1, adapter=adapter)
    monkeypatch.setattr(adapter, "apply", AsyncMock(side_effect=AssertionError("must not repeat")))
    recovered = await ChangesService(repository, dry_run=False).recover_pending(lambda *_: adapter)
    assert recovered[0].status == "applied" and original.status == "reverted"
    adapter.apply.assert_not_called()


async def test_experiment_rollback_restores_control_and_freezes_group_ads(repository, tree):
    adapter, refs = tree
    ads = {
        ("ad", refs.ads[name]): Ad(id=index, ad_group_id=3, external_id=refs.ads[name], frozen_until=None)
        for index, name in enumerate(("control", "variant"), 1)
    }
    repository.local_entities.return_value = ads
    original = await applied(repository, adapter, refs)
    experiment = Experiment(id=1, control_ad_id=1, variant_ad_id=2, ad_group_id=3, stats={}, status="concluded")
    repository.experiments.return_value = [experiment]
    now = datetime.now(UTC)
    await RollbackService(repository, dry_run=False).revert(original.id, business_id=1, adapter=adapter, now=now)
    states = await adapter.get_state(refs)
    assert states[refs.ads["control"]].status == "active"
    assert states[refs.ads["variant"]].status == "paused_owner"
    assert experiment.status == "cancelled" and experiment.verdict == "control_wins"
    assert experiment.stats["manual_verdict"] == "variant_lost"
    assert all(ad.frozen_until == now + timedelta(days=14) for ad in ads.values())


async def verdict_change(repository, adapter, refs, *, loser: str) -> ChangeLog:
    return await ChangesService(repository, dry_run=False).apply_change(
        business_id=1,
        adapter=adapter,
        refs=refs,
        operations=[Operation("set_status", "ad", loser, "status", "active", "paused_auto")],
        actor="autopilot",
        kind="experiment_verdict",
        platform="fake",
        reason_code="EXPERIMENT_VERDICT",
        reason_text="Оставили прежнее объявление А, Б остановили.",
    )


@pytest.mark.parametrize("hypothesis", ["text", "image", "audience"])
async def test_verdict_rollback_restores_loser_and_leaves_winner(repository, tree, hypothesis):
    """Откат вывода сравнения возвращает проигравшему статус до вывода — объявление снова работает,
    победитель остаётся как есть; текст записи отката — по факту."""
    adapter, refs = tree
    ads = {
        ("ad", refs.ads[name]): Ad(
            id=index, ad_group_id=3, external_id=refs.ads[name], title=name, frozen_until=None
        )
        for index, name in enumerate(("control", "variant"), 1)
    }
    repository.local_entities.return_value = ads
    original = await verdict_change(repository, adapter, refs, loser=refs.ads["variant"])
    experiment = Experiment(
        id=1, control_ad_id=1, variant_ad_id=2, ad_group_id=3, stats={}, status="concluded", hypothesis=hypothesis
    )
    repository.experiments.return_value = [experiment]
    revert = await RollbackService(repository, dry_run=False).revert(original.id, business_id=1, adapter=adapter)
    states = await adapter.get_state(refs)
    assert states[refs.ads["variant"]].status == "active"
    assert states[refs.ads["control"]].status == "active"
    assert revert.reason_code == "EXPERIMENT_VERDICT_REVERT"
    assert revert.reason_text == "Вы отменили вывод сравнения вариантов: объявление «variant» снова работает."
    assert experiment.status == "cancelled"


async def test_verdict_rollback_when_variant_wins_does_not_pause_it(repository, tree):
    """Зеркальный случай: проиграл контроль. Откат снова включает контроль и не записывает победивший
    вариант остановленным владельцем."""
    adapter, refs = tree
    ads = {
        ("ad", refs.ads[name]): Ad(
            id=index, ad_group_id=3, external_id=refs.ads[name], title=name, frozen_until=None
        )
        for index, name in enumerate(("control", "variant"), 1)
    }
    repository.local_entities.return_value = ads
    original = await verdict_change(repository, adapter, refs, loser=refs.ads["control"])
    experiment = Experiment(id=1, control_ad_id=1, variant_ad_id=2, ad_group_id=3, stats={}, status="concluded")
    repository.experiments.return_value = [experiment]
    revert = await RollbackService(repository, dry_run=False).revert(original.id, business_id=1, adapter=adapter)
    states = await adapter.get_state(refs)
    assert states[refs.ads["control"]].status == "active"
    assert states[refs.ads["variant"]].status == "active"
    assert revert.reason_text == "Вы отменили вывод сравнения вариантов: объявление «control» снова работает."


async def test_verdict_rollback_when_loser_deleted_by_owner(repository, tree):
    """Проигравшее объявление владелец уже удалил в кабинете: откат не падает, не воскрешает его и честно
    пишет, что восстанавливать нечего."""
    adapter, refs = tree
    ads = {
        ("ad", refs.ads[name]): Ad(
            id=index, ad_group_id=3, external_id=refs.ads[name], title=name, frozen_until=None
        )
        for index, name in enumerate(("control", "variant"), 1)
    }
    repository.local_entities.return_value = ads
    original = await verdict_change(repository, adapter, refs, loser=refs.ads["variant"])
    await adapter.apply(Operation("set_status", "ad", refs.ads["variant"], "status", "paused_auto", "archived"))
    experiment = Experiment(id=1, control_ad_id=1, variant_ad_id=2, ad_group_id=3, stats={}, status="concluded")
    repository.experiments.return_value = [experiment]
    revert = await RollbackService(repository, dry_run=False).revert(
        original.id, business_id=1, adapter=adapter, confirm=True
    )
    states = await adapter.get_state(refs)
    assert states[refs.ads["variant"]].status == "archived"
    assert revert.status == "applied" and revert.operations == []
    assert revert.reason_text == (
        "Вы отменили вывод сравнения вариантов: объявление «variant» уже удалено "
        "в кабинете площадки — восстанавливать не стали."
    )
    assert experiment.status == "cancelled"


async def test_unknown_irreversible_entity_is_not_guessed(tree):
    adapter, refs = tree
    with pytest.raises(ValueError):
        inverse_operations(
            [Operation("set_status", "campaign", refs.campaign_id, "status", "draft", "active", invertible=False)],
            await adapter.get_state(refs),
        )


@pytest.fixture
def client(monkeypatch, repository):
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://test:test@invalid/test")
    monkeypatch.setenv("APP_BASE_URL", "https://testserver")
    monkeypatch.setenv("SECRET_KEY", "test-only-secret-key-0123456789abcdef")
    monkeypatch.setenv("TOKEN_ENCRYPTION_KEY", "test-only")
    monkeypatch.setenv("DRY_RUN", "false")

    from app.api.history import get_adapter_resolver, get_history_repository, router
    from app.auth.sessions import CSRF_COOKIE, SESSION_COOKIE, AuthRepository, get_repository
    from app.config import settings
    from app.db.models import Session as AuthSession
    from app.db.session import engine

    monkeypatch.setattr(settings, "dry_run", False)
    monkeypatch.setattr(engine, "connect", MagicMock(side_effect=AssertionError("unit test must not connect to DB")))
    auth = MagicMock(spec=AuthRepository)
    session = AuthSession(id="signed", user_id=7, csrf_secret="test-csrf")
    auth.get_session.return_value = session
    auth.get_user.return_value = User(id=7)
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_repository] = lambda: auth
    app.dependency_overrides[get_history_repository] = lambda: repository
    resolver = MagicMock()
    app.dependency_overrides[get_adapter_resolver] = lambda: resolver
    with TestClient(app, base_url="https://testserver") as client:
        client.cookies.set(SESSION_COOKIE, "signed")
        client.cookies.set(CSRF_COOKIE, "test-csrf")
        yield client, resolver, auth


def test_history_requires_login(client, repository):
    http, _, _ = client
    http.cookies.clear()
    assert http.get("/api/history").status_code == 401
    repository.history.assert_not_called()


def test_history_filters_and_pagination_are_scoped_to_current_user(client, repository):
    http, _, _ = client
    repository.history.return_value = ([], 12)
    response = http.get("/api/history?platform=fake&actor=owner&page=2&page_size=5&business_id=1")
    assert response.status_code == 200 and response.json() == {"items": [], "total": 12, "page": 2, "page_size": 5}
    repository.history.assert_called_once_with(7, business_id=1, platform="fake", actor="owner", page=2, page_size=5)


@pytest.mark.parametrize(
    "query", ["page=0", "page=100001", "page=9223372036854775807", "page_size=101", "actor=admin", "platform=unknown"]
)
def test_history_rejects_invalid_filters(client, query):
    http, _, _ = client
    assert http.get("/api/history?" + query).status_code == 422


def test_revert_requires_csrf_and_hides_other_business(client, repository):
    http, resolver, _ = client
    assert http.post("/api/history/9/revert").status_code == 403
    repository.get_owned.assert_not_called()
    repository.get_owned.return_value = None
    response = http.post("/api/history/9/revert?confirm=true", headers={"X-CSRF-Token": "test-csrf"})
    assert response.status_code == 404
    repository.get_owned.assert_called_once_with(9, 7)
    resolver.assert_not_called()


async def test_api_confirmation_and_revert(client, repository, tree):
    http, resolver, _ = client
    adapter, refs = tree
    original = await applied(repository, adapter, refs)
    repository.get_owned.return_value = original
    resolver.return_value = adapter
    await adapter.apply(Operation("set_status", "ad", refs.ads["control"], "status", "paused_auto", "paused_owner"))
    url = f"/api/history/{original.id}/revert"
    headers = {"X-CSRF-Token": "test-csrf"}
    response = http.post(url, headers=headers)
    assert response.status_code == 409 and response.json()["detail"]["confirmation_required"] is True
    response = http.post(url + "?confirm=true", headers=headers)
    assert response.status_code == 200 and response.json()["reverts_id"] == original.id
    assert response.json()["status"] == "applied"
    assert "_service" not in response.json()["evidence"]
    assert http.post(url + "?confirm=true", headers=headers).status_code == 409


def test_revert_response_names_campaign_like_history(client, repository, monkeypatch):
    http, _, _ = client
    repository.get_owned.return_value = ChangeLog(id=9, business_id=1, platform="fake")
    change = ChangeLog(
        id=10, business_id=1, platform="fake", kind="revert", entity_type="campaign", entity_id="c-1", operations=[]
    )
    monkeypatch.setattr(RollbackService, "revert", AsyncMock(return_value=change))
    repository.campaign_names.return_value = {("fake", "c-1"): "Маникюр"}
    response = http.post("/api/history/9/revert", headers={"X-CSRF-Token": "test-csrf"})
    assert response.status_code == 200 and response.json()["campaign"] == "Маникюр"
    repository.campaign_names.assert_called_once_with(1)


@pytest.mark.parametrize(
    "error,status",
    [(ServerBusy(), 503), (ChangeConflict("Изменение этого бизнеса уже выполняется. Попробуйте позже."), 409)],
)
def test_revert_busy_server_is_not_a_conflict_of_the_change(client, repository, monkeypatch, error, status):
    http, _, _ = client
    repository.get_owned.return_value = ChangeLog(id=9, business_id=1, platform="fake")
    monkeypatch.setattr(RollbackService, "revert", AsyncMock(side_effect=error))
    response = http.post("/api/history/9/revert", headers={"X-CSRF-Token": "test-csrf"})
    assert (response.status_code, response.json()["detail"]) == (status, str(error))


def test_repository_ownership_queries_include_business_owner():
    db = MagicMock(spec=Session)
    db.scalars.return_value = []
    db.scalar.return_value = 0
    repository = ChangesRepository(db)
    repository.history(7, business_id=1, platform="fake", actor="owner", page=2, page_size=5)
    history_sql = db.scalars.call_args.args[0].compile(dialect=dialect())
    assert "JOIN businesses" in str(history_sql) and "businesses.user_id" in str(history_sql)
    assert {7, 1, "fake", "owner", 5} <= set(history_sql.params.values())
    repository.get_owned(9, 7)
    owned_sql = db.scalar.call_args.args[0].compile(dialect=dialect())
    assert "JOIN businesses" in str(owned_sql) and "businesses.user_id" in str(owned_sql)
    assert {9, 7} <= set(owned_sql.params.values())


@pytest.mark.db
async def test_postgresql_rollback_updates_original_ad_and_experiment(tree, monkeypatch):
    database_url = os.environ.get("TEST_DATABASE_URL")
    if not database_url:
        pytest.skip("TEST_DATABASE_URL is required")
    from cryptography.fernet import Fernet

    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://test:test@invalid/test")
    monkeypatch.setenv("APP_BASE_URL", "https://testserver")
    monkeypatch.setenv("SECRET_KEY", "test-only-secret-key-0123456789abcdef")
    monkeypatch.setenv("TOKEN_ENCRYPTION_KEY", Fernet.generate_key().decode())
    from app.config import Settings

    monkeypatch.setattr("app.config.settings", Settings())
    engine = create_engine(database_url)
    try:
        if engine.dialect.name != "postgresql":
            pytest.skip("TEST_DATABASE_URL must point to PostgreSQL")
        with engine.connect() as connection:
            transaction = connection.begin()
            try:
                with Session(bind=connection, join_transaction_mode="create_savepoint", expire_on_commit=False) as db:
                    adapter, refs = tree
                    user = User(yandex_uid="rollback-" + uuid4().hex, login="test", consent_at=datetime.now(UTC))
                    db.add(user)
                    db.flush()
                    business = Business(
                        user_id=user.id, site_url="https://example.test", city="Test", month_budget_kop=0
                    )
                    db.add(business)
                    db.flush()
                    account = PlatformAccount(
                        business_id=business.id, platform="fake", auth_mode="oauth", access_token_enc="test-only"
                    )
                    db.add(account)
                    db.flush()
                    campaign = Campaign(
                        business_id=business.id,
                        platform_account_id=account.id,
                        platform="fake",
                        external_id=refs.campaign_id,
                        type="text",
                        name="Test",
                        status="active",
                        daily_limit_kop=10_000,
                        period_cap_kop=300_000,
                    )
                    db.add(campaign)
                    db.flush()
                    group = AdGroup(
                        campaign_id=campaign.id, external_id=refs.groups["group"], theme="Test", geo={}, status="active"
                    )
                    db.add(group)
                    db.flush()
                    ads = [
                        Ad(ad_group_id=group.id, external_id=refs.ads[name], title=name, text="Test", status="active")
                        for name in ("control", "variant")
                    ]
                    db.add_all(ads)
                    db.flush()
                    repository = ChangesRepository(db)
                    original = await ChangesService(repository, dry_run=False).apply_change(
                        business_id=business.id,
                        adapter=adapter,
                        refs=refs,
                        operations=[
                            Operation("set_status", "ad", ads[0].external_id, "status", "active", "paused_auto")
                        ],
                        actor="autopilot",
                        kind="experiment_verdict",
                        platform="fake",
                        reason_code="EXPERIMENT_NO_DIFF",
                    )
                    experiment = Experiment(
                        business_id=business.id,
                        ad_group_id=group.id,
                        hypothesis="text",
                        control_ad_id=ads[0].id,
                        variant_ad_id=ads[1].id,
                        metric="clicks",
                        planned_n_per_arm=1000,
                        min_effect=Decimal("0.1"),
                        deadline=datetime.now(UTC) + timedelta(days=14),
                        status="concluded",
                        verdict="variant_wins",
                        change_id=original.id,
                    )
                    db.add(experiment)
                    db.commit()
                    now = datetime.now(UTC)
                    revert = await RollbackService(repository, dry_run=False).revert(
                        original.id, business_id=business.id, adapter=adapter, now=now
                    )
                    db.expire_all()
                    assert db.get(ChangeLog, original.id).status == "reverted"
                    assert db.get(ChangeLog, revert.id).reverts_id == original.id
                    # Откат вывода сравнения возвращает статусы из записи изменения: контроль снова работает,
                    # вариант в операциях записи не участвовал — не трогаем (раньше здесь был принудительный
                    # paused_owner, из-за которого откат выглядел как остановка владельцем).
                    assert db.get(Ad, ads[0].id).status == "active"
                    assert db.get(Ad, ads[1].id).status == "active"
                    assert all(db.get(Ad, ad.id).frozen_until == now + timedelta(days=14) for ad in ads)
                    assert db.get(Experiment, experiment.id).stats["manual_verdict"] == "variant_lost"
            finally:
                transaction.rollback()
    finally:
        engine.dispose()


@pytest.mark.parametrize(
    "reason,actor,kind,status,kill_switch,expected",
    [
        ("BUDGET_REBALANCE", "system", "budget", "running", False, False),
        ("BUDGET_STOP", "system", "pause", "running", False, False),
        ("SPEND_MISMATCH", "system", "pause", "running", False, False),
        ("MONTH_STOP", "system", "pause", "running", False, False),
        ("BUDGET_UPDATE_FAILED", "system", "pause", "running", False, False),
        ("CUSTOM", "system", "pause", "budget_stop", False, False),
        ("CUSTOM", "system", "pause", "paused", True, False),
        ("REVERT", "owner", "revert", "running", False, False),
        # Запись о маркировке информационная: операций нет, откатывать нечего.
        ("MARKING_NOT_TRANSMITTED", "system", "marking", "running", False, False),
        ("CUSTOM", "autopilot", "pause", "running", False, True),
        ("CUSTOM", "external", "pause", "running", False, True),
    ],
)
async def test_history_and_server_share_revert_eligibility(
    client, repository, tree, reason, actor, kind, status, kill_switch, expected
):
    http, resolver, _ = client
    adapter, refs = tree
    original = await applied(repository, adapter, refs)
    original.reason_code, original.actor, original.kind = reason, actor, kind
    business = repository.business.return_value
    business.status = status
    business.settings = {"_worker": {"kill_switch": kill_switch}}
    repository.history.return_value = ([original], 1)
    repository.get_owned.return_value = original
    resolver.return_value = adapter
    response = http.get("/api/history")
    assert response.status_code == 200
    assert response.json()["items"][0]["can_revert"] is expected
    response = http.post(f"/api/history/{original.id}/revert", headers={"X-CSRF-Token": "test-csrf"})
    assert response.status_code == (200 if expected else 409)


async def test_history_hides_dry_run_and_pending_reverts(client, repository, tree):
    http, _, _ = client
    adapter, refs = tree
    original = await applied(repository, adapter, refs)
    repository.history.return_value = ([original], 1)
    assert http.get("/api/history").json()["items"][0]["can_revert"] is True
    original.evidence["_service"]["dry_run"] = True
    assert http.get("/api/history").json()["items"][0]["can_revert"] is False
    original.evidence["_service"]["dry_run"] = False
    repository.save(ChangeLog(kind="revert", reverts_id=original.id, status="pending", evidence={}))
    assert http.get("/api/history").json()["items"][0]["can_revert"] is False


async def test_launch_info_record_without_operations_is_not_revertable(client, repository):
    """Служебная запись запуска о пересозданном содержимом кабинета ничего не меняла на площадке: кнопки
    отмены у неё нет, а прямой вызов отката отвечает честным 409, а не общей ошибкой подготовки."""
    http, resolver, _ = client
    record = ChangeLog(
        id=5,
        business_id=1,
        actor="system",
        kind="launch",
        platform="fake",
        entity_type="campaign",
        entity_id="c-1",
        operations=[],
        reason_code="LAUNCH_CONTENT_RECREATED",
        reason_text="В кабинете объявление «Маникюр» группы «Маникюр» отличается от плана — создали заново.",
        evidence={},
        status="applied",
    )
    repository.history.return_value = ([record], 1)
    repository.save(record)
    repository.get_owned.return_value = record
    assert http.get("/api/history").json()["items"][0]["can_revert"] is False
    response = http.post("/api/history/5/revert", headers={"X-CSRF-Token": "test-csrf"})
    assert response.status_code == 409 and response.json()["detail"] == NOTHING_TO_REVERT


@pytest.mark.parametrize(
    "kind,reason_code,evidence,expected",
    [
        # Записи проверок откатываются по связанному эксперименту, хотя собственных операций у них нет.
        ("experiment_verdict", "EXPERIMENT_NO_DIFF", {"_service": {"refs": {"campaign_id": "c-1"}}}, True),
        ("launch", "EXPERIMENT_START", {"_service": {"refs": {"campaign_id": "c-1"}}}, True),
        # Записана без ссылок на площадку — откат по эксперименту не построить: это информационная запись.
        ("experiment_verdict", "EXPERIMENT_NO_DIFF", {}, False),
        ("launch", "EXPERIMENT_START", {}, False),
        ("launch", "LAUNCH_CONTENT_RECREATED", {}, False),
    ],
)
def test_empty_operations_records_revert_only_by_experiment(kind, reason_code, evidence, expected):
    change = ChangeLog(
        id=1,
        business_id=1,
        kind=kind,
        platform="fake",
        reason_code=reason_code,
        operations=[],
        evidence=evidence,
        status="applied",
    )
    assert reverts_by_experiment(change) is expected
    assert can_revert(change) is expected


async def test_history_does_not_offer_to_turn_on_campaign_of_replaced_plan(client, repository, tree):
    """Остановку кампании прежнего плана не вернуть: откат включил бы рекламу, за которой никто не следит. Остановку
    действующей кампании вернуть можно."""
    from app.services.rollback import REPLACED

    http, resolver, _ = client
    adapter, refs = tree
    stop = Operation("set_status", "campaign", refs.campaign_id, "status", "active", "paused_owner")
    original = await applied(repository, adapter, refs, operations=[stop])
    repository.history.return_value = ([original], 1)
    repository.get_owned.return_value = original
    resolver.return_value = adapter
    repository.replaced_entities.return_value = {("fake", refs.campaign_id)}
    assert http.get("/api/history").json()["items"][0]["can_revert"] is False
    response = http.post(f"/api/history/{original.id}/revert", headers={"X-CSRF-Token": "test-csrf"})
    assert response.status_code == 409 and response.json()["detail"] == REPLACED
    repository.replaced_entities.return_value = {("vk", refs.campaign_id)}
    assert http.get("/api/history").json()["items"][0]["can_revert"] is True


async def test_history_names_campaign_of_launch_and_stop(client, repository, tree):
    """Запуск и остановка пишутся по записи на кампанию: в журнале у каждой своё название, а не N одинаковых строк."""
    http, _, _ = client
    adapter, refs = tree
    stop = Operation("set_status", "campaign", refs.campaign_id, "status", "active", "paused_owner")
    campaign = await applied(
        repository, adapter, refs, operations=[stop], entity_type="campaign", entity_id=refs.campaign_id
    )
    ad = await applied(repository, adapter, refs)
    repository.history.return_value = ([campaign, ad], 2)
    repository.campaign_names.return_value = {("fake", refs.campaign_id): "Маникюр"}
    items = http.get("/api/history").json()["items"]
    # Объявление называется по кампании из своих ссылок: «Остановка рекламы» без названия не говорит, где.
    assert [item["campaign"] for item in items] == ["Маникюр", "Маникюр"]
    repository.campaign_names.assert_called_once_with(1)


async def test_history_lets_stop_launch_of_replaced_plan_campaign(client, repository, tree):
    """Включение кампании прежнего плана (запуск, который потом упал) откатывается к остановленной: реклама не
    включается, и REPLACED здесь был бы неправдой."""
    http, resolver, _ = client
    adapter, refs = tree
    await adapter.apply(Operation("set_status", "campaign", refs.campaign_id, "status", "active", "paused_auto"))
    start = Operation("set_status", "campaign", refs.campaign_id, "status", "paused_auto", "active")
    original = await applied(repository, adapter, refs, operations=[start])
    repository.history.return_value = ([original], 1)
    repository.get_owned.return_value = original
    resolver.return_value = adapter
    repository.replaced_entities.return_value = {("fake", refs.campaign_id)}
    assert http.get("/api/history").json()["items"][0]["can_revert"] is True
    response = http.post(f"/api/history/{original.id}/revert", headers={"X-CSRF-Token": "test-csrf"})
    assert response.status_code == 200
    assert (await adapter.get_state(refs))[refs.campaign_id].status == "paused_auto"


async def test_stop_resumed_later_is_not_offered_to_resume_again(client, repository, tree):
    """После «Продолжить» на главной старая остановка той же кампании в журнале без кнопки, и сервер её не отменяет;
    остановка другого объекта остаётся отменяемой."""
    from app.services.rollback import SUPERSEDED

    http, resolver, _ = client
    adapter, refs = tree
    stop = Operation("set_status", "campaign", refs.campaign_id, "status", "active", "paused_owner")
    start = Operation("set_status", "campaign", refs.campaign_id, "status", "paused_owner", "active")
    paused = await applied(repository, adapter, refs, operations=[stop], entity_type="campaign")
    ad = await applied(repository, adapter, refs)
    resumed = await applied(repository, adapter, refs, operations=[start], entity_type="campaign")
    resumed.kind = "resume"
    repository.history.return_value = ([resumed, ad, paused], 3)
    repository.status_changes.return_value = [paused, ad, resumed]
    repository.get_owned.return_value = paused
    resolver.return_value = adapter
    items = http.get("/api/history").json()["items"]
    assert [item["can_revert"] for item in items] == [True, True, False]
    assert repository.status_changes.call_args.args == (1, paused.id)
    response = http.post(f"/api/history/{paused.id}/revert", headers={"X-CSRF-Token": "test-csrf"})
    assert response.status_code == 409 and response.json()["detail"] == SUPERSEDED
    assert repository.status_changes.call_args.args == (1, paused.id)


def test_status_changes_skip_evidence_and_earlier_changes():
    """Для superseded() нужны лишь вид, площадка и операции изменений не раньше самого старого на странице."""
    db = MagicMock(spec=Session)
    db.scalars.return_value = []
    ChangesRepository(db).status_changes(1, 42)
    sql = db.scalars.call_args.args[0].compile(dialect=dialect())
    columns = str(sql).split(" FROM ")[0]
    assert "change_log.operations" in columns and "evidence" not in columns and "reason_text" not in columns
    assert "change_log.id >=" in str(sql) and 42 in sql.params.values()


def test_superseded_tolerates_old_operations_without_keys():
    """Старые записи журнала без field или entity_id в операциях не роняют журнал."""
    old = ChangeLog(id=1, kind="pause", platform="fake", operations=[{"action": "set_status"}])
    paused = ChangeLog(id=2, kind="pause", platform="fake", operations=[{"field": "status"}, {"entity_id": "c1"}])
    resumed = ChangeLog(id=3, kind="resume", platform="fake", operations=[{"field": "status", "entity_id": "c1"}])
    assert superseded([old, paused, resumed]) == set()


def test_turns_on_replaced_tolerates_old_operations_without_keys():
    """Старые записи журнала без entity_type, field или entity_id в операциях не роняют журнал."""
    old = ChangeLog(id=1, kind="pause", platform="fake", operations=[{"action": "set_status"}])
    stop = {"entity_type": "campaign", "field": "status", "before": "active", "entity_id": "c1"}
    new = ChangeLog(id=2, kind="pause", platform="fake", operations=[{"entity_id": "c1"}, stop])
    assert not turns_on_replaced(old, {("fake", "c1")})
    assert turns_on_replaced(new, {("fake", "c1")})


async def test_launch_foreign_stops_record_reverts_to_previous_statuses(repository):
    """Запись запуска об остановке чужого содержимого кабинета откатывается: объявление возвращается из
    архива, фраза снова активна. Плановые сущности откат не трогает."""
    from app.platforms.base import TargetSpec

    server = DirectServer()
    server.next_id = 200
    server.rows["campaigns"].append({"Id": 101, "Name": "Салон ap-salon"})
    server.rows["adgroups"].append(
        {
            "Id": 102,
            "Name": "Маникюр ap-nails",
            "CampaignId": 101,
            "RegionIds": [51],
            "NegativeKeywords": {"Items": []},
            "Status": "ACCEPTED",
        }
    )
    server.rows["ads"].append(
        {
            "Id": 103,
            "AdGroupId": 102,
            "CampaignId": 101,
            "State": "ON",
            "ResponsiveAd": {
                "Titles": [{"Title": "Маникюр со скидкой"}],
                "Texts": [{"Text": "Другой текст"}],
                "Href": "https://salon.example",
            },
        }
    )
    server.rows["keywords"].append(
        {"Id": 104, "AdGroupId": 102, "CampaignId": 101, "State": "ON", "Keyword": "педикюр"}
    )
    client = httpx.AsyncClient(transport=httpx.MockTransport(server))
    adapter = DirectAdapter(
        "test-only",
        config=configuration(),
        client=client,
        client_login="advertiser",
        counter_ids=(12,),
        href="https://salon.example",
        marking_confirmed=True,
    )
    adapter._today = lambda: date(2026, 10, 26)
    plan = CampaignTreeSpec(
        "salon",
        "Салон",
        100000,
        500000,
        (
            AdGroupSpec(
                "nails",
                "Маникюр",
                (AdSpec("ad", "Маникюр", "Запишитесь", "image-hash"),),
                (TargetSpec("phrase", "phrase", "маникюр"),),
                {"region_ids": [51]},
            ),
        ),
    )
    refs = await adapter.create_campaign_tree(plan)
    stops = adapter.foreign_stops
    assert [(op.entity_type, op.entity_id, op.before, op.after) for op in stops] == [
        ("ad", "103", "active", "archived"),
        ("target", "104", "active", "paused_owner"),
    ]
    # Запись такая, как её пишет запуск: операции остановок и ссылки, включающие остановленные сущности.
    stop_refs = replace(
        refs,
        ads=refs.ads | {f"foreign-ad:{op.entity_id}": op.entity_id for op in stops if op.entity_type == "ad"},
        targets=refs.targets
        | {f"foreign-target:{op.entity_id}": op.entity_id for op in stops if op.entity_type == "target"},
    )
    record = ChangeLog(
        business_id=1,
        actor="system",
        kind="launch",
        platform="direct",
        entity_type="campaign",
        entity_id=refs.campaign_id,
        operations=[asdict(op) for op in stops],
        reason_code="LAUNCH_FOREIGN_STOPPED",
        reason_params={},
        reason_text="В кабинете нашли объявления и фразы, которых нет в плане.",
        evidence={"_service": {"refs": asdict(stop_refs), "dry_run": False}},
        status="applied",
    )
    repository.save(record)
    revert = await RollbackService(repository, dry_run=False).revert(
        record.id, business_id=1, adapter=adapter
    )
    assert revert.status == "applied" and record.status == "reverted"
    states = await adapter.get_state(stop_refs)
    assert states["103"].status == "active" and states["104"].status == "active"
    await client.aclose()
