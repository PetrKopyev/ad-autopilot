import base64
import hashlib
from collections import deque
from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
import respx
from fastapi.testclient import TestClient

from app.db.models import Business, PlatformAccount, User
from app.services.changes import ChangeConflict, ServerBusy

from .conftest import FakeRepository, hash_token


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
    session = repository.sessions[hash_token(client.cookies["__Host-session"])]
    assert timedelta(days=30) - timedelta(seconds=5) <= session.expires_at - datetime.now(UTC) <= timedelta(days=30)
    assert session.csrf_secret == client.cookies["__Host-csrf"]
    assert session.id == hash_token(client.cookies["__Host-session"])
    assert session.id != client.cookies["__Host-session"]
    assert len(client.cookies["__Host-session"]) >= 43
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
    assert hash_token(session_id) in repository.sessions
    csrf = client.cookies["__Host-csrf"]
    client.cookies.delete("__Host-csrf")
    assert client.post("/api/auth/logout", headers={"X-CSRF-Token": csrf}).status_code == 403
    client.cookies.set("__Host-csrf", "forged", domain="testserver.local", path="/")
    assert client.post("/api/auth/logout", headers={"X-CSRF-Token": "forged"}).status_code == 403
    client.cookies.delete("__Host-csrf")
    client.cookies.set("__Host-csrf", csrf, domain="testserver.local", path="/")
    response = client.post("/api/auth/logout", headers={"X-CSRF-Token": csrf})
    assert response.status_code == 204
    assert hash_token(session_id) not in repository.sessions
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
    response = callback(client, params["state"])
    assert response.status_code == 303
    assert response.headers["location"] == "https://testserver/setup/2"
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


def test_agency_direct_account_is_rejected_before_saving(
    client: TestClient,
    repository: FakeRepository,
    mock_yandex: respx.MockRouter,
) -> None:
    """В5: агентский кабинет отклоняется при подключении — с понятной ошибкой и без сохранения аккаунта."""
    login(client, repository)
    mock_yandex.get("https://login.yandex.ru/info").respond(json={"id": "200", "login": "agency"})
    mock_yandex.post("https://api.direct.yandex.com/json/v5/clients").respond(
        json={"result": {"Clients": [{"ClientId": 9, "Login": "agency", "Type": "AGENCY"}]}}
    )
    params = start(client, "direct")
    response = callback(client, params["state"])
    assert response.status_code == 400
    assert "Агентские кабинеты не поддерживаются" in response.text and "text/html" in response.headers["content-type"]
    assert 1 not in repository.accounts


def test_direct_account_survives_whoami_outage(
    client: TestClient,
    repository: FakeRepository,
    mock_yandex: respx.MockRouter,
) -> None:
    """В4 круга 4: сбой whoami, не про агентство (Директ вернул 503), подключение не обрывает:
    аккаунт сохраняется и владелец попадает на шаг подключений, где проверка повторится понятным
    текстом, а не JSON-ошибкой."""
    login(client, repository)
    mock_yandex.get("https://login.yandex.ru/info").respond(json={"id": "200", "login": "advertiser"})
    mock_yandex.post("https://api.direct.yandex.com/json/v5/clients").respond(
        503, json={"error": {"error_code": 1000, "error_string": "Сервис временно недоступен"}}
    )
    params = start(client, "direct")
    response = callback(client, params["state"])
    assert response.status_code == 303, response.text
    assert response.headers["location"] == "https://testserver/setup/2"
    account = repository.accounts[1]
    assert (account.external_login, account.status) == ("advertiser", "ok")


