import json
import logging
from datetime import UTC, datetime
from urllib.parse import parse_qs

import httpx
import pytest
import respx
from fastapi.testclient import TestClient
from pydantic import SecretStr

from app.auth.vk import MANUAL_VERSION
from app.db.models import Business, PlatformAccount, User

from .conftest import FakeRepository
from .test_auth import callback, login, start

YANDEX_REVOKE = "https://oauth.yandex.ru/revoke_token"
VK_REVOKE = "https://ads.vk.com/api/v2/oauth2/token/delete.json"


def form(call) -> dict[str, list[str]]:
    return parse_qs(call.request.content.decode())


def csrf(client: TestClient) -> dict[str, str]:
    return {"X-CSRF-Token": client.cookies["__Host-csrf"]}


def sign_in(client: TestClient, repository: FakeRepository, user_id: int = 1) -> None:
    from app.auth.sessions import create_session

    repository.users[user_id] = User(id=user_id, yandex_uid=str(user_id), consent_at=datetime.now(UTC))
    repository.businesses[user_id] = Business(id=user_id, user_id=user_id)
    session = create_session(user_id)
    repository.sessions[session.id] = session
    client.cookies.set("__Host-session", session.token)
    client.cookies.set("__Host-csrf", session.csrf_secret)


def account(repository: FakeRepository, id_: int, business_id: int, platform: str, **values) -> PlatformAccount:
    values = {"auth_mode": "oauth", "status": "ok", "scopes": []} | values
    item = PlatformAccount(id=id_, business_id=business_id, platform=platform, **values)
    repository.accounts[id_] = item
    return item


@pytest.fixture
def platforms(monkeypatch: pytest.MonkeyPatch):
    from app.config import settings

    monkeypatch.setattr(settings, "vk_client_id", "vk-app")
    monkeypatch.setattr(settings, "vk_client_secret", SecretStr("vk-app-secret"))
    with respx.mock(assert_all_called=False) as router:
        router.post(YANDEX_REVOKE, name="yandex").respond(json={"status": "ok"})
        router.post(VK_REVOKE, name="vk").respond(204)
        yield router


def test_direct_token_is_issued_for_device_and_revoked_on_disconnect(client, repository, mock_yandex) -> None:
    login(client, repository)
    token_route = mock_yandex.routes[0]
    assert "device_id" not in form(token_route.calls.last)
    assert callback(client, start(client, "direct")["state"]).status_code == 303
    exchange = form(token_route.calls.last)
    assert exchange["device_id"] == ["ad-autopilot-business-1"] and exchange["device_name"] == ["Автопилот рекламы"]
    assert client.delete("/api/accounts/1", headers=csrf(client)).status_code == 200
    revoke = mock_yandex["revoke"]
    assert revoke.call_count == 1
    assert form(revoke.calls.last) == {
        "access_token": ["test-access"],
        "client_id": ["test-client"],
        "client_secret": ["test-secret"],
    }
    assert repository.accounts[1].status == "revoked" and repository.accounts[1].access_token_enc == ""


@pytest.mark.parametrize("failure", ["unsupported", "server", "network"])
def test_revocation_failure_does_not_block_disconnect_and_logs_no_secrets(
    client, repository, mock_yandex, caplog, failure
) -> None:
    caplog.set_level(logging.DEBUG)
    login(client, repository)
    assert callback(client, start(client, "direct")["state"]).status_code == 303
    revoke = mock_yandex["revoke"]
    if failure == "unsupported":
        revoke.respond(400, json={"error": "unsupported_token_type", "error_description": "test-access is old"})
    elif failure == "server":
        revoke.respond(502, text="<html>test-access</html>")
    else:
        revoke.side_effect = httpx.ConnectError("test-access unreachable")
    assert client.delete("/api/accounts/1", headers=csrf(client)).status_code == 200
    assert repository.accounts[1].status == "revoked"
    assert "Token revocation failed: platform=direct" in caplog.text
    assert ("code=unsupported_token_type" in caplog.text) is (failure == "unsupported")
    assert "test-access" not in caplog.text and "test-secret" not in caplog.text


