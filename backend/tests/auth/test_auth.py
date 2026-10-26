import base64
import hashlib
from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
import respx
from fastapi.testclient import TestClient

from app.db.models import Business, User

from .conftest import FakeRepository


def start(client: TestClient, purpose: str = "login", **params) -> dict[str, str]:
    response = client.get("/api/auth/yandex/start", params={"purpose": purpose, **params})
    assert response.status_code == 303
    url = urlsplit(response.headers["location"])
    assert f"{url.scheme}://{url.netloc}{url.path}" == "https://oauth.yandex.ru/authorize"
    return {key: values[0] for key, values in parse_qs(url.query).items()}


def login(client: TestClient, repository: FakeRepository) -> None:
    state = start(client)["state"]
    response = client.get("/api/auth/yandex/callback", params={"state": state, "code": "login-code"})
    assert response.status_code == 303
    repository.businesses[1] = Business(id=1, user_id=1)


def callback(client: TestClient, state: str, code: str = "direct-code") -> httpx.Response:
    return client.get("/api/auth/yandex/callback", params={"state": state, "code": code})


def test_login_scopes_pkce_and_cookies(client: TestClient, repository: FakeRepository) -> None:
    params = start(client)
    state = repository.states[params["state"]]
    assert params["scope"] == "login:info login:email"
    assert params["redirect_uri"] == "https://testserver/api/auth/yandex/callback"
    assert params["response_type"] == "code"
    assert "force_confirm" not in params
    assert params["code_challenge_method"] == "S256"
    assert (
        params["code_challenge"]
        == base64.urlsafe_b64encode(hashlib.sha256(state.pkce_verifier.encode()).digest()).rstrip(b"=").decode()
    )
    assert 590 <= (state.expires_at - datetime.now(UTC)).total_seconds() <= 600
    assert 43 <= len(state.pkce_verifier) <= 128
    cookie = next(cookie for cookie in client.cookies.jar if cookie.name == "__Host-oauth")
    assert cookie.secure
    assert cookie.has_nonstandard_attr("HttpOnly")


def test_login_creates_user_and_30_day_session(
    client: TestClient,
    repository: FakeRepository,
    mock_yandex: respx.MockRouter,
) -> None:
    params = start(client)
    verifier = repository.states[params["state"]].pkce_verifier
    response = callback(client, params["state"], "login-code")
    assert response.status_code == 303
    assert response.headers["location"] == "https://testserver/"
    assert response.headers["referrer-policy"] == "no-referrer"
    assert response.headers["cache-control"] == "no-store"
    assert "__Host-oauth" not in client.cookies
    session = repository.sessions[client.cookies["__Host-session"]]
    assert timedelta(days=30) - timedelta(seconds=5) <= session.expires_at - datetime.now(UTC) <= timedelta(days=30)
    assert session.csrf_secret == client.cookies["__Host-csrf"]
    assert len(session.id) >= 43
    session_cookie, csrf_cookie = response.headers.get_list("set-cookie")[:2]
    assert "HttpOnly" in session_cookie
    assert "HttpOnly" not in csrf_cookie
    for cookie in (session_cookie, csrf_cookie):
        assert "Secure" in cookie and "SameSite=lax" in cookie and "Max-Age=2592000" in cookie
    user = repository.users[1]
    assert (user.yandex_uid, user.login, user.email) == ("100", "owner", "owner@example.test")
    assert user.consent_at is not None
    assert not repository.accounts
    token_request, profile_request = [call.request for call in mock_yandex.calls]
    form = parse_qs(token_request.content.decode())
    assert form["code_verifier"] == [verifier]
    assert form["grant_type"] == ["authorization_code"]
    assert form["redirect_uri"] == [params["redirect_uri"]]
    assert "test-access" not in str(profile_request.url)
    assert profile_request.headers["Authorization"] == "OAuth test-access"


@pytest.mark.parametrize("change", ["tamper", "expired", "no_cookie", "foreign_browser"])
def test_invalid_state_never_contacts_yandex(
    client: TestClient,
    repository: FakeRepository,
    mock_yandex: respx.MockRouter,
    change: str,
) -> None:
    state = start(client)["state"]
    if change == "tamper":
        state = state[:-1] + ("a" if state[-1] != "a" else "b")
    elif change == "expired":
        repository.states[state].expires_at = datetime.now(UTC) - timedelta(seconds=1)
    elif change == "no_cookie":
        client.cookies.clear()
    else:
        client.cookies.set("__Host-oauth", "other-browser", domain="testserver.local", path="/")
    assert callback(client, state).status_code == 400
    assert not mock_yandex.calls
    assert not repository.users


