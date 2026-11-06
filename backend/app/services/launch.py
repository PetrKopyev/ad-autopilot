from __future__ import annotations

from collections.abc import Callable
from contextlib import ExitStack
from dataclasses import asdict, replace
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from textwrap import shorten
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
    ChangeLog,
    Plan,
    PlatformAccount,
    Target,
    WorkerRun,
)
from app.demo_flags import demo_enabled
from app.domain.budget import campaign_limits, meets_minimum, plan_budget
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
from app.services.changes import (
    CLOSED_ACCOUNT,
    AdapterResolver,
    ChangeConflict,
    ChangesService,
    ServerBusy,
)
from app.services.demo import configure_adapter
from app.services.onboarding import DRAFT_STATUSES, NotFound, description_draft
from app.services.plan import (
    IMAGE_KINDS,
    PlanRepository,
    budget_limits,
    campaign_platform,
    campaign_units,
    group_key,
    launch_blockers,
    launch_unsettled,
    phrase,
    targeting,
)
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
MODERATION_WAIT = (
    "Площадка «{}» проверяет объявления: реклама включится автоматически, как только проверка закончит, — "
    "нажимать «Запустить рекламу» снова не нужно. Если проверка затянется, мы сообщим."
)
PLATFORM_CREATE_FAILED = "Площадка «{}» не создала рекламу: {}"
PLATFORM_PARTIALLY_CREATED = (
    "Площадка «{}» создала рекламу частично и не включила её: {} "
    "Повторный запуск доведёт площадку, работающие не тронет."
)
LAUNCHED_OTHER = "Реклама уже запущена по более новому плану. Обновите страницу."
OUTDATED = "План устарел: сведения о бизнесе изменились, и мы собрали новый. Обновите страницу и проверьте новый план."
CLOSED = "Кабинет площадки не подключён или требует входа заново."
INTERRUPTED_STOP = (
    "Запуск рекламы прервался, и за этой кампанией никто не следил. Остановили её, чтобы она не тратила бюджет."
)
def below_minimum(platform: str, limit: PlatformLimit, minimum: dict[str, int], today: date) -> str | None:
    """Почему площадка не примет лимиты кампании; None — примет.

    На полную неделю лимиты сверяет domain.budget.meets_minimum. Недельный лимит считается той же функцией, что у
    адаптера Директа: в конце недели или месяца он меньше семи дневных. Бюджет не ниже domain.budget.budget_minimum
    набирает 300 ₽ в неделю в любой день; совет начать с понедельника остаётся на случай, когда живой минимум Директа
    больше."""
    if not meets_minimum(limit, minimum):
        return f"Бюджета не хватает для запуска на площадке «{TITLES[platform]}». Увеличьте бюджет месяца."
    weekly = minimum.get("weekly_limit_kop", 0)
    if weekly_limit(limit.daily_limit, limit.remaining_cap, today) < weekly:
        # Недельный лимит считается до воскресенья или до конца месяца, что раньше: советуем по тому, что обрезало.
        week_end = today + timedelta(days=6 - today.weekday())
        period, start = ("месяца", "с 1-го числа") if budget_end(today) < week_end else ("недели", "с понедельника")
        return (
            f"До конца {period} осталось мало дней: «{TITLES[platform]}» не примет недельный бюджет меньше "
            f"{rubles(weekly)} ₽. Запустите рекламу {start} или увеличьте бюджет месяца."
        )
    return None


def units_below_minimum(
    units: dict[str, tuple[list[dict], PlatformLimit]], minimums: dict[str, dict[str, int]], today: date
) -> str | None:
    """Почему площадка не примет лимит одной из кампаний запуска: у Директа лимит площадки делится между
    кампаниями по услугам, и check_platforms, сверяющий лимит всей площадки, этого не видит."""
    for key, (_, limit) in units.items():
        platform = campaign_platform(key)
        if reason := below_minimum(platform, limit, minimums[platform], today):
            return reason
    return None


