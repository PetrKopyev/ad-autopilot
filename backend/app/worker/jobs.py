import asyncio
import logging
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import select

from app.db.models import (
    Ad,
    AdGroup,
    Business,
    Campaign,
    ChangeLog,
    Experiment,
    LeadsSurvey,
    StatsDaily,
    Target,
    WorkerRun,
)
from app.demo_flags import demo_now
from app.platforms.base import AdPlatform, ExternalRefs, Operation
from app.platforms.fake import FakeAdapter, VirtualClock
from app.services.changes import ChangeConflict, ChangesRepository, ChangesService
from app.worker.clock import MOSCOW, Clock, SystemClock, today
from app.worker.locks import business_lock

logger = logging.getLogger(__name__)


class WorkerRepository(ChangesRepository):
    """Хранилище воркера; тот же журнал и та же блокировка, что у ChangesService."""

    def rows(self, model, business_id: int | None = None) -> list:
        statement = select(model)
        if business_id is not None:
            if model is Business:
                statement = statement.where(Business.id == business_id)
            elif model is AdGroup:
                statement = statement.join(Campaign).where(Campaign.business_id == business_id)
            elif model in (Ad, Target):
                statement = statement.join(AdGroup).join(Campaign).where(Campaign.business_id == business_id)
            else:
                statement = statement.where(model.business_id == business_id)
        return list(self.db.scalars(statement))

    def find(self, model, **keys):
        return next(
            (
                item
                for item in self.rows(model, keys.get("business_id"))
                if all(getattr(item, name) == value for name, value in keys.items())
            ),
            None,
        )

    def save(self, *items) -> None:
        self.db.add_all(items)
        self.db.commit()

    def refresh(self, item) -> None:
        self.db.refresh(item)

    @contextmanager
    def business_lock(self, business_id: int) -> Iterator[None]:
        with business_lock(self.db.get_bind(), business_id):
            yield

    def pending(self, business_id: int | None = None) -> list[ChangeLog]:
        return [
            item
            for item in self.rows(ChangeLog, business_id)
            if item.status == "pending" and not item.evidence.get("_service", {}).get("dry_run")
        ]

    def local_entities(self, business_id: int, platform: str | None) -> dict:
        campaigns = [item for item in self.rows(Campaign, business_id) if item.platform == platform]
        groups = [item for item in self.rows(AdGroup, business_id) if item.campaign_id in {c.id for c in campaigns}]
        ads = [item for item in self.rows(Ad, business_id) if item.ad_group_id in {g.id for g in groups}]
        targets = [item for item in self.rows(Target, business_id) if item.ad_group_id in {g.id for g in groups}]
        return {
            (kind, item.external_id): item
            for kind, items in (("campaign", campaigns), ("group", groups), ("ad", ads), ("target", targets))
            for item in items
            if item.external_id
        }


class MemoryWorkerRepository(WorkerRepository):
    """Та же служба применения, модели и сценарии, без подключения к БД."""

    def __init__(self) -> None:
        self.tables: dict[type, list] = {}
        self._locked: set[int] = set()

    def rows(self, model, business_id: int | None = None) -> list:
        items = list(self.tables.get(model, ()))
        if business_id is None:
            return items
        if model is Business:
            return [item for item in items if item.id == business_id]
        if model is AdGroup:
            ids = {item.id for item in self.rows(Campaign, business_id)}
            return [item for item in items if item.campaign_id in ids]
        if model in (Ad, Target):
            ids = {item.id for item in self.rows(AdGroup, business_id)}
            return [item for item in items if item.ad_group_id in ids]
        return [item for item in items if item.business_id == business_id]

    def save(self, *items) -> None:
        for item in items:
            table = self.tables.setdefault(type(item), [])
            if item not in table:
                if hasattr(item, "id") and item.id is None:
                    item.id = max((row.id for row in table), default=0) + 1
                table.append(item)

    @contextmanager
    def business_lock(self, business_id: int) -> Iterator[None]:
        # Виртуальный воркер выполняет шаги последовательно; конкуренцию PostgreSQL проверяем отдельно.
        yield

    def refresh(self, item) -> None:
        pass

    def business(self, business_id: int) -> Business:
        business = self.find(Business, id=business_id)
        if business is None:
            raise ValueError("Бизнес не найден.")
        return business

    def get(self, change_id: int, business_id: int) -> ChangeLog | None:
        return self.find(ChangeLog, id=change_id, business_id=business_id)

    def recent_owner_changes(self, business_id: int, now: datetime) -> list[ChangeLog]:
        return [
            item
            for item in self.rows(ChangeLog, business_id)
            if item.actor == "owner"
            and item.status in {"applied", "reverted"}
            and item.created_at > now - timedelta(hours=24)
        ]

    def experiments(self, change: ChangeLog) -> list[Experiment]:
        return [item for item in self.rows(Experiment, change.business_id) if item.change_id == change.id]

    def reverts(self, change_id: int) -> list[ChangeLog]:
        return [item for item in self.rows(ChangeLog) if item.reverts_id == change_id]


