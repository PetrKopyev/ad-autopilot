import os
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from cryptography.fernet import Fernet
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session as DBSession

from app.db.models import Business, User


@pytest.mark.db
def test_tokens_are_encrypted_in_postgresql(monkeypatch: pytest.MonkeyPatch) -> None:
    database_url = os.environ.get("TEST_DATABASE_URL")
    if not database_url:
        pytest.skip("TEST_DATABASE_URL is required")
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://test:test@invalid/test")
    monkeypatch.setenv("APP_BASE_URL", "https://testserver")
    monkeypatch.setenv("SECRET_KEY", "test-only-secret-key-0123456789abcdef")
    monkeypatch.setenv("TOKEN_ENCRYPTION_KEY", Fernet.generate_key().decode())

    from app.auth.sessions import AuthRepository
    from app.config import Settings

    monkeypatch.setattr("app.config.settings", Settings())
    engine = create_engine(database_url)
    try:
        if engine.dialect.name != "postgresql":
            pytest.skip("TEST_DATABASE_URL must point to PostgreSQL")
        with engine.connect() as connection:
            transaction = connection.begin()
            try:
                with DBSession(bind=connection, join_transaction_mode="create_savepoint") as db:
                    user = User(yandex_uid="auth-test-" + uuid4().hex, login="test", consent_at=datetime.now(UTC))
                    db.add(user)
                    db.flush()
                    business = Business(
                        user_id=user.id, site_url="https://example.test", city="Test", month_budget_kop=0
                    )
                    db.add(business)
                    db.flush()
                    account = AuthRepository(db).save_account(
                        business.id,
                        "advertiser",
                        {
                            "access_token": "test-access",
                            "refresh_token": "test-refresh",
                            "expires_at": None,
                            "scopes": ["direct:api", "metrika:read"],
                        },
                    )
                    stored = db.execute(
                        text("SELECT access_token_enc, refresh_token_enc FROM platform_accounts WHERE id = :id"),
                        {"id": account.id},
                    ).one()
                    assert stored.access_token_enc != "test-access"
                    assert stored.refresh_token_enc != "test-refresh"
                    db.expire_all()
                    assert db.get(type(account), account.id).access_token_enc == "test-access"
            finally:
                transaction.rollback()
    finally:
        engine.dispose()


@pytest.mark.db
def test_demo_users_expire_with_businesses_and_files() -> None:
    database_url = os.environ.get("TEST_DATABASE_URL")
    if not database_url:
        pytest.skip("TEST_DATABASE_URL is required")
    from app.db.models import Asset
    from app.services.demo import DemoUsers

    engine = create_engine(database_url)
    try:
        if engine.dialect.name != "postgresql":
            pytest.skip("TEST_DATABASE_URL must point to PostgreSQL")
        with engine.connect() as connection:
            transaction = connection.begin()
            try:
                with DBSession(bind=connection, join_transaction_mode="create_savepoint") as db:
                    now = datetime.now(UTC)
                    users = DemoUsers(db)
                    before = users.count()
                    old, fresh, owner = (
                        User(yandex_uid=uid + uuid4().hex, login="demo", consent_at=now, created_at=created)
                        for uid, created in (
                            ("demo-", now - timedelta(hours=25)),
                            ("demo-", now),
                            ("owner-", now - timedelta(days=30)),
                        )
                    )
                    db.add_all([old, fresh, owner])
                    db.flush()
                    business = Business(
                        user_id=old.id, site_url="https://example.test", city="Test", month_budget_kop=0
                    )
                    db.add(business)
                    db.flush()
                    db.add(Asset(business_id=business.id, kind="image", file_path=f"{business.id}/a.jpg"))
                    db.flush()
                    # После commit в expire объекты перечитываются, а удалённые строки уже недоступны.
                    old_id, fresh_id, owner_id, business_id = old.id, fresh.id, owner.id, business.id
                    assert users.count() == before + 2
                    assert users.expire(now - timedelta(hours=24)) == [business_id]
                    db.expire_all()
                    assert db.get(User, old_id) is None and db.get(Business, business_id) is None
                    assert db.get(User, fresh_id) and db.get(User, owner_id)
                    assert users.count() == before + 1
            finally:
                transaction.rollback()
    finally:
        engine.dispose()


