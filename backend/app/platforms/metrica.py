from dataclasses import dataclass, field
from math import isfinite
from types import TracebackType
from typing import Protocol

import httpx

BASE_URL = "https://api-metrika.yandex.net"
PAGE_SIZE = 1000


@dataclass(frozen=True)
class MetricaCounter:
    id: int
    name: str
    site: str


@dataclass(frozen=True)
class MetricaGoal:
    id: int
    name: str
    type: str


class MetricaError(Exception):
    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


class MetricaAuthError(MetricaError):
    def __init__(self) -> None:
        super().__init__("Авторизация в Яндекс Метрике недействительна. Подключите аккаунт заново.", status_code=401)


class MetricaAccessError(MetricaError):
    def __init__(self) -> None:
        super().__init__(
            "Нет доступа к данным Яндекс Метрики. Проверьте аккаунт, доступ к счётчику и разрешение metrika:read.",
            status_code=403,
        )


class MetricaResponseError(MetricaError):
    def __init__(self) -> None:
        super().__init__("Яндекс Метрика вернула некорректные данные. Повторите попытку позже.")


class Metrica(Protocol):
    async def list_counters(self) -> list[MetricaCounter]: ...

    async def list_goals(self, counter_id: int) -> list[MetricaGoal]: ...

    async def get_visits(self, counter_id: int) -> int: ...

    async def is_alive(self, counter_id: int) -> bool: ...


def _check_counter_id(counter_id: int) -> None:
    if type(counter_id) is not int or counter_id <= 0:
        raise ValueError("Идентификатор счётчика должен быть положительным целым числом.")


def _items(data: dict, key: str) -> list[dict]:
    items = data.get(key)
    if not isinstance(items, list) or any(not isinstance(item, dict) for item in items):
        raise MetricaResponseError()
    for item in items:
        if type(item.get("id")) is not int or item["id"] <= 0 or not isinstance(item.get("name"), str):
            raise MetricaResponseError()
    return items


class MetricaClient:
    def __init__(self, token: str, *, client: httpx.AsyncClient | None = None, timeout: float = 10.0) -> None:
        if not token.strip():
            raise ValueError("Для подключения Яндекс Метрики нужен OAuth-токен.")
        self._token = token
        self._client = client if client is not None else httpx.AsyncClient()
        self._owns_client = client is None
        self._timeout = timeout

    async def __aenter__(self) -> "MetricaClient":
        return self

    async def __aexit__(
        self, exc_type: type[BaseException] | None, exc: BaseException | None, traceback: TracebackType | None
    ) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def _get(self, path: str, *, params: dict[str, str | int] | None = None) -> dict:
        try:
            response = await self._client.get(
                f"{BASE_URL}{path}",
                params=params,
                headers={"Authorization": f"OAuth {self._token}", "Accept": "application/json"},
                timeout=self._timeout,
                follow_redirects=False,
            )
        except httpx.RequestError:
            raise MetricaError("Не удалось связаться с Яндекс Метрикой. Повторите попытку позже.") from None
        if response.status_code == 401:
            raise MetricaAuthError()
        if response.status_code == 403:
            raise MetricaAccessError()
        if not response.is_success:
            raise MetricaError(
                f"Ошибка Яндекс Метрики (HTTP {response.status_code}). Повторите попытку позже.",
                status_code=response.status_code,
            )
        try:
            data = response.json()
        except ValueError:
            raise MetricaResponseError() from None
        if not isinstance(data, dict):
            raise MetricaResponseError()
        return data

    async def list_counters(self) -> list[MetricaCounter]:
        counters = []
        offset = 1
        while True:
            data = await self._get("/management/v1/counters", params={"offset": offset, "per_page": PAGE_SIZE})
            items = _items(data, "counters")
            rows = data.get("rows")
            if type(rows) is not int or rows < 0:
                raise MetricaResponseError()
            for item in items:
                site = item.get("site2", {}).get("site") if isinstance(item.get("site2"), dict) else item.get("site")
                if not isinstance(site, str):
                    raise MetricaResponseError()
                counters.append(MetricaCounter(item["id"], item["name"], site))
            offset += len(items)
            if offset > rows:
                return counters
            if not items:
                raise MetricaResponseError()

    async def list_goals(self, counter_id: int) -> list[MetricaGoal]:
        _check_counter_id(counter_id)
        data = await self._get(f"/management/v1/counter/{counter_id}/goals")
        goals = []
        for item in _items(data, "goals"):
            if not isinstance(item.get("type"), str):
                raise MetricaResponseError()
            goals.append(MetricaGoal(item["id"], item["name"], item["type"]))
        return goals

    async def get_visits(self, counter_id: int) -> int:
        """Визиты за семь календарных дней, включая сегодня, в часовом поясе счётчика."""
        _check_counter_id(counter_id)
        data = await self._get(
            "/stat/v1/data",
            params={"ids": counter_id, "metrics": "ym:s:visits", "date1": "6daysAgo", "date2": "today"},
        )
        totals = data.get("totals")
        if not isinstance(totals, list) or len(totals) != 1:
            raise MetricaResponseError()
        visits = totals[0]
        if type(visits) not in (int, float) or visits < 0:
            raise MetricaResponseError()
        if isinstance(visits, float) and (not isfinite(visits) or not visits.is_integer()):
            raise MetricaResponseError()
        return int(visits)

    async def is_alive(self, counter_id: int) -> bool:
        return await self.get_visits(counter_id) > 0


@dataclass
class FakeMetrica:
    counters: list[MetricaCounter] = field(default_factory=list)
    goals: dict[int, list[MetricaGoal]] = field(default_factory=dict)
    visits: dict[int, int] = field(default_factory=dict)

    def _check_access(self, counter_id: int) -> None:
        _check_counter_id(counter_id)
        if not any(counter.id == counter_id for counter in self.counters):
            raise MetricaAccessError()

    async def list_counters(self) -> list[MetricaCounter]:
        return list(self.counters)

    async def list_goals(self, counter_id: int) -> list[MetricaGoal]:
        self._check_access(counter_id)
        return list(self.goals.get(counter_id, []))

    async def get_visits(self, counter_id: int) -> int:
        self._check_access(counter_id)
        return self.visits.get(counter_id, 0)

    async def is_alive(self, counter_id: int) -> bool:
        return await self.get_visits(counter_id) > 0
