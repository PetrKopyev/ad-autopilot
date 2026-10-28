from __future__ import annotations

from contextlib import ExitStack
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING

from sqlalchemy import select

from app.content.images import DEFAULT_FONT, ImageRejected, make_formats, run_decoding
from app.db.models import (
    Ad,
    AdGroup,
    Asset,
    BudgetLedger,
    Business,
    Campaign,
    Plan,
    PlatformAccount,
    Target,
    WorkerRun,
)
from app.demo_flags import demo_enabled
from app.domain.explain import rubles
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
from app.platforms.direct import budget_end, weekly_limit
from app.platforms.fake import FakeAdapter
from app.services.budget_guard import STOPPED
from app.services.changes import CLOSED_ACCOUNT, AdapterResolver, ChangeConflict, ChangesService, ServerBusy
from app.services.demo import configure_adapter
from app.services.onboarding import DRAFT_STATUSES, NotFound
from app.services.plan import IMAGE_KINDS, PlanRepository, budget_limits, launch_blockers, launch_unsettled
from app.worker.jobs import NoticesRepository

if TYPE_CHECKING:
    from app.worker.jobs import Worker

TITLES = {"direct": "Яндекс Директ", "vk": "ВКонтакте"}
CAMPAIGN_TYPES = {"direct": "unified", "vk": "vk_plan"}
IMAGE_FORMATS = {"direct": "direct_1_1", "vk": "vk_square"}
NOT_STOPPED = (
    "Не все созданные кампании удалось остановить: площадка не ответила. Мы повторим остановку автоматически в "
    "течение получаса. Когда площадка снова ответит, нажмите «Запустить рекламу» ещё раз: повтор безопасен."
)
INTERRUPTED = (
    "Запуск рекламы не завершился. Всё, что он успел создать на площадках, мы остановили: деньги не тратятся. "
    "Нажмите «Запустить рекламу» ещё раз — повтор безопасен."
)
RUNNING = "Запуск уже идёт. Подождите минуту и обновите страницу."
SILENT = "Площадка «{}» не отвечает. Повторите запуск через несколько минут."
LAUNCHED_OTHER = "Реклама уже запущена по более новому плану. Обновите страницу."
OUTDATED = "План устарел: сведения о бизнесе изменились, и мы собрали новый. Обновите страницу и проверьте новый план."
CLOSED = "Кабинет площадки не подключён или требует входа заново."
INTERRUPTED_STOP = (
    "Запуск рекламы прервался, и за этой кампанией никто не следил. Остановили её, чтобы она не тратила бюджет."
)


def below_minimum(platform: str, limit: PlatformLimit, minimum: dict[str, int], today: date) -> str | None:
    """Почему площадка не примет лимиты кампании; None — примет.

    Минимумы: Директ — недельный, VK — дневной и на кампанию, симулятор — day/period. Недельный лимит считается
    той же функцией, что у адаптера Директа: в конце недели или месяца он меньше семи дневных."""
    daily = minimum.get("daily_limit_kop", minimum.get("day", 0))
    period = minimum.get("period_cap_kop", minimum.get("period", 0))
    weekly = minimum.get("weekly_limit_kop", 0)
    full_week = min(limit.daily_limit * 7, limit.remaining_cap)
    if limit.daily_limit < daily or limit.remaining_cap < period or full_week < weekly:
        return f"Бюджета не хватает для запуска на площадке «{TITLES[platform]}»."
    if weekly_limit(limit.daily_limit, limit.remaining_cap, today) < weekly:
        # Недельный лимит считается до воскресенья или до конца месяца, что раньше: советуем по тому, что обрезало.
        week_end = today + timedelta(days=6 - today.weekday())
        period, start = ("месяца", "с 1-го числа") if budget_end(today) < week_end else ("недели", "с понедельника")
        return (
            f"До конца {period} осталось мало дней: «{TITLES[platform]}» не примет недельный бюджет меньше "
            f"{rubles(weekly)} ₽. Запустите рекламу {start} или увеличьте бюджет месяца."
        )
    return None


class LaunchBlocked(Exception):
    def __init__(self, reasons: list[str]) -> None:
        super().__init__(reasons[0])
        self.reasons = reasons


def closed_account(platform: str, error: Exception) -> LaunchBlocked:
    """Кабинет не открылся: истёк вход, кабинет удалён или ключ его токена сменился."""
    return LaunchBlocked([f"{TITLES[platform]}: {error if isinstance(error, PlatformError) else CLOSED}"])