def test_me_tells_whether_setup_created_business(
    client: TestClient, repository: FakeRepository, mock_yandex: respx.MockRouter
) -> None:
    login(client, repository)
    assert client.get("/api/me").json()["has_business"] is True
    repository.businesses.clear()
    assert client.get("/api/me").json()["has_business"] is False


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
        client.cookies.set("__Host-session", session.token, domain="testserver.local", path="/")
    elif change == "expired_session":
        repository.sessions[hash_token(client.cookies["__Host-session"])].expires_at = datetime.now(UTC) - timedelta(
            seconds=1
        )
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
    assert repository.accounts[1].status == "needs_reauth" and repository.resumes == []
    mock_yandex.post("https://oauth.yandex.ru/token").respond(json={"access_token": "replacement"})
    assert callback(client, start(client, "direct")["state"], "new-code").status_code == 303
    assert len(repository.accounts) == 1
    # Подключённый кабинет: кампании, остановленные при его отключении, продолжают работать.
    assert repository.accounts[1].status == "ok" and repository.resumes == [
        (repository.accounts[1].business_id, "direct")
    ]
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
    response = client.delete("/api/accounts/1", headers=csrf)
    assert response.status_code == 200 and response.json()["stopped"] is True
    # Реклама остановлена, пока токен кабинета ещё действовал.
    assert repository.stops[-1] == (1, 1, "test-access")
    assert repository.accounts[1].status == "revoked"
    assert repository.accounts[1].access_token_enc == ""
    assert repository.accounts[1].refresh_token_enc is None
    assert client.delete("/api/accounts/999", headers=csrf).status_code == 404


def test_disconnect_reports_unconfirmed_stop_and_still_revokes(
    client: TestClient, repository: FakeRepository, mock_yandex: respx.MockRouter
) -> None:
    login(client, repository)
    assert callback(client, start(client, "direct")["state"]).status_code == 303
    repository.stop_confirmed = False
    response = client.delete("/api/accounts/1", headers={"X-CSRF-Token": client.cookies["__Host-csrf"]})
    assert response.status_code == 200 and response.json()["stopped"] is False
    assert "Яндекс Директ: площадка не подтвердила остановку" in response.json()["message"]
    assert repository.accounts[1].status == "revoked"


@pytest.mark.parametrize(
    "error,status,text",
    [
        (ChangeConflict("Этот бизнес уже обрабатывается."), 409, "Повторите отключение через минуту"),
        (ServerBusy(), 503, "Сервер сейчас занят"),
    ],
)
def test_disconnect_of_busy_business_keeps_access_and_asks_to_retry(
    client: TestClient, repository: FakeRepository, mock_yandex: respx.MockRouter, error, status, text
) -> None:
    # Реклама не остановлена: отозвать токен сейчас значит оставить её крутиться без присмотра автопилота.
    login(client, repository)
    assert callback(client, start(client, "direct")["state"]).status_code == 303
    repository.stop_error = error
    response = client.delete("/api/accounts/1", headers={"X-CSRF-Token": client.cookies["__Host-csrf"]})
    assert response.status_code == status and text in response.json()["detail"]
    assert repository.accounts[1].status == "ok" and repository.accounts[1].access_token_enc == "test-access"
    assert mock_yandex["revoke"].call_count == 0


@pytest.mark.parametrize(
    "error,status,text",
    [
        (ChangeConflict("Этот бизнес уже обрабатывается."), 409, "Повторите удаление через минуту"),
        (ServerBusy(), 503, "Сервер сейчас занят"),
    ],
)
def test_delete_me_of_busy_business_keeps_account(
    client: TestClient, repository: FakeRepository, mock_yandex: respx.MockRouter, error, status, text
) -> None:
    sign_in(client, repository)
    repository.stop_error = error
    response = client.delete("/api/me", headers={"X-CSRF-Token": client.cookies["__Host-csrf"]})
    assert response.status_code == status and text in response.json()["detail"]
    assert 1 in repository.users and mock_yandex["revoke"].call_count == 0


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
    assert hash_token(previous_session) not in repository.sessions
    assert client.cookies["__Host-session"] != previous_session
    assert len(repository.users) == 1
    assert repository.users[1].consent_at == consent


def test_expired_and_missing_user_sessions_rejected(
    client: TestClient,
    repository: FakeRepository,
    mock_yandex: respx.MockRouter,
) -> None:
    login(client, repository)
    cookies = dict(client.cookies)
    repository.users.clear()
    assert client.get("/api/me").status_code == 401
    repository.users[1] = User(id=1)
    for name, value in cookies.items():
        client.cookies.set(name, value, domain="testserver.local", path="/")
    repository.sessions[hash_token(cookies["__Host-session"])].expires_at = datetime.now(UTC) - timedelta(seconds=1)
    assert client.post("/api/auth/logout").status_code == 401
    assert client.get("/api/me").status_code == 401


