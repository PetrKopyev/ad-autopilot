import asyncio
import base64
import binascii
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Protocol
from urllib.parse import quote

import httpx

ART_COST_KOP = 223
GENERATE_URL = "https://ai.api.cloud.yandex.net/foundationModels/v1/imageGenerationAsync"
OPERATIONS_URL = "https://operation.api.cloud.yandex.net/operations"


class ArtError(Exception):
    pass


class ArtBudgetExceeded(ArtError):
    pass


@dataclass
class ArtBudget:
    limit_kop: int
    spent_kop: int = 0
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock, init=False, repr=False)

    def __post_init__(self) -> None:
        if any(type(value) is not int or value < 0 for value in (self.limit_kop, self.spent_kop)):
            raise ValueError("Budget must use non-negative integer kopecks")
        if self.spent_kop > self.limit_kop:
            raise ValueError("Spent budget exceeds limit")

    async def reserve(self) -> None:
        async with self._lock:
            if self.spent_kop + ART_COST_KOP > self.limit_kop:
                raise ArtBudgetExceeded("YandexART budget exhausted")
            self.spent_kop += ART_COST_KOP


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
    operation_id: str
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
    """record_call must durably upsert the call, assigning id on initial reservation.

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
        poll_interval: float = 1,
        max_polls: int = 120,
        connect_retries: int = 1,
    ) -> None:
        if not api_key or not folder_id:
            raise ValueError("YC_API_KEY and YC_FOLDER_ID are required")
        if poll_interval < 0 or max_polls < 1 or not 0 <= connect_retries <= 3:
            raise ValueError("Invalid polling or retry limits")
        self.client = client
        self._headers = {"Authorization": f"Api-Key {api_key}"}
        self.model_uri = f"art://{folder_id}/yandex-art/latest"
        self.budget = budget
        self.record_call = record_call
        self.poll_interval = poll_interval
        self.max_polls = max_polls
        self.connect_retries = connect_retries

    async def generate(self, prompt: str, *, seed: int, business_id: int) -> ArtResult:
        validate_request(prompt, seed)
        await self.budget.reserve()
        call = ArtCall(business_id, seed=seed)
        try:
            # Persist the reservation before a non-idempotent paid request.
            await self.record_call(call)
            payload = {
                "modelUri": self.model_uri,
                "messages": [{"text": prompt, "weight": "1"}],
                "generationOptions": {"mimeType": "image/jpeg", "seed": str(seed)},
            }
            for attempt in range(self.connect_retries + 1):
                try:
                    response = await self.client.post(
                        GENERATE_URL, headers=self._headers, json=payload, timeout=30,
                    )
                    break
                except (httpx.ConnectError, httpx.ConnectTimeout):
                    if attempt == self.connect_retries:
                        raise
            response.raise_for_status()
            operation = response.json()
            if not isinstance(operation, dict):
                raise ValueError("Invalid operation")
            call.operation_id = operation["id"]
            if not isinstance(call.operation_id, str) or not call.operation_id:
                raise ValueError("Missing operation ID")
            await self.record_call(call)
            for poll in range(self.max_polls + 1):
                if operation.get("error"):
                    call.status = "rejected"
                    error = operation["error"]
                    code = error.get("code", "unknown") if isinstance(error, dict) else "unknown"
                    call.rejection_reason = f"operation_error:{code}"
                    raise ArtError("YandexART rejected generation")
                if operation.get("done"):
                    image = base64.b64decode(operation["response"]["image"], validate=True)
                    if not image:
                        raise ValueError("Empty generated image")
                    call.status = "succeeded"
                    return ArtResult(image, seed, call.operation_id)
                if poll == self.max_polls:
                    break
                await asyncio.sleep(self.poll_interval)
                response = await self.client.get(
                    f"{OPERATIONS_URL}/{quote(call.operation_id, safe='')}",
                    headers=self._headers,
                    timeout=30,
                )
                response.raise_for_status()
                operation = response.json()
                if not isinstance(operation, dict):
                    raise ValueError("Invalid operation")
            raise ArtError("YandexART polling limit reached; operation may still be running")
        except asyncio.CancelledError:
            call.status = "unknown"
            call.rejection_reason = "cancelled"
            raise
        except (httpx.HTTPError, ValueError, KeyError, TypeError, binascii.Error) as exc:
            call.status = "unknown"
            call.rejection_reason = type(exc).__name__
            raise ArtError("YandexART request failed; paid requests are not replayed") from None
        except ArtError:
            if call.status == "pending":
                call.status = "unknown"
                call.rejection_reason = "polling_limit"
            raise
        finally:
            await self.record_call(call)


@dataclass
class FakeArt:
    image: bytes
    budget: ArtBudget
    rejection_reason: str | None = None
    calls: list[ArtCall] = field(default_factory=list)

    async def generate(self, prompt: str, *, seed: int, business_id: int) -> ArtResult:
        validate_request(prompt, seed)
        await self.budget.reserve()
        call = ArtCall(business_id, operation_id=f"fake-{len(self.calls) + 1}", seed=seed)
        self.calls.append(call)
        if self.rejection_reason:
            call.status = "rejected"
            call.rejection_reason = self.rejection_reason
            raise ArtError("FakeArt rejected generation")
        call.status = "succeeded"
        return ArtResult(self.image, seed, call.operation_id)