# Бизнесы, которые запускает этот процесс. Запуск идёт только в процессе API, а он один: занятая блокировка бизнеса
# из этого набора — идущий запуск, а не воркер или шаг мастера.
LAUNCHING: set[int] = set()


class LaunchFailed(Exception):
    pass


class LaunchRepository(NoticesRepository, PlanRepository):
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

    def read_notices(self, business_id: int, now: datetime, plan_id: int | None = None) -> None:
        """Отмечает прочитанными уведомления о прерванных запусках плана plan_id, а без него — всех планов бизнеса:
        они уже неправда."""
        theme = () if plan_id is None else (WorkerRun.log[0]["target_id"].astext == str(plan_id),)
        ids = self.db.scalars(
            select(WorkerRun.id).where(
                *self.unread_filter(business_id), WorkerRun.log[0]["reason_code"].astext == "LAUNCH_INTERRUPTED", *theme
            )
        )
        self.mark_read(business_id, list(ids), now)


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
        plan = self.repository.get_plan(plan_id, business_id)
        if plan is None:
            raise NotFound("План не найден.")
        if plan.status == "launched":
            return self.result(plan)
        # Блокировка держится от проверки плана до перевода бизнеса в running или до записи неудачи: шаги мастера и
        # перемотка демо получают 409, и бюджет со сведениями, по которым созданы кампании, не меняются, пока бизнес
        # ещё черновик. Падение процесса снимает её вместе с соединением.
        with ExitStack() as stack:
            try:
                stack.enter_context(self.repository.business_lock(business_id))
            except ServerBusy:
                raise
            except ChangeConflict as error:
                # Блокировку держит запуск, начатый соседним запросом, или воркер, шаг мастера, перемотка демо.
                if business_id in LAUNCHING:
                    raise LaunchBlocked([RUNNING]) from error
                raise
            LAUNCHING.add(business_id)
            stack.callback(LAUNCHING.discard, business_id)
            # План и бизнес прочитаны до блокировки: пока её ждали, их мог изменить мастер, соседний запуск или
            # удаление аккаунта.
            business = self.repository.fresh_business(business_id)
            if business is None:
                raise NotFound("Бизнес не найден.")
            self.repository.refresh(plan)
            if plan.status == "launched":
                return self.result(plan)
            # Запускается только последний план черновика: запуск плана из старой вкладки создал бы вторые кампании,
            # и у бизнеса стало бы два действующих плана с двойными кэпами.
            if business.status not in DRAFT_STATUSES:
                raise LaunchBlocked([LAUNCHED_OTHER])
            if self.repository.latest_plan(business_id).id != plan.id:
                raise LaunchBlocked([OUTDATED])
            await self.settle_pending(plan, now)
            # Запуск держит блокировку до finish или записи неудачи: незавершённый запуск, видный под ней, брошен
            # (упал процесс) или упал, не остановив созданное. Повтор продолжает его, как упавший, на тех же кампаниях:
            # create_paused берёт их из плана, а включение уже включённой ничего не меняет.
            unsettled = launch_unsettled(plan)
            platforms = plan.payload["platforms"]
            try:
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
                limits = budget_limits(business, platforms, today, self.domain)
                adapters, geo = await self.check_platforms(business, platforms, limits, today)
            except LaunchBlocked:
                # Повтора не будет: созданное прошлой попыткой останавливается сейчас, а не ждёт воркера.
                if unsettled:
                    await self.stop_failed(plan, plan.payload["launch"].get("reason"), self.adapters(plan), now)
                raise
            self.save_launch(plan, started_at=now.isoformat(), error=None, reason=None)
            try:
                refs = await self.create_paused(business, plan, platforms, limits, adapters, geo)
                await self.activate(business, plan, refs, adapters, limits, now)
            except Exception as error:
                await self.stop_failed(plan, failure_reason(error), adapters, now)
                raise LaunchFailed(plan.payload["launch"]["error"]) from error
            self.finish(business, plan, platforms, limits, now, today)
        return self.result(plan)

    async def settle_pending(self, plan: Plan, now: datetime) -> None:
        """Включение, оборванное посреди (упал процесс, площадка не ответила, включилась ли кампания), оставляет
        изменение pending, и до сверки новое включение той же площадки запрещено. Под блокировкой запуска сверяем его
        сразу, а не ждём воркера. Площадка не ответила и сейчас — запуск откладывается, план остаётся как был.
        Площадки не из плана запуск не включает: их изменения сверит воркер."""
        for change in self.repository.pending(plan.business_id):
            if change.platform not in plan.payload["platforms"]:
                continue
            self.repository.commit()
            try:
                adapter = self.resolve_adapter(plan.business_id, change.platform)
            except CLOSED_ACCOUNT as error:
                raise closed_account(change.platform, error) from error
            await self.changes._reconcile(change, adapter, now=now)
            if change.status == "pending":
                raise LaunchBlocked([SILENT.format(TITLES[change.platform])])

    def adapters(self, plan: Plan) -> dict[str, AdPlatform]:
        """Кабинеты кампаний плана; кабинет, который не открылся, пропускается: его кампанию остановит воркер."""
        adapters = {}
        for platform in plan.payload["launch"]["campaigns"]:
            try:
                adapters[platform] = self.resolve_adapter(plan.business_id, platform)
            except CLOSED_ACCOUNT:
                continue
        return adapters

    async def check_platforms(
        self, business: Business, platforms: list[str], limits: dict[str, PlatformLimit], today: date
    ) -> tuple[dict[str, AdPlatform], dict[str, dict]]:
        """До создания кампаний: кабинет доступен, бюджет не ниже минимумов площадки, город найден в её регионах.

        Площадки отвечают секундами: соединение с базой на это время возвращается в пул."""
        self.repository.commit()
        adapters, geo = {}, {}
        for platform in platforms:
            # Нулевой дневной остаток ниже любого минимума: объясняем настоящую причину, а не «не хватает бюджета».
            if not limits[platform].daily_limit:
                raise LaunchBlocked(["На сегодня бюджет месяца почти исчерпан: запустите рекламу с начала дня."])
            try:
                adapters[platform] = self.resolve_adapter(business.id, platform)
                minimum = await adapters[platform].min_budgets()
            except CLOSED_ACCOUNT as error:
                raise closed_account(platform, error) from error
            if reason := below_minimum(platform, limits[platform], minimum, today):
                raise LaunchBlocked([reason])
            try:
                geo[platform] = await adapters[platform].geo_targeting(business.city)
            except PlatformError as error:
                raise LaunchBlocked([f"{TITLES[platform]}: {error}"]) from error
        return adapters, geo

    def save_launch(self, plan: Plan, status: str | None = None, **values: object) -> None:
        payload = dict(plan.payload)
        payload["launch"] = payload.get("launch", {"campaigns": {}}) | values
        plan.payload = payload
        if status is not None:
            plan.status = status
        self.repository.commit()

    async def create_paused(
        self,
        business: Business,
        plan: Plan,
        platforms: list[str],
        limits: dict[str, PlatformLimit],
        adapters: dict[str, AdPlatform],
        geo: dict[str, dict],
    ) -> dict[str, tuple[Campaign, ExternalRefs]]:
        """Кампании создаются остановленными, но уже с лимитами: сбой на любом шаге не оставляет трат."""
        created = {}
        accounts = {account.platform: account for account in self.repository.accounts(business.id)}
        assets = {asset.id: asset for asset in self.repository.assets(business.id)}
        known = plan.payload.get("launch", {}).get("campaigns", {})
        for platform in platforms:
            adapter = adapters[platform]
            groups = [group for group in plan.payload["groups"] if group["platform"] == platform]
            self.repository.commit()
            images = await self.upload_images(adapter, platform, groups, plan, assets)
            campaign = self.repository.campaign(known[platform]) if platform in known else None
            if campaign is None:
                campaign = self.stage_rows(business, plan, platform, limits[platform], accounts[platform].id, groups)
                self.save_launch(
                    plan, campaigns=plan.payload.get("launch", {}).get("campaigns", {}) | {platform: campaign.id}
                )
            spec = self.spec(business, campaign, groups, images, geo[platform])
            # Картинка объявления сохраняется в строке: по ней экраны показывают миниатюру.
            self.repository.commit()
            if demo_enabled() and isinstance(adapter, FakeAdapter):
                configure_adapter(adapter, spec)
            refs = await adapter.create_campaign_tree(spec)
            self.bind(campaign, refs)
            created[platform] = (campaign, refs)
            self.repository.commit()
        return created

    async def upload_images(
        self, adapter: AdPlatform, platform: str, groups: list[dict], plan: Plan, assets: dict[int, Asset]
    ) -> dict[str, list[Asset]]:
        """Картинки с оффером нарезаются под площадку и загружаются до создания кампании; ссылка кэшируется в
        asset.platform_refs. Возвращает выбранные картинки каждой группы."""
        chosen: dict[str, list[Asset]] = {}
        for group in groups:
            chosen[str(group["id"])] = [
                assets[id_]
                for id_ in plan.payload["assets"].get(group["segment"], ())
                if id_ in assets and assets[id_].selected and assets[id_].kind in IMAGE_KINDS
            ]
            for asset in chosen[str(group["id"])]:
                await self.upload_one(adapter, platform, asset, group)
        return chosen

    async def upload_one(self, adapter: AdPlatform, platform: str, asset: Asset, group: dict) -> str:
        if platform in asset.platform_refs:
            return asset.platform_refs[platform]
        # Нарезка картинок занимает секунды: в цикле событий она задерживала бы запросы всех пользователей.
        target = await run_decoding(
            self.cut_image, self.media_root / asset.file_path, IMAGE_FORMATS[platform], group["ads"][0]["title"]
        )
        ref = await adapter.upload_image(target)
        asset.platform_refs = asset.platform_refs | {platform: ref}
        self.repository.commit()
        return ref

    def cut_image(self, path: Path, name: str, offer: str) -> Path:
        try:
            data = make_formats(path.read_bytes(), offer=offer, font_path=self.font_path)[name]
        except ImageRejected:
            data = make_formats(path.read_bytes(), font_path=self.font_path)[name]
        target = path.with_name(f"{path.stem}-{name}.jpg")
        target.write_bytes(data)
        return target

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
        self,
        business: Business,
        campaign: Campaign,
        plan_groups: list[dict],
        images: dict[str, list[Asset]],
        geo: dict,
    ) -> CampaignTreeSpec:
        """Внутренние id в дереве — id строк БД: по ним воркер собирает ExternalRefs. Объявления получают картинки
        группы по кругу; картинка запоминается в Ad.asset_id."""
        groups, ads, targets = self.repository.tree_rows(campaign.id)
        by_name = {group["name"]: group for group in plan_groups}
        for group in groups:
            chosen = images[str(by_name[group.theme]["id"])]
            for position, ad in enumerate(item for item in ads if item.ad_group_id == group.id):
                ad.asset_id = chosen[position % len(chosen)].id if chosen else None
        image_refs = {
            asset.id: asset.platform_refs[campaign.platform] for chosen in images.values() for asset in chosen
        }
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
                            image_refs[ad.asset_id] if ad.asset_id else None,
                            medical=business.medical_level != "none",
                        )
                        for ad in ads
                        if ad.ad_group_id == group.id
                    ),
                    tuple(
                        TargetSpec(str(target.id), "phrase", target.value)
                        for target in targets
                        if target.ad_group_id == group.id
                    ),
                    geo,
                )
                for group in groups
            ),
            "paused_auto",
        )

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
        # Блокировку бизнеса уже держит запуск: изменение применяется под ней, а не берёт её второй раз.
        for platform, (campaign, refs) in created.items():
            operations = [
                Operation("set_status", "ad", external_id, "status", "paused_auto", "active")
                for external_id in refs.ads.values()
            ] + [Operation("set_status", "campaign", refs.campaign_id, "status", "paused_auto", "active")]
            change = await self.changes._apply_change(
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
            if change.status == "pending":
                raise PlatformError(f"Площадка «{TITLES[platform]}» не ответила, включилась ли реклама.")
            if change.status != "applied":
                detail = change.evidence["_service"].get("error")
                raise PlatformError(f"Площадка «{TITLES[platform]}» не включила рекламу. {detail or ''}".strip())

    async def stop_failed(self, plan: Plan, reason: str | None, adapters: dict[str, AdPlatform], now: datetime) -> None:
        """Останавливает созданное неудавшимся запуском и записывает неудачу; reason — её причина, None — запуск
        прервался."""
        if await self.stop_created(plan, adapters):
            self.save_launch(plan, "failed", error=failed_launch(reason), reason=reason, started_at=None)
            # Созданное остановлено: прежнее «не удалось остановить» уже неправда.
            self.repository.read_notices(plan.business_id, now, plan.id)
        else:
            # started_at остаётся: план не пересобирается, пока воркер не остановит созданное. Причину воркер
            # сохранит в своём объяснении.
            self.save_launch(plan, "failed", error=failed_launch(reason, stopped=False), reason=reason)

    async def stop_created(self, plan: Plan, adapters: dict[str, AdPlatform]) -> bool:
        """Останавливает кампании плана; False — площадка не подтвердила остановку хотя бы одной или её кабинет не
        открылся."""
        stopped = True
        for platform, campaign_id in plan.payload.get("launch", {}).get("campaigns", {}).items():
            campaign = self.repository.campaign(campaign_id)
            if campaign is None or not campaign.external_id:
                continue
            if platform not in adapters:
                stopped = False
                continue
            try:
                await adapters[platform].apply(
                    Operation("set_status", "campaign", campaign.external_id, "status", "active", "paused_auto")
                )
            except PlatformError:
                stopped = False
                continue
            campaign.status = "paused_auto"
        self.repository.commit()
        return stopped

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
            "launch": plan.payload["launch"] | {"launched_at": now.isoformat(), "error": None, "started_at": None}
        }
        business.settings = business.settings | {
            "_worker": business.settings.get("_worker", {}) | {"started_on": today.isoformat()}
        }
        business.status = "running"
        # Уведомления о прерванных попытках этого и прежних планов: реклама запущена, их тексты уже неправда.
        self.repository.read_notices(business.id, now)
        self.repository.commit()

    def result(self, plan: Plan) -> dict:
        launch = plan.payload["launch"]
        return {
            "status": "launched",
            "plan_id": plan.id,
            "launched_at": launch.get("launched_at"),
            "campaigns": launch["campaigns"],
        }