def test_stale_session_cookies_are_cleared_by_me_but_login_in_progress_is_kept(
    client: TestClient,
    repository: FakeRepository,
    mock_yandex: respx.MockRouter,
) -> None:
    login(client, repository)
    repository.sessions.clear()
    client.cookies.set("__Host-oauth", "binding", domain="testserver.local", path="/")
    response = client.get("/api/me")
    assert response.status_code == 401 and response.json()["detail"] == "Войдите через Яндекс ID."
    assert response.headers["Cache-Control"] == "no-store"
    assert set(client.cookies.keys()) == {"__Host-oauth"}


def sign_in(client: TestClient, repository: FakeRepository, user_id: int = 1) -> None:
    from app.auth.sessions import create_session

    repository.users[user_id] = User(
        id=user_id, yandex_uid=str(user_id), login=f"owner{user_id}", consent_at=datetime.now(UTC)
    )
    repository.businesses[user_id] = Business(id=user_id, user_id=user_id)
    session = create_session(user_id)
    repository.sessions[session.id] = session
    client.cookies.set("__Host-session", session.token, domain="testserver.local", path="/")
    client.cookies.set("__Host-csrf", session.csrf_secret, domain="testserver.local", path="/")


@pytest.mark.parametrize("secret", ["short", "replace-with-random-secret", "x" * 31])
def test_weak_secret_key_rejected(client: TestClient, monkeypatch: pytest.MonkeyPatch, secret: str) -> None:
    from pydantic import ValidationError

    from app.config import Settings

    monkeypatch.setenv("SECRET_KEY", secret)
    with pytest.raises(ValidationError):
        Settings()


def test_delete_me_requires_login_and_csrf(client: TestClient, repository: FakeRepository) -> None:
    assert client.delete("/api/me").status_code == 401
    sign_in(client, repository)
    assert client.delete("/api/me").status_code == 403
    assert client.delete("/api/me", headers={"X-CSRF-Token": "wrong"}).status_code == 403
    assert 1 in repository.users


def test_delete_me_removes_user_and_own_business_directories_only(
    client: TestClient, repository: FakeRepository, tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("app.config.settings.media_dir", tmp_path / "media")
    # Прежний ролик уже не упомянут ни в одной строке assets, но лежит в каталоге бизнеса.
    mine, old_video, theirs = tmp_path / "media/1/a.jpg", tmp_path / "media/1/video-old.mp4", tmp_path / "media/2/b.jpg"
    outside = tmp_path / "outside.jpg"
    for path in (mine, old_video, theirs, outside):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"x")
    sign_in(client, repository, user_id=2)
    sign_in(client, repository)
    response = client.delete("/api/me", headers={"X-CSRF-Token": client.cookies["__Host-csrf"]})
    assert response.status_code == 200
    assert response.json() == {"stopped": True, "message": "Данные удалены. Рекламу перед удалением остановили."}
    assert 1 not in repository.users and 2 in repository.users
    assert not (tmp_path / "media/1").exists()
    assert theirs.exists() and outside.exists() and (tmp_path / "media").is_dir()
    assert not client.cookies
    assert client.get("/api/me").status_code == 401


def test_delete_me_stops_ads_before_revoking_and_reports_unconfirmed_stop(
    client: TestClient, repository: FakeRepository, mock_yandex: respx.MockRouter
) -> None:
    sign_in(client, repository)
    repository.accounts[1] = PlatformAccount(
        id=1, business_id=1, platform="direct", auth_mode="oauth", status="ok", access_token_enc="direct-token"
    )
    repository.stop_confirmed = False
    response = client.delete("/api/me", headers={"X-CSRF-Token": client.cookies["__Host-csrf"]})
    assert response.status_code == 200
    assert response.json()["stopped"] is False
    assert "не все площадки подтвердили остановку" in response.json()["message"]
    assert "Яндекс Директ: площадка не подтвердила остановку" in response.json()["message"]
    assert repository.stops == [(1, None, None)]
    assert 1 not in repository.users and mock_yandex["revoke"].call_count == 1