def test_disconnect_of_foreign_account_revokes_nothing(client, repository, platforms) -> None:
    sign_in(client, repository)
    repository.businesses[2] = Business(id=2, user_id=2)
    account(repository, 5, 2, "direct", access_token_enc="foreign-token")
    assert client.delete("/api/accounts/5", headers=csrf(client)).status_code == 404
    assert not platforms.calls and repository.accounts[5].status == "ok"


def test_delete_me_revokes_direct_vk_oauth_and_vk_manual_key(client, repository, platforms, caplog) -> None:
    caplog.set_level(logging.DEBUG)
    sign_in(client, repository)
    repository.businesses[3] = Business(id=3, user_id=1)
    account(repository, 1, 1, "direct", access_token_enc="direct-token")
    account(repository, 2, 1, "vk", access_token_enc="vk-token", external_login="777")
    envelope = {"version": MANUAL_VERSION, "client_id": "own-id", "client_secret": "own-secret", "refresh_token": None}
    account(
        repository,
        3,
        3,
        "vk",
        auth_mode="manual_key",
        access_token_enc="key-token",
        refresh_token_enc=json.dumps(envelope),
        external_login="888",
    )
    account(repository, 4, 3, "vk", access_token_enc="", external_login="999", status="revoked")
    assert client.delete("/api/me", headers=csrf(client)).status_code == 200
    assert 1 not in repository.users
    assert [form(call) for call in platforms["yandex"].calls] == [
        {"access_token": ["direct-token"], "client_id": ["test-client"], "client_secret": ["test-secret"]}
    ]
    assert sorted((form(call) for call in platforms["vk"].calls), key=str) == sorted(
        [
            {"client_id": ["vk-app"], "client_secret": ["vk-app-secret"], "user_id": ["777"]},
            {"client_id": ["own-id"], "client_secret": ["own-secret"]},
        ],
        key=str,
    )
    for secret in ("direct-token", "vk-token", "key-token", "own-secret", "vk-app-secret"):
        assert secret not in caplog.text


@pytest.mark.parametrize("other_status,revoked", [("ok", False), ("needs_reauth", False), ("revoked", True)])
def test_vk_account_connected_elsewhere_keeps_its_tokens(client, repository, platforms, other_status, revoked) -> None:
    sign_in(client, repository)
    repository.businesses[2] = Business(id=2, user_id=2)
    account(repository, 1, 1, "vk", access_token_enc="mine", external_login="777")
    account(repository, 2, 2, "vk", access_token_enc="theirs", external_login="777", status=other_status)
    assert client.delete("/api/accounts/1", headers=csrf(client)).status_code == 200
    assert platforms["vk"].called is revoked


def test_vk_oauth_account_without_user_id_is_never_revoked_as_app_owner(client, repository, platforms) -> None:
    sign_in(client, repository)
    account(repository, 1, 1, "vk", access_token_enc="mine", external_login=None)
    assert client.delete("/api/accounts/1", headers=csrf(client)).status_code == 200
    assert not platforms.calls


def config(**values):
    from types import SimpleNamespace

    defaults = {
        "yandex_client_id": "test-client",
        "yandex_client_secret": SecretStr("test-secret"),
        "vk_client_id": "vk-app",
        "vk_client_secret": SecretStr("vk-app-secret"),
    }
    return SimpleNamespace(**defaults | values)