def failure_reason(error: Exception) -> str:
    return str(error) if isinstance(error, PlatformError | ChangeConflict) else "Непредвиденная ошибка."


def failed_launch(reason: str | None, *, stopped: bool = True) -> str:
    """reason — причина, по которой упал запуск; None — запуск прервался (упал процесс)."""
    if reason is None:
        return INTERRUPTED if stopped else f"Запуск рекламы не завершился. {NOT_STOPPED}"
    if not stopped:
        return f"Запуск не удался: {reason} {NOT_STOPPED}"
    return (
        f"Запуск не удался: {reason} Всё, что уже создано, остановлено и ничего не тратит. "
        "Исправьте причину и нажмите «Запустить рекламу» ещё раз: повтор безопасен."
    )


def titles(platforms: list[str]) -> str:
    return ", ".join(f"«{TITLES[platform]}»" for platform in platforms)


def interrupted_unstopped(platforms: list[str], reason: str | None = None) -> str:
    """reason — причина, по которой упал запуск; None — запуск прервался (упал процесс)."""
    where = f"на площадке {titles(platforms)}" if len(platforms) == 1 else f"на площадках {titles(platforms)}"
    head = f"Запуск не удался: {reason} Остановить" if reason else "Запуск рекламы не завершился, а остановить"
    return (
        f"{head} созданную им рекламу {where} пока не удалось. Если площадка просит войти в кабинет заново, войдите "
        "снова. Если кабинет больше не нужен, остановите кампанию «Автопилот» в нём самом и отключите кабинет на шаге "
        "«Кабинеты». Мы пробуем снова каждые полчаса."
    )