class Worker:
    def __init__(
        self,
        repository: WorkerRepository,
        resolve_adapter: Callable[[int, str], AdPlatform],
        *,
        clock: Clock | None = None,
        dry_run: bool = True,
        rewrite_rejected: Callable | None = None,
        experiment_factory: Callable | None = None,
    ) -> None:
        self.repository = repository
        self.resolve_adapter = resolve_adapter
        self.clock = clock or SystemClock()
        self.changes = ChangesService(repository, dry_run=dry_run)
        self.dry_run = dry_run
        self.rewrite_rejected = rewrite_rejected
        self.experiment_factory = experiment_factory
        self.step_key = ""
        self._context_at = None
        self._contexts: dict[int, dict[tuple[str, str], dict]] = {}

    def proposed(self, business: Business) -> bool:
        return self.dry_run or business.settings.get("dry_run", False)

    def metadata(self, business: Business) -> dict:
        return deepcopy(business.settings.get("_worker", {}))

    def remember(self, business: Business, **values) -> None:
        business.settings = business.settings | {"_worker": self.metadata(business) | values}
        self.repository.save(business)

    def refs(self, campaign: Campaign) -> ExternalRefs:
        entities = self.repository.local_entities(campaign.business_id, campaign.platform)
        groups = {
            item.id: item for item in entities.values() if isinstance(item, AdGroup) and item.campaign_id == campaign.id
        }
        context = {"daily_limit_kop": campaign.daily_limit_kop, "period_cap_kop": campaign.period_cap_kop}
        slot = schedule_key("budget_guard", self.clock.now)
        if self._context_at != slot:
            self._contexts.clear()
            self._context_at = slot
        if campaign.business_id not in self._contexts:
            self._contexts[campaign.business_id] = {}
            history = sorted(self.repository.rows(ChangeLog, campaign.business_id), key=lambda item: item.id)
            for change in history:
                self.cache_context(change)
        context = self._contexts[campaign.business_id].get((campaign.platform, campaign.external_id), {}) | context
        return ExternalRefs(
            campaign.external_id,
            {str(item.id): item.external_id for item in groups.values()},
            {
                str(item.id): item.external_id
                for item in entities.values()
                if isinstance(item, Ad) and item.ad_group_id in groups
            },
            {
                str(item.id): item.external_id
                for item in entities.values()
                if isinstance(item, Target) and item.ad_group_id in groups
            },
            context,
        )

    def cache_context(self, change: ChangeLog) -> None:
        if change.status != "applied" or change.business_id not in self._contexts:
            return
        contexts = self._contexts[change.business_id]
        stored = change.evidence.get("_service", {}).get("refs", {})
        if stored.get("budget_context"):
            contexts[(change.platform, stored["campaign_id"])] = stored["budget_context"]
        for op in change.operations:
            if op.get("budget_context"):
                contexts[(change.platform, op["entity_id"])] = op["budget_context"]

    def demo_available(self, business: Business) -> bool:
        for campaign in self.campaigns(business):
            adapter = self.resolve_adapter(business.id, campaign.platform)
            if isinstance(adapter, FakeAdapter) and campaign.external_id not in adapter._states:
                self.notice(
                    business,
                    "DEMO_STATE_LOST",
                    "Демо-данные площадки потеряны после перезапуска API. Создайте новый демо-запуск.",
                    key=f"demo_state_lost:{business.id}",
                )
                return False
        return True

    @contextmanager
    def business_clock(self, business: Business) -> Iterator[None]:
        """Шаг демо-бизнеса идёт в его дате после перемотки; затем часы воркера возвращаются."""
        base = self.clock
        fallback = base.now
        now = demo_now(business, fallback)
        if now != fallback:
            self.clock = VirtualClock(now)
        try:
            yield
        finally:
            self.clock = base

    def campaigns(self, business: Business) -> list[Campaign]:
        return [item for item in self.repository.rows(Campaign, business.id) if item.external_id]

    async def apply(
        self,
        business: Business,
        campaign: Campaign,
        operations: list[Operation],
        *,
        reason_code: str,
        kind: str,
        reason_params: dict | None = None,
        reason_text: str | None = None,
        actor: str = "autopilot",
        suffix: str = "",
        evidence: dict | None = None,
        entity_type: str | None = None,
        entity_id: str | None = None,
    ):
        if not operations:
            return None
        key = f"{self.step_key}:{campaign.id}:{suffix}"
        existing = [
            item for item in self.repository.rows(ChangeLog, business.id) if item.evidence.get("worker_key") == key
        ]
        applied = next((item for item in existing if item.status == "applied"), None)
        proposed = next((item for item in existing if item.evidence.get("_service", {}).get("dry_run")), None)
        if applied is not None or proposed is not None:
            return applied if applied is not None else proposed
        change = await self.changes.apply_change(
            business_id=business.id,
            adapter=self.resolve_adapter(business.id, campaign.platform),
            refs=self.refs(campaign),
            operations=operations,
            actor=actor,
            kind=kind,
            platform=campaign.platform,
            reason_code=reason_code,
            reason_params=reason_params,
            reason_text=reason_text,
            evidence={"worker_key": key, **(evidence or {})},
            entity_type=entity_type,
            entity_id=entity_id,
            now=self.clock.now,
        )
        if change.status != "applied" and not self.proposed(business):
            raise ChangeConflict("Применение не подтверждено; шаг будет повторён после сверки.")
        self.cache_context(change)
        return change

    def notice(self, business: Business, reason_code: str, text: str, *, key: str = "", **details) -> None:
        notice_key = key or f"{self.step_key}:{reason_code}"
        if details.get("target_id") is not None:
            notice_key = f"{notice_key}:{details['target_id']}"
        if any(
            row.kind == "notification" and row.log[0].get("key") == notice_key
            for row in self.repository.rows(WorkerRun, business.id)
            if row.log
        ):
            return
        self.repository.save(
            WorkerRun(
                kind="notification",
                business_id=business.id,
                started_at=self.clock.now,
                finished_at=self.clock.now,
                status="proposed" if self.proposed(business) else "ok",
                units_spent=0,
                log=[{"key": notice_key, "reason_code": reason_code, "text": text, **details}],
            )
        )

    async def startup(self) -> None:
        from app.services.budget_guard import budget_guard

        for business in self.repository.rows(Business):
            try:
                with self.repository.business_lock(business.id):
                    if not self.demo_available(business):
                        continue
                    await self.changes.recover_pending(
                        self.resolve_adapter, business_id=business.id, now=self.clock.now
                    )
                    if business.status in {"running", "budget_stop"}:
                        self.step_key = f"startup:{self.clock.now.isoformat()}"
                        await budget_guard(self, business, reconcile=True)
            except Exception:
                logger.error("Startup reconciliation failed for business %s", business.id)

    async def run(self, kind: str, business_id: int | None = None, *, key: str | None = None) -> None:
        if kind == "heartbeat":
            self.repository.save(
                WorkerRun(
                    kind=kind,
                    business_id=None,
                    started_at=self.clock.now,
                    finished_at=self.clock.now,
                    status="ok",
                    units_spent=0,
                    log=[],
                )
            )
            return
        business = self.repository.business(business_id)
        if business.status not in {"running", "budget_stop", "paused"}:
            return
        with self.repository.business_lock(business.id), self.business_clock(business):
            if not self.demo_available(business):
                return
            self.step_key = key or schedule_key(kind, self.clock.now)
            runs = self.repository.rows(WorkerRun, business.id)
            if any(
                item.kind == kind and item.status == "ok" and item.log and item.log[0].get("key") == self.step_key
                for item in runs
            ):
                return
            run = WorkerRun(
                kind=kind,
                business_id=business.id,
                started_at=self.clock.now,
                status="running",
                units_spent=0,
                log=[{"key": self.step_key}],
            )
            self.repository.save(run)
            try:
                recovered = await self.changes.recover_pending(
                    self.resolve_adapter, business_id=business.id, now=self.clock.now
                )
                for change in recovered:
                    self.cache_context(change)
                if kind != "budget_guard" and self.repository.pending(business.id):
                    raise ChangeConflict("Площадка пока недоступна для сверки.")
                await handlers()[kind](self, business)
                run.status = "ok"
            except Exception:
                run.status = "failed"
                # Ответы площадок могут содержать закрытые данные: сохраняем только безопасный код.
                run.log = run.log + [{"reason_code": "JOB_FAILED"}]
                raise
            finally:
                run.finished_at = self.clock.now
                self.repository.save(run)

    async def run_due(self, skip: Callable[[Business], bool] | None = None) -> None:
        """Те же слоты расписания для APScheduler и виртуальных часов симулятора."""
        self._contexts.clear()
        await self.run("heartbeat")
        now = self.clock.now.astimezone(MOSCOW)
        for business in self.repository.rows(Business):
            if skip and skip(business):
                continue
            kinds = ["budget_guard"]
            if now.minute < 30:
                kinds.append("sync_moderation")
                if now.hour % 3 == 0:
                    kinds.append("sync_stats")
            if now.hour >= 6:
                kinds.extend(("optimize", "experiments_tick", "rebalance_budget"))
            if now.weekday() == 0 and now.hour >= 10:
                kinds.append("clients_survey")
            # Ежедневная проверка догоняет первый день после простоя и напоминает за три дня.
            kinds.insert(0, "month_rollover")
            for kind in kinds:
                try:
                    await self.run(kind, business.id)
                except Exception:
                    logger.error("Worker job %s failed for business %s", kind, business.id)


