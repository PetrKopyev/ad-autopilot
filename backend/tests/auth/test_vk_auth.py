import asyncio
import json
import logging
from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
import respx
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy.dialects.postgresql import dialect

from app.auth import vk
from app.db.models import Business, PlatformAccount, User
from app.platforms.base import AuthExpired, AuthSession

from .conftest import FakeRepository, hash_token

TOKEN = "https://ads.vk.com/api/v2/oauth2/token.json"
USER = "https://ads.vk.com/api/v2/user.json"


class FakeDB:
    def __init__(self, repository: FakeRepository, business_id: int) -> None:
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


def token_reply(access: str = "vk-access", refresh: str | None = "vk-refresh", **extra) -> dict:
    reply = {
        "access_token": access,
        "token_type": "Bearer",
        "expires_in": 86400,
        "scope": "read_ads,create_ads,read_payments",
    }
    return {**reply, **({"refresh_token": refresh} if refresh else {}), **extra}


@pytest.fixture
def vk_api(client: TestClient, repository: FakeRepository, monkeypatch: pytest.MonkeyPatch):
    from app.api.vk import get_vk_accounts
    from app.config import settings
    from app.main import app

    monkeypatch.setattr(settings, "vk_client_id", "vk-app")
    monkeypatch.setattr(settings, "vk_client_secret", SecretStr("vk-app-secret"))
    app.dependency_overrides[get_vk_accounts] = lambda: _accounts(repository)
    with respx.mock(assert_all_called=False) as router:
        router.post(TOKEN).respond(json=token_reply())
        router.get(USER).respond(json={"id": 777, "username": "vk-owner"})
        yield router
    app.dependency_overrides.pop(get_vk_accounts, None)


def _accounts(repository: FakeRepository) -> vk.VkAccountRepository:
    return vk.VkAccountRepository(FakeDB(repository, 1))


def sign_in(client: TestClient, repository: FakeRepository, user_id: int = 1) -> None:
    repository.users[user_id] = User(
        id=user_id, yandex_uid=str(user_id), login=f"owner{user_id}", consent_at=datetime.now(UTC)
    )
    repository.businesses[user_id] = Business(id=user_id, user_id=user_id)
    from app.auth.sessions import create_session

    session = create_session(user_id)
    repository.sessions[session.id] = session
    client.cookies.set("__Host-session", session.token)
    client.cookies.set("__Host-csrf", session.csrf_secret)


def start(client: TestClient) -> dict[str, str]:
    response = client.get("/api/auth/vk/start")
    assert response.status_code == 303
    url = urlsplit(response.headers["location"])
    assert f"{url.scheme}://{url.netloc}{url.path}" == "https://ads.vk.com/hq/settings/access"
    return {key: values[0] for key, values in parse_qs(url.query).items()}


def key_request(client: TestClient, repository: FakeRepository, **body) -> httpx.Response:
    csrf = repository.sessions[hash_token(client.cookies["__Host-session"])].csrf_secret
    payload = {"client_id": "own-id", "client_secret": "own-secret", **body}
    return client.post("/api/accounts/vk/key", json=payload, headers={"X-CSRF-Token": csrf})


def test_start_builds_vk_url_and_stores_one_time_state(client, repository, vk_api) -> None:
    assert client.get("/api/auth/vk/start").status_code == 401
    sign_in(client, repository)
    params = start(client)
    assert params["action"] == "oauth2" and params["response_type"] == "code" and params["client_id"] == "vk-app"
    assert params["scope"] == "read_ads,create_ads,read_payments"
    assert params["redirect_uri"] == "https://testserver/api/auth/vk/callback"
    state = repository.states[params["state"]]
    assert state.purpose == "vk" and state.user_id == 1
    assert 590 <= (state.expires_at - datetime.now(UTC)).total_seconds() <= 600


def test_start_without_configuration_is_unavailable(client, repository, vk_api, monkeypatch) -> None:
    from app.config import settings

    monkeypatch.setattr(settings, "vk_client_id", "")
    sign_in(client, repository)
    assert client.get("/api/auth/vk/start").status_code == 503


def test_callback_exchanges_code_and_stores_tokens_encrypted(client, repository, vk_api, caplog) -> None:
    caplog.set_level(logging.DEBUG)
    sign_in(client, repository)
    state = start(client)["state"]
    response = client.get("/api/auth/vk/callback", params={"state": state, "code": "vk-code"})
    assert response.status_code == 303
    assert response.headers["location"] == "https://testserver/"
    form = parse_qs(vk_api.calls[0].request.content.decode())
    assert form["grant_type"] == ["authorization_code"] and form["code"] == ["vk-code"]
    assert form["redirect_uri"] == ["https://testserver/api/auth/vk/callback"]
    assert vk_api.calls[1].request.headers["authorization"] == "Bearer vk-access"
    account = repository.accounts[1]
    assert (account.platform, account.auth_mode, account.external_login, account.status) == ("vk", "oauth", "777", "ok")
    assert account.refresh_token_enc == "vk-refresh" and account.scopes == ["read_ads", "create_ads", "read_payments"]
    assert 86300 < (account.expires_at - datetime.now(UTC)).total_seconds() <= 86400
    stored = repository.stored_tokens[1]
    assert "vk-access" not in stored["access_token_enc"] and "vk-refresh" not in stored["refresh_token_enc"]
    assert "vk-access" not in caplog.text and "vk-refresh" not in caplog.text and "vk-app-secret" not in caplog.text


