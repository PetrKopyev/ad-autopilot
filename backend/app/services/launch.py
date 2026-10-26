from datetime import date, datetime
from pathlib import Path

from sqlalchemy import select

from app.content.images import DEFAULT_FONT, ImageRejected, make_formats
from app.db.models import Ad, AdGroup, Asset, BudgetLedger, Business, Campaign, Plan, Target
from app.demo_flags import demo_enabled
from app.domain.settings import DomainSettings
from app.domain.types import PlatformLimit
from app.platforms.base import (
    AdGroupSpec,
    AdPlatform,
    AdSpec,
    CampaignTreeSpec,
    ExternalRefs,
    Operation,
    PlatformError,
    TargetSpec,
)
from app.platforms.fake import FakeAdapter
from app.services.changes import AdapterResolver, ChangeConflict, ChangesService
from app.services.demo import configure_adapter
from app.services.plan import IMAGE_KINDS, PlanRepository, budget_limits, launch_blockers

TITLES = {"direct": "Яндекс Директ", "vk": "ВКонтакте"}
CAMPAIGN_TYPES = {"direct": "unified", "vk": "vk_plan"}
IMAGE_FORMATS = {"direct": "direct_1_1", "vk": "vk_square"}


class LaunchBlocked(Exception):
    def __init__(self, reasons: list[str]) -> None:
        super().__init__(reasons[0])
        self.reasons = reasons


class LaunchFailed(Exception):
    pass


class LaunchRepository(PlanRepository):
    def stage(self, *items: object) -> None:
        self.db.add_all(items)
        self.db.flush()

    def campaign(self, campaign_id: int) -> Campaign | None:
        return self.db.get(Campaign, campaign_id)

    def tree_rows(self, campaign_id: int) -> tuple[list[AdGroup], list[Ad], list[Target]]:
        groups = list(self.db.scalars(select(AdGroup).where(AdGroup.campaign_id == campaign_id).order_by(AdGroup.id)))
        ids = [group.id for group in groups]
        ads = list(self.db.scalars(select(Ad).where(Ad.ad_group_id.in_(ids)).order_by(Ad.id)))
        targets = list(self.db.scalars(select(Target).where(Target.ad_group_id.in_(ids)).order_by(Target.id)))
        return groups, ads, targets

    def save_ledger(self, ledger: BudgetLedger) -> None:
        self.db.merge(ledger)


