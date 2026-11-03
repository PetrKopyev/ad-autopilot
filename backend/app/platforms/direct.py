import asyncio
import base64
import csv
import io
import json
from collections.abc import Collection, Mapping
from copy import deepcopy
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from hashlib import sha256
from pathlib import Path
from types import TracebackType
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from zoneinfo import ZoneInfo

import httpx

from app.platforms.base import (
    AdGroupSpec,
    AuthExpired,
    AuthSession,
    AwaitingModeration,
    CampaignTreeSpec,
    EntityState,
    ExternalRefs,
    ModerationInfo,
    ModerationRejected,
    Operation,
    PlatformError,
    PlatformLimits,
    RateLimited,
    StatLevel,
    StatRow,
    _money,
)
from app.platforms.direct_forecast import DirectForecastClient
from app.platforms.regions import CITY_IDS, Region, RegionCache, find_region, normalize, not_found

MICROS_PER_KOP = 10_000
# Цели в отчёте и ключевые цели стратегии: не больше десяти.
MAX_GOALS = 10
AUTH_URL = "https://oauth.yandex.ru/authorize"
TOKEN_URL = "https://oauth.yandex.ru/token"
STRATEGIES = {"WB_MAXIMUM_CLICKS": "WbMaximumClicks", "WB_MAXIMUM_CONVERSION_RATE": "WbMaximumConversionRate"}
# Служебная фраза-автотаргетинга единых кампаний: Директ создаёт её сам, API не даёт ни остановить, ни удалить.
AUTOTARGETING_KEYWORD = "---autotargeting"
MARKING_UNSUPPORTED_CODES = {54, 55, 8000}
MARKING_FIELDS = {
    "FieldNames": ["ClientId", "Phone"],
    "TinInfoFieldNames": ["TinType", "Tin"],
    "OrganizationFieldNames": ["Name", "Kpp", "EpayNumber", "RegNumber", "OksmNumber", "OkvedCode"],
    "ContractFieldNames": ["Number", "Date", "Price", "Type", "ActionType", "SubjectType", "IsAgencyPayment"],
    "ContragentFieldNames": ["Name", "Kpp", "Phone", "EpayNumber", "RegNumber", "OksmNumber"],
    "ContragentTinInfoFieldNames": ["TinType", "Tin"],
}

RUSSIA = 225
GEO_REGIONS = RegionCache()


def _invalid() -> PlatformError:
    return PlatformError("Яндекс Директ вернул некорректные данные.")


