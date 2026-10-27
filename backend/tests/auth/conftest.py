import hashlib
from collections.abc import Iterator
from datetime import UTC, datetime

import pytest
import respx
from cryptography.fernet import Fernet
from fastapi import HTTPException
from fastapi.testclient import TestClient
from sqlalchemy.dialects.postgresql import dialect

from app.db.models import Business, OAuthState, PlatformAccount, Session, User


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line("markers", "db: requires an explicit TEST_DATABASE_URL and PostgreSQL")


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


class FakeRepository:
    def __init__(self) -> None:
        self.sessions: dict[str, Session] = {}
        self.states: dict[str, OAuthState] = {}
        self.users: dict[int, User] = {}
        self.businesses: dict[int, Business] = {}
        self.accounts: dict[int, PlatformAccount] = {}
        self.used_codes: set[str] = set()
        self.stored_tokens: dict[int, dict[str, str | None]] = {}
        # Остановка рекламы перед отзывом: (user_id, account_id, токен кабинета в момент остановки).
        self.stops: list[tuple[int, int | None, str | None]] = []
        self.stop_confirmed = True
        self.stop_error: Exception | None = None

    async def stop_ads(self, user_id: int, account_id: int | None, reason_text: str) -> list[str]:
        if self.stop_error:
            raise self.stop_error
        account = self.accounts.get(account_id) if account_id else None
        self.stops.append((user_id, account_id, account.access_token_enc if account else None))
        return [] if self.stop_confirmed else ["Яндекс Директ: площадка не подтвердила остановку"]

    def get_session(self, token: str) -> Session | None:
        session = self.sessions.get(hash_token(token))
        return session if session and session.expires_at > datetime.now(UTC) else None

    def get_user(self, user_id: int) -> User | None:
        return self.users.get(user_id)

    def save_session(self, session: Session, previous_token: str | None = None) -> None:
        self.sessions.pop(hash_token(previous_token) if previous_token else None, None)
        self.sessions[session.id] = session

    def delete_session(self, session_id: str) -> None:
        self.sessions.pop(session_id, None)

    def delete_user(self, user_id: int, wait: float = 0) -> list[int]:
        business_ids = [business.id for business in self.businesses.values() if business.user_id == user_id]
        self.users.pop(user_id, None)
        self.sessions = {key: item for key, item in self.sessions.items() if item.user_id != user_id}
        self.businesses = {key: item for key, item in self.businesses.items() if key not in business_ids}
        return business_ids

    def save_state(self, state: OAuthState) -> None:
        self.states[state.state] = state

    def get_state(self, state: str) -> OAuthState | None:
        return self.states.get(state)

    def consume_state(self, state: str, code: str | None) -> OAuthState | None:
        fingerprint = hashlib.sha256(code.encode()).hexdigest() if code else None
        item = self.states.get(state)
        if not item or item.expires_at <= datetime.now(UTC) or fingerprint in self.used_codes:
            return None
        if fingerprint:
            self.used_codes.add(fingerprint)
        return self.states.pop(state)

    def save_user(self, uid: str, login: str, email: str | None) -> User:
        user = next((user for user in self.users.values() if user.yandex_uid == uid), None)
        if user is None:
            now = datetime.now(UTC)
            user = User(id=max(self.users, default=0) + 1, yandex_uid=uid, consent_at=now, created_at=now)
            self.users[user.id] = user
        user.login = login
        user.email = email
        return user

    def get_business(self, user_id: int, business_id: int | None) -> Business:
        businesses = [
            business
            for business in self.businesses.values()
            if business.user_id == user_id and (business_id is None or business.id == business_id)
        ]
        if not businesses:
            raise HTTPException(404, "Сначала заполните сведения о бизнесе.")
        if len(businesses) != 1:
            raise HTTPException(400, "Выберите бизнес для подключения кабинета.")
        return businesses[0]

    def save_account(self, business_id: int, login: str, tokens: dict) -> PlatformAccount:
        from app.auth.sessions import AuthRepository

        class FakeDB:
            def __init__(self, repository: FakeRepository) -> None:
                self.repository = repository
                self.reads = iter(
                    [
                        repository.businesses[business_id],
                        next((item for item in repository.accounts.values() if item.business_id == business_id), None),
                    ]
                )

            def scalar(self, statement):
                return next(self.reads)

            def add(self, account: PlatformAccount) -> None:
                account.id = len(self.repository.accounts) + 1
                self.repository.accounts[account.id] = account

            def commit(self) -> None:
                for account in self.repository.accounts.values():
                    self.repository.stored_tokens[account.id] = {
                        name: PlatformAccount.__table__.c[name].type.bind_processor(dialect())(getattr(account, name))
                        for name in ("access_token_enc", "refresh_token_enc")
                    }

        return AuthRepository(FakeDB(self)).save_account(business_id, login, tokens)

    def has_business(self, user_id: int) -> bool:
        return any(business.user_id == user_id for business in self.businesses.values())

    def list_accounts(self, user_id: int) -> list[PlatformAccount]:
        return [
            account for account in self.accounts.values() if self.businesses[account.business_id].user_id == user_id
        ]

    def revocations(self, user_id: int, account_id: int | None = None) -> list:
        from app.auth.revoke import revocation, same_vk_access

        mine = [
            account
            for account in self.accounts.values()
            if self.businesses[account.business_id].user_id == user_id
            and account.status != "revoked"
            and account_id in (None, account.id)
        ]
        removed = {account.id for account in mine} if account_id is None else {account_id}
        result = []
        for account in mine:
            shared = account.platform == "vk" and any(
                same_vk_access(account, other) and other.status != "revoked" and other.id not in removed
                for other in self.accounts.values()
            )
            if not shared and (item := revocation(account)) is not None:
                result.append(item)
        return result

    def disconnect_account(self, user_id: int, account_id: int) -> bool:
        from app.auth.sessions import AuthRepository

        repository = self

        class FakeDB:
            def scalar(self, statement):
                account = repository.accounts.get(account_id)
                if account and repository.businesses[account.business_id].user_id == user_id:
                    return account
                return None

            def scalars(self, statement):
                # Неподтверждённые изменения при отключении кабинета проверяет тест с базой.
                return []

            def commit(self) -> None:
                pass

        return AuthRepository(FakeDB()).disconnect_account(user_id, account_id)


