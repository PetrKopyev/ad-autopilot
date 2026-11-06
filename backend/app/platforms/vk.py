import asyncio
import json
import re
from collections.abc import Collection, Mapping
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from PIL import Image, UnidentifiedImageError

from app.auth.vk import API_URL, VkClient, invalid_response
from app.platforms.base import (
    LEAD,
    AdGroupSpec,
    CampaignTreeSpec,
    EntityState,
    ExternalRefs,
    ModerationInfo,
    ModerationRejected,
    Operation,
    PlatformError,
    PlatformLimits,
    StatLevel,
    StatRow,
)
from app.platforms.regions import AREAS, Region, RegionCache, find_region, not_found

# Все места, где документация VK неоднозначна, собраны здесь; сверять с живым кабинетом.
API_FORMAT = {
    "autobidding_mode": "max_goals",
    "objective": "site_conversions",
    # Баннер универсального пакета сайта (паттерн multiformat_square, banner_patterns.json): главная
    # картинка 600×600 + иконка 256×256, заголовок 40 знаков, текст 90, кнопка — обязательные поля.
    "image_key": "image_600x600",
    "icon_key": "icon_256x256",
    "title_key": "title_40_vkads",
    "text_key": "text_90",
    "cta_key": "cta_sites_full",
    "cta_text": "Подробнее",
    "title_limit": 40,
    "text_limit": 90,
    "statuses": {"active": "active", "paused_owner": "blocked", "paused_auto": "blocked", "archived": "deleted"},
    "min_daily_limit_kop": 30000,
    "page": 250,
}
# Пакет VK обязателен в campaign плана. Подходит не каждый пакет из packages.json (живые пробы 02.11):
# - универсальные пакеты выводятся из работы: в списке остаются «active», но создание падает «Invalid package»
#   (пакета нет и в деталях — GET packages/3104.json → 404), поэтому подходящих кандидатов перебираем при отказе;
# - доступные VK универсальные пакеты сайта: 3229/3232/3509/3858/4617; пакеты oCPM conversions требуют
#   цель priced_goal (событие пикселя VK: счётчик Top@Mail + цель, Метрика не подходит — ответ поддержки VK),
#   cpc-пакеты (3232) создаются без цели, objective и autobidding_mode живут на уровне ПЛАНА, не campaign.
PACKAGE_REJECT_FLAGS = {"no_create_campaigns"}
# Отсечение по токенам имени (разделитель «_»): «max»/«post» подстрокой зацепили бы «multiformat» —
# а это как раз универсальные пакеты сайта, которые нам нужны.
PACKAGE_REJECT_TOKENS = {"carousel", "карусель", "пост", "post", "max", "charity"}
PACKAGE_GOAL_REQUIRED_HINT = "ocpm"
PACKAGE_UNAVAILABLE = (
    "В кабинете VK Рекламы нет пакета, на котором API может создать кампанию сайта без цели пикселя. "
    "Создайте пиксель VK (ads.vk.com → счётчик Top@Mail) и цель, задайте её в VK_PRICED_GOAL_SOURCE_ID "
    "и VK_PRICED_GOAL_NAME — или укажите подходящий пакет в VK_PACKAGE_ID. Поддержка VK: ads_api@vk.team."
)
# Регионы VK со своими id (не Яндекса): GET /api/v2/regions.json, см. https://ads.vk.com/doc/api/object/Region
RUSSIA = 188
RUSSIAN = {"Accept-Language": "ru-RU"}
REGIONS = RegionCache()
PATHS = {"campaign": "ad_plans", "group": "ad_groups", "ad": "banners"}
STAT_PATHS = {"campaign": "ad_plans", "group": "ad_groups", "ad": "banners"}
MODERATION = {"allowed": "approved", "banned": "rejected"}
INDIVIDUAL_TYPES = {"individual", "self_employed", "physical"}
LEGAL_MARKING = (
    "В кабинете VK Рекламы откройте «Настройки» → «Маркировка рекламы» (ОРД), заполните данные юридического лица "
    "или ИП (ИНН, название, договор) и подтвердите заполнение в приложении. Токены креативов назначает VK."
)


def image_size(file: Path) -> dict[str, int]:
    with Image.open(file) as image:
        return {"width": image.width, "height": image.height}


def _rub(kop: int) -> str:
    return f"{kop // 100}.{kop % 100:02d}"


def _kop(value: Any) -> int:
    try:
        amount = Decimal(str(value))
    except InvalidOperation:
        raise ValueError("Ожидается сумма в рублях.") from None
    if not amount.is_finite() or amount < 0:
        raise ValueError("Сумма не может быть отрицательной.")
    return int((amount * 100).quantize(Decimal(1), ROUND_HALF_UP))


