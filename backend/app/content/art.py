import asyncio
import base64
import binascii
import math
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, Protocol

import httpx

# Цена одной картинки. Тариф Alice AI ART в публичных тарифах Yandex AI Studio проверить не удалось (страница цен
# закрыта капчей): берём цену YandexART по исследованию 26.10.2026 — 2,23 ₽ за запрос с НДС. Новая модель может стоить
# дороже: сверить с yandex.cloud/ru/docs/ai-studio/pricing и поднять.
ART_COST_KOP = 223
IMAGES_URL = "https://ai.api.cloud.yandex.net/v1/images/generations"
DEFAULT_ART_MODEL = "aliceai-image-art-3.0/latest"
# Квадрат подходит обеим площадкам: ВКонтакте берёт image_600x600, Директ — картинку от 1:1 до 4:3.
ART_SIZE = "1024x1024"

class ArtError(Exception):
    pass


class ArtBudgetExceeded(ArtError):
    pass


@dataclass
class ArtBudget:
    limit_kop: int
    spent_kop: int = 0
    # Резерв в общем пуле владельца до генерации (В11): закрывает гонку параллельных запросов.
    # on_reserve возвращает токен резерва (id строки ledger с final=false); on_settle фиксирует трату,
    # on_release снимает резерв. Ставит сервисный слой; бросает ArtBudgetExceeded при переполнении пула.
    on_reserve: Callable[[], Awaitable[Any]] | None = None
    on_settle: Callable[[Any], Awaitable[None]] | None = None
    on_release: Callable[[Any], Awaitable[None]] | None = None
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock, init=False, repr=False)

    def __post_init__(self) -> None:
        if any(type(value) is not int or value < 0 for value in (self.limit_kop, self.spent_kop)):
            raise ValueError("Budget must use non-negative integer kopecks")
        if self.spent_kop > self.limit_kop:
            raise ValueError("Spent budget exceeds limit")

    async def reserve(self) -> Any:
        # Сначала локальная проверка: если она не пройдена, резерв в общем пуле ещё не создан —
        # удалять нечего, и строка ledger не зависает (В3 круга 3).
        async with self._lock:
            if self.spent_kop + ART_COST_KOP > self.limit_kop:
                raise ArtBudgetExceeded("YandexART budget exhausted")
            self.spent_kop += ART_COST_KOP
        if self.on_reserve is None:
            return None
        try:
            return await self.on_reserve()
        except BaseException:
            async with self._lock:
                self.spent_kop -= ART_COST_KOP
            raise

    async def settle(self, token: Any) -> None:
        if token is not None and self.on_settle is not None:
            await self.on_settle(token)

    async def release(self, token: Any = None) -> None:
        async with self._lock:
            self.spent_kop -= ART_COST_KOP
        if token is not None and self.on_release is not None:
            await self.on_release(token)


@dataclass
class ArtCall:
    business_id: int
    cost_kop: int = ART_COST_KOP
    status: str = "pending"
    rejection_reason: str | None = None
    operation_id: str | None = None
    seed: int = 0
    id: int | None = None


@dataclass(frozen=True)
class ArtResult:
    image: bytes
    seed: int
    operation_id: str | None
    cost_kop: int = ART_COST_KOP
    label: str = "иллюстрация"
    selected: bool = False


class ArtClient(Protocol):
    async def generate(self, prompt: str, *, seed: int, business_id: int) -> ArtResult: ...


def validate_request(prompt: str, seed: int) -> None:
    if not prompt.strip():
        raise ValueError("Prompt must not be empty")
    if type(seed) is not int or not 0 <= seed < 2**63:
        raise ValueError("Seed must be a non-negative int64")


