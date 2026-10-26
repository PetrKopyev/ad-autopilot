from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from enum import StrEnum


def nonnegative_int(value: int, name: str) -> None:
    if type(value) is not int or value < 0:
        raise ValueError(f"{name} must be a nonnegative int")


@dataclass(frozen=True)
class Metrics:
    spend: int = 0
    impressions: int = 0
    clicks: int = 0
    leads: int = 0

    def __post_init__(self) -> None:
        for name in ("spend", "impressions", "clicks", "leads"):
            nonnegative_int(getattr(self, name), name)
        if self.leads > self.clicks or self.clicks > self.impressions:
            raise ValueError("Expected leads <= clicks <= impressions")

    @property
    def ctr(self) -> Decimal:
        return Decimal(self.clicks) / self.impressions if self.impressions else Decimal(0)

    @property
    def cpl(self) -> int | None:
        return self.spend // self.leads if self.leads else None


@dataclass(frozen=True)
class DaySpend:
    day: date
    spend: int = 0
    impressions: int = 0
    clicks: int = 0
    leads: int = 0

    def __post_init__(self) -> None:
        Metrics(self.spend, self.impressions, self.clicks, self.leads)


def total_metrics(days: tuple[DaySpend, ...]) -> Metrics:
    return Metrics(*(sum(getattr(day, name) for day in days) for name in ("spend", "impressions", "clicks", "leads")))


@dataclass(frozen=True)
class AdSnapshot:
    id: str
    group_id: str
    platform: str
    approved_at: datetime | None
    days: tuple[DaySpend, ...] = ()
    active: bool = True
    last_changed_at: datetime | None = None
    frozen_until: datetime | None = None
    owner_edited_at: datetime | None = None
    paused_by_autopilot_at: datetime | None = None
    lifetime_impressions: int = 0

    def __post_init__(self) -> None:
        nonnegative_int(self.lifetime_impressions, "lifetime_impressions")
        if len({day.day for day in self.days}) != len(self.days):
            raise ValueError("Ad days must be unique (upsert before taking a snapshot)")


@dataclass(frozen=True)
class GroupSnapshot:
    id: str
    platform: str
    daily_limit: int
    active: bool = True
    last_changed_at: datetime | None = None
    frozen_until: datetime | None = None
    owner_edited_at: datetime | None = None

    def __post_init__(self) -> None:
        nonnegative_int(self.daily_limit, "daily_limit")


@dataclass(frozen=True)
class ClientAnswer:
    week_end: date
    leads: int
    clients: int | None
    spend: int = 0

    def __post_init__(self) -> None:
        nonnegative_int(self.leads, "leads")
        nonnegative_int(self.spend, "spend")
        if self.clients is not None:
            nonnegative_int(self.clients, "clients")
            if self.clients > self.leads:
                raise ValueError("Clients cannot exceed leads")


class Classification(StrEnum):
    GOOD = "good"
    BAD = "bad"
    BORDERLINE = "borderline"
    LOW_CTR = "low_ctr"
    NOT_SHOWING = "not_showing"
    INSUFFICIENT = "insufficient"


class Action(StrEnum):
    PAUSE = "pause"
    SCALE = "scale"
    NOTIFY = "notify"
    START_TEST = "start_test"


@dataclass(frozen=True)
class Assessment:
    classification: Classification
    metrics: Metrics
    reason_code: str | None = None


@dataclass(frozen=True)
class Decision:
    target_id: str
    action: Action
    reason_code: str
    entity: str = "ad"
    new_daily_limit: int | None = None
    numbers: dict[str, int | str | Decimal] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.new_daily_limit is not None:
            nonnegative_int(self.new_daily_limit, "new_daily_limit")


@dataclass(frozen=True)
class PlatformLimit:
    platform: str
    remaining_cap: int
    daily_limit: int
    weekly_limit: int

    def __post_init__(self) -> None:
        for name in ("remaining_cap", "daily_limit", "weekly_limit"):
            nonnegative_int(getattr(self, name), name)
        if max(self.daily_limit, self.weekly_limit) > self.remaining_cap:
            raise ValueError("Daily and weekly limits cannot exceed the remaining cap")


@dataclass(frozen=True)
class GroupLimit:
    group_id: str
    platform: str
    remaining_cap: int
    daily_limit: int
    weekly_limit: int

    def __post_init__(self) -> None:
        PlatformLimit(self.platform, self.remaining_cap, self.daily_limit, self.weekly_limit)


@dataclass(frozen=True)
class BudgetPlan:
    authorized: int
    spent: int
    remaining: int
    limits: tuple[PlatformLimit, ...]

    def __post_init__(self) -> None:
        for name in ("authorized", "spent", "remaining"):
            nonnegative_int(getattr(self, name), name)
        if self.remaining != max(0, self.authorized - self.spent):
            raise ValueError("Remaining budget must account for all spend")
        if sum(limit.remaining_cap for limit in self.limits) > self.remaining:
            raise ValueError("Platform caps exceed the remaining budget")


@dataclass(frozen=True)
class GuardResult:
    stop: bool
    reason_code: str | None
    stop_margin: int
    spent: int


class Verdict(StrEnum):
    RUNNING = "running"
    VARIANT = "variant"
    CONTROL = "control"
    NO_DIFFERENCE = "no_difference"
    EXTEND = "extend"
    CLICKBAIT = "clickbait"


@dataclass(frozen=True)
class ExperimentDesign:
    n_per_arm: int
    estimated_days: int
    daily_budget: int
    hypothesis: str
    accepted: bool
    reason_code: str


@dataclass(frozen=True)
class ExperimentResult:
    verdict: Verdict
    reason_code: str
    p_value: float | None = None
    relative_lift: float | None = None
    guardrail_p_value: float | None = None
