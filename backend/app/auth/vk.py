import asyncio
import hashlib
import hmac
import json
import secrets
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import urlencode

import httpx
from sqlalchemy import select

from app.db.models import Business, OAuthState, PlatformAccount
from app.platforms.base import AuthExpired, AuthSession, PlatformError, PlatformLimits, RateLimited

API_URL = "https://ads.vk.com/api/v2"
AUTHORIZE_URL = "https://ads.vk.com/hq/settings/access"
SCOPES = ("read_ads", "create_ads", "read_payments")
TOKEN_TTL = 86400
REFRESH_MARGIN = timedelta(minutes=1)
# Запас до истечения, за который фоновый шаг воркера продлевает токен кабинета заранее:
# тик каждые 30 минут успевает продлить токен раньше, чем доступ упадёт в needs_reauth.
RENEW_MARGIN = timedelta(minutes=45)

# Ambiguities in the supplied research are isolated here and in platforms.vk.API_FORMAT:
# reset headers are Unix timestamps (Retry-After is seconds); missing expires_in = 24h.
# Manual credentials live in a versioned envelope in the existing encrypted refresh field.
# They are needed to renew a manual token, including when VK omits refresh_token.
TOKEN_ERRORS = {"invalid_grant", "invalid_client", "invalid_request", "access_denied"}
MANUAL_VERSION = "vk-manual-v1"


def _settings():
    from app.config import settings

    return settings


def redirect_uri(config: Any = None) -> str:
    return (config or _settings()).app_base_url.rstrip("/") + "/api/auth/vk/callback"


def _signature(payload: str, user_id: int, binding: str) -> str:
    message = f"{payload}|vk|{user_id}|{binding}".encode()
    return hmac.new(_settings().secret_key.get_secret_value().encode(), message, hashlib.sha256).hexdigest()


def create_state(binding: str, user_id: int, business_id: int) -> OAuthState:
    from app.auth.sessions import STATE_TTL

    payload = f"{secrets.token_urlsafe(32)}.{business_id}"
    return OAuthState(
        state=f"{payload}.{_signature(payload, user_id, binding)}",
        purpose="vk",
        user_id=user_id,
        expires_at=datetime.now(UTC) + STATE_TTL,
    )


def verify_state(state: OAuthState, binding: str, user_id: int) -> int:
    try:
        nonce, business, signature = state.state.split(".")
        business_id = int(business)
    except (ValueError, AttributeError):
        raise PlatformError("Не удалось проверить вход. Начните заново.") from None
    expected = _signature(f"{nonce}.{business}", user_id, binding)
    if (
        not binding
        or state.purpose != "vk"
        or state.user_id != user_id
        or business_id <= 0
        or state.expires_at <= datetime.now(UTC)
        or not secrets.compare_digest(signature.encode(), expected.encode())
    ):
        raise PlatformError("Срок входа истёк или запрос недействителен. Начните заново.")
    return business_id


def _secret(value: Any) -> str:
    return value.get_secret_value() if hasattr(value, "get_secret_value") else str(value)


def invalid_response() -> PlatformError:
    return PlatformError("VK Реклама вернула некорректные данные. Повторите попытку позже.")