def test_state_is_single_use_and_bound_to_user(client, repository, vk_api) -> None:
    sign_in(client, repository)
    state = start(client)["state"]
    assert client.get("/api/auth/vk/callback", params={"state": "forged", "code": "c"}).status_code == 400
    assert client.get("/api/auth/vk/callback", params={"state": state}).status_code == 400
    assert client.get("/api/auth/vk/callback", params={"state": state, "code": "c1"}).status_code == 303
    assert client.get("/api/auth/vk/callback", params={"state": state, "code": "c1"}).status_code == 400
    assert len(vk_api.calls) == 2


def test_foreign_state_is_rejected_without_token_request(client, repository, vk_api) -> None:
    sign_in(client, repository)
    state = start(client)["state"]
    client.cookies.clear()
    sign_in(client, repository, user_id=2)
    response = client.get("/api/auth/vk/callback", params={"state": state, "code": "stolen"})
    assert response.status_code == 400
    assert not vk_api.calls and not repository.accounts
    assert state in repository.states


def test_state_of_other_session_of_same_user_is_rejected(client, repository, vk_api) -> None:
    sign_in(client, repository)
    state = start(client)["state"]
    from app.auth.sessions import create_session

    session = create_session(1)
    repository.sessions[session.id] = session
    client.cookies.set("__Host-session", session.token)
    assert client.get("/api/auth/vk/callback", params={"state": state, "code": "c"}).status_code == 400
    assert not vk_api.calls


def test_expired_state_and_denied_access(client, repository, vk_api) -> None:
    sign_in(client, repository)
    state = start(client)["state"]
    repository.states[state].expires_at = datetime.now(UTC) - timedelta(seconds=1)
    assert client.get("/api/auth/vk/callback", params={"state": state, "code": "c"}).status_code == 400
    state = start(client)["state"]
    assert client.get("/api/auth/vk/callback", params={"state": state, "error": "access_denied"}).status_code == 400
    assert not vk_api.calls and not repository.accounts


def test_callback_rejects_token_without_required_scope(client, repository, vk_api) -> None:
    vk_api.post(TOKEN).respond(json=token_reply(scope="read_ads"))
    sign_in(client, repository)
    state = start(client)["state"]
    assert client.get("/api/auth/vk/callback", params={"state": state, "code": "c"}).status_code == 400
    assert not repository.accounts


def test_reconnect_updates_same_account(client, repository, vk_api) -> None:
    sign_in(client, repository)
    for access in ("first", "second"):
        vk_api.post(TOKEN).respond(json=token_reply(access=access))
        state = start(client)["state"]
        assert client.get("/api/auth/vk/callback", params={"state": state, "code": f"code-{access}"}).status_code == 303
    assert list(repository.accounts) == [1] and repository.accounts[1].access_token_enc == "second"


def test_manual_key_requires_csrf_and_session(client, repository, vk_api) -> None:
    assert client.post("/api/accounts/vk/key", json={"client_id": "a", "client_secret": "b"}).status_code == 401
    sign_in(client, repository)
    response = client.post("/api/accounts/vk/key", json={"client_id": "a", "client_secret": "b"})
    assert response.status_code == 403
    response = client.post(
        "/api/accounts/vk/key", json={"client_id": "a", "client_secret": "b"}, headers={"X-CSRF-Token": "wrong"}
    )
    assert response.status_code == 403 and not vk_api.calls


def test_manual_key_verifies_and_stores_encrypted(client, repository, vk_api, caplog) -> None:
    caplog.set_level(logging.DEBUG)
    vk_api.post(TOKEN).respond(json=token_reply(access="key-access", refresh=None))
    sign_in(client, repository)
    response = key_request(client, repository)
    assert response.status_code == 200
    assert response.json() == {"id": 1, "business_id": 1, "external_login": "777", "status": "ok"}
    assert "own-secret" not in response.text and "key-access" not in response.text
    form = parse_qs(vk_api.calls[0].request.content.decode())
    assert form["grant_type"] == ["client_credentials"] and form["client_id"] == ["own-id"]
    assert vk_api.calls[1].request.url.path == "/api/v2/user.json"
    account = repository.accounts[1]
    assert account.auth_mode == "manual_key" and account.status == "ok"
    envelope = json.loads(account.refresh_token_enc)
    assert envelope["client_secret"] == "own-secret" and envelope["refresh_token"] is None
    stored = repository.stored_tokens[1]
    assert "own-secret" not in stored["refresh_token_enc"] and "key-access" not in stored["access_token_enc"]
    assert "own-secret" not in caplog.text and "key-access" not in caplog.text
    assert vk.account_credentials(account)[1] == {
        "client_id": "own-id",
        "client_secret": "own-secret",
        "manual_key": True,
    }