@pytest.mark.parametrize("missing", ["yandex_client_id", "yandex_client_secret", "vk_client_id", "vk_client_secret"])
async def test_revocation_is_skipped_without_client_credentials(caplog, missing) -> None:
    from app.auth.revoke import Revocation, revoke_tokens

    platform = "direct" if missing.startswith("yandex") else "vk"
    item = Revocation("direct", "direct-token") if platform == "direct" else Revocation("vk", user_id="777")
    empty = SecretStr("") if missing.endswith("secret") else ""
    with respx.mock(assert_all_called=False) as router:
        route = router.post(url__regex=r".*").respond(200)
        async with httpx.AsyncClient() as http:
            await revoke_tokens(http, [item], config=config(**{missing: empty}))
    assert not route.called
    assert f"Token revocation skipped: platform={platform}" in caplog.text


async def test_revocation_does_not_follow_redirects(caplog) -> None:
    from app.auth.revoke import Revocation, revoke_tokens

    with respx.mock(assert_all_called=False) as router:
        router.post(YANDEX_REVOKE).respond(302, headers={"Location": "https://evil.example/steal"})
        evil = router.route(host="evil.example").respond(200)
        async with httpx.AsyncClient() as http:
            await revoke_tokens(http, [Revocation("direct", "direct-token")], config=config())
    assert not evil.called
    assert "Token revocation failed: platform=direct status=302" in caplog.text


@pytest.mark.parametrize("failure", ["client", "network"])
async def test_vk_revocation_failure_logs_no_tokens_or_secrets(caplog, failure) -> None:
    from app.auth.revoke import Revocation, revoke_tokens

    caplog.set_level(logging.DEBUG)
    with respx.mock(assert_all_called=False) as router:
        route = router.post(VK_REVOKE)
        if failure == "client":
            route.respond(400, json={"error": {"code": "invalid_client", "message": "own-secret is wrong"}})
        else:
            route.side_effect = httpx.ConnectError("own-secret unreachable")
        async with httpx.AsyncClient() as http:
            await revoke_tokens(
                http, [Revocation("vk", client_id="own-id", client_secret="own-secret")], config=config()
            )
    assert "Token revocation failed: platform=vk" in caplog.text
    assert ("code=invalid_client" in caplog.text) is (failure == "client")
    assert "own-secret" not in caplog.text and "vk-app-secret" not in caplog.text


def test_vk_key_account_is_revoked_even_if_same_vk_user_is_connected_by_oauth(client, repository, platforms) -> None:
    """Ключ API снимает токены только своего client_id: OAuth-кабинет того же пользователя VK не затронут."""
    sign_in(client, repository)
    repository.businesses[2] = Business(id=2, user_id=2)
    envelope = {"version": MANUAL_VERSION, "client_id": "own-id", "client_secret": "own-secret", "refresh_token": None}
    account(
        repository, 1, 1, "vk", auth_mode="manual_key", access_token_enc="key", refresh_token_enc=json.dumps(envelope),
        external_login="777",
    )
    account(repository, 2, 2, "vk", access_token_enc="theirs", external_login="777")
    assert client.delete("/api/accounts/1", headers=csrf(client)).status_code == 200
    expected = {"client_id": ["own-id"], "client_secret": ["own-secret"]}
    assert [form(call) for call in platforms["vk"].calls] == [expected]


def test_vk_key_shared_by_two_businesses_is_not_revoked(client, repository, platforms) -> None:
    sign_in(client, repository)
    repository.businesses[2] = Business(id=2, user_id=2)
    envelope = {"version": MANUAL_VERSION, "client_id": "own-id", "client_secret": "own-secret", "refresh_token": None}
    for id_, business_id in ((1, 1), (2, 2)):
        account(
            repository, id_, business_id, "vk", auth_mode="manual_key", access_token_enc="key",
            refresh_token_enc=json.dumps(envelope), external_login="777",
        )
    assert client.delete("/api/accounts/1", headers=csrf(client)).status_code == 200
    assert not platforms["vk"].called


def test_already_revoked_account_is_not_revoked_again(client, repository, platforms) -> None:
    sign_in(client, repository)
    account(repository, 1, 1, "direct", access_token_enc="", status="revoked")
    assert client.delete("/api/accounts/1", headers=csrf(client)).status_code == 200
    assert not platforms.calls
