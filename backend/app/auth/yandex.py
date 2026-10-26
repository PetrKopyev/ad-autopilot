import base64
import hashlib
import hmac
import secrets
from datetime import UTC, datetime, timedelta
from typing import Literal
from urllib.parse import urlencode

import httpx

from app.auth.sessions import STATE_TTL
from app.config import settings
from app.db.models import OAuthState

Purpose = Literal["login", "direct"]
SCOPES = {"login": ("login:info", "login:email"), "direct": ("direct:api", "metrika:read")}
AUTHORIZE_URL = "https://oauth.yandex.ru/authorize"
TOKEN_URL = "https://oauth.yandex.ru/token"
PROFILE_URL = "https://login.yandex.ru/info"


class YandexError(Exception):
    pass


def redirect_uri() -> str:
    return settings.app_base_url.rstrip("/") + "/api/auth/yandex/callback"


def _signature(payload: str, purpose: str, user_id: int | None, binding: str) -> str:
    message = f"{payload}|{purpose}|{user_id}|{binding}".encode()
    return hmac.new(settings.secret_key.get_secret_value().encode(), message, hashlib.sha256).hexdigest()


def create_state(purpose: Purpose, binding: str, user_id: int | None, business_id: int | None = None) -> OAuthState:
    # The signed state carries the business and session binding missing from the model.
    payload = f"{secrets.token_urlsafe(32)}.{business_id or 0}"
    state = f"{payload}.{_signature(payload, purpose, user_id, binding)}"
    return OAuthState(
        state=state,
        purpose=purpose,
        user_id=user_id,
        pkce_verifier=secrets.token_urlsafe(64),
        expires_at=datetime.now(UTC) + STATE_TTL,
    )


def verify_state(state: OAuthState, binding: str) -> int | None:
    try:
        nonce, business, signature = state.state.split(".")
        business_id = int(business)
    except (ValueError, AttributeError):
        raise YandexError("Не удалось проверить вход. Начните заново.") from None
    expected = _signature(f"{nonce}.{business}", state.purpose, state.user_id, binding)
    if (
        not binding
        or not secrets.compare_digest(signature.encode(), expected.encode())
        or state.purpose not in SCOPES
        or state.expires_at <= datetime.now(UTC)
        or not state.pkce_verifier
        or (state.purpose == "direct" and (business_id <= 0 or state.user_id is None))
        or (state.purpose == "login" and business_id != 0)
    ):
        raise YandexError("Срок входа истёк или запрос недействителен. Начните заново.")
    return business_id or None


def authorization_url(state: OAuthState) -> str:
    if not settings.yandex_client_id:
        raise YandexError("Вход через Яндекс пока не настроен. Попробуйте позже.")
    challenge = base64.urlsafe_b64encode(hashlib.sha256(state.pkce_verifier.encode()).digest()).rstrip(b"=").decode()
    params = {
        "response_type": "code",
        "client_id": settings.yandex_client_id,
        "redirect_uri": redirect_uri(),
        "scope": " ".join(SCOPES[state.purpose]),
        "state": state.state,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
    }
    if state.purpose == "direct":
        params["force_confirm"] = "yes"
    return AUTHORIZE_URL + "?" + urlencode(params)


def _json(response: httpx.Response) -> dict:
    if not response.is_success:
        raise YandexError("Яндекс не подтвердил вход. Попробуйте войти заново.")
    try:
        data = response.json()
    except ValueError:
        raise YandexError("Яндекс вернул некорректный ответ. Повторите попытку позже.") from None
    if not isinstance(data, dict) or "error" in data:
        raise YandexError("Яндекс не подтвердил вход. Попробуйте войти заново.")
    return data


async def exchange_code(code: str, state: OAuthState) -> dict:
    data = {
        "grant_type": "authorization_code",
        "code": code,
        "client_id": settings.yandex_client_id,
        "redirect_uri": redirect_uri(),
        "code_verifier": state.pkce_verifier,
    }
    if settings.yandex_client_secret.get_secret_value():
        data["client_secret"] = settings.yandex_client_secret.get_secret_value()
    try:
        async with httpx.AsyncClient(timeout=10, follow_redirects=False) as client:
            tokens = _json(await client.post(TOKEN_URL, data=data))
    except httpx.RequestError:
        raise YandexError("Не удалось связаться с Яндексом. Попробуйте войти позже.") from None
    access_token = tokens.get("access_token")
    refresh_token = tokens.get("refresh_token")
    expires_in = tokens.get("expires_in")
    scopes = tokens.get("scope", " ".join(SCOPES[state.purpose]))
    if (
        not isinstance(access_token, str)
        or not access_token.strip()
        or (refresh_token is not None and (not isinstance(refresh_token, str) or not refresh_token.strip()))
        or (expires_in is not None and (type(expires_in) is not int or expires_in < 0))
        or not isinstance(scopes, str)
        or str(tokens.get("token_type", "bearer")).lower() != "bearer"
    ):
        raise YandexError("Яндекс вернул некорректный ответ. Повторите попытку позже.")
    try:
        expires_at = datetime.now(UTC) + timedelta(seconds=expires_in) if expires_in is not None else None
    except OverflowError:
        raise YandexError("Яндекс вернул некорректный срок доступа. Повторите попытку позже.") from None
    return {
        "access_token": access_token,
        "refresh_token": refresh_token,
        "expires_at": expires_at,
        "scopes": scopes.split(),
    }


async def get_profile(access_token: str) -> dict:
    try:
        async with httpx.AsyncClient(timeout=10, follow_redirects=False) as client:
            profile = _json(
                await client.get(
                    PROFILE_URL, params={"format": "json"}, headers={"Authorization": f"OAuth {access_token}"}
                )
            )
    except httpx.RequestError:
        raise YandexError("Не удалось получить данные Яндекс ID. Попробуйте войти позже.") from None
    if (
        not isinstance(profile.get("id"), str)
        or not profile["id"]
        or not isinstance(profile.get("login"), str)
        or not profile["login"]
        or (profile.get("default_email") is not None and not isinstance(profile["default_email"], str))
    ):
        raise YandexError("Яндекс вернул некорректные данные профиля. Повторите попытку позже.")
    return profile