DEMO_HEADERS = {"X-Requested-With": "XMLHttpRequest"}


@pytest.mark.parametrize("mode,flag,enabled", [("live", "1", False), ("fake", None, False), ("fake", "0", False),
                                              ("fake", "1", True)])
def test_demo_gate_uses_both_explicit_settings(client, repository, monkeypatch, mode, flag, enabled):
    from app.config import Settings

    monkeypatch.setenv("PLATFORM_MODE", mode)
    monkeypatch.setenv("APP_BASE_URL", "https://localhost")
    monkeypatch.delenv("VK_CLIENT_ID", raising=False)
    if flag is None:
        monkeypatch.delenv("DEMO_LOGIN", raising=False)
    else:
        monkeypatch.setenv("DEMO_LOGIN", flag)
    monkeypatch.setattr("app.config.settings", Settings())
    config = client.get("/api/auth/config")
    assert config.json() == {"demo": enabled, "vk_oauth": False, "operator_requisites": ""}
    assert config.headers["cache-control"] == "no-store"
    assert client.post("/api/auth/demo", headers=DEMO_HEADERS).status_code == (204 if enabled else 404)
    assert bool(repository.users) is enabled
    assert bool(repository.sessions) is enabled
    assert not repository.businesses


def test_demo_login_rotates_session_and_issues_working_csrf(client, repository, monkeypatch):
    monkeypatch.setattr("app.config.settings.demo_login", True)
    response = client.post("/api/auth/demo", headers=DEMO_HEADERS)
    assert response.status_code == 204
    assert response.headers["cache-control"] == "no-store"
    previous = client.cookies["__Host-session"]
    session = repository.get_session(previous)
    assert session.csrf_secret == client.cookies["__Host-csrf"]
    assert client.get("/api/me").json()["login"] == "demo"
    for cookie in response.headers.get_list("set-cookie"):
        assert "Secure" in cookie and "SameSite=lax" in cookie
    assert "HttpOnly" in response.headers.get_list("set-cookie")[0]
    assert "HttpOnly" not in response.headers.get_list("set-cookie")[1]
    assert client.post("/api/auth/logout").status_code == 403
    assert client.post("/api/auth/demo", headers=DEMO_HEADERS).status_code == 204
    assert repository.get_session(previous) is None
    assert client.cookies["__Host-session"] != previous
    assert len(repository.users) == 2
    assert client.post("/api/auth/logout", headers={"X-CSRF-Token": client.cookies["__Host-csrf"]}).status_code == 204
    assert client.get("/api/me").status_code == 401


@pytest.mark.parametrize(
    "headers",
    [{}, {"X-Requested-With": "fetch"}, DEMO_HEADERS | {"Sec-Fetch-Site": "cross-site"},
     DEMO_HEADERS | {"Sec-Fetch-Site": "same-site"}],
)
def test_demo_login_rejects_cross_site_requests(client, repository, monkeypatch, headers):
    monkeypatch.setattr("app.config.settings.demo_login", True)
    assert client.post("/api/auth/demo", headers=headers).status_code == 403
    assert not repository.users and "__Host-session" not in client.cookies
    response = client.post("/api/auth/demo", headers=DEMO_HEADERS | {"Sec-Fetch-Site": "same-origin"})
    assert response.status_code == 204


def test_demo_login_rate_limited_per_ip(client, repository, monkeypatch):
    from app.api import limits

    now = [1000.0]
    monkeypatch.setattr("app.config.settings.demo_login", True)
    monkeypatch.setattr(limits, "monotonic", lambda: now[0])
    limits.demo_login_limit.hits["ip:203.0.113.7"] = deque([now[0]] * 10)
    for _ in range(10):
        assert client.post("/api/auth/demo", headers=DEMO_HEADERS).status_code == 204
    response = client.post("/api/auth/demo", headers=DEMO_HEADERS)
    assert response.status_code == 429 and len(repository.users) == 10
    assert response.json()["detail"] == "Слишком много входов в демо. Подождите минуту."
    now[0] += 60
    assert client.post("/api/auth/demo", headers=DEMO_HEADERS).status_code == 204