def campaign_name(business: Business, key: str, groups: list[dict]) -> str:
    services = [group["service"] for group in groups if group.get("service")]
    if ":" not in key or not services:
        return f"Автопилот: {business.city}"
    return shorten(f"Автопилот: {', '.join(services)} — {business.city}", 150, placeholder="…")


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
        if plan.status == "launched" and not plan.payload["launch"].get("platform_errors"):
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
                errors = plan.payload["launch"].get("platform_errors") or {}
                if not errors:
                    return self.result(plan)
                # Частичный запуск: не запустившиеся площадки владелец может попробовать снова — остальные уже
                # работают, и трогать их не нужно.
                return await self.retry_platforms(business, plan, list(errors), now, today)
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
                    self.domain,
                    medical_acknowledged,
                )
                if reasons:
                    raise LaunchBlocked(reasons)
                limits = budget_limits(business, platforms, today, self.domain)
                adapters, geo, minimums = await self.check_platforms(business, platforms, limits, today)
                units = campaign_units(plan, platforms, limits, minimums, today, self.created_keys(plan))
                if reason := units_below_minimum(units, minimums, today):
                    raise LaunchBlocked([reason])
            except LaunchBlocked:
                # Повтора не будет: созданное прошлой попыткой останавливается сейчас, а не ждёт воркера.
                if unsettled:
                    await self.stop_failed(plan, plan.payload["launch"].get("reason"), self.adapters(plan), now)
                raise
            self.save_launch(
                plan,
                started_at=now.isoformat(),
                error=None,
                reason=None,
                batches={key: [group["id"] for group in groups] for key, (groups, _) in units.items()},
                campaigns=self.laid_out(plan, units),
            )
            try:
                created, create_failures = await self.create_paused(business, plan, units, adapters, geo)
                failures, awaiting = await self.activate(
                    business, plan, created, adapters, units, now, set(create_failures)
                )
                failures = create_failures | failures
            except ChangeConflict as error:
                # Конфликт очереди платформы — не сбой площадки: созданное этой попыткой останавливается,
                # как при любом провале, а владелец получает текст конфликта с 409, а не 502.
                await self.stop_failed(plan, failure_reason(error), adapters, now)
                raise
            except Exception as error:
                await self.stop_failed(plan, failure_reason(error), adapters, now)
                raise LaunchFailed(plan.payload["launch"]["error"]) from error
            # Изоляция площадок: сбой одной не роняет остальные — она фиксируется честной записью, а запуск
            # доводится до конца на площадках, которые не отказали.
            self.save_platform_failures(business, plan, failures)
            # Площадка, которая проверяет объявления, — не неудача: включение применено, после одобрения рекламу
            # включит шаг модерации (sync.settle_review), владельцу нажимать кнопку снова не нужно.
            self.save_launch(
                plan,
                awaiting_moderation=sorted(
                    set(plan.payload["launch"].get("awaiting_moderation", ())) | set(awaiting)
                ),
            )
            succeeded = [platform for platform in platforms if platform not in failures]
            if not succeeded:
                await self.stop_failed(plan, " ".join(failures.values()), adapters, now)
                raise LaunchFailed(plan.payload["launch"]["error"])
            self.finish(business, plan, succeeded, limits, now, today)
        return self.result(plan)

    def retry_limits(self, business: Business, plan: Plan, today: date) -> dict[str, PlatformLimit]:
        """Лимиты повторного запуска: доли — по всем площадкам плана (повтор одной не отдаёт ей бюджет
        работающих), расход месяца вычитаем из разрешённого, дату старта не сдвигаем. Иначе кампания
        повтора получает дневной лимит всего бюджета, а площадки вместе держат больше 100%."""
        plan_platforms = tuple(plan.payload["platforms"])
        month = today.replace(day=1)
        ledgers = self.repository.rows(BudgetLedger, business_id=business.id, month=month)
        spent = sum(row.spent_kop for row in ledgers)
        meta = business.settings.get("_worker", {})
        started_on = date.fromisoformat(meta.get("started_on", today.isoformat()))
        # Доли после переноса бюджета между площадками (rebalance их запоминает в meta["shares"]):
        # без них повтор вернул бы дефолтные пропорции и перекрыл бы перенесённое.
        shares = {key: Decimal(value) for key, value in meta.get("shares", {}).items()}
        budget = plan_budget(
            business.month_budget_kop,
            spent,
            today,
            plan_platforms,
            started_on=started_on,
            shares=shares if set(shares) == set(plan_platforms) else None,
            settings=self.domain,
        )
        result = {}
        for limit in budget.limits:
            daily, cap = campaign_limits(limit.daily_limit, limit.remaining_cap, today)
            result[limit.platform] = PlatformLimit(limit.platform, cap, daily, cap)
        return result

    async def retry_platforms(
        self, business: Business, plan: Plan, platforms: list[str], now: datetime, today: date
    ) -> dict:
        """Повторный запуск площадок, отказавших в основной попытке: уже работающие не трогаем, а отказавшие
        проходят тот же путь — создание (с переиспользованием кампаний попытки) и включение. Ошибки этих
        площадок снова фиксируются честно, запись о неудаче плана не пишется: реклама на остальных работает."""
        if (business.settings.get("_worker") or {}).get("kill_switch"):
            # В5 круга 7: повтор не снимает «Остановить всю рекламу» сам — иначе бизнес получал running
            # с зажатым kill_switch: «Продолжить» отвечало «уже работает», а автопилот был выключен.
            raise LaunchBlocked(
                ["Реклама остановлена вами. Сначала нажмите «Продолжить» на главной, потом повторите запуск."]
            )
        await self.settle_pending(plan, now)
        limits = self.retry_limits(business, plan, today)
        adapters, geo, minimums = await self.check_platforms(business, platforms, limits, today)
        units = campaign_units(plan, platforms, limits, minimums, today, self.created_keys(plan))
        if reason := units_below_minimum(units, minimums, today):
            raise LaunchBlocked([reason])
        self.save_launch(
            plan,
            started_at=now.isoformat(),
            error=None,
            reason=None,
            campaigns=self.laid_out(plan, units),
            batches={
                key: ids
                for key, ids in plan.payload["launch"].get("batches", {}).items()
                if campaign_platform(key) not in platforms
            }
            | {key: [group["id"] for group in groups] for key, (groups, _) in units.items()},
        )
        try:
            created, create_failures = await self.create_paused(business, plan, units, adapters, geo)
            failures, awaiting = await self.activate(
                business, plan, created, adapters, units, now, set(create_failures)
            )
            failures = create_failures | failures
        except ChangeConflict:
            # Конфликт очереди платформы доходит до владельца своим текстом (409), а не ошибкой шлюза.
            self.save_launch(plan, started_at=None, error=None, reason=None)
            raise
        except Exception as error:
            # Повтор не роняет уже работающие площадки и не останавливает их: причина отдана владельцу,
            # пометка неудачной площадки в плане осталась — повторить можно сразу.
            reason = failure_reason(error)
            self.save_launch(plan, started_at=None, error=None, reason=None)
            raise LaunchFailed(
                f"{reason} Реклама на уже запущенных площадках продолжает работать, они не тронуты."
            ) from error
        self.save_platform_failures(business, plan, failures)
        self.save_launch(
            plan,
            awaiting_moderation=sorted(
                set(plan.payload["launch"].get("awaiting_moderation", ())) | set(awaiting)
            ),
        )
        succeeded = [platform for platform in platforms if platform not in failures]
        if succeeded:
            self.finish(business, plan, succeeded, limits, now, today)
            errors = dict(plan.payload["launch"].get("platform_errors") or {})
            for platform in succeeded:
                errors.pop(platform, None)
            self.save_launch(plan, platform_errors=errors)
        return self.result(plan)

    def save_platform_failures(self, business: Business, plan: Plan, failures: dict[str, str]) -> None:
        """Незапустившаяся площадка — запись в журнале с понятной причиной и пометка в плане: экран плана и
        повторные запуски видят, что реклама частичная и почему. Записи информационные: операций нет, откат
        не нужен — на площадке ничего не создано или оно уже остановлено."""
        if not failures:
            return
        for platform, reason in failures.items():
            campaign_id = next(
                (
                    id_
                    for key, id_ in plan.payload["launch"]["campaigns"].items()
                    if campaign_platform(key) == platform
                ),
                None,
            )
            self.repository.save(
                ChangeLog(
                    business_id=business.id,
                    actor="system",
                    kind="launch",
                    platform=platform,
                    entity_type="campaign",
                    entity_id=str(campaign_id or ""),
                    operations=[],
                    reason_code="LAUNCH_PLATFORM_FAILED",
                    reason_params={},
                    reason_text=reason,
                    evidence={},
                    status="failed",
                )
            )
        errors = dict(plan.payload["launch"].get("platform_errors") or {}) | failures
        self.save_launch(plan, platform_errors=errors)

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
        for platform in {campaign_platform(key) for key in plan.payload["launch"]["campaigns"]}:
            try:
                adapters[platform] = self.resolve_adapter(plan.business_id, platform)
            except CLOSED_ACCOUNT:
                continue
        return adapters

    async def check_platforms(
        self, business: Business, platforms: list[str], limits: dict[str, PlatformLimit], today: date
    ) -> tuple[dict[str, AdPlatform], dict[str, dict], dict[str, dict[str, int]]]:
        """До создания кампаний: кабинет доступен, бюджет не ниже минимумов площадки, город найден в её регионах.

        Площадки отвечают секундами: соединение с базой на это время возвращается в пул."""
        self.repository.commit()
        adapters, geo, minimums = {}, {}, {}
        for platform in platforms:
            # Нулевой дневной остаток ниже любого минимума: объясняем настоящую причину, а не «не хватает бюджета».
            if not limits[platform].daily_limit:
                raise LaunchBlocked(["На сегодня бюджет месяца почти исчерпан: запустите рекламу с начала дня."])
            try:
                adapters[platform] = self.resolve_adapter(business.id, platform)
                minimum = minimums[platform] = await adapters[platform].min_budgets()
                if platform == "direct":
                    # Агентские кабинеты не поддерживаются: понятный отказ до создания кампаний (В5).
                    await adapters[platform].whoami()
            except CLOSED_ACCOUNT as error:
                raise closed_account(platform, error) from error
            except PlatformError as error:
                raise LaunchBlocked([f"{TITLES[platform]}: {error}"]) from error
            if reason := below_minimum(platform, limits[platform], minimum, today):
                raise LaunchBlocked([reason])
            try:
                geo[platform] = await adapters[platform].geo_targeting(business.city)
            except PlatformError as error:
                raise LaunchBlocked([f"{TITLES[platform]}: {error}"]) from error
        return adapters, geo, minimums

    def created_keys(self, plan: Plan) -> set[str]:
        """Кампании плана, созданные на площадке: повтор запуска сохраняет их состав (campaign_units)."""
        return {
            key
            for key, campaign_id in plan.payload.get("launch", {}).get("campaigns", {}).items()
            if (campaign := self.repository.campaign(campaign_id)) is not None and campaign.external_id
        }

    def laid_out(self, plan: Plan, units: dict[str, tuple[list[dict], PlatformLimit]]) -> dict[str, int]:
        """Кампании прошлой попытки для новой раскладки. Кампания по услугам Директа, не созданная на площадке,
        переиспользуется, только если под её ключом те же услуги: её строки собраны под прежние, иначе create_paused
        заведёт новую. Забытая строка без external_id воркеру не видна (Worker.campaigns)."""
        launch = plan.payload.get("launch", {})
        created = self.created_keys(plan)
        saved = launch.get("batches", {})
        return {
            key: campaign_id
            for key, campaign_id in launch.get("campaigns", {}).items()
            if ":" not in key
            or key in created
            or (key in units and saved.get(key) == [group["id"] for group in units[key][0]])
        }

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
        units: dict[str, tuple[list[dict], PlatformLimit]],
        adapters: dict[str, AdPlatform],
        geo: dict[str, dict],
    ) -> tuple[dict[str, tuple[Campaign, ExternalRefs]], dict[str, str]]:
        """Кампании создаются остановленными, но уже с лимитами: сбой на любом шаге не оставляет трат. Сбой
        площадки фиксируется причиной и не роняет создание остальных: возвращает созданные кампании и причины
        отказавших площадок (изоляция площадок запуска)."""
        created = {}
        failures: dict[str, str] = {}
        accounts = {account.platform: account for account in self.repository.accounts(business.id)}
        assets = {asset.id: asset for asset in self.repository.assets(business.id)}
        known = plan.payload.get("launch", {}).get("campaigns", {})
        # Демо раздаёт объявлениям сильную, слабую и пограничную эффективность по порядку групп площадки.
        configured = dict.fromkeys(adapters, 0)
        for key, (groups, limit) in units.items():
            platform = campaign_platform(key)
            if platform in failures:
                continue
            adapter = adapters[platform]
            self.repository.commit()
            try:
                images = await self.upload_images(adapter, platform, groups, plan, assets)
                campaign = self.repository.campaign(known[key]) if key in known else None
                if campaign is None:
                    campaign = self.stage_rows(
                        business, platform, campaign_name(business, key, groups), limit, accounts[platform].id, groups
                    )
                    known = known | {key: campaign.id}
                    self.save_launch(plan, campaigns=known)
                else:
                    # Повтор переиспользует кампанию неудачной попытки, а лимиты за это время пересчитались
                    # (другие расход месяца и доли площадок): оставленные значения прошлой попытки обновляем,
                    # иначе кампания создастся с лимитом «как будто площадка одна».
                    campaign.daily_limit_kop = limit.daily_limit
                    campaign.period_cap_kop = limit.remaining_cap
                    campaign.platform_account_id = accounts[platform].id
                spec = self.spec(
                    business, campaign, groups, images, lambda group: geo[platform] | targeting(plan, platform, group)
                )
                # Картинка объявления сохраняется в строке: по ней экраны показывают миниатюру.
                self.repository.commit()
                if demo_enabled() and isinstance(adapter, FakeAdapter):
                    configure_adapter(adapter, spec, configured[platform])
                    configured[platform] += len(spec.groups)
                refs = await adapter.create_campaign_tree(spec)
            except PlatformError as error:
                if any(campaign_platform(item) == platform for item in created):
                    # Часть кампаний площадки уже создана: созданное остаётся остановленным, пока повтор
                    # не доведёт площадку, — текст говорит об этом честно.
                    failures[platform] = PLATFORM_PARTIALLY_CREATED.format(TITLES[platform], error)
                else:
                    failures[platform] = PLATFORM_CREATE_FAILED.format(TITLES[platform], error)
                continue
            for text in getattr(adapter, "content_mismatches", []):
                # Содержимое кабинета отличалось от плана: адаптер пересоздал объявления и фразы, запуск честно
                # пишет об этом в журнал — иначе «лишние» объявления в кабинете были бы непонятны владельцу.
                self.repository.save(
                    ChangeLog(
                        business_id=business.id,
                        actor="system",
                        kind="launch",
                        platform=platform,
                        entity_type="campaign",
                        entity_id=refs.campaign_id,
                        operations=[],
                        reason_code="LAUNCH_CONTENT_RECREATED",
                        reason_params={},
                        reason_text=text,
                        evidence={},
                        status="applied",
                    )
                )
            stops = getattr(adapter, "foreign_stops", [])
            if stops:
                # Содержимого кабинета, которого нет в плане, у адаптера больше нет в активных: не привязанное
                # к плану тратило бы бюджет мимо статистики и недоступным для остановки автопилотом. Операции
                # остановок сохраняем единой откатываемой записью: откат вернёт объявлениям и фразам статусы.
                stop_refs = replace(
                    refs,
                    ads=refs.ads
                    | {f"foreign-ad:{op.entity_id}": op.entity_id for op in stops if op.entity_type == "ad"},
                    targets=refs.targets
                    | {f"foreign-target:{op.entity_id}": op.entity_id for op in stops if op.entity_type == "target"},
                )
                self.repository.save(
                    ChangeLog(
                        business_id=business.id,
                        actor="system",
                        kind="launch",
                        platform=platform,
                        entity_type="campaign",
                        entity_id=refs.campaign_id,
                        operations=[asdict(op) for op in stops],
                        reason_code="LAUNCH_FOREIGN_STOPPED",
                        reason_params={},
                        reason_text=(
                            "В кабинете нашли объявления и фразы, которых нет в плане: они тратили бы бюджет "
                            "незаметно для статистики. Чужие объявления остановили и убрали в архив, чужие "
                            "фразы остановили."
                        ),
                        evidence={"_service": {"refs": asdict(stop_refs), "dry_run": False}},
                        status="applied",
                    )
                )
            self.bind(campaign, refs)
            created[key] = (campaign, refs)
            self.repository.commit()
        return created, failures

    async def upload_images(
        self, adapter: AdPlatform, platform: str, groups: list[dict], plan: Plan, assets: dict[int, Asset]
    ) -> dict[str, list[Asset]]:
        """Картинки с оффером нарезаются под площадку и загружаются до создания кампании; ссылка кэшируется в
        asset.platform_refs. Возвращает выбранные картинки каждой группы."""
        chosen: dict[str, list[Asset]] = {}
        for group in groups:
            chosen[str(group["id"])] = [
                assets[id_]
                for id_ in plan.payload["assets"].get(group_key(group), ())
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
        platform: str,
        name: str,
        limit: PlatformLimit,
        account_id: int,
        groups: list[dict],
    ) -> Campaign:
        campaign = Campaign(
            business_id=business.id,
            platform_account_id=account_id,
            platform=platform,
            type=CAMPAIGN_TYPES[platform],
            name=name,
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
                        title2=ad.get("title2", ""),
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
        group_targeting: Callable[[dict], dict],
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
                            title2=ad.title2,
                        )
                        for ad in ads
                        if ad.ad_group_id == group.id
                    ),
                    tuple(
                        TargetSpec(str(target.id), "phrase", target.value)
                        for target in targets
                        if target.ad_group_id == group.id
                    ),
                    group_targeting(by_name[group.theme]),
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
        units: dict[str, tuple[list[dict], PlatformLimit]],
        now: datetime,
        skip: set[str],
    ) -> tuple[dict[str, str], dict[str, str]]:
        """Включение созданного: возвращает причины площадок, которые рекламу не включили, и площадки, чьи
        объявления площадка ещё проверяет (не ошибка: включение применено, после одобрения цель доводит
        sync.settle_review). Запуск доводит до конца остальные. Платформы из skip (сбой создания) не включаются
        целиком: уже созданное ими остаётся остановленным, пока повтор не доведёт площадку. Включение каждой
        кампании — своей записью. Запись, оставшаяся pending (площадка молчит), прерывает запуск: её доведёт
        сверка, повтор продолжит."""
        # Блокировку бизнеса уже держит запуск: изменение применяется под ней, а не берёт её второй раз.
        failures = {}
        awaiting = {}
        for key, (campaign, refs) in created.items():
            platform, limit = campaign_platform(key), units[key][1]
            if platform in skip:
                continue
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
                reason_params={"budget": business.month_budget_kop, "campaign": campaign.name},
                evidence={
                    "plan_id": plan.id,
                    "daily_limit_kop": limit.daily_limit,
                    "period_cap_kop": limit.remaining_cap,
                },
                entity_type="campaign",
                entity_id=refs.campaign_id,
                now=now,
            )
            if change.status == "pending":
                raise PlatformError(f"Площадка «{TITLES[platform]}» не ответила, включилась ли реклама.")
            if change.status != "applied":
                detail = change.evidence["_service"].get("error")
                failures[platform] = f"Площадка «{TITLES[platform]}» не включила рекламу. {detail or ''}".strip()
            elif change.evidence["_service"].get("review"):
                awaiting[platform] = MODERATION_WAIT.format(TITLES[platform])
        return failures, awaiting

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
        for key, campaign_id in plan.payload.get("launch", {}).get("campaigns", {}).items():
            platform = campaign_platform(key)
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
        for key, campaign_id in plan.payload["launch"]["campaigns"].items():
            if campaign_platform(key) not in platforms:
                continue
            campaign = self.repository.campaign(campaign_id)
            campaign.status = "active"
            groups, ads, _ = self.repository.tree_rows(campaign.id)
            for row in groups:
                row.status = "active"
            for ad in ads:
                ad.status = "moderation"
        for platform in platforms:
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
        # Повтор запуска никогда не сдвигает дату первого старта. Разрешённый расход месяца plan_budget
        # считает как max(начало месяца, started_on): в новом месяце остаток и так полный, а перезапись
        # даты (в том числе «Повторить» по второй площадке) ложно обнуляла бы остаток против полного расхода.
        worker_meta = business.settings.get("_worker", {})
        previous = worker_meta.get("started_on")
        worker_meta = worker_meta | {"started_on": previous if previous is not None else today.isoformat()}
        business.settings = business.settings | {"_worker": worker_meta}
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
            "platform_errors": launch.get("platform_errors", {}),
            "awaiting_moderation": launch.get("awaiting_moderation", []),
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
            key: await stop_interrupted(worker, business, campaign_id)
            for key, campaign_id in plan.payload["launch"]["campaigns"].items()
        }
        failed = list(dict.fromkeys(campaign_platform(key) for key, result in stops.items() if result == "failed"))
        revoked = list(dict.fromkeys(campaign_platform(key) for key, result in stops.items() if result == "revoked"))
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
    # Кампания, которую площадка проверяла во время остановки, могла не принять паузу черновика: после проверки её
    # останавливает settle_review (у бизнеса до запуска — только паузы).
    from app.services.sync import settle_review

    await settle_review(worker, business)


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
            # Включение запуска применено (кампания на проверке или выключена площадкой после неё): цель — пауза.
            if campaign.status == "active":
                campaign.status = "paused_auto"
                worker.repository.save(campaign)
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