def test_repeated_state_and_code_rejected(
    client: TestClient,
    repository: FakeRepository,
    mock_yandex: respx.MockRouter,
) -> None:
    state = start(client)["state"]
    assert callback(client, state, "login-code").status_code == 303
    assert callback(client, state, "login-code").status_code == 400
    state = start(client)["state"]
    assert callback(client, state, "login-code").status_code == 400
    assert len(mock_yandex.calls) == 2


def test_logout_requires_csrf_and_deletes_session(
    client: TestClient,
    repository: FakeRepository,
    mock_yandex: respx.MockRouter,
) -> None:
    login(client, repository)
    session_id = client.cookies["__Host-session"]
    assert client.post("/api/auth/logout").status_code == 403
    assert client.post("/api/auth/logout", headers={"X-CSRF-Token": "wrong"}).status_code == 403
    assert session_id in repository.sessions
    csrf = client.cookies["__Host-csrf"]
    client.cookies.delete("__Host-csrf")
    assert client.post("/api/auth/logout", headers={"X-CSRF-Token": csrf}).status_code == 403
    client.cookies.set("__Host-csrf", "forged", domain="testserver.local", path="/")
    assert client.post("/api/auth/logout", headers={"X-CSRF-Token": "forged"}).status_code == 403
    client.cookies.delete("__Host-csrf")
    client.cookies.set("__Host-csrf", csrf, domain="testserver.local", path="/")
    response = client.post("/api/auth/logout", headers={"X-CSRF-Token": csrf})
    assert response.status_code == 204
    assert session_id not in repository.sessions
    assert client.get("/api/me").status_code == 401
    assert not client.cookies


def test_direct_requires_login(client: TestClient) -> None:
    assert client.get("/api/auth/yandex/start?purpose=direct").status_code == 401
    assert client.get("/api/me").status_code == 401


def test_direct_uses_separate_account_and_encrypted_fields(
    client: TestClient,
    repository: FakeRepository,
    mock_yandex: respx.MockRouter,
) -> None:
    login(client, repository)
    original_session = client.cookies["__Host-session"]
    params = start(client, "direct")
    assert params["scope"] == "direct:api metrika:read"
    assert params["force_confirm"] == "yes"
    mock_yandex.get("https://login.yandex.ru/info").respond(json={"id": "200", "login": "advertiser"})
    assert callback(client, params["state"]).status_code == 303
    assert client.cookies["__Host-session"] == original_session
    assert repository.users[1].login == "owner"
    account = repository.accounts[1]
    assert (account.business_id, account.platform, account.auth_mode) == (1, "direct", "oauth")
    assert (account.external_login, account.status) == ("advertiser", "ok")
    assert set(account.scopes) == {"direct:api", "metrika:read"}
    assert account.last_ok_at is not None and account.expires_at > datetime.now(UTC)
    assert repository.stored_tokens[1]["access_token_enc"] != "test-access"
    assert repository.stored_tokens[1]["refresh_token_enc"] != "test-refresh"
    me = client.get("/api/me")
    assert me.status_code == 200
    assert me.json()["login"] == "owner"
    assert me.json()["accounts"][0]["external_login"] == "advertiser"
    assert "test-access" not in me.text and "test-refresh" not in me.text
    assert "csrf_secret" not in me.text


@pytest.mark.parametrize("change", ["other_user", "same_user_other_session", "expired_session", "purpose", "business"])
def test_direct_state_bound_to_session_purpose_and_business(
    client: TestClient,
    repository: FakeRepository,
    mock_yandex: respx.MockRouter,
    change: str,
) -> None:
    from app.auth.sessions import create_session

    login(client, repository)
    state = start(client, "direct")["state"]
    if change in ("other_user", "same_user_other_session"):
        user_id = 2 if change == "other_user" else 1
        session = create_session(user_id)
        repository.save_session(session)
        client.cookies.set("__Host-session", session.id, domain="testserver.local", path="/")
    elif change == "expired_session":
        repository.sessions[client.cookies["__Host-session"]].expires_at = datetime.now(UTC) - timedelta(seconds=1)
    elif change == "purpose":
        repository.states[state].purpose = "login"
    else:
        nonce, _, signature = state.split(".")
        altered = f"{nonce}.2.{signature}"
        repository.states[altered] = repository.states.pop(state)
        repository.states[altered].state = altered
        state = altered
    assert callback(client, state).status_code == 400
    assert len(mock_yandex.calls) == 2
    assert not repository.accounts


def test_business_selection_and_ownership(
    client: TestClient,
    repository: FakeRepository,
    mock_yandex: respx.MockRouter,
) -> None:
    login(client, repository)
    repository.businesses.clear()
    assert client.get("/api/auth/yandex/start?purpose=direct").status_code == 404
    repository.businesses[1] = Business(id=1, user_id=1)
    repository.businesses[2] = Business(id=2, user_id=1)
    repository.businesses[3] = Business(id=3, user_id=2)
    assert client.get("/api/auth/yandex/start?purpose=direct").status_code == 400
    assert client.get("/api/auth/yandex/start?purpose=direct&business_id=3").status_code == 404
    params = start(client, "direct", business_id=2)
    assert callback(client, params["state"]).status_code == 303
    assert repository.accounts[1].business_id == 2