def _integer(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        raise ValueError("Ожидается целое число.")
    return int(value)


def _kop(value: Any) -> int:
    micros = _integer(value)
    if micros < 0:
        raise ValueError("Сумма не может быть отрицательной.")
    return (micros + MICROS_PER_KOP // 2) // MICROS_PER_KOP


def _named(name: str, key: str) -> str:
    return f"{name[: 254 - len(key)]} {key}"


def _status(item: dict, *, campaign: bool = False) -> str:
    # Живой Direct у сущности на проверке шлёт Status=MODERATION/DRAFT при State=OFF: маппим по Status
    # первым, иначе ожидание модерации неотличимо от паузы (changes.py держит изменение pending по этим
    # статусам). Status не запрашивается у keywords — там прежняя логика по State.
    # У кампаний по доке различаются OFF (показы не идут, в том числе без денег на счёте) и SUSPENDED
    # (остановлена владельцем): OFF маппим в паузу площадки, иначе «Продолжить» падало бы на пустом счёте.
    status = item.get("Status")
    if status == "DRAFT":
        return "draft"
    if status == "MODERATION":
        return "moderation"
    if status == "REJECTED":
        return "rejected"
    state = item.get("State")
    if state == "ARCHIVED":
        return "archived"
    if state == "OFF":
        return "paused_auto" if campaign else "paused_owner"
    if state in {"SUSPENDED", "ENDED"}:
        return "paused_owner"
    if state == "ON":
        return "active"
    return "pending"


def budget_end(today: date) -> date:
    """Последний день недельного лимита: воскресенье или конец месяца, что раньше."""
    next_month = (today.replace(day=28) + timedelta(days=4)).replace(day=1)
    return min(today + timedelta(days=6 - today.weekday()), next_month - timedelta(days=1))


def weekly_limit(daily_kop: int, period_kop: int, today: date) -> int:
    """WeeklySpendLimit новой кампании: дневной лимит на оставшиеся дни недели (месяца), не больше кэпа.

    Ту же сумму проверяет запуск против MinimumWeeklySpendLimit: в воскресенье она равна одному дневному лимиту."""
    return min(daily_kop * ((budget_end(today) - today).days + 1), period_kop)


class DirectAdapter:
    def __init__(
        self,
        token: str = "",
        *,
        config: Any = None,
        client: httpx.AsyncClient | None = None,
        client_login: str | None = None,
        counter_ids: tuple[int, ...] = (),
        priority_goals: Mapping[int, int] | None = None,
        goal_ids: tuple[int, ...] = (),
        strategy: str = "WB_MAXIMUM_CLICKS",
        text_campaign: bool = False,
        href: str = "",
        region_ids: tuple[int, ...] = (),
        marking_confirmed: bool = False,
        timeout: float = 30.0,
        report_attempts: int = 20,
    ) -> None:
        if config is None:
            from app.config import settings

            config = settings
        if strategy not in STRATEGIES:
            raise ValueError("Неизвестная стратегия Директа.")
        if any(type(id_) is not int or id_ <= 0 for id_ in counter_ids):
            raise ValueError("CounterIds должны быть положительными целыми числами.")
        goals = dict(priority_goals or {})
        for goal, value in goals.items():
            _money(value, "priority_goal_value_kop")
        # Цели обращений бизнеса: по ним отчёт отдаёт конверсии (Goals), даже без ключевых целей стратегии.
        report_goals = tuple(dict.fromkeys([*goals, *goal_ids]))
        if any(type(goal) is not int or goal <= 0 for goal in report_goals):
            raise ValueError("GoalId должен быть положительным целым числом.")
        if len(report_goals) > MAX_GOALS or report_attempts < 1:
            raise ValueError("Допускается до десяти целей; число попыток отчёта должно быть положительным.")
        if strategy == "WB_MAXIMUM_CONVERSION_RATE" and not goals:
            raise ValueError("Для оптимизации конверсий нужны PriorityGoals.")
        self._config = config
        self._end_dates: dict[str, str] = {}
        self._base_url = config.direct_api_url.rstrip("/")
        self._v501_url = config.direct_api_v501_url.rstrip("/")
        self._sandbox = config.direct_sandbox
        self._vat_percent = 0 if config.direct_amounts_include_vat else config.vat_percent
        if self._sandbox:
            self._base_url = self._sandbox_url(self._base_url)
            self._v501_url = self._sandbox_url(self._v501_url)
        self._token = token
        self._login = client_login
        self._counter_ids = counter_ids
        self._goals = goals
        self._report_goals = report_goals
        self._strategy = strategy
        self._text_campaign = text_campaign
        self._href = href
        self._region_ids = list(region_ids)
        self._marking_confirmed = marking_confirmed
        self._timeout = timeout
        self._report_attempts = report_attempts
        self._client = client if client is not None else httpx.AsyncClient()
        self._owns_client = client is None
        self._semaphore = asyncio.Semaphore(5)
        self._create_lock = asyncio.Lock()
        self._limits = PlatformLimits()
        self._minimums: dict[str, int] | None = None
        self._trees: dict[str, ExternalRefs] = {}
        self._tz = ZoneInfo(getattr(config, "tz_default", "Europe/Moscow"))
        # Содержимое кабинета, отличное от плана и пересозданное при запуске: тексты для журнала запуска.
        self._content_mismatches: list[str] = []
        # Остановки содержимого кабинета, которого нет в плане: операции для записи запуска (откат вернёт).
        self._foreign_stops: list[Operation] = []

    @property
    def content_mismatches(self) -> list[str]:
        """Что в кабинете отличалось от плана (объявления и фразы пересозданы): читает запуск для журнала."""
        return list(self._content_mismatches)

    @property
    def foreign_stops(self) -> list[Operation]:
        """Остановки объявлений и фраз, не привязанных к плану: читает запуск для откатываемой записи журнала."""
        return list(self._foreign_stops)

    @staticmethod
    def _sandbox_url(url: str) -> str:
        parts = urlsplit(url)
        return urlunsplit((parts.scheme, "api-sandbox.direct.yandex.com", parts.path, "", ""))

    def _today(self) -> date:
        return datetime.now(self._tz).date()

    async def __aenter__(self) -> "DirectAdapter":
        return self

    async def __aexit__(
        self, exc_type: type[BaseException] | None, exc: BaseException | None, traceback: TracebackType | None
    ) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def _post(self, service: str, body: dict, *, headers: dict | None = None) -> httpx.Response:
        if not self._token:
            raise AuthExpired()
        request_headers = {"Authorization": f"Bearer {self._token}", "Accept-Language": "ru"}
        if self._login:
            request_headers["Client-Login"] = self._login
        request_headers.update(headers or {})
        base_url = (
            self._v501_url
            if not self._text_campaign and service in {"campaigns", "adgroups", "ads", "keywords"}
            else self._base_url
        )
        try:
            async with self._semaphore:
                response = await self._client.post(
                    f"{base_url}/{service}",
                    json=body,
                    headers=request_headers,
                    timeout=self._timeout,
                    follow_redirects=False,
                )
                if units := response.headers.get("Units"):
                    try:
                        used, remaining, total = map(int, units.split("/"))
                        if min(used, remaining, total) < 0:
                            raise ValueError
                        self._limits = PlatformLimits(remaining_units=remaining)
                    except ValueError:
                        pass
        except httpx.RequestError:
            raise PlatformError("Не удалось связаться с Яндекс Директом.") from None
        if response.status_code == 401:
            raise AuthExpired()
        if response.status_code == 429:
            raise RateLimited(self._retry_after(response))
        return response

    @staticmethod
    def _retry_after(response: httpx.Response) -> int:
        try:
            return max(1, int(response.headers.get("retryIn", response.headers.get("Retry-After", "60"))))
        except ValueError:
            return 60

    @staticmethod
    def _raise_api(error: dict, status: int) -> None:
        code = error.get("error_code", error.get("Code"))
        if str(code) == "53":
            raise AuthExpired()
        if str(code) in {"152", "506", "56"}:
            raise RateLimited()
        message = ": ".join(
            str(error[key]) for key in ("error_string", "error_detail", "Message", "Details") if error.get(key)
        )
        failure = PlatformError(message or f"Ошибка Яндекс Директа ({code}).", status_code=status)
        failure.direct_code = code
        raise failure

    def _json(self, response: httpx.Response) -> dict:
        try:
            data = response.json()
        except ValueError:
            if response.status_code != 200:
                raise PlatformError(
                    f"Ошибка Яндекс Директа (HTTP {response.status_code}).", status_code=response.status_code
                ) from None
            raise _invalid() from None
        if not isinstance(data, dict):
            raise _invalid()
        if isinstance(data.get("error"), dict):
            self._raise_api(data["error"], response.status_code)
        if response.status_code != 200:
            raise PlatformError(
                f"Ошибка Яндекс Директа (HTTP {response.status_code}).", status_code=response.status_code
            )
        return data

    async def _api(self, service: str, method: str, params: dict) -> dict:
        data = self._json(await self._post(service, {"method": method, "params": params}))
        result = data.get("result")
        if not isinstance(result, dict):
            raise _invalid()
        if method != "get":
            results = result.get(method.capitalize() + "Results")
            if not isinstance(results, list) or not results:
                raise _invalid()
        for name, items in result.items():
            if name.endswith("Results") and isinstance(items, list):
                for item in items:
                    if not isinstance(item, dict):
                        raise _invalid()
                    errors = item.get("Errors", [])
                    if not isinstance(errors, list) or any(not isinstance(error, dict) for error in errors):
                        raise _invalid()
                    for error in errors:
                        self._raise_api(error, 200)
        return result

    async def _get(self, service: str, key: str, params: dict) -> list[dict]:
        items = []
        offset = 0
        while True:
            data = await self._api(service, "get", {**params, "Page": {"Limit": 1000, "Offset": offset}})
            # Живой API опускает пустую коллекцию в ответе: нет ключа — пустой список,
            # а вот ключ с несписком под ним — битый ответ.
            batch = data.get(key, [])
            if not isinstance(batch, list) or any(not isinstance(item, dict) for item in batch):
                raise _invalid()
            if any(type(item.get("Id")) is not int or item["Id"] <= 0 for item in batch):
                raise _invalid()
            items.extend(batch)
            if "LimitedBy" not in data:
                return items
            next_offset = data["LimitedBy"]
            if type(next_offset) is not int or next_offset <= offset or not batch:
                raise _invalid()
            offset = next_offset

    async def _add(self, service: str, key: str, item: dict, result_key: str = "Id") -> str:
        data = await self._api(service, "add", {key: [item]})
        results = data.get("AddResults")
        if not isinstance(results, list) or len(results) != 1 or not results[0].get(result_key):
            raise _invalid()
        value = results[0][result_key]
        if result_key == "Id" and (type(value) is not int or value <= 0):
            raise _invalid()
        if result_key != "Id" and not isinstance(value, str):
            raise _invalid()
        return str(results[0][result_key])

    def auth_url(self, state: str) -> str:
        return (
            AUTH_URL
            + "?"
            + urlencode(
                {
                    "response_type": "code",
                    "client_id": self._config.yandex_client_id,
                    "redirect_uri": self._redirect_uri(),
                    "state": state,
                    "scope": "direct:api",
                }
            )
        )

    def _redirect_uri(self) -> str:
        return f"{self._config.app_base_url.rstrip('/')}/api/auth/yandex/callback"

    async def _oauth(self, params: dict, account_id: str = "") -> AuthSession:
        secret = self._config.yandex_client_secret
        try:
            async with self._semaphore:
                response = await self._client.post(
                    TOKEN_URL,
                    data={
                        **params,
                        "client_id": self._config.yandex_client_id,
                        "client_secret": secret.get_secret_value() if hasattr(secret, "get_secret_value") else secret,
                    },
                    timeout=self._timeout,
                    follow_redirects=False,
                )
        except httpx.RequestError:
            raise PlatformError("Не удалось связаться с OAuth Яндекса.") from None
        try:
            data = response.json()
            if response.status_code in {400, 401} or "error" in data:
                raise AuthExpired()
            if response.status_code != 200:
                raise PlatformError("Ошибка OAuth Яндекса.", status_code=response.status_code)
            token = data["access_token"]
            if not isinstance(token, str) or not token:
                raise ValueError
            expires_at = (
                datetime.now(UTC) + timedelta(seconds=_integer(data["expires_in"])) if "expires_in" in data else None
            )
        except (ValueError, KeyError, TypeError):
            raise _invalid() from None
        self._token = token
        if not account_id:
            account_id = (await self.whoami())["id"]
        return AuthSession(account_id, token, data.get("refresh_token", params.get("refresh_token")), expires_at)

    async def exchange_code(self, code: str) -> AuthSession:
        return await self._oauth(
            {"grant_type": "authorization_code", "code": code, "redirect_uri": self._redirect_uri()}
        )

    async def refresh(self, account: AuthSession) -> AuthSession:
        if not account.refresh_token:
            raise AuthExpired()
        return await self._oauth(
            {"grant_type": "refresh_token", "refresh_token": account.refresh_token}, account.account_id
        )

    async def whoami(self) -> dict[str, str]:
        # Type просим для понятной ошибки про агентские кабинеты: живой API у самостоятельного клиента
        # отвечает "CLIENT" (или не отдаёт поле вовсе) — тогда проверка прозрачна.
        data = await self._api(
            "clients", "get", {"FieldNames": ["ClientId", "Login", "ClientInfo", "Currency", "Type"]}
        )
        try:
            item = data["Clients"][0]
            if str(item.get("Type", "CLIENT")).upper() != "CLIENT":
                raise PlatformError(
                    "Агентские кабинеты не поддерживаются: подключите рекламный кабинет рекламодателя, "
                    "не управляемый через агентство (самостоятельный или прямой договор с площадкой)."
                )
            id_ = item["ClientId"]
            name = item.get("ClientInfo") or item["Login"]
            if type(id_) is not int or id_ <= 0 or not isinstance(name, str) or not name:
                raise ValueError
            return {"id": str(id_), "name": name}
        except PlatformError:
            raise
        except (KeyError, IndexError, TypeError, ValueError):
            raise _invalid() from None

    def _to_api(self, kop: int) -> int:
        """Сумма с НДС (как бюджет и расход в отчёте) в микроединицы API; без НДС — вниз, чтобы не превысить бюджет."""
        return kop * 100 // (100 + self._vat_percent) * MICROS_PER_KOP

    def _from_api(self, micros: Any, *, round_up: bool = False) -> int:
        """Сумма API в копейки с НДС: в тех же единицах, что бюджет и расход."""
        net = _integer(micros)
        if net < 0:
            raise ValueError("Сумма не может быть отрицательной.")
        gross = net * (100 + self._vat_percent)
        step = 100 * MICROS_PER_KOP
        return (gross + (step - 1 if round_up else step // 2)) // step

    def _weekly_limit(self, daily_kop: int, period_kop: int) -> int:
        return weekly_limit(daily_kop, period_kop, self._today())

    def _budget_end(self) -> date:
        return budget_end(self._today())

    def _campaign_settings(self, weekly_kop: int) -> dict:
        strategy = {"WeeklySpendLimit": self._to_api(weekly_kop)}
        if self._strategy == "WB_MAXIMUM_CONVERSION_RATE":
            strategy["GoalId"] = next(iter(self._goals)) if len(self._goals) == 1 else 13
        settings = {
            "BiddingStrategy": {
                "Search": {"BiddingStrategyType": self._strategy, STRATEGIES[self._strategy]: strategy},
                "Network": {"BiddingStrategyType": "NETWORK_DEFAULT"},
            },
            "CounterIds": {"Items": list(self._counter_ids)},
        }
        if self._text_campaign:
            settings["BiddingStrategy"]["Network"]["NetworkDefault"] = {}
        # Ключевые цели нужны только стратегии по конверсиям; при оплате за клики они меняли бы оптимизацию,
        # а для отчёта по обращениям хватает goal_ids.
        if self._strategy == "WB_MAXIMUM_CONVERSION_RATE":
            settings["PriorityGoals"] = {
                "Items": [{"GoalId": goal, "Value": value * MICROS_PER_KOP} for goal, value in self._goals.items()]
            }
        return settings

    def _targeting(self, targeting: dict) -> dict:
        if set(targeting) - {"region_ids", "negative_keywords"}:
            raise PlatformError(
                "Директ принимает region_ids и negative_keywords; остальные настройки требуют преобразования."
            )
        regions = targeting.get("region_ids", self._region_ids)
        if not isinstance(regions, (list, tuple)) or not regions or any(type(id_) is not int for id_ in regions):
            raise ValueError("Нужен непустой список RegionIds.")
        negatives = targeting.get("negative_keywords", [])
        if not isinstance(negatives, (list, tuple)) or any(not isinstance(value, str) for value in negatives):
            raise ValueError("Минус-фразы должны быть строками.")
        return {"RegionIds": list(regions), "NegativeKeywords": {"Items": list(negatives)}}

    def _ad_fields(self) -> dict:
        fields = {"FieldNames": ["Id", "CampaignId", "AdGroupId", "State", "Status", "StatusClarification", "Type"]}
        if self._text_campaign:
            fields["TextAdFieldNames"] = ["Title", "Title2", "Text", "Href", "AdImageHash", "AdImageModeration"]
        else:
            fields["ResponsiveAdFieldNames"] = ["Titles", "Texts", "Href", "AdImages"]
        return fields

    async def _ads(self, selection: dict) -> list[dict]:
        return await self._get("ads", "Ads", {"SelectionCriteria": selection, **self._ad_fields()})

    async def _image_ref(self, ref: str | None) -> str | None:
        if ref and ("/" in ref or Path(ref).suffix.lower() in {".jpg", ".jpeg", ".png", ".gif"}):
            return await self.upload_image(Path(ref))
        return ref

    async def create_campaign_tree(self, plan_part: CampaignTreeSpec) -> ExternalRefs:
        async with self._create_lock:
            # Несовпадения отчитываются за один проход создания: повторный вызов читает только свежие.
            self._content_mismatches = []
            self._foreign_stops = []
            return await self._create_tree(plan_part)

    async def _create_tree(self, plan: CampaignTreeSpec) -> ExternalRefs:
        key = plan.idempotency_key
        if key in self._trees:
            return deepcopy(self._trees[key])
        if not self._counter_ids:
            raise PlatformError("Для создания кампании обязательны CounterIds Метрики.")
        if not self._href:
            raise PlatformError("Для объявлений нужна ссылка href.")
        for group in plan.groups:
            self._targeting(group.targeting)
            if any(target.kind != "phrase" for target in group.targets):
                raise PlatformError("Поддерживаются ключевые фразы; минус-фразы задаются в negative_keywords группы.")
        campaigns = await self._get("campaigns", "Campaigns", {"SelectionCriteria": {}, "FieldNames": ["Id", "Name"]})
        # Campaigns.get не поддерживает фильтр по имени: отбираем суффикс после постраничного чтения.
        found = [item for item in campaigns if item.get("Name", "").endswith(f" {key}")]
        if len(found) > 1:
            raise PlatformError("В Директе несколько кампаний с одним ключом идемпотентности.")
        campaign_kind = "TextCampaign" if self._text_campaign else "UnifiedCampaign"
        if found:
            campaign_id = str(found[0]["Id"])
            # Повтор переиспользует кампанию неудачной попытки: лимиты между попытками пересчитались —
            # переносим на площадку сразу, иначе кабинет и база расходятся до первого rebalance.
            weekly = self._weekly_limit(plan.daily_limit_kop, plan.period_cap_kop)
            if weekly:
                await self._api(
                    "campaigns",
                    "update",
                    {"Campaigns": [{"Id": int(campaign_id), campaign_kind: self._campaign_settings(weekly)}]},
                )
        else:
            weekly = self._weekly_limit(plan.daily_limit_kop, plan.period_cap_kop)
            if not weekly:
                raise PlatformError(
                    "Недельный лимит должен быть положительным; нулевой бюджет задаётся остановкой кампании."
                )
            campaign_id = await self._add(
                "campaigns",
                "Campaigns",
                {
                    "Name": _named(plan.name, key),
                    "StartDate": self._today().isoformat(),
                    "EndDate": self._budget_end().isoformat(),
                    campaign_kind: self._campaign_settings(weekly),
                },
            )
            # Единые кампании (API 501) создаются черновиком, а suspend для черновика Direct отклоняет;
            # по живому API у черновика State="OFF", Status="DRAFT", поэтому проверяем оба поля;
            # черновик сам не показывает объявления — для «создать остановленной» пауза не нужна.
            created = await self._get(
                "campaigns",
                "Campaigns",
                {"SelectionCriteria": {"Ids": [int(campaign_id)]}, "FieldNames": ["Id", "State", "Status"]},
            )
            if (
                created
                and created[0].get("State") != "DRAFT"
                and created[0].get("Status") != "DRAFT"
            ):
                await self._api("campaigns", "suspend", {"SelectionCriteria": {"Ids": [int(campaign_id)]}})
        refs = ExternalRefs(
            campaign_id,
            {},
            {},
            {},
            {
                "daily_limit_kop": plan.daily_limit_kop,
                "period_cap_kop": plan.period_cap_kop,
                "weekly_limit_kop": self._weekly_limit(plan.daily_limit_kop, plan.period_cap_kop),
            },
        )
        groups = await self._get(
            "adgroups",
            "AdGroups",
            {
                "SelectionCriteria": {"CampaignIds": [int(campaign_id)]},
                "FieldNames": ["Id", "Name"],
            },
        )
        for group in plan.groups:
            name = _named(group.name, f"ap-{group.id}")
            matches = [item for item in groups if item.get("Name", "").endswith(f" ap-{group.id}")]
            if len(matches) > 1:
                raise PlatformError("Неоднозначный ключ группы в Директе.")
            group_id = (
                str(matches[0]["Id"])
                if matches
                else await self._add(
                    "adgroups",
                    "AdGroups",
                    {
                        "Name": name,
                        "CampaignId": int(campaign_id),
                        **self._targeting(group.targeting),
                        **({} if self._text_campaign else {"UnifiedAdGroup": {"OfferRetargeting": "NO"}}),
                    },
                )
            )
            refs.groups[group.id] = group_id
            ads = await self._ads({"AdGroupIds": [int(group_id)]})
            complete_ads = len(ads) == len(group.ads)
            for ad in group.ads:
                image = await self._image_ref(ad.image_ref)
                if self._text_campaign:
                    ad_kind = "TextAd"
                    content = {"Title": ad.title, "Text": ad.text, "Href": self._href, "Mobile": "NO"}
                    if ad.title2:
                        content["Title2"] = ad.title2
                    if image:
                        content["AdImageHash"] = image
                else:
                    ad_kind = "ResponsiveAd"
                    content = {
                        "Titles": [ad.title] + ([ad.title2] if ad.title2 else []),
                        "Texts": [ad.text],
                        "Href": self._href,
                    }
                    if image:
                        content["AdImageHashes"] = [image]
                match = next((item for item in ads if self._same_ad(item.get(ad_kind, {}), content)), None)
                if match is None and complete_ads and ads:
                    # Число объявлений совпало с планом, но содержимое — нет: в кабинете объявление правили
                    # или план пересобрал тексты под тем же ключом. Чужое не привязываем — создаём плановое
                    # заново, а запуск честно пишет об этом в журнал (content_mismatches).
                    self._content_mismatches.append(
                        f"В кабинете объявление «{ad.title}» группы «{group.name}» отличается от плана — "
                        "создали заново."
                    )
                if match:
                    ads.remove(match)
                    ad_id = str(match["Id"])
                else:
                    ad_id = await self._add("ads", "Ads", {"AdGroupId": int(group_id), ad_kind: content})
                    if ad.status != "active" or group.status != "active":
                        await self._api("ads", "suspend", {"SelectionCriteria": {"Ids": [int(ad_id)]}})
                refs.ads[ad.id] = ad_id
            for item in ads:
                # Остатки не из плана (владелец правил или добавил объявление): в refs не попадают, и тратить
                # бюджет вне статистики и контроля автопилота им нельзя — убираем в архив. Операция уходит в
                # журнал запуска: откат вернёт объявлению прежний статус.
                before = _status(item)
                if before not in {"active", "paused_owner", "paused_auto"}:
                    # Черновики, объявления на модерации и отклонённые бюджет не тратят; архивировать их
                    # Директ не даст, а откат умеет восстанавливать только рабочие статусы.
                    continue
                if before == "active":
                    # Директ не архивирует работающее объявление: сначала пауза, потом архив.
                    await self._api("ads", "suspend", {"SelectionCriteria": {"Ids": [item["Id"]]}})
                await self._api("ads", "archive", {"SelectionCriteria": {"Ids": [item["Id"]]}})
                self._foreign_stops.append(
                    Operation("set_status", "ad", str(item["Id"]), "status", before, "archived")
                )
            keywords = await self._get(
                "keywords",
                "Keywords",
                {
                    "SelectionCriteria": {"AdGroupIds": [int(group_id)]},
                    # State нужен: по нему отличаем активные чужие фразы, которые останавливаем ниже.
                    "FieldNames": ["Id", "Keyword", "State"],
                },
            )
            # Служебная фраза-автотаргетинга единых кампаний: её API не даёт ни остановить, ни удалить,
            # а тратить бюджет она начнёт только когда кампания показывает объявления — недельный лимит
            # всё равно ограничивает расход. Не привязываем к плану и не считаем чужой (иначе падал
            # suspend ниже), в эвристике совпадения по числу не участвует.
            keywords = [item for item in keywords if item.get("Keyword") != AUTOTARGETING_KEYWORD]
            complete_targets = len(keywords) == len(group.targets)
            for target in group.targets:
                match = next((item for item in keywords if item.get("Keyword") == target.value), None)
                if match is None and complete_targets and keywords:
                    # Число фраз совпало с планом, но содержимое — нет: чужую фразу не привязываем, создаём
                    # плановую заново и честно пишем об этом в журнал запуска.
                    self._content_mismatches.append(
                        f"В кабинете фраза «{target.value}» группы «{group.name}» отличается от плана — "
                        "создали заново."
                    )
                if match:
                    keywords.remove(match)
                    target_id = str(match["Id"])
                else:
                    target_id = await self._add(
                        "keywords", "Keywords", {"AdGroupId": int(group_id), "Keyword": target.value}
                    )
                    if target.status != "active" or group.status != "active":
                        await self._api("keywords", "suspend", {"SelectionCriteria": {"Ids": [int(target_id)]}})
                refs.targets[target.id] = target_id
            for item in keywords:
                # Чужие фразы (правил или добавлял владелец) убираем из активных остановкой: архивирования
                # у ключевых фраз Директа нет. Неактивные не трогаем: неактивная фраза бюджет не тратит.
                before = _status(item)
                if before != "active":
                    continue
                await self._api("keywords", "suspend", {"SelectionCriteria": {"Ids": [item["Id"]]}})
                self._foreign_stops.append(
                    Operation("set_status", "target", str(item["Id"]), "status", before, "paused_owner")
                )
        if not found and plan.status == "active" and self._marking_confirmed:
            for group in plan.groups:
                for ad in group.ads:
                    if group.status == "active" and ad.status == "active":
                        await self.apply(Operation("set_status", "ad", refs.ads[ad.id], "status", "pending", "active"))
            await self.apply(Operation("set_status", "campaign", campaign_id, "status", "paused_owner", "active"))
        elif not found and plan.status == "archived":
            await self.apply(Operation("set_status", "campaign", campaign_id, "status", "paused_owner", "archived"))
        self._trees[key] = deepcopy(refs)
        return refs

    async def add_group(self, campaign_id: str, group: AdGroupSpec) -> ExternalRefs:
        """Новая группа в работающей кампании — проверка другой аудитории. Группа и фразы ищутся по ключу ap-<id>,
        объявления добавляются как варианты (ap_ref в ссылке), поэтому повтор после сбоя не создаёт дублей."""
        if any(target.kind != "phrase" for target in group.targets):
            raise PlatformError("Поддерживаются ключевые фразы; минус-фразы задаются в negative_keywords группы.")
        async with self._create_lock:
            groups = await self._get(
                "adgroups",
                "AdGroups",
                {"SelectionCriteria": {"CampaignIds": [int(campaign_id)]}, "FieldNames": ["Id", "Name"]},
            )
            matches = [item for item in groups if item.get("Name", "").endswith(f" ap-{group.id}")]
            if len(matches) > 1:
                raise PlatformError("Неоднозначный ключ группы в Директе.")
            group_id = (
                str(matches[0]["Id"])
                if matches
                else await self._add(
                    "adgroups",
                    "AdGroups",
                    {
                        "Name": _named(group.name, f"ap-{group.id}"),
                        "CampaignId": int(campaign_id),
                        **self._targeting(group.targeting),
                        **({} if self._text_campaign else {"UnifiedAdGroup": {"OfferRetargeting": "NO"}}),
                    },
                )
            )
            keywords = await self._get(
                "keywords",
                "Keywords",
                {"SelectionCriteria": {"AdGroupIds": [int(group_id)]}, "FieldNames": ["Id", "Keyword"]},
            )
            targets = {}
            for target in group.targets:
                match = next((item for item in keywords if item.get("Keyword") == target.value), None)
                targets[target.id] = (
                    str(match["Id"])
                    if match
                    else await self._add("keywords", "Keywords", {"AdGroupId": int(group_id), "Keyword": target.value})
                )
        ads = {}
        for ad in group.ads:
            content = {"group_id": group_id, "title": ad.title, "text": ad.text, "image_ref": ad.image_ref}
            await self._add_ad(Operation("add_ad", "ad", ad.id, "creative", None, content))
            ads[ad.id] = await self._find_added_ad(ad.id, [group_id])
        return ExternalRefs(campaign_id, {group.id: group_id}, ads, targets)

    @staticmethod
    def _same_ad(remote: dict, content: dict) -> bool:
        remote = DirectAdapter._ad_content(remote)
        for key, value in content.items():
            if key == "Mobile":
                continue
            actual = remote.get(key)
            if isinstance(actual, dict) and "Items" in actual:
                actual = actual["Items"]
            if actual != value:
                return False
        return True

    @staticmethod
    def _ad_content(content: dict) -> dict:
        if not isinstance(content, dict):
            raise _invalid()
        normalized = dict(content)
        for field, key in (("Titles", "Title"), ("Texts", "Text")):
            if field in content:
                values = content[field]
                if not isinstance(values, list) or any(
                    not isinstance(item, dict) or not isinstance(item.get(key), str) for item in values
                ):
                    raise _invalid()
                normalized[field] = [item[key] for item in values]
        if "AdImages" in content:
            images = content["AdImages"]
            if isinstance(images, dict):
                items = images.get("Items") or []
            elif isinstance(images, list):
                # Ответ API иногда приходит списком, а не обёрткой Items: не падаем, нормализуем.
                items = images
            else:
                raise _invalid()
            normalized["AdImageHashes"] = [
                item["ImageHash"] if isinstance(item, dict) else str(item) for item in items
            ]
        return normalized

    async def upload_image(self, file: Path) -> str:
        if file.suffix.lower() not in {".jpg", ".jpeg", ".png", ".gif"}:
            raise ValueError("AdImages принимает JPG, PNG или GIF.")
        if file.stat().st_size > 10 * 1024 * 1024:
            raise ValueError("Изображение не должно превышать 10 МБ.")
        content = await asyncio.to_thread(file.read_bytes)
        return await self._add(
            "adimages",
            "AdImages",
            {
                "Name": file.name,
                "ImageData": base64.b64encode(content).decode("ascii"),
            },
            "AdImageHash",
        )

    async def upload_video(self, file: Path) -> str:
        raise PlatformError(
            "Загрузка видео требует отдельного процесса AdVideos/Creatives; используйте кабинет Директа."
        )

    def _variant_href(self, ref: str) -> str:
        parts = urlsplit(self._href)
        query = [(key, value) for key, value in parse_qsl(parts.query) if key != "ap_ref"]
        return urlunsplit(parts._replace(query=urlencode([*query, ("ap_ref", ref)])))

    async def _find_added_ad(self, ref: str, group_ids: list[str]) -> str | None:
        matches = [
            str(item["Id"])
            for item in await self._ads({"AdGroupIds": [int(id_) for id_ in group_ids]})
            if dict(parse_qsl(urlsplit(item.get("TextAd", item.get("ResponsiveAd", {})).get("Href", "")).query)).get(
                "ap_ref"
            )
            == ref
        ]
        if len(matches) > 1:
            raise PlatformError("Неоднозначный ключ варианта в Директе.")
        return matches[0] if matches else None

    async def _add_ad(self, operation: Operation) -> None:
        if not self._marking_confirmed or not self._href:
            raise PlatformError("Для добавления варианта нужны ссылка и подтверждение рекламодателя.")
        content = operation.after
        async with self._create_lock:
            ad_id = await self._find_added_ad(operation.entity_id, [content["group_id"]])
            if ad_id is None:
                image = await self._image_ref(content["image_ref"])
                if self._text_campaign:
                    kind = "TextAd"
                    body = {
                        "Title": content["title"],
                        "Text": content["text"],
                        "Href": self._variant_href(operation.entity_id),
                        "Mobile": "NO",
                    }
                    if content.get("title2"):
                        body["Title2"] = content["title2"]
                    if image:
                        body["AdImageHash"] = image
                else:
                    kind = "ResponsiveAd"
                    body = {
                        "Titles": [content["title"]] + ([content["title2"]] if content.get("title2") else []),
                        "Texts": [content["text"]],
                        "Href": self._variant_href(operation.entity_id),
                    }
                    if image:
                        body["AdImageHashes"] = [image]
                ad_id = await self._add("ads", "Ads", {"AdGroupId": int(content["group_id"]), kind: body})
            # Повтор завершает модерацию после сбоя между Ads.add и moderate.
            await self.apply(Operation("set_status", "ad", ad_id, "status", "pending", "active"))

    async def apply(self, operation: Operation) -> None:
        op = operation
        if op.kind == "add_ad":
            await self._add_ad(op)
            return
        id_ = int(op.entity_id)
        services = {"campaign": "campaigns", "ad": "ads", "target": "keywords"}
        if op.kind == "set_status":
            if op.after == "active" and not self._marking_confirmed:
                raise PlatformError("Сначала подтвердите данные рекламодателя. " + (self.marking_instruction() or ""))
            if op.entity_type == "group":
                if op.after == "archived":
                    raise PlatformError("Директ не поддерживает обратимое архивирование группы.")
                if op.after not in {"active", "paused_owner", "paused_auto"}:
                    raise PlatformError("Неизвестный статус.")
                ads = await self._ads({"AdGroupIds": [id_]})
                for item in ads:
                    await self.apply(Operation("set_status", "ad", str(item["Id"]), "status", op.before, op.after))
                keywords = await self._get(
                    "keywords",
                    "Keywords",
                    {
                        "SelectionCriteria": {"AdGroupIds": [id_]},
                        # Keyword нужен, чтобы отличить служебный автотаргетинг единых кампаний:
                        # API не даёт ни остановить, ни запустить его, поэтому в смену статуса
                        # группы он не включается.
                        "FieldNames": ["Id", "Keyword"],
                    },
                )
                for item in keywords:
                    if item.get("Keyword") == AUTOTARGETING_KEYWORD:
                        continue
                    await self.apply(Operation("set_status", "target", str(item["Id"]), "status", op.before, op.after))
                return
            method = {
                "active": "resume",
                "paused_owner": "suspend",
                "paused_auto": "suspend",
                "archived": "archive",
            }.get(op.after)
            if method is None or op.entity_type == "target" and method == "archive":
                raise PlatformError("Директ не поддерживает этот статус сущности.")
            if op.entity_type == "ad":
                items = await self._ads({"Ids": [id_]})
            else:
                key = "Campaigns" if op.entity_type == "campaign" else "Keywords"
                items = await self._get(
                    services[op.entity_type],
                    key,
                    {
                        "SelectionCriteria": {"Ids": [id_]},
                        "FieldNames": ["Id", "State"],
                    },
                )
            if not items:
                raise PlatformError("Сущность не найдена в Директе.")
            item = items[0]
            if op.entity_type == "ad" and op.after == "active":
                rejection = self._moderation(item)
                if rejection.status == "rejected":
                    raise ModerationRejected(op.entity_id, rejection.reason or "Объявление отклонено.")
            current_status = _status(item, campaign=op.entity_type == "campaign")
            if current_status == "archived" and op.after != "archived":
                await self._api(services[op.entity_type], "unarchive", {"SelectionCriteria": {"Ids": [id_]}})
            elif current_status == op.after or current_status == "paused_owner" and op.after == "paused_auto":
                if not (op.entity_type == "ad" and op.after == "active" and item.get("Status") == "DRAFT"):
                    return
            if op.entity_type == "ad" and op.after == "active" and item.get("Status") == "DRAFT":
                await self._api("ads", "moderate", {"SelectionCriteria": {"Ids": [id_]}})
            if op.entity_type == "ad" and op.after == "archived" and current_status == "active":
                # Директ не архивирует работающее объявление: сначала пауза, потом архив.
                await self._api("ads", "suspend", {"SelectionCriteria": {"Ids": [id_]}})
            try:
                await self._api(services[op.entity_type], method, {"SelectionCriteria": {"Ids": [id_]}})
            except PlatformError as error:
                # Включение сущности, которая ещё на проверке (объявление после moderate ждёт модерации,
                # кампания-черновик v501 — принятия её объявлений), отклоняется площадкой: это не провал
                # изменения — запись остаётся pending, воркер доведёт сверкой.
                waiting = (
                    op.entity_type == "ad"
                    and "модерац" in str(error).lower()
                    or op.entity_type == "campaign"
                    and "черновик" in str(error).lower()
                )
                if not (
                    op.after == "active"
                    and waiting
                    and not isinstance(error, (AuthExpired, RateLimited))
                ):
                    raise
                raise AwaitingModeration(op.entity_id) from error
        elif op.kind == "set_budget":
            await self._set_budget(id_, op.field, op.after, op.budget_context)
        elif op.kind in {"update_ad_text", "swap_ad_image"}:
            if self._text_campaign:
                field = {"title": "Title", "text": "Text", "image_ref": "AdImageHash"}[op.field]
                value = await self._image_ref(op.after) if op.kind == "swap_ad_image" else op.after
                content = {"TextAd": {field: value}}
            else:
                field = {"title": "Titles", "text": "Texts", "image_ref": "AdImageHashes"}[op.field]
                value = await self._image_ref(op.after) if op.kind == "swap_ad_image" else op.after
                ads = await self._ads({"Ids": [id_]})
                if not ads or "ResponsiveAd" not in ads[0]:
                    raise PlatformError("Комбинаторное объявление не найдено.")
                current = self._ad_content(ads[0]["ResponsiveAd"])
                updated = {key: current[key] for key in ("Titles", "Texts", "Href") if key in current}
                # По доке API AdImageHashes — обычный массив строк, как в add (форма {Items: [...]} отклоняется).
                updated[field] = [value] if value is not None else None
                content = {"ResponsiveAd": updated}
            await self._api("ads", "update", {"Ads": [{"Id": id_, **content}]})
        elif op.kind == "set_targeting":
            await self._api("adgroups", "update", {"AdGroups": [{"Id": id_, **self._targeting(op.after)}]})

    async def _set_budget(self, id_: int, field: str, value: int, context: dict[str, int]) -> None:
        if value == 0:
            raise PlatformError("Нулевой недельный бюджет недопустим; остановите кампанию операцией set_status.")
        kind = "TextCampaign" if self._text_campaign else "UnifiedCampaign"
        items = await self._get(
            "campaigns",
            "Campaigns",
            {
                "SelectionCriteria": {"Ids": [id_]},
                "FieldNames": ["Id"],
                f"{kind}FieldNames": ["BiddingStrategy"],
            },
        )
        try:
            bidding = deepcopy(items[0][kind]["BiddingStrategy"])
            active = []
            for placement in ("Search", "Network"):
                strategy = bidding[placement]
                if strategy["BiddingStrategyType"] in {"SERVING_OFF", "NETWORK_DEFAULT"}:
                    continue
                key = STRATEGIES[strategy["BiddingStrategyType"]]
                settings = strategy[key]
                if "CustomPeriodBudget" in settings:
                    raise KeyError
                active.append(settings)
            if len(active) != 1:
                raise KeyError
            current = _integer(active[0]["WeeklySpendLimit"])
            if context:
                values = context | {field: value}
                weekly = self._to_api(self._context_weekly(values))
            else:
                weekly = self._to_api(self._weekly_limit(value, value * 7) if field == "daily_limit_kop" else value)
                if weekly > current:
                    raise PlatformError("Для повышения бюджета нужен план с дневным и накопленным лимитами.")
            if weekly <= 0:
                raise PlatformError("Бюджет исчерпан; остановите кампанию операцией set_status.")
            active[0]["WeeklySpendLimit"] = weekly
        except (KeyError, IndexError, TypeError, ValueError):
            raise PlatformError(
                "Изменение бюджета поддерживается для одной общей недельной стратегии Search/Network."
            ) from None
        if weekly == current:
            return
        await self._api(
            "campaigns",
            "update",
            {
                "Campaigns": [
                    {
                        "Id": id_,
                        kind: {"BiddingStrategy": bidding},
                    }
                ]
            },
        )

    async def extend_end_date(self, campaign_id: str) -> None:
        known = self._end_dates.get(campaign_id)
        if known and (date.fromisoformat(known) - self._today()).days > 2:
            return
        items = await self._get(
            "campaigns",
            "Campaigns",
            {"SelectionCriteria": {"Ids": [int(campaign_id)]}, "FieldNames": ["Id", "EndDate"]},
        )
        if not items:
            raise PlatformError("Кампания не найдена в Директе.")
        end = items[0].get("EndDate")
        if end is not None:
            self._end_dates[campaign_id] = end
        if end is None or (date.fromisoformat(end) - self._today()).days > 2:
            return
        next_month = (self._today().replace(day=28) + timedelta(days=4)).replace(day=1)
        desired = min(self._today() + timedelta(days=6), next_month - timedelta(days=1)).isoformat()
        if end < desired:
            await self._api("campaigns", "update", {"Campaigns": [{"Id": int(campaign_id), "EndDate": desired}]})
            self._end_dates[campaign_id] = desired

    def _context_weekly(self, context: dict[str, int]) -> int:
        # WeeklySpendLimit включает уже потраченное на этой неделе. Наш period_cap
        # накопленный за жизнь кампании: убираем только расход предыдущих недель.
        before = context.get("spent_before_week_kop", 0)
        week = context.get("spent_week_kop", 0)
        return min(
            max(0, context["period_cap_kop"] - before),
            week + self._weekly_limit(context["daily_limit_kop"], context["period_cap_kop"]),
        )

    async def get_state(self, refs: ExternalRefs) -> dict[str, EntityState]:
        aliases = {}
        for ref in refs.ads.values():
            if not ref.isdecimal():
                found = await self._find_added_ad(ref, list(refs.groups.values()))
                if found:
                    aliases[ref] = found
        refs = replace(
            refs,
            ads={
                key: aliases.get(value, value)
                for key, value in refs.ads.items()
                if value.isdecimal() or value in aliases
            },
        )
        kind = "TextCampaign" if self._text_campaign else "UnifiedCampaign"
        campaigns = await self._get(
            "campaigns",
            "Campaigns",
            {
                "SelectionCriteria": {"Ids": [int(refs.campaign_id)]},
                # Status нужен: у кампании-черновика и кампании на модерации State="OFF", отличие — в Status.
                "FieldNames": ["Id", "Name", "State", "Status"],
                f"{kind}FieldNames": ["BiddingStrategy"],
            },
        )
        states = {}
        for item in campaigns:
            limits = {}
            for placement, strategy in item.get(kind, {}).get("BiddingStrategy", {}).items():
                key = STRATEGIES.get(strategy.get("BiddingStrategyType"))
                if key and "WeeklySpendLimit" in strategy.get(key, {}):
                    limits[placement] = _integer(strategy[key]["WeeklySpendLimit"])
            budgets = {placement: self._from_api(micros) for placement, micros in limits.items()}
            weekly = self._from_api(sum(limits.values()))
            context = refs.budget_context
            # Сверка — в единицах API: пересчёт НДС туда и обратно может разойтись на копейку.
            if context and sum(limits.values()) == self._to_api(
                context.get("weekly_limit_kop", self._context_weekly(context))
            ):
                daily, period = context["daily_limit_kop"], context["period_cap_kop"]
            else:
                days = (self._budget_end() - self._today()).days + 1
                daily = weekly // days
                period = weekly + context.get("spent_before_week_kop", 0)
            states[str(item["Id"])] = EntityState(
                str(item["Id"]),
                "campaign",
                None,
                item["Name"],
                _status(item, campaign=True),
                daily_limit_kop=daily,
                period_cap_kop=period,
                targeting={"weekly_limits_kop": budgets},
            )
        if refs.groups:
            groups = await self._get(
                "adgroups",
                "AdGroups",
                {
                    "SelectionCriteria": {"Ids": [int(id_) for id_ in refs.groups.values()]},
                    "FieldNames": ["Id", "CampaignId", "Name", "RegionIds", "NegativeKeywords", "Status"],
                },
            )
            for item in groups:
                states[str(item["Id"])] = EntityState(
                    str(item["Id"]),
                    "group",
                    str(item["CampaignId"]),
                    item["Name"],
                    "pending",
                    targeting={
                        "region_ids": item["RegionIds"],
                        "negative_keywords": (item.get("NegativeKeywords") or {}).get("Items", []),
                    },
                )
        if refs.ads:
            for item in await self._ads({"Ids": [int(id_) for id_ in refs.ads.values()]}):
                content = self._ad_content(item.get("TextAd", item.get("ResponsiveAd", {})))
                titles = content.get("Titles") or []
                texts = content.get("Texts") or []
                images = content.get("AdImageHashes") or []
                if isinstance(titles, dict):
                    titles = titles["Items"]
                if isinstance(texts, dict):
                    texts = texts["Items"]
                if isinstance(images, dict):
                    images = images["Items"]
                states[str(item["Id"])] = EntityState(
                    str(item["Id"]),
                    "ad",
                    str(item["AdGroupId"]),
                    content.get("Title", titles[0] if titles else ""),
                    _status(item),
                    title=content.get("Title", titles[0] if titles else None),
                    title2=content.get("Title2", titles[1] if len(titles) > 1 else None),
                    text=content.get("Text", texts[0] if texts else None),
                    image_ref=content.get("AdImageHash", images[0] if images else None),
                )
        if refs.targets:
            for item in await self._get(
                "keywords",
                "Keywords",
                {
                    "SelectionCriteria": {"Ids": [int(id_) for id_ in refs.targets.values()]},
                    "FieldNames": ["Id", "AdGroupId", "Keyword", "State"],
                },
            ):
                states[str(item["Id"])] = EntityState(
                    str(item["Id"]), "target", str(item["AdGroupId"]), item["Keyword"], _status(item)
                )
        for state in list(states.values()):
            if state.entity_type == "group":
                children = [child for child in states.values() if child.parent_id == state.entity_id]
                statuses = {child.status for child in children}
                status = statuses.pop() if len(statuses) == 1 else "mixed" if statuses else "pending"
                states[state.entity_id] = replace(state, status=status)
        states.update({ref: states[id_] for ref, id_ in aliases.items() if id_ in states})
        return states

    @staticmethod
    def _moderation(item: dict) -> ModerationInfo:
        checks = [item]

        def collect(value: Any) -> None:
            if isinstance(value, dict):
                if "Status" in value:
                    checks.append(value)
                for child in value.values():
                    collect(child)
            elif isinstance(value, list):
                for child in value:
                    collect(child)

        collect(item.get("TextAd", {}))
        collect(item.get("ResponsiveAd", {}))
        rejected = [value for value in checks if value.get("Status") == "REJECTED"]
        status = (
            "rejected"
            if rejected
            else "approved"
            if all(value.get("Status") == "ACCEPTED" for value in checks)
            else "pending"
        )
        reason = (
            "; ".join(value["StatusClarification"] for value in rejected if value.get("StatusClarification")) or None
        )
        return ModerationInfo(str(item["Id"]), status, reason, datetime.now(UTC))

    async def get_moderation(self, refs: ExternalRefs) -> list[ModerationInfo]:
        if not refs.ads:
            return []
        return [self._moderation(item) for item in await self._ads({"Ids": [int(id_) for id_ in refs.ads.values()]})]

    async def _report(self, params: dict) -> str:
        name = sha256(json.dumps(params, sort_keys=True).encode()).hexdigest()[:24]
        try:
            recent = date.fromisoformat(params.get("SelectionCriteria", {}).get("DateTo", "")) >= (
                self._today() - timedelta(days=1)
            )
        except ValueError:
            recent = False
        # Отчёт за сегодня/вчера площадка ещё дописывает: заказ под тем же именем отдал бы старую
        # версию, поэтому к свежим интервалам добавляем метку получаса.
        marker = f"-{int(datetime.now(UTC).timestamp()) // 1800}" if recent else ""
        body = {"params": {**params, "ReportName": f"ap-{name}{marker}"}}
        headers = {"processingMode": "offline", "skipReportHeader": "true", "skipReportSummary": "true"}
        for attempt in range(self._report_attempts):
            response = await self._post("reports", body, headers=headers)
            if response.status_code in {201, 202}:
                if attempt + 1 == self._report_attempts:
                    raise RateLimited(self._retry_after(response))
                await asyncio.sleep(self._retry_after(response))
                continue
            if response.status_code != 200:
                self._json(response)
            if response.text.lstrip().startswith("{"):
                self._json(response)
                raise _invalid()
            return response.text
        raise _invalid()

    async def get_stats(
        self,
        date_from: date,
        date_to: date,
        level: StatLevel = "ad",
        campaign_ids: Collection[str] | None = None,
    ) -> list[StatRow]:
        if level not in {"campaign", "group", "ad"} or date_from > date_to:
            raise ValueError("Некорректный уровень или интервал статистики.")
        own = None if campaign_ids is None else {str(id_) for id_ in campaign_ids}
        if own is not None and not own:
            return []
        dimensions = ["Date", "CampaignId"]
        if level in {"group", "ad"}:
            dimensions.append("AdGroupId")
        if level == "ad":
            dimensions.append("AdId")
        params = {
            "SelectionCriteria": {"DateFrom": date_from.isoformat(), "DateTo": date_to.isoformat()},
            "FieldNames": [*dimensions, "Impressions", "Clicks", "Cost", "Conversions"],
            "ReportType": "CUSTOM_REPORT",
            "DateRangeType": "CUSTOM_DATE",
            "Format": "TSV",
            "IncludeVAT": "YES",
            "IncludeDiscount": "YES",
            "AttributionModels": ["LSCCD"],
        }
        if own is not None:
            # Кабинет может содержать чужую рекламу владельца (вне сервиса): в отчёт идём только
            # за своими кампаниями, иначе их расход урезает наши лимиты и портит сверку бюджета.
            params["SelectionCriteria"]["Filter"] = [
                {"Field": "CampaignId", "Operator": "IN", "Values": sorted(own)}
            ]
        if self._report_goals:
            params["Goals"] = [str(goal) for goal in self._report_goals]
        if self._sandbox:
            campaigns = await self._get("campaigns", "Campaigns", {"SelectionCriteria": {}, "FieldNames": ["Id"]})
            reports = []
            for campaign in campaigns:
                if own is not None and str(campaign["Id"]) not in own:
                    continue
                selection = {
                    **params["SelectionCriteria"],
                    "Filter": [{"Field": "CampaignId", "Operator": "EQUALS", "Values": [str(campaign["Id"])]}],
                }
                reports.append(await self._report({**params, "SelectionCriteria": selection}))
            if not reports:
                return []
            header = reports[0].splitlines()[0] if reports[0].splitlines() else ""
            if any(not report.splitlines() or report.splitlines()[0] != header for report in reports):
                raise _invalid()
            content = "\n".join([header, *(line for report in reports for line in report.splitlines()[1:])])
        else:
            content = await self._report(params)
        reader = csv.DictReader(io.StringIO(content.lstrip("\ufeff")), delimiter="\t")
        conversion_fields = (
            {f"Conversions_{goal}_LSCCD": str(goal) for goal in self._report_goals}
            if self._report_goals
            else {"Conversions": "all"}
        )
        required = {*dimensions, "Impressions", "Clicks", "Cost", *conversion_fields}
        if not reader.fieldnames or not required.issubset(reader.fieldnames):
            raise _invalid()
        fetched_at = datetime.now(UTC)
        rows = []
        try:
            for item in reader:
                conversions = {}
                for field, goal in conversion_fields.items():
                    value = Decimal(item[field]) if item[field] != "--" else Decimal(0)
                    if not value.is_finite() or value != value.to_integral_value():
                        raise ValueError
                    conversions[goal] = int(value)
                day = date.fromisoformat(item["Date"])
                if not date_from <= day <= date_to:
                    raise ValueError
                entity_id = item[dimensions[-1]]
                if _integer(entity_id) <= 0 or _integer(item["CampaignId"]) <= 0:
                    raise ValueError
                if own is not None and item["CampaignId"] not in own:
                    continue
                rows.append(
                    StatRow(
                        entity_id,
                        item["CampaignId"],
                        day,
                        level,
                        _integer(item["Impressions"]),
                        _integer(item["Clicks"]),
                        _kop(item["Cost"]),
                        conversions,
                        fetched_at,
                        # Подпись в интерфейсе и RULES.md: «последние 2 дня уточняются» — устоявшиеся дни
                        # заканчиваются позавчера, сегодня и вчера площадка ещё пересчитывает.
                        day <= self._today() - timedelta(days=2),
                    )
                )
        except (ValueError, InvalidOperation, TypeError, KeyError):
            raise _invalid() from None
        return rows

    async def get_spend_today(self, campaign_ids: Collection[str] | None = None) -> int:
        today = self._today()
        return sum(
            row.cost_kop for row in await self.get_stats(today, today, "campaign", campaign_ids=campaign_ids)
        )

    async def get_balance_hint(self) -> int | None:
        """Остаток общего счёта в копейках (API v4 Live); общего счёта нет — None.

        Amount берём как есть, без пересчёта на НДС: не подтверждено, с НДС он или без. Если без, остаток
        занижен и предупреждение о балансе придёт раньше — это безопаснее, чем опоздать."""
        async with DirectForecastClient(
            self._token, config=self._config, client=self._client, timeout=self._timeout
        ) as live:
            amount = await live.balance()
        return None if amount is None else int(amount * 100)

    async def limits(self) -> PlatformLimits:
        return self._limits

    async def min_budgets(self) -> dict[str, int]:
        """Минимум, который относится к нашей стратегии: MinimumWeeklySpendLimit (Dictionaries v5, Currencies).

        Дневной бюджет кампании (DailyBudget) мы не задаём, а MinimumAccountDailyBudget относится к общему счёту,
        поэтому дневного минимума у Директа для нас нет. Валютные минимумы не меняются, читаем их один раз."""
        if self._minimums is not None:
            return dict(self._minimums)
        clients = await self._api("clients", "get", {"FieldNames": ["Currency"]})
        currencies = await self._api("dictionaries", "get", {"DictionaryNames": ["Currencies"]})
        try:
            currency = clients["Clients"][0]["Currency"]
            properties = next(item["Properties"] for item in currencies["Currencies"] if item["Currency"] == currency)
            values = {item["Name"]: item["Value"] for item in properties}
            self._minimums = {"weekly_limit_kop": self._from_api(values["MinimumWeeklySpendLimit"], round_up=True)}
        except (KeyError, IndexError, StopIteration, TypeError, ValueError):
            raise _invalid() from None
        return dict(self._minimums)

    async def geo_targeting(self, city: str) -> dict[str, Any]:
        """Город из словаря без запроса; остальные — по справочнику GeoRegions (Dictionaries API v5)."""
        if (region := CITY_IDS.get(normalize(city))) is not None:
            return {"region_ids": [region]}
        regions = await GEO_REGIONS.get(self._base_url, self._geo_regions)
        region = find_region(city, regions.values(), regions, country=RUSSIA, platform="Яндекс Директа")
        if region is None:
            raise not_found(city, "Яндекс Директа")
        return {"region_ids": [region]}

    async def _geo_regions(self) -> list[Region]:
        data = await self._api("dictionaries", "get", {"DictionaryNames": ["GeoRegions"]})
        try:
            return [
                Region(
                    _integer(item["GeoRegionId"]),
                    str(item["GeoRegionName"]),
                    _integer(item["ParentId"]) if item.get("ParentId") is not None else None,
                    item.get("GeoRegionType") == "City",
                )
                for item in data["GeoRegions"]
            ]
        except (KeyError, TypeError, ValueError):
            raise _invalid() from None

    async def set_marking(self, profile: Mapping[str, str]) -> None:
        required = {"type", "inn", "name", "phone"}
        if not required.issubset(profile) or any(not profile[key].strip() for key in required):
            raise ValueError("Нужны тип рекламодателя, ИНН, название/ФИО и телефон.")
        tin_types = {"ip": "INDIVIDUAL", "ooo": "LEGAL", "self_employed": "PHYSICAL", "individual": "PHYSICAL"}
        tin_type = tin_types.get(profile["type"], profile["type"])
        if tin_type not in {"INDIVIDUAL", "LEGAL", "PHYSICAL", "FOREIGN_PHYSICAL", "FOREIGN_LEGAL"}:
            raise ValueError("Неизвестный тип рекламодателя.")
        desired = {"TinInfo": {"TinType": tin_type, "Tin": profile["inn"]}, "Phone": profile["phone"]}
        try:
            data = await self._api("clients", "get", MARKING_FIELDS)
            current = data["Clients"][0]
            erir = current.get("ErirAttributes") or {}
            organization = erir.get("Organization") or {}
            desired["ErirAttributes"] = {"Organization": {**organization, "Name": profile["name"]}}
            changed = (
                current.get("TinInfo") != desired["TinInfo"]
                or organization.get("Name") != profile["name"]
                or current.get("Phone") != profile["phone"]
            )
            existing = bool(
                current.get("TinInfo")
                and any(current["TinInfo"].values())
                or any(organization.values())
                or erir.get("Contract")
                or erir.get("Contragent")
                or current.get("Phone")
            )
            if changed and existing and profile.get("confirm_overwrite") != "yes":
                raise PlatformError(
                    "Изменение существующих данных ЕРИР требует confirm_overwrite=yes после подтверждения владельцем."
                )
            if changed:
                await self._api("clients", "update", {"Clients": [desired]})
        except (AuthExpired, RateLimited):
            raise
        except PlatformError as error:
            code = str(getattr(error, "direct_code", ""))
            unsupported_field = "erir" in str(error).lower() and any(
                word in str(error).lower() for word in ("unsupported", "unknown", "неизвестн", "не поддерж")
            )
            if (
                code not in {str(code) for code in MARKING_UNSUPPORTED_CODES}
                or code == "8000"
                and not unsupported_field
            ):
                raise
            self._marking_confirmed = False
            return
        except (KeyError, IndexError, TypeError):
            raise _invalid() from None
        self._marking_confirmed = True

    def marking_instruction(self) -> str | None:
        if self._marking_confirmed:
            return None
        return (
            "В Директ Про откройте «Данные рекламодателя», выберите тип, заполните ИНН, название/ФИО и телефон, "
            "сохраните согласие на передачу в ЕРИР и подтвердите заполнение в приложении. ERID назначает Яндекс."
        )