def test_manual_key_rejected_by_vk_saves_nothing(client, repository, vk_api) -> None:
    vk_api.post(TOKEN).respond(401, json={"error": "invalid_client"})
    sign_in(client, repository)
    response = key_request(client, repository)
    assert response.status_code == 400 and "own-secret" not in response.text
    assert not repository.accounts
    assert key_request(client, repository, client_id="  ").status_code == 400


def test_manual_key_with_broken_user_check_saves_nothing(client, repository, vk_api) -> None:
    vk_api.get(USER).respond(200, json={"error": {"code": "invalid_token"}})
    sign_in(client, repository)
    assert key_request(client, repository).status_code == 400
    assert not repository.accounts


def session_of(expires_in: timedelta, refresh: str | None = "old-refresh") -> AuthSession:
    return AuthSession("777", "old-access", refresh, datetime.now(UTC) + expires_in)


async def test_valid_token_is_reused_without_requests(vk_api) -> None:
    async with vk.VkClient() as client:
        session = session_of(timedelta(hours=5))
        assert await client.refresh(session) is session
    assert not vk_api.calls


async def test_expired_token_is_refreshed_once_for_parallel_callers(vk_api) -> None:
    vk_api.post(TOKEN).respond(json=token_reply(access="new-access", refresh="new-refresh"))
    async with vk.VkClient() as client:
        results = await asyncio.gather(*(client.refresh(session_of(timedelta(seconds=-5))) for _ in range(4)))
    assert {result.access_token for result in results} == {"new-access"} and {r.account_id for r in results} == {"777"}
    form = parse_qs(vk_api.calls[0].request.content.decode())
    assert form["grant_type"] == ["refresh_token"] and form["refresh_token"] == ["old-refresh"]
    assert len(vk_api.calls) == 1


async def test_refresh_keeps_old_refresh_token_when_vk_omits_it(vk_api) -> None:
    vk_api.post(TOKEN).respond(json=token_reply(access="new-access", refresh=None))
    async with vk.VkClient() as client:
        updated = await client.refresh(session_of(timedelta(seconds=30)))
    assert updated.refresh_token == "old-refresh" and updated.access_token == "new-access"


async def test_refresh_failure_and_missing_refresh_token_need_reauth(vk_api) -> None:
    vk_api.post(TOKEN).respond(400, json={"error": "invalid_grant"})
    async with vk.VkClient() as client:
        with pytest.raises(AuthExpired):
            await client.refresh(session_of(timedelta(seconds=-5)))
        with pytest.raises(AuthExpired):
            await client.refresh(session_of(timedelta(seconds=-5), refresh=None))


async def test_manual_key_without_refresh_token_renews_by_client_credentials(vk_api) -> None:
    vk_api.post(TOKEN).respond(json=token_reply(access="renewed", refresh=None))
    async with vk.VkClient(client_id="own-id", client_secret="own-secret", manual_key=True) as client:
        updated = await client.refresh(session_of(timedelta(seconds=-5), refresh=None))
    assert updated.access_token == "renewed"
    assert parse_qs(vk_api.calls[0].request.content.decode())["grant_type"] == ["client_credentials"]


async def test_renew_account_reuses_valid_token_and_refreshes_expired_one(repository, vk_api) -> None:
    repository.businesses[1] = Business(id=1, user_id=1)
    accounts = _accounts(repository)
    accounts.save(1, None, session_of(timedelta(hours=5)), auth_mode="oauth")
    renewed = await vk.renew_account(FakeDB(repository, 1), 1)
    assert renewed.access_token_enc == "old-access" and not vk_api.calls
    repository.accounts[1].expires_at = datetime.now(UTC) - timedelta(seconds=1)
    vk_api.post(TOKEN).respond(json=token_reply(access="fresh", refresh="fresh-refresh"))
    renewed = await vk.renew_account(FakeDB(repository, 1), 1)
    assert (renewed.access_token_enc, renewed.refresh_token_enc, renewed.status) == ("fresh", "fresh-refresh", "ok")
    assert list(repository.accounts) == [1] and len(vk_api.calls) == 1


async def test_renew_account_marks_account_for_reauth(repository, vk_api) -> None:
    repository.businesses[1] = Business(id=1, user_id=1)
    _accounts(repository).save(1, None, session_of(timedelta(seconds=-5)), auth_mode="oauth")
    vk_api.post(TOKEN).respond(400, json={"error": "invalid_grant"})
    with pytest.raises(AuthExpired):
        await vk.renew_account(FakeDB(repository, 1), 1)
    assert repository.accounts[1].status == "needs_reauth"
