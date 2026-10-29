from collections.abc import Iterable, Mapping
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any, Literal, Protocol, runtime_checkable

type EntityType = Literal["campaign", "group", "ad", "target"]
type StatLevel = Literal["campaign", "group", "ad"]
type OperationKind = Literal["set_status", "set_budget", "update_ad_text", "swap_ad_image", "set_targeting", "add_ad"]

# Ключ конверсий площадки, которая не знает целей Метрики (VK — цели пикселя кампании): тоже обращения.
LEAD = "lead"


def lead_count(conversions: Mapping[str, int], goal_ids: Iterable[int]) -> int:
    """Обращения строки статистики: конверсии по целям бизнеса из Метрики и обращения площадки без целей Метрики."""
    keys = {str(goal) for goal in goal_ids} | {LEAD}
    return sum(value for key, value in conversions.items() if key in keys)


def _money(value: int, name: str) -> None:
    if type(value) is not int or value < 0:
        raise ValueError(f"{name}: ожидается неотрицательное целое число копеек.")


@dataclass(frozen=True)
class TargetSpec:
    id: str
    kind: Literal["phrase", "interest", "audience", "negative"]
    value: str
    status: str = "active"


@dataclass(frozen=True)
class AdSpec:
    id: str
    title: str
    text: str
    image_ref: str | None = None
    status: str = "active"
    medical: bool = False


@dataclass(frozen=True)
class AdGroupSpec:
    id: str
    name: str
    ads: tuple[AdSpec, ...]
    targets: tuple[TargetSpec, ...] = ()
    targeting: dict[str, Any] = field(default_factory=dict)
    status: str = "active"


@dataclass(frozen=True)
class CampaignTreeSpec:
    id: str
    name: str
    daily_limit_kop: int
    period_cap_kop: int
    groups: tuple[AdGroupSpec, ...]
    status: str = "active"

    @property
    def idempotency_key(self) -> str:
        return f"ap-{self.id}"

    def __post_init__(self) -> None:
        if not isinstance(self.id, str) or not self.id.strip():
            raise ValueError("Нужен внутренний id кампании для ключа ap-<id>.")
        _money(self.daily_limit_kop, "daily_limit_kop")
        _money(self.period_cap_kop, "period_cap_kop")
        for items in (
            self.groups,
            tuple(ad for group in self.groups for ad in group.ads),
            tuple(target for group in self.groups for target in group.targets),
        ):
            ids = [item.id for item in items]
            if any(not isinstance(id_, str) or not id_.strip() for id_ in ids) or len(set(ids)) != len(ids):
                raise ValueError("Внутренние id каждого вида сущностей должны быть непустыми и уникальными.")


@dataclass(frozen=True)
class ExternalRefs:
    campaign_id: str
    groups: dict[str, str]
    ads: dict[str, str]
    targets: dict[str, str]
    budget_context: dict[str, int] = field(default_factory=dict)

    def entity_ids(self) -> tuple[str, ...]:
        return (self.campaign_id, *self.groups.values(), *self.ads.values(), *self.targets.values())


@dataclass(frozen=True)
class Operation:
    kind: OperationKind
    entity_type: EntityType
    entity_id: str
    field: str
    before: Any
    after: Any
    invertible: bool = True
    budget_context: dict[str, int] = field(default_factory=dict)

    def __post_init__(self) -> None:
        fields = {
            "set_status": {"status"},
            "set_budget": {"daily_limit_kop", "period_cap_kop"},
            "update_ad_text": {"title", "text"},
            "swap_ad_image": {"image_ref"},
            "set_targeting": {"targeting"},
            "add_ad": {"creative"},
        }
        if self.kind not in fields or self.field not in fields[self.kind]:
            raise ValueError("Поле не соответствует виду операции.")
        entities = {
            "set_status": {"campaign", "group", "ad", "target"},
            "set_budget": {"campaign"},
            "update_ad_text": {"ad"},
            "swap_ad_image": {"ad"},
            "set_targeting": {"group"},
            "add_ad": {"ad"},
        }
        if self.entity_type not in entities[self.kind] or not self.entity_id:
            raise ValueError("Сущность не соответствует виду операции.")
        if self.kind == "add_ad":
            if self.before is not None or not isinstance(self.after, dict):
                raise ValueError("Добавление объявления требует before=None и описание варианта.")
            required = {"group_id", "title", "text", "image_ref"}
            if (
                set(self.after) != required
                or any(
                    not isinstance(self.after[name], str) or not self.after[name].strip()
                    for name in ("group_id", "title", "text")
                )
                or self.after["image_ref"] is not None
                and not isinstance(self.after["image_ref"], str)
            ):
                raise ValueError("Нужны группа, заголовок, текст и ссылка на картинку варианта.")
        for value in (self.before, self.after):
            if self.kind == "add_ad":
                continue
            if self.kind == "set_budget":
                _money(value, self.field)
            elif self.kind == "set_targeting":
                if not isinstance(value, dict):
                    raise ValueError("Таргетинг должен быть словарём.")
            elif value is None and self.kind == "swap_ad_image":
                continue
            elif not isinstance(value, str):
                raise ValueError("Значение операции должно быть строкой.")
        object.__setattr__(self, "before", deepcopy(self.before))
        object.__setattr__(self, "after", deepcopy(self.after))
        for name, value in self.budget_context.items():
            _money(value, name)
        object.__setattr__(self, "budget_context", deepcopy(self.budget_context))

    def inverse(self) -> "Operation":
        if self.kind == "add_ad":
            return Operation("set_status", "ad", self.entity_id, "status", "active", "paused_owner")
        if not self.invertible:
            raise ValueError("Операция необратима: требуется отдельная операция остановки/архивации.")
        return Operation(
            self.kind,
            self.entity_type,
            self.entity_id,
            self.field,
            self.after,
            self.before,
            budget_context=self.budget_context,
        )


