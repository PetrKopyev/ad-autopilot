"""В11: учёт трат на ИИ идёт по ai_spend_ledger — таблице без внешних ключей.

Недельный пул владельца и суточный потолок считаются по хэшу yandex_uid, а не по id пользователя
или JOIN с бизнесами: удаление бизнеса или аккаунта не обнуляет лимит, иначе трату оператору
можно было бы сжигать бесконечным пересозданием аккаунта.
"""

import os
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session


@pytest.mark.db
async def test_owner_ai_limit_survives_account_deletion(monkeypatch):
    database_url = os.environ.get("TEST_DATABASE_URL")
    if not database_url:
        pytest.skip("TEST_DATABASE_URL is required")
    from cryptography.fernet import Fernet

    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://test:test@invalid/test")
    monkeypatch.setenv("APP_BASE_URL", "https://testserver")
    monkeypatch.setenv("SECRET_KEY", "test-only-secret-key-0123456789abcdef")
    monkeypatch.setenv("TOKEN_ENCRYPTION_KEY", Fernet.generate_key().decode())
    # Пул в reserve_ai берётся из серверного конфига: приводим его к лимитам теста (50 ₽ + 10 ₽ = 6000 коп).
    monkeypatch.setenv("AI_SPEND_WEEKLY_LIMIT_RUB", "50")
    monkeypatch.setenv("AI_SPEND_ONBOARDING_LIMIT_RUB", "10")
    monkeypatch.setenv("AI_DAILY_CAP_RUB", "0")
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
                    from app.db.models import AiSpendLedger, Business, User
                    from app.services.onboarding import AiLimits, OnboardingRepository, ai_budget

                    repository = OnboardingRepository(db)
                    limits = AiLimits(onboarding_kop=1000, weekly_kop=5000, daily_cap_kop=0)
                    yandex_uid = "ai-spend-" + uuid4().hex
                    day = datetime.now(UTC).date()

                    user = User(yandex_uid=yandex_uid, login="test", consent_at=datetime.now(UTC))
                    db.add(user)
                    db.flush()
                    business = Business(
                        user_id=user.id, site_url="https://example.test", city="Test", month_budget_kop=0
                    )
                    db.add(business)
                    db.flush()

                    # Трата на ИИ: резерв до вызова уточняется до фактической стоимости.
                    reservation = repository.reserve_ai(business.id, "llm", 800)
                    assert reservation is not None
                    repository.settle_ai(reservation, 800)
                    owner = repository.owner_key(business)
                    assert owner is not None
                    assert repository.ai_spent_for_owner_key(owner) == 800

                    # Удаление аккаунта каскадом сносит бизнес и llm_calls/art_calls, но не ledger.
                    db.delete(user)
                    db.flush()
                    assert db.scalar(select(AiSpendLedger.cost_kop).where(AiSpendLedger.owner_key == owner)) == 800

                    # Повторный вход тем же Яндекс ID: тот же ключ владельца, тот же израсходованный пул.
                    user2 = User(yandex_uid=yandex_uid, login="test", consent_at=datetime.now(UTC))
                    db.add(user2)
                    db.flush()
                    business2 = Business(
                        user_id=user2.id, site_url="https://example.test", city="Test", month_budget_kop=0
                    )
                    db.add(business2)
                    db.flush()
                    owner2 = repository.owner_key(business2)
                    assert owner2 == owner
                    assert repository.ai_spent_for_owner_key(owner2) == 800

                    # Дожимаем пул до предела: 6000 − 800 − 4400 = 800 остаётся новому бизнесу владельца.
                    reservation = repository.reserve_ai(business2.id, "llm", 4400)
                    assert reservation is not None
                    repository.settle_ai(reservation, 4400)
                    budget = ai_budget(repository, business2, limits, day)
                    assert budget.limit_kop == 800

                    # Следующая трата выше остатка отклоняется: пул не бесконечен.
                    with pytest.raises(Exception, match="Лимит трат на ИИ"):
                        repository.reserve_ai(business2.id, "llm", 801)

                    # Отказ до ответа снимает резерв: деньги не списались.
                    refused = repository.reserve_ai(business2.id, "llm", 500)
                    assert refused is not None
                    repository.release_ai(refused)
                    assert repository.ai_spent_for_owner_key(owner2) == 5200
            finally:
                transaction.rollback()
    finally:
        engine.dispose()


