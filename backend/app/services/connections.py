import logging
from collections.abc import Awaitable, Callable
from functools import partial

from starlette.concurrency import run_in_threadpool

from app.platforms.base import AdPlatform, AuthExpired, PlatformError, RateLimited
from app.platforms.metrica import Metrica, MetricaAccessError, MetricaAuthError, MetricaError

logger = logging.getLogger(__name__)

RECONNECT = "Доступ истёк — подключите кабинет заново."
# Коды ошибок API Директа, которые владелец может исправить сам.
DIRECT_CODES = {
    "58": "Яндекс ещё не одобрил доступ к API Директа. Дождитесь ответа на заявку и проверьте снова.",
    "513": "К этому аккаунту Яндекса не подключён Директ. Подключите аккаунт, в котором есть рекламный кабинет.",
    "54": "У этого аккаунта нет прав на рекламный кабинет Директа.",
    "3000": "Яндекс закрыл этому аккаунту доступ к API Директа. Проверьте в кабинете Директа, не заблокирован ли "
    "аккаунт, или напишите в поддержку Директа.",
}
DIRECT_FAILED = "Директ ответил ошибкой. Проверьте ещё раз позже."
WORKING = {"direct": "Директ отвечает.", "vk": "VK Реклама отвечает."}
NO_COUNTERS = "На этом аккаунте Яндекса нет счётчиков Метрики."


def failure(error: Exception) -> str:
    if isinstance(error, (AuthExpired, MetricaAuthError)):
        return RECONNECT
    if isinstance(error, RateLimited):
        return "Площадка просит подождать. Проверьте ещё раз через минуту."
    if isinstance(error, MetricaAccessError):
        return "Нет доступа к Метрике. Подключите Директ заново и разрешите доступ к Метрике."
    code = str(getattr(error, "direct_code", ""))
    if not code:
        return str(error)
    if code in DIRECT_CODES:
        return DIRECT_CODES[code]
    logger.warning("Проверка подключения: Директ ответил ошибкой %s: %s", code, error)
    return DIRECT_FAILED


async def check_connections(
    business_id: int,
    statuses: dict[str, str],
    resolve: Callable[[int, str], AdPlatform],
    metrica: Callable[[int], Metrica],
    expire: Callable[[int, str], None],
) -> list[dict]:
    """Живой запрос к каждой подключённой площадке: Директ обязателен и проверяется всегда, VK — если подключён."""
    expired: set[str] = set()

    async def check(platform: str, call: Callable[[], Awaitable[tuple[bool, str]]]) -> dict:
        try:
            ok, message = await call()
        except (PlatformError, MetricaError) as error:
            if isinstance(error, (AuthExpired, MetricaAuthError)):
                account = "direct" if platform == "metrica" else platform
                expired.add(account)
                await run_in_threadpool(expire, business_id, account)
            ok, message = False, failure(error)
        return {"platform": platform, "ok": ok, "message": message}

    async def whoami(platform: str) -> tuple[bool, str]:
        await (await run_in_threadpool(resolve, business_id, platform)).whoami()
        return True, WORKING[platform]

    async def counters() -> tuple[bool, str]:
        found = await (await run_in_threadpool(metrica, business_id)).list_counters()
        return (True, f"Метрика отвечает, счётчиков: {len(found)}.") if found else (False, NO_COUNTERS)

    results = []
    if statuses.get("direct", "revoked") == "revoked":
        results.append({"platform": "direct", "ok": False, "message": "Директ не подключён."})
    for platform in ("direct", "vk"):
        if statuses.get(platform) == "ok":
            results.append(await check(platform, partial(whoami, platform)))
        elif statuses.get(platform) == "needs_reauth":
            results.append({"platform": platform, "ok": False, "message": RECONNECT})
    if statuses.get("direct") == "ok" and "direct" not in expired:
        results.append(await check("metrica", counters))
    return results
