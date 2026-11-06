import logging
import re
from dataclasses import dataclass, field
from typing import Any

import httpx

from app.auth.vk import API_URL, account_credentials, key_client_id
from app.db.models import PlatformAccount
from app.platforms.base import AuthExpired

logger = logging.getLogger(__name__)

YANDEX_REVOKE_URL = "https://oauth.yandex.ru/revoke_token"
# Удаляет все токены пользователя VK у этого client_id; без user_id — токены владельца ключа API.
VK_REVOKE_URL = f"{API_URL}/oauth2/token/delete.json"
TIMEOUT = 10


@dataclass(frozen=True)
class Revocation:
    platform: str
    access_token: str = field(default="", repr=False)
    user_id: str | None = None
    client_id: str = ""
    client_secret: str = field(default="", repr=False)


def revocation(account: PlatformAccount) -> Revocation | None:
    if account.platform == "direct":
        return Revocation("direct", account.access_token_enc) if account.access_token_enc else None
    if account.platform != "vk":
        return None
    if account.auth_mode != "manual_key":
        # Без user_id VK удалил бы токены владельца нашего приложения, а не пользователя.
        return Revocation("vk", user_id=account.external_login) if account.external_login else None
    try:
        _, credentials = account_credentials(account)
    except AuthExpired:
        return None
    return Revocation("vk", client_id=credentials["client_id"], client_secret=credentials["client_secret"])


def same_vk_access(account: PlatformAccount, other: PlatformAccount) -> bool:
    """Отзыв у VK снимает все токены пользователя у одного client_id: при OAuth — у нашего приложения,
    при ключе API — у client_id этого ключа. Кабинет, подключённый по-другому, отзыв не затрагивает."""
    if (other.platform, other.auth_mode, other.external_login) != ("vk", account.auth_mode, account.external_login):
        return False
    return account.auth_mode != "manual_key" or key_client_id(account) == key_client_id(other)


async def revoke_tokens(http: httpx.AsyncClient, revocations: list[Revocation], *, config: Any = None) -> None:
    """Отзыв у площадок после удаления у нас: ошибка не мешает удалению, в журнал — без токенов и ответов."""
    if config is None:
        from app.config import settings

        config = settings
    for item in revocations:
        if item.platform == "direct":
            url = YANDEX_REVOKE_URL
            data = {
                "access_token": item.access_token,
                "client_id": config.yandex_client_id,
                "client_secret": config.yandex_client_secret.get_secret_value(),
            }
        else:
            url = VK_REVOKE_URL
            data = {
                "client_id": item.client_id or config.vk_client_id,
                "client_secret": item.client_secret or config.vk_client_secret.get_secret_value(),
            }
            if item.user_id:
                data["user_id"] = item.user_id
        if not data["client_id"] or not data["client_secret"]:
            logger.warning("Token revocation skipped: platform=%s has no client credentials", item.platform)
            continue
        try:
            response = await http.post(url, data=data, timeout=TIMEOUT, follow_redirects=False)
        except httpx.HTTPError as error:
            logger.warning("Token revocation failed: platform=%s error=%s", item.platform, type(error).__name__)
            continue
        if not response.is_success:
            logger.warning(
                "Token revocation failed: platform=%s status=%s code=%s",
                item.platform,
                response.status_code,
                error_code(response),
            )


async def revoke_refused(http: httpx.AsyncClient, revocations: list[Revocation]) -> None:
    """Отзыв токенов кабинета, который не подключили (OtherAccount): сбой отзыва любого рода не должен подменять
    ответ «это другой кабинет» ошибкой сервера."""
    try:
        await revoke_tokens(http, revocations)
    except Exception as error:
        logger.warning("Token revocation of refused account failed: %s", type(error).__name__)


def error_code(response: httpx.Response) -> str:
    """Только машинный код ошибки из ответа; описания и тела запросов в журнал не попадают."""
    try:
        error = response.json().get("error")
    except (ValueError, AttributeError):
        return "-"
    if isinstance(error, dict):
        error = error.get("code")
    return error if isinstance(error, str) and re.fullmatch(r"[a-z_]{1,64}", error) else "-"