def test_partial_permissions_require_reauth_and_reconnect_updates_account(
    client: TestClient,
    repository: FakeRepository,
    mock_yandex: respx.MockRouter,
) -> None:
    login(client, repository)
    mock_yandex.post("https://oauth.yandex.ru/token").respond(json={"access_token": "partial", "scope": "direct:api"})
    assert callback(client, start(client, "direct")["state"]).status_code == 303
    assert repository.accounts[1].status == "needs_reauth"
    mock_yandex.post("https://oauth.yandex.ru/token").respond(json={"access_token": "replacement"})
    assert callback(client, start(client, "direct")["state"], "new-code").status_code == 303
    assert len(repository.accounts) == 1
    assert repository.accounts[1].status == "ok"
    assert repository.accounts[1].refresh_token_enc is None


def test_disconnect_requires_csrf_and_ownership(
    client: TestClient,
    repository: FakeRepository,
    mock_yandex: respx.MockRouter,
) -> None:
    login(client, repository)
    assert callback(client, start(client, "direct")["state"]).status_code == 303
    assert client.delete("/api/accounts/1").status_code == 403
    csrf = {"X-CSRF-Token": client.cookies["__Host-csrf"]}
    repository.businesses[1].user_id = 2
    assert client.delete("/api/accounts/1", headers=csrf).status_code == 404
    repository.businesses[1].user_id = 1
    assert client.delete("/api/accounts/1", headers=csrf).status_code == 204
    assert repository.accounts[1].status == "revoked"
    assert repository.accounts[1].access_token_enc == ""
    assert repository.accounts[1].refresh_token_enc is None
    assert client.delete("/api/accounts/999", headers=csrf).status_code == 404


def test_provider_denial_consumes_state_without_exposing_error(
    client: TestClient,
    repository: FakeRepository,
    mock_yandex: respx.MockRouter,
) -> None:
    state = start(client)["state"]
    response = client.get("/api/auth/yandex/callback", params={"state": state, "error": "private-provider-error"})
    assert response.status_code == 400
    assert "private-provider-error" not in response.text
    assert state not in repository.states
    assert callback(client, state).status_code == 400
    assert not mock_yandex.calls


@pytest.mark.parametrize("target", ["token", "profile"])
@pytest.mark.parametrize("failure", ["http_error", "redirect", "network", "invalid_json", "invalid_data"])
def test_provider_failure_is_safe_and_cannot_replay(
    client: TestClient,
    repository: FakeRepository,
    mock_yandex: respx.MockRouter,
    target: str,
    failure: str,
) -> None:
    state = start(client)["state"]
    route = (
        mock_yandex.post("https://oauth.yandex.ru/token")
        if target == "token"
        else mock_yandex.get("https://login.yandex.ru/info")
    )
    if failure == "http_error":
        route.respond(400, json={"error": "invalid_grant", "error_description": "private-error"})
    elif failure == "redirect":
        route.respond(302, headers={"Location": "https://untrusted.example/"})
    elif failure == "network":
        route.mock(side_effect=httpx.ConnectError("private-error"))
    elif failure == "invalid_json":
        route.respond(text="private-error")
    else:
        route.respond(json={"access_token": True, "id": True})
    response = callback(client, state)
    assert response.status_code == 400
    assert "private-error" not in response.text
    assert "test-access" not in response.text
    assert not repository.users
    calls = len(mock_yandex.calls)
    assert callback(client, state).status_code == 400
    assert len(mock_yandex.calls) == calls


def test_login_rotates_session_and_preserves_consent(
    client: TestClient,
    repository: FakeRepository,
    mock_yandex: respx.MockRouter,
) -> None:
    login(client, repository)
    previous_session = client.cookies["__Host-session"]
    consent = repository.users[1].consent_at
    assert callback(client, start(client)["state"], "second-login").status_code == 303
    assert previous_session not in repository.sessions
    assert client.cookies["__Host-session"] != previous_session
    assert len(repository.users) == 1
    assert repository.users[1].consent_at == consent


def test_expired_and_missing_user_sessions_rejected(
    client: TestClient,
    repository: FakeRepository,
    mock_yandex: respx.MockRouter,
) -> None:
    login(client, repository)
    repository.users.clear()
    assert client.get("/api/me").status_code == 401
    repository.users[1] = User(id=1)
    repository.sessions[client.cookies["__Host-session"]].expires_at = datetime.now(UTC) - timedelta(seconds=1)
    assert client.get("/api/me").status_code == 401
    assert client.post("/api/auth/logout").status_code == 401