class VkClient:
    def __init__(
        self,
        token: str = "",
        *,
        config: Any = None,
        client: httpx.AsyncClient | None = None,
        client_id: str | None = None,
        client_secret: str | None = None,
        manual_key: bool = False,
        timeout: float = 30.0,
    ) -> None:
        self._config = config or _settings()
        self._client_id = self._config.vk_client_id if client_id is None else client_id
        self._client_secret = _secret(self._config.vk_client_secret) if client_secret is None else client_secret
        self._manual_key = manual_key
        self._token = token
        self._client = client if client is not None else httpx.AsyncClient()
        self._owns_client = client is None
        self._timeout = timeout
        self._limits = PlatformLimits()
        self._blocked_until: datetime | None = None
        self._refresh_lock = asyncio.Lock()
        self._session: AuthSession | None = None

    async def __aenter__(self) -> "VkClient":
        return self

    async def __aexit__(self, *args) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    def _read_limits(self, response: httpx.Response) -> int:
        now = datetime.now(UTC)
        windows: dict[str, dict[str, Any]] = {}
        for name, value in response.headers.items():
            # Окна считаем по парам заголовков: x-ratelimit-rps-remaining/x-ratelimit-rps-reset и т.п. —
            # каждая пара своё окно (sec/rps, hour, day). Блокируем до reset исчерпанного окна: секундный
            # отпускает через секунду, часовой и суточный ждут своего reset, а не вслепую 60 секунд.
            tokens = name.lower().split("-")
            if len(tokens) < 3 or tokens[0] != "x" or tokens[1] != "ratelimit":
                continue
            key = next((token for token in tokens[2:] if token in {"remaining", "reset"}), None)
            if key is None:
                continue
            window = "-".join(token for token in tokens[2:] if token != key)
            try:
                parsed = (
                    max(0, int(value)) if key == "remaining" else datetime.fromtimestamp(int(value), UTC)
                )
            except (ValueError, OverflowError, OSError):
                continue
            windows.setdefault(window, {})[key] = parsed
        remaining = [item["remaining"] for item in windows.values() if "remaining" in item]
        resets = [item["reset"] for item in windows.values() if "reset" in item]
        if remaining:
            self._limits = PlatformLimits(rate_limit_remaining=min(remaining), resets_at=max(resets, default=None))
        try:
            retry = max(1, int(response.headers.get("Retry-After", "60")))
        except ValueError:
            retry = 60
        if response.status_code == 429 or remaining and min(remaining) == 0:
            # Блокируем до reset исчерпанных окон. Если reset неизвестен (429 без заголовков, remaining=0
            # без reset), берём Retry-After или его умолчание — max() никогда не вызывается по пустой
            # последовательности, а живое окно не продлевает блокировку дольше своего reset.
            exhausted = [item.get("reset") for item in windows.values() if item.get("remaining") == 0]
            candidates = [reset for reset in exhausted if reset is not None]
            # Retry-After длиннее reset исчерпанного окна не игнорируем: ждём max() от обоих (мелочь круга 3).
            try:
                candidates.append(now + timedelta(seconds=max(1, int(response.headers["Retry-After"]))))
            except (KeyError, ValueError, OverflowError):
                # Retry-After не число или не влезает в timedelta (абсурдно большое значение):
                # не ломаемся, просто опираемся на reset окон.
                pass
            if not candidates:
                candidates = [now + timedelta(seconds=retry)]
            self._blocked_until = max(candidates)
            retry = max(1, int((self._blocked_until - now).total_seconds()))
        return retry

    @staticmethod
    def _error_code(response: httpx.Response) -> Any:
        try:
            return response.json().get("error")
        except (ValueError, AttributeError):
            return None

    @staticmethod
    def _field_errors(value: Any, path: str = "", depth: int = 0) -> list[str]:
        """Пути полей с ошибками из вложенного 'fields' живого ответа: 'campaigns[0].priced_goal: required ...'.
        Узлы-обёртки списков (items) и контейнеры с items прозрачны: спускаемся до листьев, где конкретная
        причина. Глубина ограничена."""
        if depth > 8:
            return []
        if isinstance(value, list):
            return [
                item
                for index, element in enumerate(value)
                for item in VkClient._field_errors(element, f"{path}[{index}]", depth + 1)
            ]
        if not isinstance(value, dict):
            return []
        reason = " ".join(str(value[key]) for key in ("code", "message") if value.get(key))
        here = [f"{path}: {reason}"] if reason and path else []
        items = value.get("items")
        nested = [
            item
            for key, element in value.items()
            if key != "items"
            for item in VkClient._field_errors(element, f"{path}.{key}" if path else str(key), depth + 1)
        ]
        if isinstance(items, list):
            nested += VkClient._field_errors(items, path, depth + 1)
        # Контейнер с вложенными причинами раскрываем глубже; лист без вложенного — сам причина.
        return nested or here

    @staticmethod
    def _error_detail(data: Any) -> str | None:
        """Причина отказа из тела ответа VK: code/message/description плюс вложенные fields с полем-виновником."""
        error = data.get("error") if isinstance(data, dict) else None
        if isinstance(error, dict):
            parts = [str(error[key]) for key in ("code", "message", "description") if error.get(key)]
            fields = VkClient._field_errors(error.get("fields"))
            if fields:
                parts.append("; ".join(fields[:5]))
            detail = "; ".join(parts)
            return detail[:600].rstrip(".; ") or None
        if isinstance(error, str) and error:
            return error.rstrip(".")
        return None

    async def _request(
        self, method: str, path: str, *, authenticated: bool = True, headers: dict | None = None, **kwargs
    ) -> dict:
        if self._blocked_until and self._blocked_until > datetime.now(UTC):
            raise RateLimited(max(1, int((self._blocked_until - datetime.now(UTC)).total_seconds())))
        if authenticated and not self._token:
            raise AuthExpired()
        headers = {"Accept": "application/json", **(headers or {})}
        if authenticated:
            headers["Authorization"] = f"Bearer {self._token}"
        try:
            response = await self._client.request(
                method, f"{API_URL}/{path}", headers=headers, timeout=self._timeout, follow_redirects=False, **kwargs
            )
        except httpx.RequestError:
            raise PlatformError("Не удалось связаться с VK Рекламой. Повторите попытку позже.") from None
        retry = self._read_limits(response)
        if response.status_code == 401:
            raise AuthExpired()
        if response.status_code == 429:
            raise RateLimited(retry)
        if not response.is_success:
            if not authenticated and response.status_code == 403:
                raise PlatformError(
                    "У VK достигнут предел в 5 токенов для этой связки. Подключите кабинет позже.", status_code=403
                )
            if not authenticated and self._error_code(response) in TOKEN_ERRORS:
                raise AuthExpired()
            try:
                detail = self._error_detail(response.json())
            except ValueError:
                detail = None
            message = f"Ошибка VK Рекламы (HTTP {response.status_code})"
            if detail:
                message = f"{message}: {detail}"
            raise PlatformError(
                f"{message}. Повторите попытку позже.",
                status_code=response.status_code,
            )
        if response.status_code == 204:
            return {}
        try:
            data = response.json()
        except ValueError:
            raise invalid_response() from None
        if not isinstance(data, dict):
            raise invalid_response()
        if "error" in data:
            error = data["error"]
            code = error.get("code") if isinstance(error, dict) else error
            if code in {"invalid_token", "invalid_grant", "token_expired"}:
                raise AuthExpired()
            if code in {"rate_limit_exceeded", "request_limit_exceeded"}:
                raise RateLimited(retry)
            detail = self._error_detail(data)
            raise PlatformError(
                f"VK Реклама отклонила запрос: {detail}." if detail else "VK Реклама отклонила запрос."
            )
        return data

    def auth_url(self, state: str) -> str:
        if not self._client_id:
            raise PlatformError("Подключение VK пока не настроено. Используйте ключ API.")
        return (
            AUTHORIZE_URL
            + "?"
            + urlencode(
                {
                    "action": "oauth2",
                    "response_type": "code",
                    "client_id": self._client_id,
                    "state": state,
                    "redirect_uri": redirect_uri(self._config),
                    "scope": ",".join(SCOPES),
                }
            )
        )

    async def _grant(self, grant_type: str, **params) -> AuthSession:
        if not self._client_id or not self._client_secret:
            raise AuthExpired()
        data = await self._request(
            "POST",
            "oauth2/token.json",
            authenticated=False,
            data={
                "grant_type": grant_type,
                "client_id": self._client_id,
                "client_secret": self._client_secret,
                **params,
            },
        )
        token = data.get("access_token")
        refresh = data.get("refresh_token", params.get("refresh_token"))
        expires = data.get("expires_in", TOKEN_TTL)
        scope = data.get("scope", [])
        if isinstance(scope, str):
            scope = scope.replace(",", " ").split()
        if (
            not isinstance(token, str)
            or not token.strip()
            or (refresh is not None and (not isinstance(refresh, str) or not refresh.strip()))
            or type(expires) is not int
            or expires <= 0
            or str(data.get("token_type", "bearer")).lower() != "bearer"
            or not isinstance(scope, list)
            or not all(isinstance(item, str) for item in scope)
            or (scope and not set(SCOPES) <= set(scope))
        ):
            raise invalid_response()
        try:
            expires_at = datetime.now(UTC) + timedelta(seconds=expires)
        except OverflowError:
            raise invalid_response() from None
        self._token = token
        return AuthSession("", token, refresh, expires_at)

    async def exchange_code(self, code: str) -> AuthSession:
        session = await self._grant("authorization_code", code=code, redirect_uri=redirect_uri(self._config))
        profile = await self.whoami()
        self._session = replace(session, account_id=profile["id"])
        return self._session

    async def client_credentials(self) -> AuthSession:
        session = await self._grant("client_credentials")
        profile = await self.whoami()
        self._session = replace(session, account_id=profile["id"])
        return self._session

    async def refresh(self, account: AuthSession, *, force: bool = False) -> AuthSession:
        async with self._refresh_lock:
            now = datetime.now(UTC) + REFRESH_MARGIN
            if not force:
                if self._session and self._session.account_id == account.account_id and self._session.expires_at > now:
                    self._token = self._session.access_token
                    return self._session
                if account.expires_at is not None and account.expires_at > now and account.access_token:
                    self._token = account.access_token
                    self._session = account
                    return account
            if account.refresh_token:
                updated = await self._grant("refresh_token", refresh_token=account.refresh_token)
            elif self._manual_key:
                updated = await self._grant("client_credentials")
            else:
                raise AuthExpired()
            self._session = replace(updated, account_id=account.account_id)
            return self._session

    async def whoami(self) -> dict[str, str]:
        data = await self._request("GET", "user.json")
        id_ = data.get("id")
        if type(id_) not in (int, str) or not str(id_) or isinstance(id_, int) and id_ <= 0:
            raise invalid_response()
        return {"id": str(id_), "name": str(data.get("username") or data.get("name") or id_)}