class LaunchService:
    def __init__(
        self,
        repository: LaunchRepository,
        resolve_adapter: AdapterResolver,
        media_root: Path,
        *,
        changes: ChangesService | None = None,
        domain: DomainSettings | None = None,
        font_path: str = DEFAULT_FONT,
    ) -> None:
        self.repository = repository
        self.resolve_adapter = resolve_adapter
        self.media_root = media_root
        self.changes = changes or ChangesService(repository, dry_run=False)
        if domain is None:
            from app.config import get_domain_settings

            domain = get_domain_settings()
        self.domain = domain
        self.font_path = font_path

    async def launch(
        self, business_id: int, plan_id: int, medical_acknowledged: bool, now: datetime, today: date
    ) -> dict:
        business = self.repository.business(business_id)
        plan = self.repository.get_plan(plan_id, business_id)
        if plan is None:
            raise LookupError("План не найден.")
        if plan.status == "launched":
            return self.result(plan)
        reasons = launch_blockers(
            business,
            plan,
            self.repository.accounts(business_id),
            self.repository.marking(business_id),
            self.repository.assets(business_id),
            medical_acknowledged,
        )
        if reasons:
            raise LaunchBlocked(reasons)
        platforms = plan.payload["platforms"]
        limits = budget_limits(business, platforms, today, self.domain)
        adapters = await self.check_platforms(business, platforms, limits)
        entered = False
        try:
            with self.repository.business_lock(business_id):
                entered = True
                refs = await self.create_paused(business, plan, platforms, limits, adapters)
            await self.activate(business, plan, refs, adapters, limits, now)
        except Exception as error:
            if isinstance(error, ChangeConflict) and not entered:
                raise
            await self.stop_created(plan, adapters)
            self.save_launch(plan, error=self.explain(error))
            raise LaunchFailed(self.explain(error)) from error
        self.finish(business, plan, platforms, limits, now, today)
        return self.result(plan)

    async def check_platforms(
        self, business: Business, platforms: list[str], limits: dict[str, PlatformLimit]
    ) -> dict[str, AdPlatform]:
        adapters = {}
        for platform in platforms:
            try:
                adapters[platform] = self.resolve_adapter(business.id, platform)
                minimum = await adapters[platform].min_budgets()
            except PlatformError as error:
                raise LaunchBlocked([f"{TITLES[platform]}: {error}"]) from error
            if limits[platform].daily_limit < minimum.get("day", 0) or limits[platform].remaining_cap < minimum.get(
                "period", 0
            ):
                raise LaunchBlocked([f"Бюджета не хватает для запуска на площадке «{TITLES[platform]}»."])
            if not limits[platform].daily_limit:
                raise LaunchBlocked(["На сегодня бюджет месяца почти исчерпан: запустите рекламу с начала дня."])
        return adapters

    def explain(self, error: Exception) -> str:
        reason = str(error) if isinstance(error, PlatformError | ChangeConflict) else "Непредвиденная ошибка."
        return (
            f"Запуск не удался: {reason} Всё, что уже создано, остановлено и ничего не тратит. "
            "Исправьте причину и нажмите «Запустить рекламу» ещё раз: повтор безопасен."
        )

    def save_launch(self, plan: Plan, **values: object) -> None:
        payload = dict(plan.payload)
        payload["launch"] = payload.get("launch", {"campaigns": {}}) | values
        plan.payload = payload
        if "error" in values:
            plan.status = "failed"
        self.repository.commit()

    async def create_paused(
        self,
        business: Business,
        plan: Plan,
        platforms: list[str],
        limits: dict[str, PlatformLimit],
        adapters: dict[str, AdPlatform],
    ) -> dict[str, tuple[Campaign, ExternalRefs]]:
        """Кампании создаются остановленными, но уже с лимитами: сбой на любом шаге не оставляет трат."""
        created = {}
        accounts = {account.platform: account for account in self.repository.accounts(business.id)}
        assets = {asset.id: asset for asset in self.repository.assets(business.id)}
        known = plan.payload.get("launch", {}).get("campaigns", {})
        for platform in platforms:
            adapter = adapters[platform]
            groups = [group for group in plan.payload["groups"] if group["platform"] == platform]
            images = await self.upload_images(adapter, platform, groups, plan, assets)
            campaign = self.repository.campaign(known[platform]) if platform in known else None
            if campaign is None:
                campaign = self.stage_rows(business, plan, platform, limits[platform], accounts[platform].id, groups)
                self.save_launch(
                    plan, campaigns=plan.payload.get("launch", {}).get("campaigns", {}) | {platform: campaign.id}
                )
            spec = self.spec(business, campaign, groups, images)
            if demo_enabled() and isinstance(adapter, FakeAdapter):
                configure_adapter(adapter, spec)
            refs = await adapter.create_campaign_tree(spec)
            self.bind(campaign, refs)
            created[platform] = (campaign, refs)
            self.repository.commit()
        return created

    async def upload_images(
        self, adapter: AdPlatform, platform: str, groups: list[dict], plan: Plan, assets: dict[int, Asset]
    ) -> dict[str, list[str]]:
        """Картинки с оффером нарезаются под площадку и загружаются до создания кампании; ссылка кэшируется."""
        refs: dict[str, list[str]] = {}
        for group in groups:
            chosen = [
                assets[id_]
                for id_ in plan.payload["assets"].get(group["segment"], ())
                if id_ in assets and assets[id_].selected and assets[id_].kind in IMAGE_KINDS
            ]
            refs[str(group["id"])] = [await self.upload_one(adapter, platform, asset, group) for asset in chosen]
        return refs

    async def upload_one(self, adapter: AdPlatform, platform: str, asset: Asset, group: dict) -> str:
        if platform in asset.platform_refs:
            return asset.platform_refs[platform]
        path = self.media_root / asset.file_path
        name = IMAGE_FORMATS[platform]
        try:
            data = make_formats(path.read_bytes(), offer=group["ads"][0]["title"], font_path=self.font_path)[name]
        except ImageRejected:
            data = make_formats(path.read_bytes(), font_path=self.font_path)[name]
        target = path.with_name(f"{path.stem}-{name}.jpg")
        target.write_bytes(data)
        ref = await adapter.upload_image(target)
        asset.platform_refs = asset.platform_refs | {platform: ref}
        self.repository.commit()
        return ref

    def stage_rows(
        self,
        business: Business,
        plan: Plan,
        platform: str,
        limit: PlatformLimit,
        account_id: int,
        groups: list[dict],
    ) -> Campaign:
        campaign = Campaign(
            business_id=business.id,
            platform_account_id=account_id,
            platform=platform,
            type=CAMPAIGN_TYPES[platform],
            name=f"Автопилот: {business.city}",
            status="paused_auto",
            daily_limit_kop=limit.daily_limit,
            period_cap_kop=limit.remaining_cap,
        )
        self.repository.stage(campaign)
        for group in groups:
            row = AdGroup(campaign_id=campaign.id, theme=group["name"], geo={"city": business.city}, status="active")
            self.repository.stage(row)
            self.repository.stage(
                *(
                    Target(ad_group_id=row.id, kind="phrase", value=phrase, status="active")
                    for phrase in group["phrases"]
                )
            )
            self.repository.stage(
                *(
                    Ad(
                        ad_group_id=row.id,
                        title=ad["title"],
                        text=ad["text"],
                        display_url=business.site_url,
                        status="draft",
                    )
                    for ad in group["ads"]
                )
            )
        self.repository.commit()
        return campaign

    def spec(
        self, business: Business, campaign: Campaign, plan_groups: list[dict], images: dict[str, list[str]]
    ) -> CampaignTreeSpec:
        """Внутренние id в дереве — id строк БД: по ним воркер собирает ExternalRefs."""
        groups, ads, targets = self.repository.tree_rows(campaign.id)
        by_name = {group["name"]: group for group in plan_groups}
        return CampaignTreeSpec(
            str(campaign.id),
            campaign.name,
            campaign.daily_limit_kop,
            campaign.period_cap_kop,
            tuple(
                AdGroupSpec(
                    str(group.id),
                    group.theme,
                    tuple(
                        AdSpec(
                            str(ad.id),
                            ad.title,
                            ad.text,
                            self.pick(images[str(by_name[group.theme]["id"])], position),
                            medical=business.medical_level != "none",
                        )
                        for position, ad in enumerate(item for item in ads if item.ad_group_id == group.id)
                    ),
                    tuple(
                        TargetSpec(str(target.id), "phrase", target.value)
                        for target in targets
                        if target.ad_group_id == group.id
                    ),
                    {"city": business.city},
                )
                for group in groups
            ),
            "paused_auto",
        )

    @staticmethod
    def pick(refs: list[str], position: int) -> str | None:
        return refs[position % len(refs)] if refs else None

    def bind(self, campaign: Campaign, refs: ExternalRefs) -> None:
        groups, ads, targets = self.repository.tree_rows(campaign.id)
        campaign.external_id = refs.campaign_id
        for rows, mapping in ((groups, refs.groups), (ads, refs.ads), (targets, refs.targets)):
            for row in rows:
                row.external_id = mapping[str(row.id)]

    async def activate(
        self,
        business: Business,
        plan: Plan,
        created: dict[str, tuple[Campaign, ExternalRefs]],
        adapters: dict[str, AdPlatform],
        limits: dict[str, PlatformLimit],
        now: datetime,
    ) -> None:
        for platform, (campaign, refs) in created.items():
            operations = [
                Operation("set_status", "ad", external_id, "status", "paused_auto", "active")
                for external_id in refs.ads.values()
            ] + [Operation("set_status", "campaign", refs.campaign_id, "status", "paused_auto", "active")]
            change = await self.changes.apply_change(
                business_id=business.id,
                adapter=adapters[platform],
                refs=refs,
                operations=operations,
                actor="owner",
                kind="launch",
                platform=platform,
                reason_code="LAUNCH",
                reason_params={"budget": business.month_budget_kop},
                evidence={
                    "plan_id": plan.id,
                    "daily_limit_kop": limits[platform].daily_limit,
                    "period_cap_kop": limits[platform].remaining_cap,
                },
                entity_type="campaign",
                entity_id=refs.campaign_id,
                now=now,
            )
            if change.status != "applied":
                detail = change.evidence["_service"].get("error")
                raise PlatformError(f"Площадка «{TITLES[platform]}» не включила рекламу. {detail or ''}".strip())

    async def stop_created(self, plan: Plan, adapters: dict[str, AdPlatform]) -> None:
        for platform, campaign_id in plan.payload.get("launch", {}).get("campaigns", {}).items():
            campaign = self.repository.campaign(campaign_id)
            if campaign is None or not campaign.external_id:
                continue
            try:
                await adapters[platform].apply(
                    Operation("set_status", "campaign", campaign.external_id, "status", "active", "paused_auto")
                )
            except PlatformError:
                continue
            campaign.status = "paused_auto"
        self.repository.commit()

    def finish(
        self,
        business: Business,
        plan: Plan,
        platforms: list[str],
        limits: dict[str, PlatformLimit],
        now: datetime,
        today: date,
    ) -> None:
        for platform in platforms:
            campaign = self.repository.campaign(plan.payload["launch"]["campaigns"][platform])
            campaign.status = "active"
            groups, ads, _ = self.repository.tree_rows(campaign.id)
            for row in groups:
                row.status = "active"
            for ad in ads:
                ad.status = "moderation"
            self.repository.save_ledger(
                BudgetLedger(
                    business_id=business.id,
                    month=today.replace(day=1),
                    platform=platform,
                    allocated_kop=limits[platform].remaining_cap,
                    period_cap_kop=limits[platform].remaining_cap,
                )
            )
        plan.status = "launched"
        plan.payload = plan.payload | {
            "launch": plan.payload["launch"] | {"launched_at": now.isoformat(), "error": None}
        }
        business.settings = business.settings | {
            "_worker": business.settings.get("_worker", {}) | {"started_on": today.isoformat()}
        }
        business.status = "running"
        self.repository.commit()

    def result(self, plan: Plan) -> dict:
        launch = plan.payload["launch"]
        return {
            "status": "launched",
            "plan_id": plan.id,
            "launched_at": launch.get("launched_at"),
            "campaigns": launch["campaigns"],
        }