class FakeDemoUsers:
    def __init__(self, repository: FakeRepository) -> None:
        self.repository = repository

    def demo(self) -> list[User]:
        return [user for user in self.repository.users.values() if user.yandex_uid.startswith("demo-")]

    def expire(self, before: datetime) -> list[int]:
        expired = {user.id for user in self.demo() if user.created_at < before}
        return [business_id for user_id in expired for business_id in self.repository.delete_user(user_id)]

    def missing(self, business_ids: set[int]) -> set[int]:
        return business_ids - set(self.repository.businesses)

    def count(self) -> int:
        return len(self.demo())


@pytest.fixture
def repository() -> FakeRepository:
    return FakeRepository()


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch, repository: FakeRepository) -> Iterator[TestClient]:
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://test:test@invalid/test")
    monkeypatch.setenv("APP_BASE_URL", "https://testserver")
    monkeypatch.setenv("SECRET_KEY", "test-only-secret-key-0123456789abcdef")
    monkeypatch.setenv("TOKEN_ENCRYPTION_KEY", Fernet.generate_key().decode())
    monkeypatch.setenv("PLATFORM_MODE", "fake")
    monkeypatch.setenv("YANDEX_CLIENT_ID", "test-client")
    monkeypatch.setenv("YANDEX_CLIENT_SECRET", "test-secret")

    from unittest.mock import AsyncMock

    from app.api.auth import get_ad_stopper, get_demo_users
    from app.auth import yandex
    from app.auth.sessions import get_repository
    from app.config import Settings
    from app.db.session import engine
    from app.main import app

    monkeypatch.setattr("app.main.start_scheduler", AsyncMock(return_value=None))

    settings = Settings()
    monkeypatch.setattr("app.config.settings", settings)
    monkeypatch.setattr(yandex, "settings", settings)

    def forbid_database(*args, **kwargs):
        pytest.fail("Auth unit tests must not connect to a database")

    monkeypatch.setattr(engine, "connect", forbid_database)
    app.dependency_overrides[get_repository] = lambda: repository
    app.dependency_overrides[get_demo_users] = lambda: FakeDemoUsers(repository)
    app.dependency_overrides[get_ad_stopper] = lambda: repository.stop_ads
    with TestClient(app, base_url="https://testserver", follow_redirects=False) as test_client:
        yield test_client
    app.dependency_overrides.pop(get_repository, None)
    app.dependency_overrides.pop(get_demo_users, None)
    app.dependency_overrides.pop(get_ad_stopper, None)


@pytest.fixture
def mock_yandex() -> Iterator[respx.MockRouter]:
    with respx.mock(assert_all_called=False) as router:
        router.post("https://oauth.yandex.ru/token").respond(
            json={
                "access_token": "test-access",
                "refresh_token": "test-refresh",
                "expires_in": 3600,
                "token_type": "bearer",
            }
        )
        router.post("https://oauth.yandex.ru/revoke_token", name="revoke").respond(json={"status": "ok"})
        router.get("https://login.yandex.ru/info").respond(
            json={
                "id": "100",
                "login": "owner",
                "default_email": "owner@example.test",
            }
        )
        yield router
