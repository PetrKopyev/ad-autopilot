"""Прогноз бюджета Директа: API v4 Live (CreateNewForecast → GetForecastList → GetForecast → DeleteForecastReport).

В v5 прогноза нет. Отчёт готовится до минуты и хранится на стороне Яндекса пять часов, у пользователя одновременно
не больше пяти отчётов: шестой даёт ошибку 31, поэтому готовый отчёт удаляется сразу после чтения."""

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from types import TracebackType
from typing import Any, Literal

import httpx

from app.platforms.base import AuthExpired, PlatformError, RateLimited

LIVE_URL = "https://api.direct.yandex.ru/live/v4/json/"
SANDBOX_URL = "https://api-sandbox.direct.yandex.ru/live/v4/json/"
MAX_PHRASES = 100
MAX_REPORTS = 5
# Без Currency цены приходят в у. е.; с ним — в рублях.
CURRENCY = "RUB"
ACCESS_DENIED = 58
REPORTS_LIMIT = 31
# Отчёты, заказанные этим процессом API: освобождая место, сервис удаляет только их, а не отчёты владельца кабинета.
CREATED: set[int] = set()

type ForecastStatus = Literal["Done", "Pending", "Failed"]


class ForecastAccessDenied(PlatformError):
    """Ошибка 58: приложению ещё не одобрен доступ к API Директа."""

    def __init__(self) -> None:
        super().__init__("Прогноз появится, когда Яндекс одобрит доступ к API Директа.")


class ReportsBusy(PlatformError):
    """Ошибка 31, а все пять мест заняты не нашими отчётами."""

    def __init__(self) -> None:
        super().__init__(
            "в Яндекс Директе все пять мест под отчёты прогноза заняты отчётами, которые заказали не мы. Директ "
            "удаляет их сам не позже чем через пять часов — попробуйте позже."
        )


@dataclass(frozen=True)
class PhraseForecast:
    phrase: str
    shows: int
    clicks: int
    first_place_clicks: int
    min_cpc: Decimal
    max_cpc: Decimal


def _invalid() -> PlatformError:
    return PlatformError("Яндекс Директ вернул некорректный прогноз.")


def _count(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise _invalid()
    return value


def _price(value: Any) -> Decimal:
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        raise _invalid()
    try:
        price = Decimal(str(value))
    except InvalidOperation:
        raise _invalid() from None
    if not price.is_finite() or price < 0:
        raise _invalid()
    return price


class DirectForecastClient:
    def __init__(
        self,
        token: str,
        *,
        config: Any = None,
        client: httpx.AsyncClient | None = None,
        timeout: float = 30.0,
    ) -> None:
        if config is None:
            from app.config import settings

            config = settings
        self._url = SANDBOX_URL if config.direct_sandbox else LIVE_URL
        self._token = token
        self._client = client if client is not None else httpx.AsyncClient()
        self._owns_client = client is None
        self._timeout = timeout

    async def __aenter__(self) -> "DirectForecastClient":
        return self

    async def __aexit__(
        self, exc_type: type[BaseException] | None, exc: BaseException | None, traceback: TracebackType | None
    ) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def _call(self, method: str, param: Any) -> Any:
        if not self._token:
            raise AuthExpired()
        # Live 4 принимает OAuth-токен в теле запроса, а не в заголовке.
        body = {"method": method, "param": param, "locale": "ru", "token": self._token}
        try:
            response = await self._client.post(self._url, json=body, timeout=self._timeout, follow_redirects=False)
        except httpx.RequestError:
            raise PlatformError("Не удалось связаться с Яндекс Директом.") from None
        if response.status_code == 401:
            raise AuthExpired()
        if response.status_code == 429:
            raise RateLimited()
        try:
            data = response.json()
        except ValueError:
            raise PlatformError(
                f"Ошибка Яндекс Директа (HTTP {response.status_code}).", status_code=response.status_code
            ) from None
        if not isinstance(data, dict):
            raise _invalid()
        if "error_code" in data:
            code = data["error_code"]
            if code == ACCESS_DENIED:
                raise ForecastAccessDenied()
            if code == 53:
                raise AuthExpired()
            if code in (52, 152):
                raise RateLimited()
            message = ": ".join(str(data[key]) for key in ("error_str", "error_detail") if data.get(key))
            failure = PlatformError(message or f"Ошибка Яндекс Директа ({code}).", status_code=response.status_code)
            failure.direct_code = code
            raise failure
        if response.status_code != 200 or "data" not in data:
            raise PlatformError(
                f"Ошибка Яндекс Директа (HTTP {response.status_code}).", status_code=response.status_code
            )
        return data["data"]

    async def create(self, phrases: list[str], region_ids: list[int]) -> int:
        """Новый отчёт; при занятых пяти местах освобождает место от своих отчётов и пробует ещё раз."""
        param = {"Phrases": list(dict.fromkeys(phrases))[:MAX_PHRASES], "GeoID": region_ids, "Currency": CURRENCY}
        try:
            forecast_id = await self._call("CreateNewForecast", param)
        except PlatformError as error:
            if getattr(error, "direct_code", None) != REPORTS_LIMIT:
                raise
            await self._free_slot()
            forecast_id = await self._call("CreateNewForecast", param)
        forecast_id = _count(forecast_id)
        CREATED.add(forecast_id)
        return forecast_id

    async def _free_slot(self) -> None:
        reports = {id_: status for id_, status in (await self.statuses()).items() if id_ in CREATED}
        finished = [id_ for id_, status in reports.items() if status != "Pending"]
        # Свои ещё готовятся: освобождаем самый старый, его прогноз закажется заново.
        if not (doomed := finished or sorted(reports)[:1]):
            raise ReportsBusy()
        for forecast_id in doomed:
            await self.delete(forecast_id)

    async def statuses(self) -> dict[int, ForecastStatus]:
        items = await self._call("GetForecastList", None)
        if not isinstance(items, list):
            raise _invalid()
        result = {}
        for item in items:
            if not isinstance(item, dict) or item.get("StatusForecast") not in ("Done", "Pending", "Failed"):
                raise _invalid()
            result[_count(item.get("ForecastID"))] = item["StatusForecast"]
        return result

    async def status(self, forecast_id: int) -> ForecastStatus:
        """Отчёта нет в списке — он удалён или истёк: для вызывающего это неудача, отчёт заказывается заново."""
        return (await self.statuses()).get(forecast_id, "Failed")

    async def get(self, forecast_id: int) -> list[PhraseForecast]:
        data = await self._call("GetForecast", forecast_id)
        phrases = data.get("Phrases") if isinstance(data, dict) else None
        if not isinstance(phrases, list):
            raise _invalid()
        result = []
        for item in phrases:
            if not isinstance(item, dict) or not isinstance(item.get("Phrase"), str):
                raise _invalid()
            if item.get("Currency", CURRENCY) != CURRENCY:
                raise _invalid()
            result.append(
                PhraseForecast(
                    item["Phrase"],
                    _count(item.get("Shows")),
                    _count(item.get("Clicks")),
                    _count(item.get("FirstPlaceClicks")),
                    _price(item.get("Min")),
                    _price(item.get("Max")),
                )
            )
        return result

    async def delete(self, forecast_id: int) -> None:
        await self._call("DeleteForecastReport", forecast_id)
        CREATED.discard(forecast_id)
