import os
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.dialects.postgresql import dialect
from sqlalchemy.orm import Session

from app.db.models import Ad, AdGroup, Business, Campaign, ChangeLog, Experiment, PlatformAccount, User
from app.platforms.base import AdGroupSpec, AdSpec, CampaignTreeSpec, Operation, PlatformError
from app.platforms.fake import FakeAdapter
from app.services.changes import ChangeConflict, ChangesRepository, ChangesService, load_operations
from app.services.rollback import ConfirmationRequired, RollbackService, inverse_operations


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
            original.id, business_id=1, adapter=adapter, confirm=True,
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


@pytest.mark.parametrize("query", ["page=0", "page_size=101", "actor=admin", "platform=unknown"])
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
                    assert db.get(Ad, ads[0].id).status == "active"
                    assert db.get(Ad, ads[1].id).status == "paused_owner"
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