def interrupted_revoked(platforms: list[str], reason: str | None = None) -> str:
    """Кабинет отключён владельцем: остановить созданную в нём кампанию уже нечем, и «деньги не тратятся» было бы
    неправдой."""
    head = f"Запуск не удался: {reason}" if reason else "Запуск рекламы не завершился."
    where = (
        f"Кабинет {titles(platforms)} отключён" if len(platforms) == 1 else f"Кабинеты {titles(platforms)} отключены"
    )
    return (
        f"{head} {where}, и управлять созданной там кампанией «Автопилот» мы больше не можем: проверьте в самом "
        "кабинете, что она остановлена. Всё, что запуск создал в подключённых кабинетах, остановлено. Чтобы запустить "
        "рекламу, нажмите «Запустить рекламу» ещё раз."
    )


async def recover_launch(worker: Worker, business: Business) -> None:
    """Запуск, который не дошёл до finish и не остановил созданное (упал процесс API, площадка не ответила на
    остановку), оставляет кампании, за которыми никто не следит: бизнес ещё не running, и шаги воркера его не трогают.

    Вызывается под блокировкой бизнеса. Запуск держит её от записи started_at до finish или до записи своей неудачи,
    поэтому незавершённый запуск, который виден под ней, брошен, даже если начат минуту назад. Кампании плана
    останавливаются, план становится failed с объяснением (причина упавшего запуска сохраняется), и повтор запуска
    подхватывает те же кампании. Владелец, нажавший «Запустить» раньше воркера, продолжает брошенный запуск сам
    (LaunchService.launch). Пока остановка не подтверждена, started_at остаётся:
    план не пересобирается (PlanService.get_plan), а следующий ход воркера пробует снова.

    Ключ уведомления — с попыткой запуска (started_at): повторные ходы одной попытки не дублируют уведомление (одно на
    ключ), а следующая попытка того же плана, прерванная снова, даёт новое. Остановленное повторный ход не трогает:
    остановка применяется, только если площадка показывает кампанию включённой."""
    # Включение, оборванное посреди, оставило изменение pending: сверка узнаёт, включилась ли кампания. Оно остаётся и
    # после запуска, который упал и сам остановил созданное, и до сверки запрещает повтор включения на той площадке.
    if worker.repository.pending(business.id):
        await worker.changes.recover_pending(worker.resolve_adapter, business_id=business.id, now=worker.clock.now)
    for plan in worker.repository.rows(Plan, business.id):
        # Планы могли быть прочитаны до блокировки (Worker.recover_launch): под ней — то, что записал запуск.
        worker.repository.refresh(plan)
        if not launch_unsettled(plan):
            continue
        attempt = plan.payload["launch"]["started_at"]
        # Ключ остановки — ход, а не только попытка: кампанию, которую владелец включил в кабинете между ходами,
        # следующий ход останавливает снова, а не находит прошлую остановку под тем же ключом.
        worker.step_key = f"launch_recovery:{plan.id}:{attempt}:{worker.clock.now.isoformat()}"
        stops = {
            platform: await stop_interrupted(worker, business, campaign_id)
            for platform, campaign_id in plan.payload["launch"]["campaigns"].items()
        }
        failed = [platform for platform, result in stops.items() if result == "failed"]
        revoked = [platform for platform, result in stops.items() if result == "revoked"]
        reason = plan.payload["launch"].get("reason")
        if failed:
            launch = {"error": interrupted_unstopped(failed, reason)}
        elif revoked:
            launch = {"error": interrupted_revoked(revoked, reason), "started_at": None}
        else:
            launch = {"error": failed_launch(reason), "started_at": None}
        plan.payload = plan.payload | {"launch": plan.payload["launch"] | launch}
        plan.status = "failed"
        worker.repository.save(plan)
        # Одна тема на план: на главной остаётся только последнее уведомление.
        worker.notice(
            business,
            "LAUNCH_INTERRUPTED",
            launch["error"],
            key=f"launch_unstopped:{attempt}" if failed else f"launch_stopped:{attempt}",
            target_id=plan.id,
        )