@pytest.mark.db
def test_demo_reset_frees_demo_places_now(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    database_url = os.environ.get("TEST_DATABASE_URL")
    if not database_url:
        pytest.skip("TEST_DATABASE_URL is required")
    from app.config import settings
    from app.demo_reset import reset

    monkeypatch.setattr(settings, "media_dir", tmp_path)
    engine = create_engine(database_url)
    try:
        with engine.connect() as connection:
            transaction = connection.begin()
            try:
                with DBSession(bind=connection, join_transaction_mode="create_savepoint") as db:
                    now = datetime.now(UTC)
                    demo, owner = (
                        User(yandex_uid=uid + uuid4().hex, login="demo", consent_at=now, created_at=now)
                        for uid in ("demo-", "owner-")
                    )
                    db.add_all([demo, owner])
                    db.flush()
                    business = Business(
                        user_id=demo.id, site_url="https://example.test", city="Test", month_budget_kop=0
                    )
                    db.add(business)
                    db.flush()
                    media = tmp_path / str(business.id)
                    media.mkdir()
                    (media / "a.jpg").write_bytes(b"jpg")
                    demo_id, owner_id = demo.id, owner.id
                    assert reset(db, now + timedelta(seconds=1)) == 50
                    db.expire_all()
                    assert db.get(User, demo_id) is None and db.get(User, owner_id)
                    assert not media.exists()
            finally:
                transaction.rollback()
    finally:
        engine.dispose()


def test_demo_reset_refuses_outside_demo(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.config import settings
    from app.demo_reset import main

    monkeypatch.setattr(settings, "demo_login", False)
    with pytest.raises(SystemExit, match="только на демо-стенде"):
        main()


@pytest.mark.db
def test_users_are_deleted_under_business_locks() -> None:
    """Перемотка или сборка ролика держат блокировку бизнеса: демо-пользователь остаётся до следующего входа, а
    удаление аккаунта дожидается блокировки и не даёт фоновой задаче записать файлы после удаления каталога."""
    database_url = os.environ.get("TEST_DATABASE_URL")
    if not database_url:
        pytest.skip("TEST_DATABASE_URL is required")
    from threading import Timer

    from app.auth.sessions import AuthRepository
    from app.services.changes import ChangeConflict
    from app.services.demo import DemoUsers

    engine = create_engine(database_url)
    try:
        with engine.connect() as connection, engine.connect() as holder:
            transaction = connection.begin()
            try:
                with DBSession(bind=connection, join_transaction_mode="create_savepoint") as db:
                    now = datetime.now(UTC)
                    old = now - timedelta(days=2)
                    users = [
                        User(yandex_uid="demo-" + uuid4().hex, login="demo", consent_at=now, created_at=old)
                        for _ in range(2)
                    ]
                    db.add_all(users)
                    db.flush()
                    busy, free = (
                        Business(user_id=user.id, site_url="https://example.test", city="Test", month_budget_kop=0)
                        for user in users
                    )
                    db.add_all([busy, free])
                    db.flush()
                    busy_user, busy_id, free_id = users[0].id, busy.id, free.id
                    holder.execute(text("SELECT pg_advisory_lock(:id)"), {"id": busy_id})
                    removed = DemoUsers(db).expire(now - timedelta(hours=24))
                    assert free_id in removed and busy_id not in removed
                    db.expire_all()
                    assert db.get(Business, busy_id) is not None and db.get(Business, free_id) is None
                    with pytest.raises(ChangeConflict):
                        AuthRepository(db).delete_user(busy_user)
                    Timer(1, lambda: holder.execute(text("SELECT pg_advisory_unlock(:id)"), {"id": busy_id})).start()
                    assert AuthRepository(db).delete_user(busy_user, wait=10) == [busy_id]
                    db.expire_all()
                    assert db.get(Business, busy_id) is None
            finally:
                transaction.rollback()
    finally:
        engine.dispose()