@dataclass(frozen=True)
class EntityState:
    entity_id: str
    entity_type: EntityType
    parent_id: str | None
    name: str
    status: str
    daily_limit_kop: int | None = None
    period_cap_kop: int | None = None
    title: str | None = None
    text: str | None = None
    image_ref: str | None = None
    targeting: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        for name in ("daily_limit_kop", "period_cap_kop"):
            if (value := getattr(self, name)) is not None:
                _money(value, name)

    @property
    def creative(self) -> dict[str, Any]:
        """Снимок добавленного объявления для сверки операции add_ad."""
        return {"group_id": self.parent_id, "title": self.title, "text": self.text, "image_ref": self.image_ref}


@dataclass(frozen=True)
class StatRow:
    entity_id: str
    campaign_id: str
    date: date
    level: StatLevel
    impressions: int
    clicks: int
    cost_kop: int
    conversions: dict[str, int]
    fetched_at: datetime
    is_settled: bool

    def __post_init__(self) -> None:
        _money(self.cost_kop, "cost_kop")
        if any(type(n) is not int or n < 0 for n in (self.impressions, self.clicks, *self.conversions.values())):
            raise ValueError("Статистика должна содержать неотрицательные целые счётчики.")
        if self.clicks > self.impressions:
            raise ValueError("Кликов не может быть больше показов.")


@dataclass(frozen=True)
class ModerationInfo:
    entity_id: str
    status: Literal["pending", "approved", "rejected"]
    reason: str | None
    updated_at: datetime


@dataclass(frozen=True)
class PlatformLimits:
    remaining_units: int | None = None
    rate_limit_remaining: int | None = None
    resets_at: datetime | None = None


@dataclass(frozen=True)
class AuthSession:
    account_id: str
    access_token: str = field(repr=False)
    refresh_token: str | None = field(default=None, repr=False)
    expires_at: datetime | None = None


class PlatformError(Exception):
    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


class AuthExpired(PlatformError):
    def __init__(self) -> None:
        super().__init__("Авторизация площадки истекла. Подключите аккаунт заново.", status_code=401)


class RateLimited(PlatformError):
    def __init__(self, retry_after: int = 60) -> None:
        super().__init__("Превышен лимит запросов площадки.", status_code=429)
        self.retry_after = retry_after


class ModerationRejected(PlatformError):
    def __init__(self, entity_id: str, reason: str) -> None:
        super().__init__(reason, status_code=422)
        self.entity_id = entity_id
        self.reason = reason


@runtime_checkable
class AdPlatform(Protocol):
    def auth_url(self, state: str) -> str: ...

    async def exchange_code(self, code: str) -> AuthSession: ...

    async def refresh(self, account: AuthSession) -> AuthSession: ...

    async def whoami(self) -> dict[str, str]: ...

    async def create_campaign_tree(self, plan_part: CampaignTreeSpec) -> ExternalRefs: ...

    async def apply(self, operation: Operation) -> None:
        """Установить after; повтор безопасен. before хранится для отката, сверку делает вызывающий код."""
        ...

    async def upload_image(self, file: Path) -> str: ...

    async def upload_video(self, file: Path) -> str: ...

    async def get_state(self, refs: ExternalRefs) -> dict[str, EntityState]: ...

    async def get_moderation(self, refs: ExternalRefs) -> list[ModerationInfo]: ...

    async def get_stats(self, date_from: date, date_to: date, level: StatLevel = "ad") -> list[StatRow]:
        """Дневные строки за включительный интервал; конверсии отнесены к дате клика."""
        ...

    async def get_spend_today(self) -> int: ...

    async def get_balance_hint(self) -> int | None: ...

    async def set_marking(self, profile: Mapping[str, str]) -> None: ...

    def marking_instruction(self) -> str | None: ...

    async def limits(self) -> PlatformLimits: ...

    async def min_budgets(self) -> dict[str, int]: ...

    async def geo_targeting(self, city: str) -> dict[str, Any]:
        """Таргетинг группы на город владельца; город не найден в регионах площадки — PlatformError."""
        ...