def test_yandex_start_is_rate_limited_per_ip_with_page(client, monkeypatch):
    from app.api import limits

    now = [1000.0]
    monkeypatch.setattr(limits, "monotonic", lambda: now[0])
    for _ in range(20):
        start(client)
    response = client.get("/api/auth/yandex/start")
    assert response.status_code == 429 and response.headers["content-type"].startswith("text/html")
    assert "Слишком много попыток входа" in response.text and response.headers["Retry-After"] == "61"
    now[0] += 60
    start(client)


def test_demo_login_expires_old_demo_users_and_caps_live_ones(client, repository, monkeypatch, tmp_path):
    from app.config import settings
    from app.demo_flags import advance_jobs
    from app.platforms.fake import FakeAdapter

    monkeypatch.setattr(settings, "demo_login", True)
    monkeypatch.setattr(settings, "media_dir", tmp_path)
    old = repository.save_user("demo-old", "demo", None)
    old.created_at -= timedelta(hours=25)
    owner = repository.save_user("100", "owner", None)
    owner.created_at -= timedelta(days=30)
    fresh = repository.save_user("demo-fresh", "demo", None)
    repository.businesses = {
        7: Business(id=7, user_id=old.id),
        8: Business(id=8, user_id=fresh.id),
        9: Business(id=9, user_id=owner.id),
    }
    (tmp_path / "7").mkdir()
    (tmp_path / "7" / "photo.jpg").write_bytes(b"x")
    fakes = client.app.state.platform_adapters.fakes
    fakes.update({(7, "direct"): FakeAdapter(), (7, "vk"): FakeAdapter(), (8, "direct"): FakeAdapter()})
    advance_jobs.start(7, 1)
    assert client.post("/api/auth/demo", headers=DEMO_HEADERS).status_code == 204
    assert old.id not in repository.users and {owner.id, fresh.id} <= set(repository.users)
    assert set(repository.businesses) == {8, 9}
    assert not (tmp_path / "7").exists()
    assert set(fakes) == {(8, "direct")} and advance_jobs.get(7) is None
    # Бизнес удалён не через истечение демо (владелец удалил аккаунт сам): его площадки и перемотка тоже забываются.
    del repository.businesses[8]
    advance_jobs.start(8, 1)
    assert client.post("/api/auth/demo", headers=DEMO_HEADERS).status_code == 204
    assert not fakes and advance_jobs.get(8) is None
    for index in range(46):
        repository.save_user(f"demo-{index}", "demo", None)
    assert len([user for user in repository.users.values() if user.yandex_uid.startswith("demo-")]) == 49
    assert client.post("/api/auth/demo", headers=DEMO_HEADERS).status_code == 204
    users = len(repository.users)
    response = client.post("/api/auth/demo", headers=DEMO_HEADERS)
    assert response.status_code == 503 and len(repository.users) == users


def test_delete_me_waits_for_business_lock_and_reports_busy(
    client: TestClient, repository: FakeRepository, mock_yandex: respx.MockRouter, monkeypatch
) -> None:
    from app.services.changes import ChangeConflict

    sign_in(client, repository)
    waits = []

    def busy(user_id: int, wait: float = 0) -> list[int]:
        waits.append(wait)
        raise ChangeConflict("Этот бизнес уже обрабатывается.")

    monkeypatch.setattr(repository, "delete_user", busy)
    response = client.delete("/api/me", headers={"X-CSRF-Token": client.cookies["__Host-csrf"]})
    assert response.status_code == 409 and "Повторите удаление" in response.json()["detail"]
    assert waits == [15] and 1 in repository.users


@pytest.mark.parametrize(
    "client_id,enabled,shown", [("", True, False), ("vk-app", False, False), ("vk-app", True, True)]
)
def test_auth_config_tells_whether_vk_login_is_set_up(client, monkeypatch, client_id, enabled, shown):
    monkeypatch.setattr("app.config.settings.vk_client_id", client_id)
    monkeypatch.setattr("app.config.settings.vk_oauth_enabled", enabled)
    assert client.get("/api/auth/config").json()["vk_oauth"] is shown