async def sync_removed_services(worker: Worker, business: Business) -> None:
    """Описание поменяли после запуска: кампания Директа, все услуги которой из описания убраны, останавливается, а в
    общей с другими услугами кампании останавливаются объявления групп убранных услуг. Услугу вернули — её
    кампания или объявления, остановленные так, продолжают работать. Услуги сравниваются по ключевой фразе, как при
    сборке групп (direct_services): «маникюр» и « Маникюр » — одна услуга. Действие владельца: идёт и в пробном
    режиме."""
    launched = [plan for plan in worker.repository.rows(Plan, business.id) if plan.status == "launched"]
    if not launched:
        return
    plan = max(launched, key=lambda item: item.id)
    services = {phrase(service.name, business.city) for service in description_draft(business).services}
    # Неподтверждённое описание сохраняется и без услуг: пустой список — не «убрали всё».
    if not services:
        return
    groups = {
        group["id"]: group for group in plan.payload["groups"] if group["platform"] == "direct" and "service" in group
    }
    removed = {id_ for id_, group in groups.items() if phrase(group["service"], business.city) not in services}
    batches = plan.payload["launch"].get("batches", {})
    current = {campaign.id: campaign for campaign in worker.current_campaigns(business)}
    # Объявления, которые останавливали из-за убранной услуги: при её возврате включаются только они.
    paused_ads = {
        operation["entity_id"]
        for change in worker.repository.rows(ChangeLog, business.id, reason_code="SERVICE_REMOVED", status="applied")
        for operation in change.operations
        if operation["entity_type"] == "ad"
    }
    for key, campaign_id in plan.payload["launch"]["campaigns"].items():
        campaign = current.get(campaign_id)
        if campaign_platform(key) != "direct" or campaign is None or not campaign.external_id:
            continue
        ids = [id_ for id_ in batches.get(key, groups) if id_ in groups]
        gone = [groups[id_]["service"] for id_ in ids if id_ in removed]
        last = worker.repository.last_status_change(business.id, "direct", "campaign", campaign.external_id)
        stopped = last is not None and last.reason_code == "SERVICE_REMOVED" and last.kind == "pause"
        if not (gone or stopped or paused_ads):
            continue
        themes = {groups[id_]["name"]: groups[id_]["service"] for id_ in ids}
        gone_themes = {groups[id_]["name"] for id_ in ids if id_ in removed}
        rows = {row.id: row.theme for row in worker.repository.rows(AdGroup, business.id, campaign_id=campaign.id)}
        ads = [ad for ad in worker.repository.rows(Ad, business.id) if ad.ad_group_id in rows and ad.external_id]
        stale = [ad for ad in ads if rows[ad.ad_group_id] in gone_themes]
        back = [ad for ad in ads if ad.external_id in paused_ads and rows[ad.ad_group_id] not in gone_themes]
        if not (gone or stopped or back):
            continue
        states = await worker.resolve_adapter(business.id, "direct").get_state(worker.refs(campaign))
        if campaign.external_id not in states:
            continue
        status = states[campaign.external_id].status
        if gone and len(gone) == len(ids):
            if status in STOPPED:
                continue
            await worker.apply(
                business,
                campaign,
                [Operation("set_status", "campaign", campaign.external_id, "status", status, "paused_owner")],
                actor="owner",
                kind="pause",
                reason_code="SERVICE_REMOVED",
                # Запись от имени владельца («Вы»): услугу убрал он, а кампанию по его правке остановил автопилот.
                reason_text=f"Вы убрали из описания: {', '.join(gone)}. "
                f"Автопилот остановил кампанию «{campaign.name}».",
                suffix=f"services:{campaign.id}:{','.join(gone)}",
            )
            continue
        pauses = [
            Operation("set_status", "ad", ad.external_id, "status", states[ad.external_id].status, "paused_owner")
            for ad in stale
            if ad.external_id in states and states[ad.external_id].status not in STOPPED
        ]
        if pauses:
            await worker.apply(
                business,
                campaign,
                pauses,
                actor="owner",
                kind="pause",
                reason_code="SERVICE_REMOVED",
                reason_text=f"Вы убрали из описания: {', '.join(gone)}. Автопилот остановил объявления этих услуг "
                f"в кампании «{campaign.name}».",
                suffix=f"services:{campaign.id}:{','.join(gone)}",
            )
        if business.status != "running":
            continue
        if stopped and status == "paused_owner":
            await worker.apply(
                business,
                campaign,
                [Operation("set_status", "campaign", campaign.external_id, "status", status, "active")],
                actor="owner",
                kind="resume",
                reason_code="SERVICE_RETURNED",
                reason_text=f"Вы вернули услугу в описание. Автопилот снова включил кампанию «{campaign.name}».",
                suffix=f"services:{campaign.id}:returned",
            )
        resumed = [
            ad
            for ad in back
            if ad.external_id in states
            and states[ad.external_id].status == "paused_owner"
            and (change := worker.repository.last_status_change(business.id, "direct", "ad", ad.external_id))
            and change.reason_code == "SERVICE_REMOVED"
        ]
        if resumed:
            names = sorted({themes[rows[ad.ad_group_id]] for ad in resumed if rows[ad.ad_group_id] in themes})
            await worker.apply(
                business,
                campaign,
                [Operation("set_status", "ad", ad.external_id, "status", "paused_owner", "active") for ad in resumed],
                actor="owner",
                kind="resume",
                reason_code="SERVICE_RETURNED",
                reason_text=f"Вы вернули в описание: {', '.join(names)}. Автопилот снова включил их объявления "
                f"в кампании «{campaign.name}».",
                suffix=f"services:{campaign.id}:returned-ads",
            )
