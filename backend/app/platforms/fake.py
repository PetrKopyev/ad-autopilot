import asyncio
from collections import deque
from collections.abc import Collection, Mapping
from copy import deepcopy
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime, timedelta
from hashlib import sha256
from heapq import heappop, heappush
from math import isfinite
from pathlib import Path
from random import Random
from urllib.parse import urlencode

from app.platforms.base import (
    LEAD,
    AdGroupSpec,
    AdSpec,
    AuthExpired,
    AuthSession,
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
    TargetSpec,
    _money,
)
from app.worker.clock import MOSCOW, today


class VirtualClock:
    def __init__(self, start: datetime | None = None) -> None:
        start = start if start is not None else datetime(2026, 1, 1, tzinfo=UTC)
        if start.tzinfo is None or start.utcoffset() is None:
            raise ValueError("Виртуальным часам нужен часовой пояс.")
        self._now = start

    @property
    def now(self) -> datetime:
        return self._now

    def advance(self, *, hours: float = 0, days: float = 0) -> datetime:
        if any(not isfinite(value) or value < 0 for value in (hours, days)):
            raise ValueError("Время можно двигать только вперёд на конечное число часов/дней.")
        self._now += timedelta(hours=hours, days=days)
        return self._now


@dataclass(frozen=True)
class AdTruth:
    ctr: float = 0.04
    cpc_kop: int = 1500
    conversion_rate: float = 0.08
    impressions_per_hour: int = 600
    moderation_reason: str | None = None

    def __post_init__(self) -> None:
        if any(not isfinite(value) or not 0 <= value <= 1 for value in (self.ctr, self.conversion_rate)):
            raise ValueError("CTR и конверсия должны быть вероятностями от 0 до 1.")
        _money(self.cpc_kop, "cpc_kop")
        if not self.cpc_kop:
            raise ValueError("CPC должен быть положительным.")
        if type(self.impressions_per_hour) is not int or self.impressions_per_hour < 0:
            raise ValueError("Число показов в час должно быть неотрицательным целым.")


@dataclass(frozen=True)
class FailureScenario:
    rate_limited: float = 0
    server_error: float = 0
    auth_expired: float = 0

    def __post_init__(self) -> None:
        values = (self.rate_limited, self.server_error, self.auth_expired)
        if any(not isfinite(p) or not 0 <= p <= 1 for p in values) or sum(values) > 1:
            raise ValueError("Вероятности сбоев должны быть от 0 до 1, сумма — не больше 1.")


@dataclass(frozen=True)
class FakeScenario:
    plan: CampaignTreeSpec
    truths: dict[str, AdTruth]


def salon_scenario(
    id: str = "salon", *, daily_limit_kop: int = 100_000, period_cap_kop: int = 3_000_000
) -> FakeScenario:
    groups = []
    truths = {}
    for name, theme, truth in (
        ("strong", "Маникюр", AdTruth(0.08, 1000, 0.14)),
        ("weak", "Укладки", AdTruth(0.007, 2500, 0.01)),
        ("borderline", "Уход за волосами", AdTruth(0.035, 1800, 0.06)),
    ):
        ads = tuple(AdSpec(f"{name}-{i}", f"{theme} в салоне", f"Запишитесь на {theme.lower()}") for i in (1, 2))
        groups.append(
            AdGroupSpec(
                name,
                theme,
                ads,
                (TargetSpec(f"{name}-phrase", "phrase", theme.lower()),),
                {"city": "Самара"},
            )
        )
        truths.update({ad.id: truth for ad in ads})
    return FakeScenario(CampaignTreeSpec(id, "Салон", daily_limit_kop, period_cap_kop, tuple(groups)), truths)