class VkAccountRepository:
    def __init__(self, db) -> None:
        self.db = db

    def lock_account(self, business_id: int) -> PlatformAccount | None:
        # Serialize all VK connections for a business, including its first connection.
        if self.db.scalar(select(Business).where(Business.id == business_id).with_for_update()) is None:
            raise PlatformError("Бизнес не найден.")
        return self.db.scalar(
            select(PlatformAccount)
            .where(PlatformAccount.business_id == business_id, PlatformAccount.platform == "vk")
            .with_for_update()
        )

    def save(
        self,
        business_id: int,
        account: PlatformAccount | None,
        session: AuthSession,
        *,
        auth_mode: str,
        client_id: str = "",
        client_secret: str = "",
    ) -> PlatformAccount:
        if account is None:
            account = PlatformAccount(business_id=business_id, platform="vk")
            self.db.add(account)
        account.auth_mode = auth_mode
        account.external_login = session.account_id
        account.access_token_enc = session.access_token
        account.refresh_token_enc = (
            json.dumps(
                {
                    "version": MANUAL_VERSION,
                    "client_id": client_id,
                    "client_secret": client_secret,
                    "refresh_token": session.refresh_token,
                }
            )
            if auth_mode == "manual_key"
            else session.refresh_token
        )
        account.expires_at = session.expires_at
        account.scopes = list(SCOPES)
        account.status = "ok"
        account.last_ok_at = datetime.now(UTC)
        self.db.commit()
        return account

    def release(self) -> None:
        self.db.commit()