async def stop_interrupted(worker: Worker, business: Business, campaign_id: int) -> str:
    """stopped — кампания запуска не тратит: остановлена сейчас или раньше. Кампания без external_id на площадке либо
    не создана, либо создана остановленной (create_paused) и ещё не включалась. Кампании, которой на площадке нет
    (владелец удалил её в кабинете), тратить нечего. failed — остановить не удалось, следующий ход попробует снова.

    revoked — кабинет, который владелец отключил, больше не наш: перед отключением его кампании останавливаются, а
    если площадка не подтвердила остановку, владелец узнаёт об этом сразу (api.auth.stop_user_ads). План не ждёт
    кабинета, которого уже нет, иначе не пересобирался бы никогда, но проверить кампанию владелец может только сам."""
    campaign = worker.repository.find(Campaign, business_id=business.id, id=campaign_id)
    if campaign is None or not campaign.external_id:
        return "stopped"
    account = worker.repository.find(PlatformAccount, business_id=business.id, id=campaign.platform_account_id)
    # Отключённый кабинет не спросить: остановку, которую площадка подтвердила раньше, знает строка кампании.
    if account.status == "revoked":
        return "stopped" if campaign.status in STOPPED else "revoked"
    try:
        adapter = worker.resolve_adapter(business.id, campaign.platform)
        state = (await adapter.get_state(worker.refs(campaign))).get(campaign.external_id)
        if state is None or state.status in STOPPED:
            return "stopped"
        # Защитное действие: выполняется и в пробном режиме, ведь включал кампанию настоящий запуск.
        await worker.apply(
            business,
            campaign,
            [Operation("set_status", "campaign", campaign.external_id, "status", state.status, "paused_auto")],
            actor="system",
            kind="pause",
            reason_code="LAUNCH_INTERRUPTED",
            reason_text=INTERRUPTED_STOP,
            force=True,
        )
    except (*CLOSED_ACCOUNT, ChangeConflict):
        # Кабинет требует входа заново, площадка не ответила или не подтвердила остановку: попробуем в следующий ход.
        return "failed"
    return "stopped"
