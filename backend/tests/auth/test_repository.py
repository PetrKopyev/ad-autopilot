from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock

import pytest
from sqlalchemy.dialects.postgresql import dialect
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session as DBSession

from app.db.models import OAuthState, PlatformAccount


@pytest.mark.parametrize("outcome", ["consumed", "already_used", "replayed_code"])
def test_consume_state_is_atomic_and_records_only_code_hash(client, outcome: str) -> None:
    from app.auth.sessions import AuthRepository

    db = MagicMock(spec=DBSession)
    state = OAuthState(state="state", purpose="login", expires_at=datetime.now(UTC) + timedelta(minutes=10))
    db.scalar.return_value = None if outcome == "already_used" else state
    if outcome == "replayed_code":
        db.commit.side_effect = IntegrityError("duplicate", {}, RuntimeError())
    result = AuthRepository(db).consume_state("state", "test-code")
    statement = db.scalar.call_args.args[0]
    sql = str(statement.compile(dialect=dialect()))
    assert sql.startswith("DELETE FROM oauth_states")
    assert "expires_at >" in sql and "RETURNING" in sql
    assert (result is state) is (outcome == "consumed")
    if outcome == "already_used":
        db.add.assert_not_called()
    else:
        marker = db.add.call_args.args[0]
        assert marker.state.startswith("used-code:")
        assert "test-code" not in marker.state
        assert marker.pkce_verifier is None
        assert marker.expires_at > datetime.now(UTC)
    if outcome != "consumed":
        db.rollback.assert_called_once()


def test_account_expiry_status_preserves_revocation_and_ownership_filter(client) -> None:
    from app.auth.sessions import AuthRepository

    now = datetime.now(UTC)
    accounts = [
        PlatformAccount(id=1, status="ok", expires_at=now - timedelta(seconds=1)),
        PlatformAccount(id=2, status="ok", expires_at=now + timedelta(hours=1)),
        PlatformAccount(id=3, status="revoked", expires_at=now - timedelta(seconds=1)),
        PlatformAccount(id=4, status="ok", expires_at=None),
    ]
    db = MagicMock(spec=DBSession)
    db.scalars.return_value = accounts
    assert AuthRepository(db).list_accounts(7) == accounts
    assert [account.status for account in accounts] == ["needs_reauth", "ok", "revoked", "ok"]
    statement = db.scalars.call_args.args[0].compile(dialect=dialect())
    assert "JOIN businesses" in str(statement)
    assert "businesses.user_id" in str(statement)
    assert 7 in statement.params.values()


@pytest.mark.parametrize("found", [None, 3])
def test_has_business_looks_only_at_users_businesses(client, found: int | None) -> None:
    from app.auth.sessions import AuthRepository

    db = MagicMock(spec=DBSession)
    db.scalar.return_value = found
    assert AuthRepository(db).has_business(7) is (found is not None)
    statement = db.scalar.call_args.args[0].compile(dialect=dialect())
    assert "businesses.user_id" in str(statement)
    assert 7 in statement.params.values()