class YandexArt:
    """Картинки Alice AI ART через OpenAI-совместимый синхронный API Yandex AI Studio.

    record_call must durably upsert the call, assigning id on initial reservation.
    Share the budget across ART clients and initialize it from the AI spending ledger.
    Credentials are supplied by the caller from YC_API_KEY and YC_FOLDER_ID.
    """

    def __init__(
        self,
        client: httpx.AsyncClient,
        *,
        api_key: str,
        folder_id: str,
        budget: ArtBudget,
        record_call: Callable[[ArtCall], Awaitable[None]],
        model: str = DEFAULT_ART_MODEL,
        timeout: float = 90,
        max_retries: int = 2,
        retry_delay: float = 1,
    ) -> None:
        if not api_key or not folder_id or not model:
            raise ValueError("YC_API_KEY, YC_FOLDER_ID and YANDEXART_MODEL are required")
        if not math.isfinite(timeout) or timeout <= 0 or not 0 <= max_retries <= 3 or retry_delay < 0:
            raise ValueError("Invalid timeout or retry limits")
        self.client = client
        self._headers = {"Authorization": f"Bearer {api_key}", "OpenAI-Project": folder_id}
        self.model_uri = f"art://{folder_id}/{model}"
        self.budget = budget
        self.record_call = record_call
        self.timeout = timeout
        self.max_retries = max_retries
        self.retry_delay = retry_delay

    async def generate(self, prompt: str, *, seed: int, business_id: int) -> ArtResult:
        validate_request(prompt, seed)
        token = await self.budget.reserve()
        call = ArtCall(business_id, seed=seed)
        try:
            # Persist the reservation before a paid request.
            await self.record_call(call)
            response = await self.post({"model": self.model_uri, "prompt": prompt, "size": ART_SIZE})
            if response is None or not response.is_success:
                # Запрос не дошёл или отклонён: картинка не создана, и платить за неё не за что.
                call.status = "rejected"
                call.rejection_reason = f"http_{response.status_code}" if response is not None else "connect_error"
                call.cost_kop = 0
                await self.budget.release(token)
                raise ArtError("YandexART rejected generation")
            image = base64.b64decode(response.json()["data"][0]["b64_json"], validate=True)
            if not image:
                raise ValueError("Empty generated image")
            call.status = "succeeded"
            await self.budget.settle(token)
            return ArtResult(image, seed, None)
        except asyncio.CancelledError:
            call.status = "unknown"
            call.rejection_reason = "cancelled"
            # Резерв не снимаем: запрос мог уйти и списать деньги. Просроченный резерв закроет свипер (В4).
            raise
        except (httpx.HTTPError, ValueError, KeyError, IndexError, TypeError, binascii.Error) as exc:
            call.status = "unknown"
            call.rejection_reason = type(exc).__name__
            # Деньги могли уйти (запрос ушёл, ответ потерян): резерв остаётся открытым для свипера (В3/В4).
            raise ArtError("YandexART request failed; paid requests are not replayed") from None
        finally:
            await self.record_call(call)

    async def post(self, payload: dict) -> httpx.Response | None:
        """Повторяет только то, за что не платят: неотправленный запрос, 429 и 5xx. Обрыв после отправки (таймаут
        чтения) не повторяется: картинка могла быть создана. None — запрос так и не ушёл."""
        for attempt in range(self.max_retries + 1):
            delay = min(self.retry_delay * 2**attempt, 30)
            try:
                response = await self.client.post(
                    IMAGES_URL, headers=self._headers, json=payload, timeout=self.timeout, follow_redirects=False
                )
            except (httpx.ConnectError, httpx.ConnectTimeout):
                if attempt == self.max_retries:
                    return None
                await asyncio.sleep(delay)
                continue
            if (response.status_code == 429 or response.status_code >= 500) and attempt < self.max_retries:
                retry_after = response.headers.get("Retry-After", "")
                if retry_after.isascii() and retry_after.isdecimal():
                    delay = min(max(delay, int(retry_after)), 30)
                await asyncio.sleep(delay)
                continue
            return response
        raise AssertionError("Unreachable retry state")


@dataclass
class FakeArt:
    image: bytes
    budget: ArtBudget
    rejection_reason: str | None = None
    calls: list[ArtCall] = field(default_factory=list)

    async def generate(self, prompt: str, *, seed: int, business_id: int) -> ArtResult:
        validate_request(prompt, seed)
        token = await self.budget.reserve()
        call = ArtCall(business_id, operation_id=f"fake-{len(self.calls) + 1}", seed=seed)
        self.calls.append(call)
        if self.rejection_reason:
            call.status = "rejected"
            call.rejection_reason = self.rejection_reason
            await self.budget.release(token)
            raise ArtError("FakeArt rejected generation")
        call.status = "succeeded"
        await self.budget.settle(token)
        return ArtResult(self.image, seed, call.operation_id)