def test_accounts_check_needs_login_csrf_and_own_business(
    client: TestClient, repository: FakeRepository, mock_yandex: respx.MockRouter
) -> None:
    login(client, repository)
    assert callback(client, start(client, "direct")["state"]).status_code == 303
    assert client.post("/api/accounts/check").status_code == 403
    csrf = {"X-CSRF-Token": client.cookies["__Host-csrf"]}
    repository.businesses[2] = Business(id=2, user_id=2)
    assert client.post("/api/accounts/check", params={"business_id": 2}, headers=csrf).status_code == 404
    response = client.post("/api/accounts/check", params={"business_id": 1}, headers=csrf)
    assert response.status_code == 200 and response.headers["cache-control"] == "no-store"
    client.cookies.clear()
    assert client.post("/api/accounts/check", headers=csrf).status_code == 401


def test_accounts_check_asks_each_connected_platform_in_fake_mode(
    client: TestClient, repository: FakeRepository, mock_yandex: respx.MockRouter
) -> None:
    login(client, repository)
    assert callback(client, start(client, "direct")["state"]).status_code == 303
    repository.accounts[2] = PlatformAccount(id=2, business_id=1, platform="vk", status="ok", external_login="vk")
    csrf = {"X-CSRF-Token": client.cookies["__Host-csrf"]}
    items = client.post("/api/accounts/check", headers=csrf).json()["items"]
    assert items == [
        {"platform": "direct", "ok": True, "message": "Работает."},
        {"platform": "vk", "ok": True, "message": "Работает."},
        {"platform": "metrica", "ok": True, "message": "Работает. Счётчиков на аккаунте: 1."},
    ]
    client.app.state.platform_adapters.resolve(1, "vk").inject_error(401)
    repository.accounts[1].status = "needs_reauth"
    items = client.post("/api/accounts/check", headers=csrf).json()["items"]
    assert items == [
        {"platform": "direct", "ok": False, "message": "Доступ истёк — подключите кабинет заново."},
        {"platform": "vk", "ok": False, "message": "Доступ истёк — подключите кабинет заново."},
    ]
    assert repository.accounts[2].status == "needs_reauth"


def test_accounts_check_is_rate_limited(
    client: TestClient, repository: FakeRepository, mock_yandex: respx.MockRouter
) -> None:
    login(client, repository)
    csrf = {"X-CSRF-Token": client.cookies["__Host-csrf"]}
    statuses = [client.post("/api/accounts/check", headers=csrf).status_code for _ in range(7)]
    assert statuses == [200] * 6 + [429]
    assert "слишком часто" in client.post("/api/accounts/check", headers=csrf).json()["detail"]


def test_failed_resume_after_reconnect_keeps_redirect_and_logs_no_details(
    client: TestClient,
    repository: FakeRepository,
    mock_yandex: respx.MockRouter,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    from app.api.auth import get_ad_resumer

    def broken_session():
        raise RuntimeError("access_token=secret-token")

    monkeypatch.setattr("app.db.session.SessionLocal", broken_session)
    client.app.dependency_overrides.pop(get_ad_resumer)
    login(client, repository)
    response = callback(client, start(client, "direct")["state"])
    assert response.status_code == 303 and repository.accounts[1].status == "ok"
    assert "RuntimeError" in caplog.text and "secret-token" not in caplog.text


def test_callback_error_is_a_page_not_json(client: TestClient) -> None:
    """Браузер пришёл переходом от Яндекса: ошибка входа — страница с текстом и ссылкой, без голого JSON."""
    response = client.get("/api/auth/yandex/callback", params={"state": "unknown<script>", "code": "x"})
    assert response.status_code == 400 and "text/html" in response.headers["content-type"]
    assert "Не удалось проверить вход" in response.text and 'href="/"' in response.text
    assert "<script>" not in response.text and response.headers["referrer-policy"] == "no-referrer"
    # CSP default-src 'self' режет встроенные стили: страница без них.
    assert "<style" not in response.text and "style=" not in response.text