def _integer(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        raise ValueError("Ожидается целое число.")
    return int(value)


def _named(name: str, key: str) -> str:
    return f"{name[: 254 - len(key)]} {key}"


def _status(item: dict) -> str:
    return {"active": "active", "blocked": "paused_owner", "deleted": "archived"}.get(item.get("status"), "pending")


def _package_fits(item: dict) -> bool:
    """Можно ли создавать на пакете кампанию нашего формата; перечень пакетов — packages.json."""
    if not isinstance(item.get("id"), int) or item.get("status", "active") != "active":
        return False
    if any(flag in PACKAGE_REJECT_FLAGS for flag in item.get("flags") or []):
        return False
    text = f"{item.get('name', '')} {item.get('description', '')}".lower()
    tokens = {part for part in re.split(r"[_\W]+", text) if part}
    if tokens & PACKAGE_REJECT_TOKENS:
        return False
    objective = item.get("objective")
    if isinstance(objective, list) and objective and objective[0] != API_FORMAT["objective"]:
        # Пакет под другую цель (например branding) — для рекламы сайта с конверсиями не подходит.
        return False
    return True


def _needs_priced_goal(item: dict) -> bool:
    """Пакету нужна цель пикселя в priced_goal: живые пробы 02.11 — oCPM-пакеты conversions требуют цель,
    cpc-пакеты (3232) создаются без неё. По имени пакета, признака в packages.json нет."""
    return PACKAGE_GOAL_REQUIRED_HINT in str(item.get("name", "")).lower()


def _package_candidates(items: list[dict[str, Any]], priced_goal_set: bool) -> list[tuple[int, dict[str, Any]]]:
    """Кандидаты в порядке попыток создания: сначала пакеты без обязательной цели (минимальный id),
    затем требующие цели — только если она задана в конфиге. Выведенные из работы пакеты отсеиваются
    живым ответом «Invalid package» и перебираются дальше."""
    suitable = sorted((item for item in items if _package_fits(item)), key=lambda item: item["id"])
    without_goal = [item for item in suitable if not _needs_priced_goal(item)]
    with_goal = [item for item in suitable if _needs_priced_goal(item)]
    ordered = [*without_goal, *(with_goal if priced_goal_set else [])]
    return [(item["id"], item) for item in ordered]


def _targetings(targeting: dict) -> dict:
    result: dict[str, Any] = {}
    if targeting.get("regions"):
        result["geo"] = {"regions": [_integer(id_) for id_ in targeting["regions"]]}
    if "age_from" in targeting or "age_to" in targeting:
        low, high = _integer(targeting.get("age_from", 12)), _integer(targeting.get("age_to", 75))
        if not 12 <= low <= high <= 75:
            raise ValueError("Возраст должен быть от 12 до 75 лет, начало не больше конца.")
        result["age"] = {"age_list": list(range(low, high + 1))}
    if targeting.get("sex"):
        result["sex"] = list(targeting["sex"])
    if targeting.get("interests"):
        result["interests"] = [_integer(id_) for id_ in targeting["interests"]]
    return result


class VkAdapter(VkClient):
    def __init__(
        self,
        token: str = "",
        *,
        href: str = "",
        package_id: int | None = None,
        marking_confirmed: bool = False,
        priced_goal_source_id: int | None = None,
        priced_goal_name: str | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(token, **kwargs)
        self._href = href
        self._package_id = package_id
        self._goal_on_plan: bool | None = None
        self._package_choice: list[tuple[int, dict[str, Any]]] | None = None
        # Пакет, на котором реально создан план: новые группы кампании живут на нём же.
        self._used_package_id: int | None = package_id
        self._marking_confirmed = marking_confirmed
        self._tz = ZoneInfo(getattr(self._config, "tz_default", "Europe/Moscow"))
        self._trees: dict[str, ExternalRefs] = {}
        self._add_ad_lock = asyncio.Lock()
        # Ссылка плана создаётся один раз на дерево: при переборе пакетов (два круга) без кэша
        # каждый кандидат создавал бы свою urls.json — до 2N лишних POST.
        self._url_id: int | None = None
        # Суммы лимитов в API VK: по умолчанию уходят как есть. VK_AMOUNTS_INCLUDE_VAT=true — ставка НДС
        # сверх лимита VK (как у Директа при DIRECT_AMOUNTS_INCLUDE_VAT=false): лимит делим на (1 + НДС),
        # чтобы трата с НДС не превысила бюджет, и умножаем обратно при чтении.
        self._vat_percent = (
            getattr(self._config, "vat_percent", 22)
            if getattr(self._config, "vk_amounts_include_vat", False)
            else 0
        )
        # Цель оптимизации campaign (Top@Mail/приложения): у каждого кабинета VK своя цель пикселя,
        # поэтому override уровня кабинета (priced_goal_source_id) важнее общей настройки сервиса
        # VK_PRICED_GOAL_SOURCE_ID. Не задана нигде — в campaign не уходит: без неё часть пакетов не
        # создастся, но отказ VK теперь виден в ошибке.
        self._priced_goal_source_id = (
            priced_goal_source_id
            if priced_goal_source_id is not None
            else getattr(self._config, "vk_priced_goal_source_id", None)
        )
        self._priced_goal_name = priced_goal_name or getattr(self._config, "vk_priced_goal_name", None) or "Заявка"

    def _to_api(self, kop: int) -> int:
        """Сумма с НДС (как бюджет и расход) в копейки лимита API; без НДС — вниз, чтобы не превысить бюджет."""
        return kop * 100 // (100 + self._vat_percent)

    def _from_api(self, kop: int) -> int:
        """Сумма лимита API в копейки с НДС: в тех же единицах, что бюджет и расход."""
        return (kop * (100 + self._vat_percent) + 50) // 100

    def _limit_kop(self, value: Any) -> int | None:
        """Строка лимита API в копейки с НДС; отсутствующее поле — None (VK не хранит лимит периода)."""
        return None if value is None else self._from_api(_kop(value))

    def _today(self) -> date:
        return datetime.now(self._tz).date()

    async def _items(
        self, path: str, params: dict, headers: dict | None = None, *, page: int | None = None
    ) -> list[dict]:
        # Живой пакетный лимит packages.json — меньше общего (250 отклоняются max_value, 02.11): страница меньше.
        limit = page or API_FORMAT["page"]
        items: list[dict] = []
        while True:
            data = await self._request(
                "GET",
                f"{path}.json",
                params={**params, "limit": limit, "offset": len(items)},
                headers=headers,
            )
            page_items = data.get("items")
            if not isinstance(page_items, list) or any(not isinstance(item, dict) for item in page_items):
                raise invalid_response()
            items += page_items
            if not page_items or len(items) >= data.get("count", len(items)):
                return items

    async def _add(self, path: str, body: dict) -> dict:
        data = await self._request("POST", f"{path}.json", json=body)
        if not isinstance(data.get("id"), int):
            raise invalid_response()
        return data

    async def _update(self, path: str, id_: int, body: dict) -> None:
        await self._request("POST", f"{path}/{id_}.json", json=body)

    async def _resolve_packages(self) -> list[tuple[int, dict[str, Any]]]:
        """Пакеты для campaign плана в порядке попыток: id из конфига (один), иначе кандидаты из packages.json
        (выбор кэшируется на экземпляр, чтобы новые группы работающей кампании жили на том же пакете)."""
        if self._package_id is not None:
            return [(self._package_id, {})]
        if self._package_choice is None:
            # Фильтр fields у packages.json не документирован: читаем элементы целиком (список — сотни позиций).
            # Пакетный лимит packages.json живьём меньше общего: страницы по 50, не 250.
            items = await self._items("packages", {}, page=50)
            priced_goal_set = (
                isinstance(self._priced_goal_source_id, int)
                and not isinstance(self._priced_goal_source_id, bool)
                and self._priced_goal_source_id > 0
            )
            self._package_choice = _package_candidates(items, priced_goal_set)
            if not self._package_choice:
                raise PlatformError(PACKAGE_UNAVAILABLE)
        return self._package_choice

    async def create_campaign_tree(self, plan_part: CampaignTreeSpec) -> ExternalRefs:
        key = plan_part.idempotency_key
        if key in self._trees:
            return self._trees[key]
        if not self._href:
            raise PlatformError("Для объявлений нужна ссылка href.")
        if not plan_part.daily_limit_kop:
            raise PlatformError(
                "Дневной бюджет VK должен быть положительным; нулевой бюджет задаётся остановкой кампании."
            )
        for group in plan_part.groups:
            _targetings(group.targeting)
            for ad in group.ads:
                if len(ad.title) > API_FORMAT["title_limit"] or len(ad.text) > API_FORMAT["text_limit"]:
                    raise PlatformError(
                        f"Заголовок VK — до {API_FORMAT['title_limit']} знаков, текст — до {API_FORMAT['text_limit']}."
                    )
            if any(target.kind != "interest" for target in group.targets):
                raise PlatformError("VK принимает только интересы; фразы и аудитории задаются в targeting группы.")
        # Живой API не поддерживает фильтра по имени у ad_plans (unknown_filter "_name__contains"):
        # читаем список целиком и отбираем суффикс ключа, как в адаптере Директа. Удалённые планы VK
        # отдаёт в списке вместе с живыми — они не кандидаты.
        found = [
            item
            for item in await self._items("ad_plans", {"fields": "id,name,status"})
            if item.get("status") != "deleted" and str(item.get("name", "")).endswith(f" {key}")
        ]
        if len(found) > 1:
            raise PlatformError("В VK Рекламе несколько кампаний с одним ключом идемпотентности.")
        if found:
            plan = await self._load_plan_tree(found[0]["id"])
            # Повтор переиспользует план неудачной попытки: дневной лимит между попытками пересчитался —
            # переносим на площадку сразу, иначе кабинет и база расходятся до первого rebalance.
            await self._update(
                "ad_plans", found[0]["id"], {"budget_limit_day": _rub(self._to_api(plan_part.daily_limit_kop))}
            )
        else:
            # Выведенные из работы пакеты остаются в списке «active» и падают только на создании
            # («Invalid package»): перебираем кандидатов, пока живой валидатор не примет план. Отказ из-за
            # priced_goal (cpc-пакеты) не обрывает перебор: сначала ищем пакет, который примет цель, и
            # только если никто не принял — второй круг без цели (иначе запуск умирал на первом отказе).
            packages = await self._resolve_packages()
            plan = None
            refusals: list[str] = []
            rounds = (True, False) if self._priced_goal_source_id else (True,)
            for with_goal in rounds:
                refusals = []
                for package_id, _package in packages:
                    created: dict[str, Any] | None = None
                    try:
                        created = await self._add(
                            "ad_plans", await self._plan_body(plan_part, package_id, with_goal=with_goal)
                        )
                        self._used_package_id = package_id
                        # Запоминаем, с целью ли создан план: add_group без этой цели падает на пакетах,
                        # которые её не принимают.
                        self._goal_on_plan = with_goal
                        # Живой ответ создания — только id плана и campaigns: дерево дочитываем; полный ответ
                        # (тестовый стенд) берём как есть.
                        plan = created if "ad_groups" in created else await self._load_plan_tree(created["id"])
                        break
                    except PlatformError as error:
                        text = str(error).lower()
                        if "ord_data_required" in text or "ord data" in text:
                            # Баннеры не создаются, пока в кабинете не заполнена маркировка (ОРД): план, если
                            # успел создаться, остаётся пустым — убираем его, чтобы не плодить черновики.
                            if created is not None:
                                try:
                                    await self._update("ad_plans", created["id"], {"status": "deleted"})
                                except PlatformError:
                                    pass
                            raise PlatformError(
                                "VK Реклама не приняла объявления: в кабинете не заполнены данные маркировки "
                                "(ОРД). Откройте ads.vk.com → «Маркировка рекламы», заполните ИНН и название "
                                "рекламодателя, затем повторите запуск."
                            ) from error
                        # Отказ по цели/пакету продолжает перебор; отказ по objective — про конфликт самого
                        # плана (маскирует настоящую ошибку под PACKAGE_UNAVAILABLE) — обрываем сразу.
                        if "invalid package" not in text and "priced_goal" not in text:
                            raise
                        refusals.append(f"пакет {package_id}: {error}")
                if plan is not None:
                    break
            if plan is None:
                detail = f" Отказало: {'; '.join(refusals)}." if refusals else ""
                raise PlatformError(f"{PACKAGE_UNAVAILABLE}{detail}")
        refs = self._refs(plan_part, plan)
        self._trees[key] = refs
        return refs

    async def _load_plan_tree(self, plan_id: int) -> dict:
        """Структура плана для _refs: ad_groups по плану и banners по группам (живой ответ создания
        возвращает только id плана и campaigns — дерево дочитываем)."""
        plan: dict[str, Any] = {"id": plan_id, "ad_groups": []}
        plan["ad_groups"] = await self._items("ad_groups", {"_ad_plan_id": plan_id, "fields": "id,name"})
        # Живой API: у banners фильтр плана — "_ad_group__ad_plan_id"; "_ad_plan_id" отвергается.
        banners = await self._items(
            "banners", {"_ad_group__ad_plan_id": plan_id, "fields": "id,name,ad_group_id"}
        )
        for group in plan["ad_groups"]:
            group["banners"] = [item for item in banners if item.get("ad_group_id") == group["id"]]
        return plan

    def _state(self, value: str) -> str:
        return "active" if self._marking_confirmed and API_FORMAT["statuses"].get(value) == "active" else "blocked"

    def _group_body(self, group: AdGroupSpec, url: int) -> dict:
        """Рекламная группа плана (в схеме плана — уровень campaigns, а в списках — ad_groups: это один
        объект, живой API, 02.11): имя, статус, таргетинги и баннеры по структуре паттерна пакета."""
        banners = []
        for ad in group.ads:
            banner = {
                "name": _named(ad.title, f"ap-{ad.id}"),
                "status": self._state(ad.status),
                "urls": {"primary": {"id": url}},
                "textblocks": {
                    API_FORMAT["title_key"]: {"text": ad.title},
                    API_FORMAT["text_key"]: {"text": ad.text},
                    API_FORMAT["cta_key"]: {"text": API_FORMAT["cta_text"]},
                },
            }
            if ad.image_ref:
                # Одна картинка объявления закрывает обе обязательные роли паттерна: VK самостоятельно
                # масштабирует контент (проверено загрузкой: валидатор принимает один content id в обеих ролях).
                ref = {"id": _integer(ad.image_ref)}
                banner["content"] = {API_FORMAT["image_key"]: ref, API_FORMAT["icon_key"]: ref}
            banners.append(banner)
        body = {
            "name": _named(group.name, f"ap-{group.id}"),
            "status": self._state(group.status),
            "targetings": _targetings(group.targeting),
            "banners": banners,
        }
        interests = [_integer(target.value) for target in group.targets if target.status == "active"]
        if interests:
            body["targetings"]["interests"] = [*body["targetings"].get("interests", []), *interests]
        return body

    def _priced_goal_body(self) -> dict | None:
        """Цель кабинета/конфига для плана и групп; None — цель не задана."""
        source_id = self._priced_goal_source_id
        if isinstance(source_id, int) and not isinstance(source_id, bool) and source_id > 0:
            return {"name": self._priced_goal_name, "source_id": source_id}
        return None

    def _campaign_body(self, name: str, package_id: int, *, with_goal: bool = True) -> dict:
        """Campaign плана (живой валидатор требует уровень campaigns): только имя и пакет. Цель и стратегия
        живут на уровне плана — на campaign живой API их отклоняет («Objective differ with ad_plan objective»,
        «Unallowed value», пробы 02.11). priced_goal — цель пикселя VK (счётчик Top@Mail + цель; Метрика не
        подходит — ответ поддержки VK): уходит, если задана в конфиге или для кабинета."""
        body = {"name": name, "package_id": package_id}
        priced_goal = self._priced_goal_body() if with_goal else None
        if priced_goal is not None:
            body["priced_goal"] = priced_goal
        return body

    async def _plan_body(self, plan: CampaignTreeSpec, package_id: int, *, with_goal: bool = True) -> dict:
        if self._url_id is None:
            self._url_id = (await self._add("urls", {"url": self._href}))["id"]
        url = self._url_id
        # Живой API принимает ровно один лимит плана: «Can't set both budget_limit and budget_limit_day».
        # Держим дневной лимит (рычаг пейсинга), месяц закрывает страж бюджета — механика пауз платформонезависима.
        # objective/autobidding_mode живут на уровне плана; уровень campaigns = рекламные группы (в списках —
        # ad_groups): каждая с пакетом, таргетингами и баннерами (живая схема подтверждена 02.11).
        priced_goal = self._campaign_body(plan.name, package_id, with_goal=with_goal)
        campaigns = [priced_goal | self._group_body(group, url) for group in plan.groups]
        return {
            "name": _named(plan.name, plan.idempotency_key),
            "status": self._state(plan.status),
            "budget_limit_day": _rub(self._to_api(plan.daily_limit_kop)),
            "objective": API_FORMAT["objective"],
            "autobidding_mode": API_FORMAT["autobidding_mode"],
            "campaigns": campaigns,
        }

    async def add_group(self, campaign_id: str, group: AdGroupSpec) -> ExternalRefs:
        """Новая группа в работающей кампании — проверка другой аудитории; группа ищется по ключу ap-<id>."""
        if not self._href:
            raise PlatformError("Для объявлений нужна ссылка href.")
        if any(target.kind != "interest" for target in group.targets):
            raise PlatformError("VK принимает только интересы; фразы и аудитории задаются в targeting группы.")
        key = f" ap-{group.id}"
        items = await self._items(
            "ad_groups", {"_ad_plan_id": int(campaign_id), "fields": "id,name,status,package_id"}
        )
        live = [item for item in items if item.get("status") != "deleted"]
        found = [item for item in live if str(item.get("name", "")).endswith(key)]
        if len(found) > 1:
            raise PlatformError("Неоднозначный ключ группы в VK Рекламе.")
        if found:
            group_id = found[0]["id"]
        else:
            # Пакет обязателен в рекламной группе (живой валидатор). Берём пакет существующих групп плана:
            # после пересоздания адаптера (токен живёт сутки) память о выборе теряется, а первый кандидат
            # списка — возможно отказавший пакет, не тот, на котором создан план.
            package_id = next(
                (
                    item["package_id"]
                    for item in live
                    if isinstance(item.get("package_id"), int) and not isinstance(item.get("package_id"), bool)
                ),
                None,
            )
            package_id = package_id or self._used_package_id or (await self._resolve_packages())[0][0]
            if self._url_id is None:
                self._url_id = (await self._add("urls", {"url": self._href}))["id"]
            url = self._url_id
            group_body = self._group_body(group, url) | {"ad_plan_id": int(campaign_id), "package_id": package_id}
            # Цель кабинета идёт и в группу: иначе новая группа (аудиторная проверка) создаётся без цели,
            # хотя план с ней работает. План создан без цели (второй круг перебора пакетов) — не шлём:
            # пакет её не примет и отвергнет всю группу.
            priced_goal = self._priced_goal_body()
            if priced_goal is not None and self._goal_on_plan is not False:
                group_body["priced_goal"] = priced_goal
            try:
                group_id = (await self._add("ad_groups", group_body))["id"]
            except PlatformError as error:
                if "priced_goal" not in group_body or "priced_goal" not in str(error).lower():
                    raise
                # План живёт без цели (создан до этой памяти или пережил смену кэша адаптера):
                # повторяем без цели, как при создании плана. Повтор только по отказу с priced_goal в
                # тексте: на любой другой ошибке (таймаут, валидация имени) повтор маскировал бы
                # настоящую причину и дублировал запрос.
                del group_body["priced_goal"]
                group_id = (await self._add("ad_groups", group_body))["id"]
        banners = {
            str(item.get("name", "")).rsplit(" ", 1)[-1]: str(item["id"])
            for item in await self._items("banners", {"_ad_group_id": group_id, "fields": "id,name"})
        }
        try:
            ads = {ad.id: banners[f"ap-{ad.id}"] for ad in group.ads}
        except KeyError:
            raise invalid_response() from None
        return ExternalRefs(
            campaign_id, {group.id: str(group_id)}, ads, {target.id: str(group_id) for target in group.targets}
        )

    @staticmethod
    def _refs(spec: CampaignTreeSpec, plan: dict) -> ExternalRefs:
        try:
            groups, ads, targets = {}, {}, {}
            remote = {item["name"].rsplit(" ", 1)[-1]: item for item in plan["ad_groups"]}
            for group in spec.groups:
                item = remote[f"ap-{group.id}"]
                groups[group.id] = str(item["id"])
                banners = {banner["name"].rsplit(" ", 1)[-1]: banner for banner in item["banners"]}
                for ad in group.ads:
                    ads[ad.id] = str(banners[f"ap-{ad.id}"]["id"])
                # Интересы в VK живут в targetings группы, поэтому цель ссылается на группу.
                targets.update({target.id: groups[group.id] for target in group.targets})
            return ExternalRefs(str(plan["id"]), groups, ads, targets)
        except (KeyError, TypeError, AttributeError):
            raise invalid_response() from None

    async def _upload(self, path: str, file: Path, data: dict | None = None) -> str:
        try:
            content = await asyncio.to_thread(file.read_bytes)
        except OSError:
            raise PlatformError("Не удалось прочитать файл для загрузки.") from None
        result = await self._request(
            "POST",
            path,
            files={"file": (file.name, content)},
            data={"data": json.dumps(data)} if data else None,
        )
        if not isinstance(result.get("id"), int):
            raise invalid_response()
        return str(result["id"])

    async def upload_image(self, file: Path) -> str:
        try:
            size = await asyncio.to_thread(image_size, file)
        except (OSError, UnidentifiedImageError):
            raise PlatformError("Файл не похож на изображение jpg или png.") from None
        return await self._upload("content/static.json", file, size)

    async def upload_video(self, file: Path) -> str:
        return await self._upload("content/video.json", file)

    async def _find_added_ad(self, ref: str, group_ids: list[str]) -> str | None:
        matches = [
            str(item["id"])
            for group_id in group_ids
            for item in await self._items("banners", {"_ad_group_id": group_id, "fields": "id,name,ad_group_id"})
            if str(item.get("ad_group_id")) == group_id and item.get("name", "").endswith(f" ap-{ref}")
        ]
        if len(matches) > 1:
            raise PlatformError("Неоднозначный ключ варианта в VK.")
        return matches[0] if matches else None

    async def _add_ad(self, operation: Operation) -> None:
        if not self._marking_confirmed or not self._href:
            raise PlatformError("Для добавления варианта нужны ссылка и подтверждение рекламодателя.")
        content = operation.after
        if len(content["title"]) > API_FORMAT["title_limit"] or len(content["text"]) > API_FORMAT["text_limit"]:
            raise PlatformError("Текст варианта превышает ограничения VK.")
        async with self._add_ad_lock:
            if await self._find_added_ad(operation.entity_id, [content["group_id"]]):
                return
            url = (await self._add("urls", {"url": self._href}))["id"]
            body = {
                "name": _named(content["title"], f"ap-{operation.entity_id}"),
                "ad_group_id": int(content["group_id"]),
                "status": "active",
                "urls": {"primary": {"id": url}},
                "textblocks": {
                    API_FORMAT["title_key"]: {"text": content["title"]},
                    API_FORMAT["text_key"]: {"text": content["text"]},
                    API_FORMAT["cta_key"]: {"text": API_FORMAT["cta_text"]},
                },
            }
            if content["image_ref"]:
                ref = {"id": _integer(content["image_ref"])}
                body["content"] = {API_FORMAT["image_key"]: ref, API_FORMAT["icon_key"]: ref}
            await self._add("banners", body)

    async def apply(self, operation: Operation) -> None:
        op = operation
        if op.kind == "add_ad":
            await self._add_ad(op)
            return
        id_ = int(op.entity_id)
        if op.kind == "set_status":
            if op.entity_type == "target":
                raise PlatformError("VK не поддерживает статус отдельной цели таргетинга.")
            status = API_FORMAT["statuses"].get(op.after)
            if status is None:
                raise PlatformError("Неизвестный статус.")
            if status == "active":
                if not self._marking_confirmed:
                    raise PlatformError(
                        "Сначала подтвердите данные рекламодателя. " + (self.marking_instruction() or "")
                    )
                if op.entity_type == "ad":
                    banner = await self._request(
                        "GET", f"banners/{id_}.json", params={"fields": "moderation_status,moderation_reasons"}
                    )
                    if (info := self._moderation(id_, banner)).status == "rejected":
                        raise ModerationRejected(op.entity_id, info.reason or "Объявление отклонено.")
            await self._update(PATHS[op.entity_type], id_, {"status": status})
        elif op.kind == "set_budget":
            if op.after == 0:
                raise PlatformError("Нулевой бюджет недопустим; остановите кампанию операцией set_status.")
            if op.field == "period_cap_kop":
                raise PlatformError("VK не хранит лимит периода: месяц закрывает страж бюджета.")
            await self._update("ad_plans", id_, {"budget_limit_day": _rub(self._to_api(op.after))})
        elif op.kind == "update_ad_text":
            key = API_FORMAT["title_key" if op.field == "title" else "text_key"]
            limit = API_FORMAT["title_limit" if op.field == "title" else "text_limit"]
            if len(op.after) > limit:
                raise PlatformError(f"Слишком длинный текст для VK: до {limit} знаков.")
            await self._update("banners", id_, {"textblocks": {key: {"text": op.after}}})
        elif op.kind == "swap_ad_image":
            if op.after is None:
                raise PlatformError("VK не позволяет убрать картинку у объявления.")
            ref = {"id": _integer(op.after)}
            await self._update(
                "banners", id_, {"content": {API_FORMAT["image_key"]: ref, API_FORMAT["icon_key"]: ref}}
            )
        elif op.kind == "set_targeting":
            await self._update("ad_groups", id_, {"targetings": _targetings(op.after)})

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
        states = {}
        try:
            for item in await self._items(
                "ad_plans", {"_id__in": refs.campaign_id, "fields": "id,name,status,budget_limit_day,budget_limit"}
            ):
                # План держит только дневной лимит: отсутствующий budget_limit — не ошибка, периода у VK нет.
                states[str(item["id"])] = EntityState(
                    str(item["id"]),
                    "campaign",
                    None,
                    item["name"],
                    _status(item),
                    daily_limit_kop=self._from_api(_kop(item["budget_limit_day"])),
                    period_cap_kop=self._limit_kop(item.get("budget_limit")),
                )
            if refs.groups:
                for item in await self._items(
                    "ad_groups",
                    {"_id__in": ",".join(refs.groups.values()), "fields": "id,name,status,ad_plan_id,targetings"},
                ):
                    targetings = item.get("targetings") or {}
                    states[str(item["id"])] = EntityState(
                        str(item["id"]),
                        "group",
                        str(item["ad_plan_id"]),
                        item["name"],
                        _status(item),
                        targeting={
                            "regions": (targetings.get("geo") or {}).get("regions", []),
                            "interests": targetings.get("interests", []),
                            "age_list": (targetings.get("age") or {}).get("age_list", []),
                            "sex": targetings.get("sex", []),
                        },
                    )
            if refs.ads:
                for item in await self._items(
                    "banners",
                    {"_id__in": ",".join(refs.ads.values()), "fields": "id,name,status,ad_group_id,textblocks,content"},
                ):
                    blocks = item.get("textblocks") or {}
                    image = (item.get("content") or {}).get(API_FORMAT["image_key"]) or {}
                    states[str(item["id"])] = EntityState(
                        str(item["id"]),
                        "ad",
                        str(item["ad_group_id"]),
                        item["name"],
                        _status(item),
                        title=(blocks.get(API_FORMAT["title_key"]) or {}).get("text"),
                        text=(blocks.get(API_FORMAT["text_key"]) or {}).get("text"),
                        image_ref=str(image["id"]) if "id" in image else None,
                    )
        except (KeyError, TypeError, ValueError):
            raise invalid_response() from None
        states.update({ref: states[id_] for ref, id_ in aliases.items() if id_ in states})
        return states

    @staticmethod
    def _moderation(id_: int | str, item: dict) -> ModerationInfo:
        reasons = item.get("moderation_reasons") or []
        if isinstance(reasons, (str, dict)):
            reasons = [reasons]
        texts = [
            str(reason.get("description") or reason.get("text") or reason.get("id", ""))
            if isinstance(reason, dict)
            else str(reason)
            for reason in reasons
        ]
        status = MODERATION.get(item.get("moderation_status"), "pending")
        return ModerationInfo(str(id_), status, "; ".join(text for text in texts if text) or None, datetime.now(UTC))

    async def get_moderation(self, refs: ExternalRefs) -> list[ModerationInfo]:
        if not refs.ads:
            return []
        items = await self._items(
            "banners", {"_id__in": ",".join(refs.ads.values()), "fields": "id,moderation_status,moderation_reasons"}
        )
        try:
            return [self._moderation(item["id"], item) for item in items]
        except KeyError:
            raise invalid_response() from None

    async def _parents(self, level: StatLevel) -> dict[str, str]:
        if level == "campaign":
            return {}
        groups = {
            str(item["id"]): str(item["ad_plan_id"])
            for item in await self._items("ad_groups", {"fields": "id,ad_plan_id"})
        }
        if level == "group":
            return groups
        parents = {}
        for item in await self._items("banners", {"fields": "id,ad_group_id"}):
            # Группа баннера могла быть удалена в кабинете: без родителя строка всё равно не сходится с нашим
            # деревом (get_stats отфильтрует по кампаниям), но вся статистика VK не должна падать из-за одного
            # такого баннера.
            parent = groups.get(str(item.get("ad_group_id")))
            if parent is not None:
                parents[str(item["id"])] = parent
        return parents

    async def get_stats(
        self,
        date_from: date,
        date_to: date,
        level: StatLevel = "ad",
        campaign_ids: Collection[str] | None = None,
    ) -> list[StatRow]:
        if level not in STAT_PATHS or date_from > date_to:
            raise ValueError("Некорректный уровень или интервал статистики.")
        # Кабинет может содержать чужую рекламу владельца: светим статистику только по своим планам.
        # Для ad_plans id в запросе есть; для групп и баннеров id другого уровня — фильтруем ответ.
        own = None if campaign_ids is None else {str(id_) for id_ in campaign_ids}
        if own is not None and not own:
            return []
        params: dict[str, Any] = {
            "date_from": date_from.isoformat(),
            "date_to": date_to.isoformat(),
            "metrics": "base",
        }
        if own is not None and level == "campaign":
            params["id"] = ",".join(sorted(own))
        try:
            parents = await self._parents(level)
            data = await self._request("GET", f"statistics/{STAT_PATHS[level]}/day.json", params=params)
            fetched_at = datetime.now(UTC)
            rows = []
            for item in data["items"]:
                entity_id = str(_integer(item["id"]))
                for row in item["rows"]:
                    day = date.fromisoformat(row["date"])
                    base = row["base"]
                    if not date_from <= day <= date_to:
                        raise ValueError
                    if own is not None:
                        # Уровень campaign — это сами планы: родителя нет, строка наша, если id плана в own.
                        # У групп и баннеров родитель обязателен: без него (удалили в кабинете) строку пропускаем.
                        parent = parents.get(entity_id) or (entity_id if level == "campaign" else None)
                        if parent not in own:
                            continue
                    rows.append(
                        StatRow(
                            entity_id,
                            parents.get(entity_id, entity_id),
                            day,
                            level,
                            _integer(base["shows"]),
                            _integer(base["clicks"]),
                            # Суммы API при VK_AMOUNTS_INCLUDE_VAT=true — без НДС, а бюджет с НДС: приводим
                            # к общим единицам, иначе расход занижается на ставку НДС и бюджет превышается.
                            self._from_api(_kop(base["spent"])),
                            {LEAD: _integer(base.get("goals", 0))},
                            fetched_at,
                            # Как в Директе и в подписи интерфейса «последние 2 дня уточняются»:
                            # устоявшиеся дни заканчиваются позавчера, сегодня и вчера площадка ещё пересчитывает.
                            day <= self._today() - timedelta(days=2),
                        )
                    )
        except (KeyError, TypeError, ValueError, InvalidOperation):
            raise invalid_response() from None
        return rows

    async def get_spend_today(self, campaign_ids: Collection[str] | None = None) -> int:
        today = self._today()
        return sum(
            row.cost_kop for row in await self.get_stats(today, today, "campaign", campaign_ids=campaign_ids)
        )

    async def get_balance_hint(self) -> int | None:
        data = await self._request("GET", "user.json", params={"fields": "account"})
        try:
            return self._from_api(_kop(data["account"]["balance"]))
        except (KeyError, TypeError, ValueError):
            return None

    async def limits(self) -> PlatformLimits:
        return self._limits

    async def geo_targeting(self, city: str) -> dict[str, Any]:
        """Город ищется в дереве регионов VK; не найден там — поиском _q (мелкие города в дерево не входят). Пункт из
        нескольких регионов (AREAS) — регионы его частей."""
        if parts := AREAS.get(city):
            return {"regions": [(await self.geo_targeting(part))["regions"][0] for part in parts]}
        regions = await REGIONS.get(API_URL, self._regions)
        region = find_region(city, regions.values(), regions, country=RUSSIA, platform="VK Рекламы")
        if region is None:
            try:
                found = await self._region_items({"_q": city.strip(), "_flags": "geo_tree"})
            except PlatformError as error:
                # Поиск по _q в документации VK не описан: отказ в нём значит «не нашли», а не сбой площадки.
                if error.status_code != 400:
                    raise
                found = []
            region = find_region(city, found, regions, country=RUSSIA, platform="VK Рекламы")
        if region is None:
            raise not_found(city, "VK Рекламы")
        return {"regions": [region]}

    async def _regions(self) -> list[Region]:
        return await self._region_items({"_flags__in": "geo_tree_extended,rb_active"})

    async def _region_items(self, params: dict) -> list[Region]:
        items = await self._items("regions", params | {"fields": "id,name,parent_id"}, RUSSIAN)
        try:
            return [
                Region(
                    _integer(item["id"]),
                    str(item["name"]),
                    _integer(item["parent_id"]) if item.get("parent_id") is not None else None,
                )
                for item in items
            ]
        except (KeyError, TypeError, ValueError):
            raise invalid_response() from None

    async def min_budgets(self) -> dict[str, int]:
        # На плане VK живёт только дневной лимит: периодного минимума у площадки нет, месяц закрывает страж бюджета.
        return {"daily_limit_kop": API_FORMAT["min_daily_limit_kop"]}

    async def set_marking(self, profile: Mapping[str, str]) -> None:
        required = {"type", "inn", "name", "phone"}
        if not required.issubset(profile) or any(not profile[key].strip() for key in required):
            raise ValueError("Нужны тип рекламодателя, ИНН, название/ФИО и телефон.")
        if profile["type"] not in INDIVIDUAL_TYPES:
            self._marking_confirmed = False
            return
        try:
            await self._request(
                "POST",
                "ord_user.json",
                json={"name": profile["name"], "phone": profile["phone"], "inn": profile["inn"]},
            )
        except PlatformError as error:
            # Метод не подтверждён документацией VK Ads API (ОРД VK — отдельный сервис с ord.vk.com и своими
            # токенами): если площадка его не знает, честно отправляем владельца заполнять маркировку в кабинете.
            if error.status_code == 404:
                raise PlatformError(
                    "VK Реклама не приняла данные маркировки автоматически. Заполните маркировку в кабинете: "
                    "ads.vk.com → «Маркировка рекламы», затем повторите."
                ) from error
            raise
        self._marking_confirmed = True

    def marking_instruction(self) -> str | None:
        return None if self._marking_confirmed else LEGAL_MARKING