# Демо-кабинет: группы Директа по порядку плана — сильная, слабая и пограничная, остальные — как по умолчанию.
# Те же числа берёт прогноз в демо, чтобы прогноз плана и цифры после запуска не расходились. Показов хватает, чтобы
# группы тратили свой дневной лимит, как на площадке: иначе бюджет недовыбирался из-за спроса демо, а не из-за правил.
# Клики дешёвые нарочно: с жизненной ценой 30–80 ₽ проверка вариантов по
# кликабельности при демо-бюджете не набрала бы выборку за срок демо (30–40 дней), и эксперименты не были бы видны
# приёмщику вообще. Это осознанный компромисс: цены демо обозначены в интерфейсе как выдуманные, живая экономика —
# забота боевых площадок. Цену клика поднимать только вместе с бюджетом демо и окном перемотки.
DEMO_TRUTHS = (AdTruth(0.08, 25, 0.2, 3000), AdTruth(0.007, 62, 0.01, 3000), AdTruth(0.035, 45, 0.06, 3000))


def demo_truths(count: int) -> list[AdTruth]:
    return [DEMO_TRUTHS[index] if index < len(DEMO_TRUTHS) else AdTruth() for index in range(count)]


@dataclass
class _Counters:
    impressions: int = 0
    clicks: int = 0
    cost_kop: int = 0
    conversions: int = 0


@dataclass
class _Review:
    info: ModerationInfo
    due: datetime
    rejection: str | None


