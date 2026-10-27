from dataclasses import dataclass, replace
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

from app.db.models import (
    Ad,
    Asset,
    BudgetLedger,
    Business,
    Campaign,
    ChangeLog,
    MarkingProfile,
    Plan,
    PlatformAccount,
)
from app.platforms.base import EntityState, ExternalRefs, PlatformError
from app.platforms.fake import FakeAdapter, VirtualClock, salon_scenario
from app.services.launch import LaunchService
from app.services.plan import inputs_hash
from app.worker.jobs import MemoryWorkerRepository, Worker
from tests.services.test_onboarding import MemoryRepository

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
    scenario = salon_scenario("salon")
    truths = {ad_id: replace(truth, impressions_per_hour=100) for ad_id, truth in scenario.truths.items()}
    adapter = adapter_class(clock=clock, seed=seed, evil=evil, scenario=scenario, truths=truths)
    launch_repository = MemoryRepository()
    business = Business(
        id=1,
        user_id=1,
        site_url="https://salon.example",
        city="Самара",
        medical_level="none",
        month_budget_kop=budget,
        auto_continue=auto_continue,
        status="plan_ready",
        metrica_counter_id=12345,
        lead_goal_ids=[1],
        description_approved_at=START,
        settings={},
    )
    launch_repository.put(business)
    launch_repository.put(PlatformAccount(business_id=1, platform="direct", status="ok"))
    launch_repository.put(
        MarkingProfile(
            business_id=1,
            subject_type="ip",
            inn_enc="526317984689",
            name="ИП Иванов",
            phone_enc="+79001231122",
            confirmed_in_platform={"direct": True},
        )
    )
    asset = Asset(business_id=1, kind="upload", selected=True, platform_refs={"direct": "fake-image"})
    launch_repository.put(asset)
    plan = Plan(
        business_id=1,
        version=1,
        status="approved",
        payload={
            "platforms": ["direct"],
            "inputs_hash": inputs_hash(business, ["direct"]),
            "groups": [
                {
                    "id": group.id,
                    "platform": "direct",
                    "name": group.name,
                    "segment": group.id,
                    "phrases": [target.value for target in group.targets],
                    "ads": [{"title": ad.title, "text": ad.text} for ad in group.ads],
                }
                for group in scenario.plan.groups
            ],
            "assets": {group.id: [asset.id] for group in scenario.plan.groups},
        },
    )
    launch_repository.put(plan)
    await LaunchService(launch_repository, lambda *_: adapter, Path("/unused")).launch(
        business.id, plan.id, True, START, START.date()
    )
    business.settings = business.settings | (business_settings or {})
    campaign = launch_repository.campaign(plan.payload["launch"]["campaigns"]["direct"])
    repository = MemoryWorkerRepository()
    repository.tables = launch_repository.tables
    groups, local_ads, targets = launch_repository.tree_rows(campaign.id)
    refs = ExternalRefs(campaign.external_id, {}, {}, {})
    ads = {}
    for spec, group in zip(scenario.plan.groups, groups, strict=True):
        refs.groups[spec.id] = group.external_id
        own_ads = [ad for ad in local_ads if ad.ad_group_id == group.id]
        for ad_spec, ad in zip(spec.ads, own_ads, strict=True):
            refs.ads[ad_spec.id] = ad.external_id
            ads[ad_spec.id] = ad
            adapter._ad_truths[ad.external_id] = truths[ad_spec.id]
        for target_spec, target in zip(spec.targets, (t for t in targets if t.ad_group_id == group.id), strict=True):
            refs.targets[target_spec.id] = target.external_id
    # История запуска остаётся в общем журнале. На старте ещё нет снимка расхода.
    for ledger in repository.rows(BudgetLedger):
        ledger.spent_kop = 0
    return World(clock, adapter, repository, refs, business, campaign, ads)
