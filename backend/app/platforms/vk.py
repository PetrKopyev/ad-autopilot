import asyncio
import json
from collections.abc import Mapping
from dataclasses import replace
from datetime import UTC, date, datetime
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from PIL import Image, UnidentifiedImageError

from app.auth.vk import API_URL, VkClient, invalid_response
from app.platforms.base import (
    LEAD,
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
    "image_key": "image_600x600",
    "title_key": "title_25",
    "text_key": "text_90",
    "title_limit": 25,
    "text_limit": 90,
    "statuses": {"active": "active", "paused_owner": "blocked", "paused_auto": "blocked", "archived": "deleted"},
    "min_daily_limit_kop": 30000,
    "min_period_cap_kop": 30000,
    "page": 250,
}
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
        **kwargs: Any,
    ) -> None:
        super().__init__(token, **kwargs)
        self._href = href
        self._package_id = package_id
        self._marking_confirmed = marking_confirmed
        self._tz = ZoneInfo(getattr(self._config, "tz_default", "Europe/Moscow"))
        self._trees: dict[str, ExternalRefs] = {}
        self._add_ad_lock = asyncio.Lock()

    def _today(self) -> date:
        return datetime.now(self._tz).date()

    async def _items(self, path: str, params: dict, headers: dict | None = None) -> list[dict]:
        items: list[dict] = []
        while True:
            data = await self._request(
                "GET",
                f"{path}.json",
                params={**params, "limit": API_FORMAT["page"], "offset": len(items)},
                headers=headers,
            )
            page = data.get("items")
            if not isinstance(page, list) or any(not isinstance(item, dict) for item in page):
                raise invalid_response()
            items += page
            if not page or len(items) >= data.get("count", len(items)):
                return items

    async def _add(self, path: str, body: dict) -> dict:
        data = await self._request("POST", f"{path}.json", json=body)
        if not isinstance(data.get("id"), int):
            raise invalid_response()
        return data

    async def _update(self, path: str, id_: int, body: dict) -> None:
        await self._request("POST", f"{path}/{id_}.json", json=body)

    async def create_campaign_tree(self, plan_part: CampaignTreeSpec) -> ExternalRefs:
        key = plan_part.idempotency_key
        if key in self._trees:
            return self._trees[key]
        if not self._href:
            raise PlatformError("Для объявлений нужна ссылка href.")
        if not plan_part.daily_limit_kop or not plan_part.period_cap_kop:
            raise PlatformError("Бюджеты VK должны быть положительными; нулевой бюджет задаётся остановкой кампании.")
        for group in plan_part.groups:
            _targetings(group.targeting)
            for ad in group.ads:
                if len(ad.title) > API_FORMAT["title_limit"] or len(ad.text) > API_FORMAT["text_limit"]:
                    raise PlatformError(
                        f"Заголовок VK — до {API_FORMAT['title_limit']} знаков, текст — до {API_FORMAT['text_limit']}."
                    )
            if any(target.kind != "interest" for target in group.targets):
                raise PlatformError("VK принимает только интересы; фразы и аудитории задаются в targeting группы.")
        found = [
            item
            for item in await self._items("ad_plans", {"_name__contains": key, "fields": "id,name"})
            if str(item.get("name", "")).endswith(f" {key}")
        ]
        if len(found) > 1:
            raise PlatformError("В VK Рекламе несколько кампаний с одним ключом идемпотентности.")
        if found:
            plan = {"id": found[0]["id"]}
            plan["ad_groups"] = await self._items("ad_groups", {"_ad_plan_id": plan["id"], "fields": "id,name"})
            banners = await self._items("banners", {"_ad_plan_id": plan["id"], "fields": "id,name,ad_group_id"})
            for group in plan["ad_groups"]:
                group["banners"] = [item for item in banners if item.get("ad_group_id") == group["id"]]
        else:
            plan = await self._add("ad_plans", await self._plan_body(plan_part))
        refs = self._refs(plan_part, plan)
        self._trees[key] = refs
        return refs

    async def _plan_body(self, plan: CampaignTreeSpec) -> dict:
        url = (await self._add("urls", {"url": self._href}))["id"]
        status = API_FORMAT["statuses"]
        live = self._marking_confirmed

        def state(value: str) -> str:
            return "active" if live and status.get(value) == "active" else "blocked"

        groups = []
        for group in plan.groups:
            banners = []
            for ad in group.ads:
                banner = {
                    "name": _named(ad.title, f"ap-{ad.id}"),
                    "status": state(ad.status),
                    "urls": {"primary": {"id": url}},
                    "textblocks": {
                        API_FORMAT["title_key"]: {"text": ad.title},
                        API_FORMAT["text_key"]: {"text": ad.text},
                    },
                }
                if ad.image_ref:
                    banner["content"] = {API_FORMAT["image_key"]: {"id": _integer(ad.image_ref)}}
                banners.append(banner)
            body = {
                "name": _named(group.name, f"ap-{group.id}"),
                "status": state(group.status),
                "targetings": _targetings(group.targeting),
                "banners": banners,
            }
            interests = [_integer(target.value) for target in group.targets if target.status == "active"]
            if interests:
                body["targetings"]["interests"] = [*body["targetings"].get("interests", []), *interests]
            if self._package_id is not None:
                body["package_id"] = self._package_id
            groups.append(body)
        return {
            "name": _named(plan.name, plan.idempotency_key),
            "status": state(plan.status),
            "objective": API_FORMAT["objective"],
            "autobidding_mode": API_FORMAT["autobidding_mode"],
            "budget_limit_day": _rub(plan.daily_limit_kop),
            "budget_limit": _rub(plan.period_cap_kop),
            "ad_groups": groups,
        }

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
                },
            }
            if content["image_ref"]:
                body["content"] = {API_FORMAT["image_key"]: {"id": _integer(content["image_ref"])}}
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
            name = {"daily_limit_kop": "budget_limit_day", "period_cap_kop": "budget_limit"}[op.field]
            await self._update("ad_plans", id_, {name: _rub(op.after)})
        elif op.kind == "update_ad_text":
            key = API_FORMAT["title_key" if op.field == "title" else "text_key"]
            limit = API_FORMAT["title_limit" if op.field == "title" else "text_limit"]
            if len(op.after) > limit:
                raise PlatformError(f"Слишком длинный текст для VK: до {limit} знаков.")
            await self._update("banners", id_, {"textblocks": {key: {"text": op.after}}})
        elif op.kind == "swap_ad_image":
            if op.after is None:
                raise PlatformError("VK не позволяет убрать картинку у объявления.")
            await self._update("banners", id_, {"content": {API_FORMAT["image_key"]: {"id": _integer(op.after)}}})
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
                states[str(item["id"])] = EntityState(
                    str(item["id"]),
                    "campaign",
                    None,
                    item["name"],
                    _status(item),
                    daily_limit_kop=_kop(item["budget_limit_day"]),
                    period_cap_kop=_kop(item["budget_limit"]),
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
        return {
            str(item["id"]): groups[str(item["ad_group_id"])]
            for item in await self._items("banners", {"fields": "id,ad_group_id"})
        }

    async def get_stats(self, date_from: date, date_to: date, level: StatLevel = "ad") -> list[StatRow]:
        if level not in STAT_PATHS or date_from > date_to:
            raise ValueError("Некорректный уровень или интервал статистики.")
        try:
            parents = await self._parents(level)
            data = await self._request(
                "GET",
                f"statistics/{STAT_PATHS[level]}/day.json",
                params={"date_from": date_from.isoformat(), "date_to": date_to.isoformat(), "metrics": "base"},
            )
            fetched_at = datetime.now(UTC)
            rows = []
            for item in data["items"]:
                entity_id = str(_integer(item["id"]))
                for row in item["rows"]:
                    day = date.fromisoformat(row["date"])
                    base = row["base"]
                    if not date_from <= day <= date_to:
                        raise ValueError
                    rows.append(
                        StatRow(
                            entity_id,
                            parents.get(entity_id, entity_id),
                            day,
                            level,
                            _integer(base["shows"]),
                            _integer(base["clicks"]),
                            _kop(base["spent"]),
                            {LEAD: _integer(base.get("goals", 0))},
                            fetched_at,
                            day < self._today(),
                        )
                    )
        except (KeyError, TypeError, ValueError, InvalidOperation):
            raise invalid_response() from None
        return rows

    async def get_spend_today(self) -> int:
        today = self._today()
        return sum(row.cost_kop for row in await self.get_stats(today, today, "campaign"))

    async def get_balance_hint(self) -> int | None:
        data = await self._request("GET", "user.json", params={"fields": "account"})
        try:
            return _kop(data["account"]["balance"])
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
        return {
            "daily_limit_kop": API_FORMAT["min_daily_limit_kop"],
            "period_cap_kop": API_FORMAT["min_period_cap_kop"],
        }

    async def set_marking(self, profile: Mapping[str, str]) -> None:
        required = {"type", "inn", "name", "phone"}
        if not required.issubset(profile) or any(not profile[key].strip() for key in required):
            raise ValueError("Нужны тип рекламодателя, ИНН, название/ФИО и телефон.")
        if profile["type"] not in INDIVIDUAL_TYPES:
            self._marking_confirmed = False
            return
        await self._request(
            "POST",
            "ord_user.json",
            json={"name": profile["name"], "phone": profile["phone"], "inn": profile["inn"]},
        )
        self._marking_confirmed = True

    def marking_instruction(self) -> str | None:
        return None if self._marking_confirmed else LEGAL_MARKING