@pytest.mark.db
async def test_history_and_get_owned_are_scoped_to_owner_on_postgresql(monkeypatch):
    """В12: на настоящей Postgres чужая история и чужая запись изменения недоступны (IDOR):
    фильтр владельца выполняется в SQL, а не в памяти."""
    database_url = os.environ.get("TEST_DATABASE_URL")
    if not database_url:
        pytest.skip("TEST_DATABASE_URL is required")
    from cryptography.fernet import Fernet

    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://test:test@invalid/test")
    monkeypatch.setenv("APP_BASE_URL", "https://testserver")
    monkeypatch.setenv("SECRET_KEY", "test-only-secret-key-0123456789abcdef")
    monkeypatch.setenv("TOKEN_ENCRYPTION_KEY", Fernet.generate_key().decode())
    from sqlalchemy import create_engine

    engine = create_engine(database_url)
    try:
        if engine.dialect.name != "postgresql":
            pytest.skip("TEST_DATABASE_URL must point to PostgreSQL")
        with engine.connect() as connection:
            transaction = connection.begin()
            try:
                with Session(bind=connection, join_transaction_mode="create_savepoint", expire_on_commit=False) as db:
                    from app.db.models import Business, ChangeLog, User
                    from app.services.changes import ChangesRepository

                    users = [
                        User(yandex_uid=f"idor-{uuid4().hex}", login=f"user{n}", consent_at=datetime.now(UTC))
                        for n in (1, 2)
                    ]
                    db.add_all(users)
                    db.flush()
                    first, second = (
                        Business(user_id=user.id, site_url=f"https://{n}.test", city="Test", month_budget_kop=0)
                        for user, n in zip(users, (1, 2), strict=True)
                    )
                    db.add_all([first, second])
                    db.flush()
                    mine = ChangeLog(
                        business_id=first.id,
                        actor="autopilot",
                        kind="pause",
                        platform="direct",
                        entity_type="ad",
                        entity_id="ad-1",
                        operations=[],
                        reason_code="LOW_CTR",
                        reason_params={},
                        reason_text="Текст.",
                        status="applied",
                        created_at=datetime.now(UTC),
                    )
                    theirs = ChangeLog(
                        business_id=second.id,
                        actor="autopilot",
                        kind="pause",
                        platform="direct",
                        entity_type="ad",
                        entity_id="ad-2",
                        operations=[],
                        reason_code="LOW_CTR",
                        reason_params={},
                        reason_text="Текст.",
                        status="applied",
                        created_at=datetime.now(UTC),
                    )
                    db.add_all([mine, theirs])
                    db.flush()
                    repository = ChangesRepository(db)
                    # Чужая запись не находится, своя — находится; история каждого видит только его бизнес.
                    assert repository.get_owned(theirs.id, users[0].id) is None
                    assert repository.get_owned(mine.id, users[0].id) is not None
                    mine_history = repository.history(users[0].id)
                    theirs_history = repository.history(users[1].id)
                    assert [item.id for item in mine_history[0]] == [mine.id] and mine_history[1] == 1
                    assert [item.id for item in theirs_history[0]] == [theirs.id] and theirs_history[1] == 1
            finally:
                transaction.rollback()
    finally:
        engine.dispose()


