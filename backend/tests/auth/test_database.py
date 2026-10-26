import os
from datetime import UTC, datetime
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
    monkeypatch.setenv("SECRET_KEY", "test-only")
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