def account_credentials(account: PlatformAccount) -> tuple[AuthSession, dict[str, Any]]:
    refresh = account.refresh_token_enc
    credentials = {}
    if account.auth_mode == "manual_key":
        try:
            envelope = json.loads(refresh or "{}")
            if envelope["version"] != MANUAL_VERSION:
                raise ValueError
            credentials = {key: envelope[key] for key in ("client_id", "client_secret")}
            if any(not isinstance(value, str) or not value for value in credentials.values()):
                raise ValueError
            refresh = envelope.get("refresh_token")
        except (ValueError, KeyError, TypeError):
            raise AuthExpired() from None
        credentials["manual_key"] = True
    return AuthSession(account.external_login or "", account.access_token_enc, refresh, account.expires_at), credentials


async def renew_account(
    db,
    business_id: int,
    *,
    config: Any = None,
    client: httpx.AsyncClient | None = None,
    force: bool = False,
):
    # The row lock makes parallel workers share one refresh instead of spending another of the 5 tokens.
    repository = VkAccountRepository(db)
    account = repository.lock_account(business_id)
    if account is None or account.status == "revoked":
        repository.release()
        raise AuthExpired()
    session, credentials = account_credentials(account)
    async with VkClient(session.access_token, config=config, client=client, **credentials) as vk:
        try:
            updated = await vk.refresh(session, force=force)
        except AuthExpired:
            account.status = "needs_reauth"
            repository.release()
            raise
    if updated is session:
        repository.release()
        return account
    return repository.save(
        business_id,
        account,
        updated,
        auth_mode=account.auth_mode,
        client_id=credentials.get("client_id", ""),
        client_secret=credentials.get("client_secret", ""),
    )