class FakeAdapter:
    """Симуляция с минутным шагом; чтения и сбои API не меняют поток рекламных событий."""

    def __init__(
        self,
        *,
        clock: VirtualClock | None = None,
        seed: int = 0,
        evil: bool = False,
        scenario: FakeScenario | str = "salon",
        truths: Mapping[str, AdTruth] | None = None,
        conversion_delay: timedelta = timedelta(days=2),
        moderation_delay: timedelta = timedelta(hours=1),
        rejection_probability: float = 0,
        failures: FailureScenario | None = None,
        token_lifetime: timedelta | None = None,
    ) -> None:
        if conversion_delay < timedelta() or moderation_delay < timedelta():
            raise ValueError("Задержки не могут быть отрицательными.")
        if not isfinite(rejection_probability) or not 0 <= rejection_probability <= 1:
            raise ValueError("Вероятность отказа должна быть от 0 до 1.")
        if token_lifetime is not None and token_lifetime <= timedelta():
            raise ValueError("Срок авторизации должен быть положительным.")
        if isinstance(scenario, str):
            if scenario != "salon":
                raise ValueError("Неизвестный сценарий симулятора.")
            scenario = salon_scenario()
        self.clock = clock if clock is not None else VirtualClock()
        self.seed = seed
        self.evil = evil
        self.scenario = deepcopy(scenario)
        self.conversion_delay = conversion_delay
        self.moderation_delay = moderation_delay
        self.rejection_probability = rejection_probability
        self.failures = failures if failures is not None else FailureScenario()
        # Остаток на счёте кабинета; None — площадка его не сообщает.
        self.balance_kop: int | None = None
        self._truths = deepcopy(scenario.truths if truths is None else dict(truths))
        self._states: dict[str, EntityState] = {}
        self._trees: dict[str, ExternalRefs] = {}
        self._ads: dict[str, AdSpec] = {}
        self._ad_truths: dict[str, AdTruth] = {}
        self._campaigns: dict[str, str] = {}
        self._reviews: dict[str, _Review] = {}
        self._rngs: dict[str, Random] = {}
        self._review_rng = Random(f"{seed}:moderation")
        self._failure_rng = Random(f"{seed}:failures")
        self._injected: deque[int] = deque()
        self._stats: dict[tuple[str, date], _Counters] = {}
        self._spent: dict[str, int] = {}
        self._period_ceiling: dict[str, int] = {}
        self._spent_daily: dict[tuple[str, date], int] = {}
        self._pending: list[tuple[datetime, int, str, date]] = []
        self._sequence = 0
        self._last_tick = self.clock.now.replace(second=0, microsecond=0)
        self._expired = False
        self._token_lifetime = token_lifetime
        self._expires_at = self.clock.now + token_lifetime if token_lifetime is not None else None
        self._marking: dict[str, str] = {}

    def inject_error(self, status_code: int) -> None:
        if status_code not in (401, 429) and not 500 <= status_code <= 599:
            raise ValueError("Симулируются только 401, 429 и 5xx.")
        self._injected.append(status_code)

    def _raise_error(self, status_code: int) -> None:
        if status_code == 401:
            self._expired = True
            raise AuthExpired()
        if status_code == 429:
            raise RateLimited()
        raise PlatformError("Временная ошибка площадки.", status_code=status_code)

    def _request(self, *, check_auth: bool = True) -> None:
        self._sync()
        if check_auth and (self._expired or self._expires_at is not None and self.clock.now >= self._expires_at):
            raise AuthExpired()
        if self._injected:
            self._raise_error(self._injected.popleft())
        roll = self._failure_rng.random()
        if roll < self.failures.rate_limited:
            self._raise_error(429)
        if roll < self.failures.rate_limited + self.failures.server_error:
            self._raise_error(503)
        if check_auth and roll < sum(
            (self.failures.rate_limited, self.failures.server_error, self.failures.auth_expired)
        ):
            self._raise_error(401)

    def auth_url(self, state: str) -> str:
        return "https://fake.invalid/oauth?" + urlencode({"state": state})

    def _session(self) -> AuthSession:
        self._expired = False
        self._expires_at = self.clock.now + self._token_lifetime if self._token_lifetime is not None else None
        return AuthSession("fake-account", "fake-access", "fake-refresh", self._expires_at)

    async def exchange_code(self, code: str) -> AuthSession:
        self._request(check_auth=False)
        if not code:
            raise AuthExpired()
        return self._session()

    async def refresh(self, account: AuthSession) -> AuthSession:
        self._request(check_auth=False)
        if account.account_id != "fake-account" or not account.refresh_token:
            raise AuthExpired()
        return self._session()

    async def whoami(self) -> dict[str, str]:
        self._request()
        return {"id": "fake-account", "name": "Демо-кабинет"}

    def _external_id(self, key: str, kind: str, local_id: str) -> str:
        digest = sha256(f"{key}:{kind}:{local_id}".encode()).hexdigest()[:24]
        return f"fake-{kind}-{digest}"

    async def create_campaign_tree(self, plan_part: CampaignTreeSpec) -> ExternalRefs:
        self._request()
        for status in (
            plan_part.status,
            *(group.status for group in plan_part.groups),
            *(ad.status for group in plan_part.groups for ad in group.ads),
            *(target.status for group in plan_part.groups for target in group.targets),
        ):
            self._check_status(status)
        key = plan_part.idempotency_key
        if key in self._trees:
            return deepcopy(self._trees[key])
        campaign_id = self._external_id(key, "campaign", plan_part.id)
        refs = ExternalRefs(campaign_id, {}, {}, {})
        self._states[campaign_id] = EntityState(
            campaign_id,
            "campaign",
            None,
            f"{plan_part.name} {key}",
            plan_part.status,
            plan_part.daily_limit_kop,
            plan_part.period_cap_kop,
        )
        self._spent[campaign_id] = 0
        self._period_ceiling[campaign_id] = self._effective_cap(plan_part.period_cap_kop)
        for group in plan_part.groups:
            group_id = self._external_id(key, "group", group.id)
            refs.groups[group.id] = group_id
            self._states[group_id] = EntityState(
                group_id, "group", campaign_id, group.name, group.status, targeting=deepcopy(group.targeting)
            )
            for target in group.targets:
                target_id = self._external_id(key, "target", target.id)
                refs.targets[target.id] = target_id
                self._states[target_id] = EntityState(target_id, "target", group_id, target.value, target.status)
            for ad in group.ads:
                ad_id = self._external_id(key, "ad", ad.id)
                refs.ads[ad.id] = ad_id
                self._states[ad_id] = EntityState(
                    ad_id, "ad", group_id, ad.title, ad.status, title=ad.title, text=ad.text, image_ref=ad.image_ref
                )
                self._ads[ad_id] = ad
                self._ad_truths[ad_id] = self._truths.get(ad.id, AdTruth())
                self._campaigns[ad_id] = campaign_id
                self._rngs[ad_id] = Random(f"{self.seed}:{ad_id}")
                self._submit_review(ad_id)
        self._trees[key] = refs
        self._finish_reviews(self.clock.now)
        return deepcopy(refs)

    async def add_group(self, campaign_id: str, group: AdGroupSpec) -> ExternalRefs:
        """Новая группа в созданной кампании — проверка другой аудитории; повтор не создаёт вторую."""
        self._request()
        self._check_status(group.status)
        tree = next((item for item in self._trees.values() if item.campaign_id == campaign_id), None)
        if tree is None:
            raise PlatformError("Кампания не найдена.", status_code=404)
        if group.id not in tree.groups:
            group_id = self._external_id(campaign_id, "group", group.id)
            self._states[group_id] = EntityState(
                group_id, "group", campaign_id, group.name, group.status, targeting=deepcopy(group.targeting)
            )
            for target in group.targets:
                target_id = self._external_id(group_id, "target", target.id)
                tree.targets[target.id] = target_id
                self._states[target_id] = EntityState(target_id, "target", group_id, target.value, target.status)
            tree.groups[group.id] = group_id
        group_id = tree.groups[group.id]
        for ad in group.ads:
            content = {"group_id": group_id, "title": ad.title, "text": ad.text, "image_ref": ad.image_ref}
            await self.apply(Operation("add_ad", "ad", ad.id, "creative", None, content))
        return ExternalRefs(
            campaign_id,
            {group.id: group_id},
            {ad.id: tree.ads[ad.id] for ad in group.ads},
            {target.id: tree.targets[target.id] for target in group.targets},
        )

    def _submit_review(self, ad_id: str) -> None:
        state = self._states[ad_id]
        content = f"{state.title} {state.text}".casefold()
        medical = self._ads[ad_id].medical or any(
            word in content for word in ("медицин", "лечени", "инъекц", "ботокс", "диагностик")
        )
        reason = self._ad_truths[ad_id].moderation_reason
        if medical:
            reason = "Медицинская реклама: требуются документы и предупреждение о противопоказаниях."
        elif reason is None and self._review_rng.random() < self.rejection_probability:
            reason = "Объявление не соответствует правилам площадки."
        self._reviews[ad_id] = _Review(
            ModerationInfo(ad_id, "pending", None, self.clock.now), self.clock.now + self.moderation_delay, reason
        )

    def _finish_reviews(self, now: datetime) -> None:
        for ad_id, review in self._reviews.items():
            if review.info.status == "pending" and review.due <= now:
                review.info = ModerationInfo(
                    ad_id, "rejected" if review.rejection else "approved", review.rejection, review.due
                )

    async def apply(self, operation: Operation) -> None:
        self._request()
        if operation.kind == "add_ad":
            content = operation.after
            group = self._states.get(content["group_id"])
            if group is None or group.entity_type != "group":
                raise PlatformError("Группа не найдена.", status_code=404)
            tree = next(item for item in self._trees.values() if item.campaign_id == group.parent_id)
            if operation.entity_id in tree.ads:
                return
            ad_id = self._external_id(group.entity_id, "ad", operation.entity_id)
            ad = AdSpec(
                operation.entity_id,
                content["title"],
                content["text"],
                content["image_ref"],
                title2=content.get("title2") or "",
            )
            self._states[ad_id] = EntityState(
                ad_id,
                "ad",
                group.entity_id,
                ad.title,
                ad.status,
                title=ad.title,
                title2=ad.title2 or None,
                text=ad.text,
                image_ref=ad.image_ref,
            )
            self._ads[ad_id] = ad
            self._ad_truths[ad_id] = self._truths.get(ad.id, AdTruth())
            self._campaigns[ad_id] = group.parent_id
            self._rngs[ad_id] = Random(f"{self.seed}:{ad_id}")
            tree.ads[operation.entity_id] = ad_id
            self._submit_review(ad_id)
            self._finish_reviews(self.clock.now)
            return
        if operation.kind == "set_status":
            self._check_status(operation.after)
        state = self._states.get(operation.entity_id)
        if state is None or state.entity_type != operation.entity_type:
            raise PlatformError("Сущность не найдена.", status_code=404)
        if operation.kind == "set_status" and operation.after == "active" and state.entity_type == "ad":
            review = self._reviews[state.entity_id].info
            if review.status == "rejected":
                raise ModerationRejected(state.entity_id, review.reason or "Отклонено модерацией.")
        if getattr(state, operation.field) == operation.after:
            return
        if operation.kind == "set_budget" and operation.field == "period_cap_kop":
            spent = self._spent[state.entity_id]
            self._period_ceiling[state.entity_id] = spent + self._effective_cap(max(0, operation.after - spent))
        self._states[state.entity_id] = replace(state, **{operation.field: deepcopy(operation.after)})
        if operation.kind in ("update_ad_text", "swap_ad_image"):
            self._submit_review(state.entity_id)
            self._finish_reviews(self.clock.now)

    async def _upload(self, file: Path, kind: str) -> str:
        self._request()
        return f"fake-{kind}-{sha256(await asyncio.to_thread(file.read_bytes)).hexdigest()}"

    async def upload_image(self, file: Path) -> str:
        return await self._upload(file, "image")

    async def upload_video(self, file: Path) -> str:
        return await self._upload(file, "video")

    def _check_refs(self, refs: ExternalRefs) -> None:
        stored = next((tree for tree in self._trees.values() if tree.campaign_id == refs.campaign_id), None)
        if stored is None or not set(refs.entity_ids()) <= set(stored.entity_ids()):
            raise PlatformError("Дерево кампании не найдено.", status_code=404)

    async def get_state(self, refs: ExternalRefs) -> dict[str, EntityState]:
        self._request()
        tree = next((item for item in self._trees.values() if item.campaign_id == refs.campaign_id), None)
        if tree is None:
            raise PlatformError("Дерево кампании не найдено.", status_code=404)
        resolved = {id_: tree.ads.get(id_, id_) for id_ in refs.entity_ids()}
        unresolved = {value for key, value in refs.ads.items() if key == value and value not in self._states}
        self._check_refs(
            replace(
                refs,
                ads={
                    key: resolved[value]
                    for key, value in refs.ads.items()
                    if value not in unresolved or value in tree.ads
                },
            )
        )
        states = {key: self._states[value] for key, value in resolved.items() if value in self._states}
        states.update({state.entity_id: state for state in list(states.values())})
        return deepcopy(states)

    async def get_moderation(self, refs: ExternalRefs) -> list[ModerationInfo]:
        self._request()
        self._check_refs(refs)
        return [self._reviews[id_].info for id_ in refs.ads.values()]

    def _effective_cap(self, cap: int) -> int:
        return cap * 125 // 100 if self.evil else cap

    @staticmethod
    def _check_status(status: str) -> None:
        if status not in {"active", "paused_auto", "paused_owner", "archived"}:
            raise PlatformError("Неизвестный статус.")

    def _simulate_minute(self, tick: datetime) -> None:
        # Слот [tick - 1 минута, tick) относится к московскому дню начала слота, как сутки площадок.
        slot = tick - timedelta(minutes=1)
        day = slot.astimezone(MOSCOW).date()
        self._finish_reviews(slot)
        for ad_id, truth in self._ad_truths.items():
            ad = self._states[ad_id]
            group = self._states[ad.parent_id]
            campaign = self._states[group.parent_id]
            if any(state.status != "active" for state in (ad, group, campaign)):
                continue
            if self._reviews[ad_id].info.status != "approved" or self._reviews[ad_id].due > slot:
                continue
            daily_key = (campaign.entity_id, day)
            remaining = min(
                self._effective_cap(campaign.daily_limit_kop) - self._spent_daily.get(daily_key, 0),
                self._period_ceiling[campaign.entity_id] - self._spent[campaign.entity_id],
            )
            if remaining < truth.cpc_kop:
                continue
            rng = self._rngs[ad_id]
            impressions, fraction = divmod(truth.impressions_per_hour, 60)
            impressions += rng.random() < fraction / 60
            clicks = min(sum(rng.random() < truth.ctr for _ in range(impressions)), remaining // truth.cpc_kop)
            cost = clicks * truth.cpc_kop
            counters = self._stats.setdefault((ad_id, day), _Counters())
            counters.impressions += impressions
            counters.clicks += clicks
            counters.cost_kop += cost
            self._spent[campaign.entity_id] += cost
            self._spent_daily[daily_key] = self._spent_daily.get(daily_key, 0) + cost
            for _ in range(clicks):
                if rng.random() < truth.conversion_rate:
                    lag = timedelta(microseconds=rng.randrange(self.conversion_delay // timedelta(microseconds=1) + 1))
                    self._sequence += 1
                    heappush(self._pending, (tick + lag, self._sequence, ad_id, day))

    def _sync(self) -> None:
        tick = self._last_tick + timedelta(minutes=1)
        while tick <= self.clock.now:
            self._simulate_minute(tick)
            self._last_tick = tick
            tick += timedelta(minutes=1)
        self._finish_reviews(self.clock.now)
        while self._pending and self._pending[0][0] <= self.clock.now:
            _, _, ad_id, day = heappop(self._pending)
            self._stats[ad_id, day].conversions += 1

    async def get_stats(
        self,
        date_from: date,
        date_to: date,
        level: StatLevel = "ad",
        campaign_ids: Collection[str] | None = None,
    ) -> list[StatRow]:
        self._request()
        if date_from > date_to or level not in ("ad", "group", "campaign"):
            raise ValueError("Нужны включительный интервал дат и уровень ad/group/campaign.")
        own = None if campaign_ids is None else {str(id_) for id_ in campaign_ids}
        if own is not None and not own:
            return []
        entity_ids = {
            ad_id: ad_id if level == "ad" else self._states[ad_id].parent_id if level == "group" else campaign_id
            for ad_id, campaign_id in self._campaigns.items()
        }
        unsettled = {(entity_ids[ad_id], day) for _, _, ad_id, day in self._pending}
        aggregated: dict[tuple[str, str, date], _Counters] = {}
        for (ad_id, day), counters in self._stats.items():
            if not date_from <= day <= date_to:
                continue
            campaign_id = self._campaigns[ad_id]
            if own is not None and campaign_id not in own:
                continue
            target = aggregated.setdefault((entity_ids[ad_id], campaign_id, day), _Counters())
            target.impressions += counters.impressions
            target.clicks += counters.clicks
            target.cost_kop += counters.cost_kop
            target.conversions += counters.conversions
        return [
            StatRow(
                entity_id,
                campaign_id,
                day,
                level,
                counters.impressions,
                counters.clicks,
                counters.cost_kop,
                {LEAD: counters.conversions},
                self.clock.now,
                day <= today(self.clock) - timedelta(days=2) and (entity_id, day) not in unsettled,
            )
            for (entity_id, campaign_id, day), counters in sorted(aggregated.items())
        ]

    async def get_spend_today(self, campaign_ids: Collection[str] | None = None) -> int:
        self._request()
        own = None if campaign_ids is None else {str(id_) for id_ in campaign_ids}
        return sum(
            cost
            for (ad_id, day), cost in self._spent_daily.items()
            if day == today(self.clock) and (own is None or self._campaigns.get(ad_id) in own)
        )

    async def get_balance_hint(self) -> int | None:
        self._request()
        return self.balance_kop

    async def set_marking(self, profile: Mapping[str, str]) -> None:
        self._request()
        self._marking = deepcopy(dict(profile))

    def marking_instruction(self) -> str | None:
        return None

    async def limits(self) -> PlatformLimits:
        self._request()
        return PlatformLimits()

    async def min_budgets(self) -> dict[str, int]:
        self._request()
        return {"day": 0, "period": 0}

    async def geo_targeting(self, city: str) -> dict[str, str]:
        return {"city": city}