@pytest.mark.db
async def test_sweep_finalizes_stale_open_reservations(monkeypatch):
    """В4: резерв (final=false) старше TTL фиксируется по оценке — запрос мог уйти и списать деньги
    после отмены задачи; свежий резерв не трогаем. Свипер работает под локом reserve_ai."""
    database_url = os.environ.get("TEST_DATABASE_URL")
    if not database_url:
        pytest.skip("TEST_DATABASE_URL is required")
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://test:test@invalid/test")
    monkeypatch.setenv("APP_BASE_URL", "https://testserver")
    monkeypatch.setenv("SECRET_KEY", "test-only-secret-key-0123456789abcdef")
    monkeypatch.setenv("TOKEN_ENCRYPTION_KEY", "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=")
    monkeypatch.setenv("AI_SPEND_WEEKLY_LIMIT_RUB", "50")
    monkeypatch.setenv("AI_SPEND_ONBOARDING_LIMIT_RUB", "10")
    monkeypatch.setenv("AI_DAILY_CAP_RUB", "0")
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
                    from datetime import timedelta

                    from app.db.models import AiSpendLedger, Business, User
                    from app.services.onboarding import AI_RESERVATION_TTL, OnboardingRepository

                    repository = OnboardingRepository(db)
                    user = User(yandex_uid="ai-sweep-" + uuid4().hex, login="test", consent_at=datetime.now(UTC))
                    db.add(user)
                    db.flush()
                    business = Business(
                        user_id=user.id, site_url="https://example.test", city="Test", month_budget_kop=0
                    )
                    db.add(business)
                    db.flush()
                    owner = repository.owner_key(business)
                    now = datetime.now(UTC)
                    stale = AiSpendLedger(
                        owner_key=owner,
                        business_id=business.id,
                        kind="llm",
                        cost_kop=100,
                        created_at=now - AI_RESERVATION_TTL - timedelta(minutes=1),
                    )
                    fresh = AiSpendLedger(
                        owner_key=owner, business_id=business.id, kind="llm", cost_kop=200, created_at=now
                    )
                    db.add_all([stale, fresh])
                    db.flush()

                    # reserve_ai поднимает свипер: просроченный резерв закрыт, свежий остался открытым.
                    reservation = repository.reserve_ai(business.id, "llm", 50)
                    assert reservation is not None
                    assert db.get(AiSpendLedger, stale.id).final is True
                    assert db.get(AiSpendLedger, fresh.id).final is False
            finally:
                transaction.rollback()
    finally:
        engine.dispose()


@pytest.mark.db
async def test_owner_key_is_hmac_with_dedicated_secret(monkeypatch):
    """В9: псевдоним владельца — HMAC с AI_OWNER_KEY_SECRET, а не голый sha256 от yandex_uid:
    идентификатор Яндекса из малого числового диапазона восстанавливался бы перебором."""
    database_url = os.environ.get("TEST_DATABASE_URL")
    if not database_url:
        pytest.skip("TEST_DATABASE_URL is required")
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://test:test@invalid/test")
    monkeypatch.setenv("APP_BASE_URL", "https://testserver")
    monkeypatch.setenv("SECRET_KEY", "test-only-secret-key-0123456789abcdef")
    monkeypatch.setenv("TOKEN_ENCRYPTION_KEY", "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=")
    monkeypatch.setenv("AI_OWNER_KEY_SECRET", "dedicated-owner-secret")
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
                    import hashlib
                    import hmac

                    from app.db.models import Business, User
                    from app.services.onboarding import OnboardingRepository

                    repository = OnboardingRepository(db)
                    yandex_uid = "42"
                    user = User(yandex_uid=yandex_uid, login="test", consent_at=datetime.now(UTC))
                    db.add(user)
                    db.flush()
                    business = Business(
                        user_id=user.id, site_url="https://example.test", city="Test", month_budget_kop=0
                    )
                    db.add(business)
                    db.flush()
                    owner = repository.owner_key(business)
                    assert owner == hmac.new(
                        b"dedicated-owner-secret", yandex_uid.encode(), hashlib.sha256
                    ).hexdigest()
                    assert owner != hashlib.sha256(yandex_uid.encode()).hexdigest()
                    # Ротация ключа меняет псевдоним — документированный эффект из .env.example.
                    monkeypatch.setenv("AI_OWNER_KEY_SECRET", "rotated-secret")
                    monkeypatch.setattr("app.config.settings", Settings())
                    assert repository.owner_key(business) != owner
            finally:
                transaction.rollback()
    finally:
        engine.dispose()
