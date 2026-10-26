from dataclasses import dataclass, replace
from datetime import UTC, date, datetime, timedelta

from app.db.models import Ad, AdGroup, BudgetLedger, Business, Campaign, ChangeLog, Target
from app.platforms.base import EntityState, ExternalRefs, PlatformError
from app.platforms.fake import FakeAdapter, VirtualClock, salon_scenario
from app.worker.jobs import MemoryWorkerRepository, Worker

START = datetime(2026, 1, 1, tzinfo=UTC)
BUDGET_KOP = 10_000_000


class Crash(BaseException):
    """Имитация смерти процесса: не перехватывается обработчиками Exception."""


class CrashingAdapter(FakeAdapter):
    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.crash: str | None = None
        self.crash_after = 1

    async def apply(self, operation) -> None:
        if self.crash == "before_apply":
            self.crash = None
            raise Crash
        await super().apply(operation)
        if self.crash == "after_apply":
            self.crash_after -= 1
            if not self.crash_after:
                self.crash = None
                raise Crash


class FlakyReadAdapter(FakeAdapter):
    """Запись проходит, а чтение для сверки сразу после неё недоступно."""

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.fail_read_after_apply = False
        self._armed = False

    async def apply(self, operation) -> None:
        await super().apply(operation)
        self._armed = self.fail_read_after_apply

    async def get_state(self, refs) -> dict[str, EntityState]:
        if self._armed:
            self._armed = False
            raise PlatformError("Площадка недоступна.", status_code=503)
        return await super().get_state(refs)


@dataclass
class World:
    clock: VirtualClock
    adapter: FakeAdapter
    repository: MemoryWorkerRepository
    refs: ExternalRefs
    business: Business
    campaign: Campaign
    ads: dict[str, Ad]

    def worker(self, **options) -> Worker:
        options.setdefault("dry_run", False)
        return Worker(self.repository, lambda business_id, platform: self.adapter, clock=self.clock, **options)

    async def advance(self, worker: Worker, *, days: float = 0, hours: float = 0, step_minutes: int = 30) -> None:
        """Часы идут по 30 минут; на каждом шаге воркер делает всё, что назначено на это время."""
        end = self.clock.now + timedelta(days=days, hours=hours)
        while self.clock.now < end:
            self.clock.advance(hours=step_minutes / 60)
            await worker.run_due()

    def platform_spend(self) -> int:
        return self.adapter._spent[self.refs.campaign_id]

    def spend_in_month(self, month: date) -> int:
        return sum(
            cost
            for (campaign, day), cost in self.adapter._spent_daily.items()
            if campaign == self.refs.campaign_id and day.replace(day=1) == month
        )

    def ledger(self) -> list[BudgetLedger]:
        return self.repository.rows(BudgetLedger, self.business.id)

    def changes(self, **keys) -> list[ChangeLog]:
        return [
            item
            for item in self.repository.rows(ChangeLog, self.business.id)
            if all(getattr(item, name) == value for name, value in keys.items())
        ]


async def make_world(
    *,
    evil: bool = False,
    adapter_class: type[FakeAdapter] = FakeAdapter,
    budget: int = BUDGET_KOP,
    business_settings: dict | None = None,
    auto_continue: bool = True,
    seed: int = 7,
) -> World:
    clock = VirtualClock(START)
    # Стартовые лимиты кампании уже учитывают запас: период × 1,25 не больше 92% бюджета.
    scenario = salon_scenario("salon", daily_limit_kop=int(budget * 0.03), period_cap_kop=int(budget * 0.73))
    # Сотня показов в час на объявление: данных набирается достаточно, а бюджет остаётся основным ограничением.
    truths = {ad_id: replace(truth, impressions_per_hour=100) for ad_id, truth in scenario.truths.items()}
    adapter = adapter_class(clock=clock, seed=seed, evil=evil, scenario=scenario, truths=truths)
    refs = await adapter.create_campaign_tree(scenario.plan)
    repository = MemoryWorkerRepository()
    business = Business(
        id=1,
        user_id=1,
        site_url="https://salon.example",
        city="Самара",
        medical_level="none",
        month_budget_kop=budget,
        auto_continue=auto_continue,
        status="running",
        lead_goal_ids=[],
        settings=business_settings or {},
    )
    repository.save(business)
    campaign = Campaign(
        business_id=1,
        platform_account_id=1,
        platform="fake",
        external_id=refs.campaign_id,
        type="unified",
        name="Салон",
        status="active",
        daily_limit_kop=scenario.plan.daily_limit_kop,
        period_cap_kop=scenario.plan.period_cap_kop,
        owner_paused=False,
    )
    repository.save(campaign)
    ads = {}
    for group_spec in scenario.plan.groups:
        group = AdGroup(
            campaign_id=campaign.id,
            external_id=refs.groups[group_spec.id],
            theme=group_spec.name,
            geo={},
            status="active",
        )
        repository.save(group)
        for target in group_spec.targets:
            repository.save(
                Target(
                    ad_group_id=group.id,
                    kind=target.kind,
                    value=target.value,
                    external_id=refs.targets[target.id],
                    status="active",
                )
            )
        for ad_spec in group_spec.ads:
            ad = Ad(
                ad_group_id=group.id,
                external_id=refs.ads[ad_spec.id],
                title=ad_spec.title,
                text=ad_spec.text,
                status="active",
                role="base",
                frozen_until=None,
            )
            repository.save(ad)
            ads[ad_spec.id] = ad
    return World(clock, adapter, repository, refs, business, campaign, ads)
