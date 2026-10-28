from collections.abc import Awaitable, Callable

from starlette.concurrency import run_in_threadpool

from app.platforms.base import AdPlatform, AuthExpired, PlatformError, RateLimited
from app.platforms.metrica import Metrica, MetricaAccessError, MetricaAuthError, MetricaError

RECONNECT = "Доступ истёк — подключите кабинет заново."
# Коды ошибок API Директа, которые владелец может исправить сам.
DIRECT_CODES = {
    "58": "Яндекс ещё не одобрил доступ к API Директа. Дождитесь ответа на заявку и проверьте снова.",
    "513": "К этому аккаунту Яндекса не подключён Директ. Подключите аккаунт, в котором есть рекламный кабинет.",
    "54": "У этого аккаунта нет прав на рекламный кабинет Директа.",
}
WORKING = {"direct": "Директ отвечает.", "vk": "VK Реклама отвечает."}
NO_COUNTERS = "На этом аккаунте Яндекса нет счётчиков Метрики."


def failure(error: Exception) -> str:
    if isinstance(error, (AuthExpired, MetricaAuthError)):
        return RECONNECT
    if isinstance(error, RateLimited):
        return "Площадка просит подождать. Проверьте ещё раз через минуту."
    if isinstance(error, MetricaAccessError):
        return "Нет доступа к Метрике. Подключите Директ заново и разрешите доступ к Метрике."
    return DIRECT_CODES.get(str(getattr(error, "direct_code", "")), str(error))


async def check(platform: str, call: Callable[[], Awaitable[tuple[bool, str]]]) -> dict:
    try:
        ok, message = await call()
    except (PlatformError, MetricaError) as error:
        ok, message = False, failure(error)
    return {"platform": platform, "ok": ok, "message": message}


async def check_connections(
    business_id: int,
    statuses: dict[str, str],
    resolve: Callable[[int, str], AdPlatform],
    metrica: Callable[[int], Metrica],
) -> list[dict]:
    """Живой запрос к каждой подключённой площадке: Директ обязателен и проверяется всегда, VK — если подключён."""

    async def whoami(platform: str) -> tuple[bool, str]:
        await (await run_in_threadpool(resolve, business_id, platform)).whoami()
        return True, WORKING[platform]

    async def counters() -> tuple[bool, str]:
        found = await (await run_in_threadpool(metrica, business_id)).list_counters()
        return (True, f"Метрика отвечает, счётчиков: {len(found)}.") if found else (False, NO_COUNTERS)

    results = []
    for platform in ("direct", "vk"):
        status = statuses.get(platform, "revoked")
        if status == "revoked":
            if platform == "direct":
                results.append({"platform": platform, "ok": False, "message": "Директ не подключён."})
        elif status != "ok":
            results.append({"platform": platform, "ok": False, "message": RECONNECT})
        else:
            results.append(await check(platform, lambda platform=platform: whoami(platform)))
    if statuses.get("direct") == "ok":
        results.append(await check("metrica", counters))
    return results