def schedule_key(kind: str, now: datetime) -> str:
    local = now.astimezone(MOSCOW)
    if kind == "budget_guard":
        slot = local.replace(minute=local.minute // 30 * 30, second=0, microsecond=0).isoformat()
    elif kind in {"sync_moderation", "sync_stats"}:
        hours = 3 if kind == "sync_stats" else 1
        slot = local.replace(hour=local.hour // hours * hours, minute=0, second=0, microsecond=0).isoformat()
    elif kind == "clients_survey":
        slot = (local.date() - timedelta(days=local.weekday())).isoformat()
    else:
        slot = local.date().isoformat()
    return f"{kind}:{slot}"


async def clients_survey(worker: Worker, business: Business) -> None:
    week_start = today(worker.clock) - timedelta(days=today(worker.clock).weekday(), weeks=1)
    if worker.repository.find(LeadsSurvey, business_id=business.id, week_start=week_start):
        return
    goals = {str(goal) for goal in business.lead_goal_ids} or {"lead"}
    leads = sum(
        sum(value for goal, value in row.conversions.items() if goal in goals)
        for row in worker.repository.rows(StatsDaily, business.id)
        if week_start <= row.date < week_start + timedelta(days=7)
    )
    if leads:
        worker.repository.save(
            LeadsSurvey(
                business_id=business.id,
                week_start=week_start,
                leads_count=leads,
                clients_count=None,
                asked_at=worker.clock.now,
            )
        )


def handlers() -> dict[str, Callable]:
    from app.services.budget_guard import budget_guard
    from app.services.month import month_rollover
    from app.services.optimize import experiments_tick, optimize, rebalance_budget
    from app.services.sync import sync_moderation, sync_stats

    return {
        function.__name__: function
        for function in (
            budget_guard,
            sync_moderation,
            sync_stats,
            optimize,
            experiments_tick,
            rebalance_budget,
            clients_survey,
            month_rollover,
        )
    }


def dispatch(session_factory, resolve_adapter, kind: str, **options: Any) -> None:
    """Точка входа планировщика: шаг для всех бизнесов, ошибка одного не мешает остальным."""
    with session_factory() as db:
        worker = Worker(WorkerRepository(db), resolve_adapter, **options)
        asyncio.run(run_everywhere(worker, kind))


async def run_everywhere(worker: Worker, kind: str) -> None:
    if kind == "heartbeat":
        await worker.run(kind)
        return
    for business in worker.repository.rows(Business):
        try:
            await worker.run(kind, business.id)
        except Exception as error:
            logger.error("Worker job %s failed for business %s: %s", kind, business.id, type(error).__name__)
