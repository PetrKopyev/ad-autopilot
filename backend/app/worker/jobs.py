import asyncio
import logging
from collections.abc import Callable, Collection, Iterator
from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import DateTime, and_, cast, delete, func, literal_column, or_, select, update
from sqlalchemy.dialects.postgresql import JSONB, distinct_on, insert
from sqlalchemy.orm import aliased

from app.auth.vk import RENEW_MARGIN
from app.db.models import (
    Ad,
    AdGroup,
    Business,
    Campaign,
    ChangeLog,
    Experiment,
    LeadsSurvey,
    OAuthState,
    Plan,
    PlatformAccount,
    Session,
    StatsDaily,
    Target,
    WorkerRun,
)
from app.db.query import Window, condition, matches
from app.demo_flags import demo_now
from app.platforms.base import AdPlatform, ExternalRefs, Operation, lead_count
from app.platforms.fake import FakeAdapter, VirtualClock
from app.services.changes import (
    CLOSED_ACCOUNT,
    PAUSES,
    PENDING,
    SETTLE,
    ChangeConflict,
    ChangesRepository,
    ChangesService,
    replaced_campaigns,
    settled_pause,
)
from app.services.onboarding import DRAFT_STATUSES
from app.worker.clock import MOSCOW, Clock, SystemClock, today
from app.worker.locks import business_lock

logger = logging.getLogger(__name__)


RUN_SLOT = ("business_id", "kind", "slot_key")
HEARTBEAT_KEEP = timedelta(days=1)
RUNS_KEEP = timedelta(days=90)
# Прогон старше этого считается брошенным (процесс воркера умер), и слот можно занять снова.
RUN_STALE = timedelta(minutes=30)
INTERRUPTED = [{"reason_code": "JOB_INTERRUPTED"}]


def budget_contexts(change: ChangeLog) -> bool:
    """Применённое изменение, из которого воркер восстанавливает контекст бюджета кампании."""
    stored = change.evidence.get("_service", {}).get("refs", {}).get("budget_context")
    return bool(stored) or any(op.get("budget_context") for op in change.operations)


def notice_theme(run: WorkerRun) -> str:
    """Тема уведомления: объект, о котором оно (target_id), а без объекта — текст. Повторы темы показываются одним."""
    entry = run.log[0]
    return str(entry["target_id"]) if entry.get("target_id") is not None else entry.get("text", "")


def sql_theme():
    return func.coalesce(WorkerRun.log[0]["target_id"].astext, WorkerRun.log[0]["text"].astext, "")


class NoticesRepository:
    """Непрочитанные уведомления: их читают воркер, экран уведомлений и запуск рекламы (services.launch)."""

    def unread_filter(self, business_id: int):
        return (
            WorkerRun.business_id == business_id,
            WorkerRun.kind == "notification",
            WorkerRun.status == "ok",
            ~func.jsonb_path_exists(WorkerRun.log, literal_column("'$[*].read_at'::jsonpath")),
        )

    def mark_read(self, business_id: int, ids: Collection[int], now: datetime) -> None:
        self.db.execute(
            update(WorkerRun)
            .where(WorkerRun.business_id == business_id, WorkerRun.kind == "notification", WorkerRun.id.in_(ids))
            .values(log=WorkerRun.log.op("||")(cast([{"read_at": now.isoformat()}], JSONB)))
        )
        self.db.commit()


class WorkerRepository(NoticesRepository, ChangesRepository):
    """Хранилище воркера; тот же журнал и та же блокировка, что у ChangesService."""

    def rows(self, model, business_id: int | None = None, **filters) -> list:
        """Строки бизнеса; фильтры — значение, набор значений или Window — выполняются в SQL."""
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
        for name, value in filters.items():
            statement = statement.where(condition(getattr(model, name), value))
        return list(self.db.scalars(statement))

    def find(self, model, **keys):
        return next(iter(self.rows(model, keys.pop("business_id", None), **keys)), None)

    def expiring_vk_accounts(self, now: datetime) -> list[PlatformAccount]:
        """VK-кабинеты с истекающим токеном: фоновый шаг воркера продлевает их до needs_reauth."""
        return list(
            self.db.scalars(
                select(PlatformAccount).where(
                    PlatformAccount.platform == "vk",
                    PlatformAccount.status == "ok",
                    PlatformAccount.expires_at.is_not(None),
                    PlatformAccount.expires_at < now + RENEW_MARGIN,
                )
            )
        )

    def save(self, *items) -> None:
        self.db.add_all(items)
        self.db.commit()

    def refresh(self, item) -> None:
        self.db.refresh(item)

    def fresh_business_state(self, business_id: int) -> tuple[str, dict] | None:
        """Свежие (status, settings) напрямую из базы, минуя identity map: тик держит копию бизнеса
        весь цикл, а владелец мог уже нажать «Остановить» или «Продолжить». None — бизнес удалён."""
        return self.db.execute(
            select(Business.status, Business.settings).where(Business.id == business_id)
        ).one_or_none()

    def remove(self, item) -> None:
        self.db.delete(item)
        self.db.commit()

    @contextmanager
    def business_lock(self, business_id: int) -> Iterator[None]:
        with business_lock(self.db.get_bind(), business_id):
            yield

    def start_run(self, business_id: int, kind: str, slot_key: str, now: datetime) -> WorkerRun | None:
        """Занимает слот шага.

        Слот с успешным прогоном не выдаётся. Упавший прогон перезапускается сразу, а идущий — только когда он старше
        RUN_STALE: второй воркер обычно сюда не доходит из-за блокировки бизнеса, а прогон умершего процесса
        перезапускается после RUN_STALE или сразу после старта нового процесса (interrupt_runs). Журнал прошлых попыток
        сохраняется, к нему добавляется новая запись.
        """
        statement = insert(WorkerRun).values(
            kind=kind,
            business_id=business_id,
            slot_key=slot_key,
            units_spent=0,
            status="running",
            started_at=now,
            finished_at=None,
            log=[{"key": slot_key}],
        )
        statement = statement.on_conflict_do_update(
            index_elements=RUN_SLOT,
            set_={
                "status": "running",
                "started_at": now,
                "finished_at": None,
                "log": WorkerRun.log.op("||")(statement.excluded.log),
            },
            where=and_(
                WorkerRun.status != "ok", or_(WorkerRun.status != "running", WorkerRun.started_at <= now - RUN_STALE)
            ),
        ).returning(WorkerRun)
        run = self.db.scalar(statement, execution_options={"populate_existing": True})
        self.db.commit()
        return run

    def interrupt_runs(self, business_id: int, now: datetime) -> None:
        """Вызывается при старте воркера под блокировкой бизнеса: прогон в статусе running брошен умершим процессом."""
        self.db.execute(
            update(WorkerRun)
            .where(WorkerRun.business_id == business_id, WorkerRun.status == "running")
            .values(status="failed", finished_at=now, log=WorkerRun.log.op("||")(cast(INTERRUPTED, JSONB)))
        )
        self.db.commit()

    def notify(self, run: WorkerRun) -> None:
        """Одно уведомление на ключ, в том числе при одновременной записи из API и воркера.

        Уведомления не чистятся: ключ вида experiment:… или demo_state_lost:… должен защищать от повтора всегда.
        """
        values = {column.key: getattr(run, column.key) for column in WorkerRun.__table__.columns if column.key != "id"}
        self.db.execute(insert(WorkerRun).values(values).on_conflict_do_nothing(index_elements=RUN_SLOT))
        self.db.commit()

    def unread_notifications(
        self, business_id: int, codes: Collection[str], limit: int, now: datetime
    ) -> list[WorkerRun]:
        """Непрочитанные уведомления бизнеса с этими кодами, новые сверху; прочитанное помечено read_at в журнале.

        Из уведомлений одной темы (notice_theme) отдаётся только самое новое: повторы не вытесняют остальные.
        Уведомление о сроке (expires_at в журнале) после него не показывается: оно уже неправда."""
        code, theme = WorkerRun.log[0]["reason_code"].astext, sql_theme()
        expires_at = WorkerRun.log[0]["expires_at"].astext
        latest = (
            select(WorkerRun)
            .where(
                *self.unread_filter(business_id),
                code.in_(codes),
                or_(expires_at.is_(None), cast(expires_at, DateTime(timezone=True)) > now),
            )
            .ext(distinct_on(code, theme))
            .order_by(code, theme, WorkerRun.started_at.desc(), WorkerRun.id.desc())
            .subquery()
        )
        row = aliased(WorkerRun, latest)
        return list(self.db.scalars(select(row).order_by(row.started_at.desc(), row.id.desc()).limit(limit)))

    def same_theme(self, run: WorkerRun) -> list[int]:
        """Непрочитанные уведомления той же темы, что и run: «Понятно» убирает их вместе."""
        return list(
            self.db.scalars(
                select(WorkerRun.id).where(
                    *self.unread_filter(run.business_id),
                    WorkerRun.log[0]["reason_code"].astext == run.log[0]["reason_code"],
                    sql_theme() == notice_theme(run),
                )
            )
        )

    def last_status_change(self, business_id: int, platform: str, entity_type: str, entity_id: str) -> ChangeLog | None:
        """Последнее применённое изменение статуса объекта, кроме паузы, доведённой шагом модерации (settled_pause);
        поиск по операциям идёт в JSONB, а не в Python."""
        operation = {"entity_type": entity_type, "entity_id": entity_id, "field": "status"}
        return self.db.scalar(
            select(ChangeLog)
            .where(
                ChangeLog.business_id == business_id,
                ChangeLog.platform == platform,
                ChangeLog.status == "applied",
                ChangeLog.operations.contains([operation]),
                or_(
                    ChangeLog.reason_code.is_distinct_from(SETTLE),
                    ~or_(*(ChangeLog.operations.contains([operation | {"after": after}]) for after in PAUSES)),
                ),
            )
            .order_by(ChangeLog.id.desc())
            .limit(1)
        )

    def last_check(self, business_id: int) -> datetime | None:
        return self.db.scalar(
            select(func.max(WorkerRun.finished_at)).where(
                or_(WorkerRun.business_id.is_(None), WorkerRun.business_id == business_id),
                WorkerRun.kind.in_(("heartbeat", "budget_guard")),
                WorkerRun.status == "ok",
            )
        )

    def latest_run(self, business_id: int, kind: str) -> WorkerRun | None:
        return self.db.scalar(
            select(WorkerRun)
            .where(WorkerRun.business_id == business_id, WorkerRun.kind == kind)
            .order_by(WorkerRun.started_at.desc(), WorkerRun.id.desc())
            .limit(1)
        )

    def applied_budget_contexts(self, business_id: int) -> list[ChangeLog]:
        """Применённые изменения с контекстом бюджета, по порядку; остальная история воркеру для refs не нужна."""
        stored = ChangeLog.evidence[("_service", "refs", "budget_context")]
        return list(
            self.db.scalars(
                select(ChangeLog)
                .where(
                    ChangeLog.business_id == business_id,
                    ChangeLog.status == "applied",
                    or_(
                        and_(stored.astext.is_not(None), stored.astext != "{}"),
                        func.jsonb_path_exists(
                            ChangeLog.operations, literal_column("'$[*] ? (@.budget_context != null)'::jsonpath")
                        ),
                    ),
                )
                .order_by(ChangeLog.id)
            )
        )

    def prune_runs(self, now: datetime) -> None:
        """Пульс хранится HEARTBEAT_KEEP, прогоны шагов — RUNS_KEEP; уведомления не удаляются (см. notify). Пульс
        «из будущего» записали перемотки демо до того, как перестали его писать: он только раздувал таблицу."""
        self.db.execute(
            delete(WorkerRun).where(
                or_(
                    and_(
                        WorkerRun.kind == "heartbeat",
                        or_(WorkerRun.started_at < now - HEARTBEAT_KEEP, WorkerRun.started_at > now),
                    ),
                    and_(WorkerRun.kind != "notification", WorkerRun.started_at < now - RUNS_KEEP),
                )
            )
        )
        self.db.commit()

    def prune_auth(self, now: datetime) -> None:
        """Просроченные сессии и запросы входа (вместе с отметками использованных кодов) больше не нужны."""
        self.db.execute(delete(Session).where(Session.expires_at <= now))
        self.db.execute(delete(OAuthState).where(OAuthState.expires_at <= now))
        self.db.commit()


class MemoryWorkerRepository(WorkerRepository):
    """Та же служба применения, модели и сценарии, без подключения к БД."""

    def __init__(self) -> None:
        self.tables: dict[type, list] = {}
        self._locked: set[int] = set()
        # Соединения с базой у памятного хранилища нет: шаги, которым оно нужно (например продление
        # токенов VK), в тестах подменяют.
        self.db = None

    def rows(self, model, business_id: int | None = None, **filters) -> list:
        items = [item for item in self.tables.get(model, ()) if matches(item, filters)]
        if business_id is None:
            return items
        if model is Business:
            scope = {"id": business_id}
        elif model is AdGroup:
            scope = {"campaign_id": {item.id for item in self.rows(Campaign, business_id)}}
        elif model in (Ad, Target):
            scope = {"ad_group_id": {item.id for item in self.rows(AdGroup, business_id)}}
        else:
            scope = {"business_id": business_id}
        return [item for item in items if matches(item, scope)]

    def expiring_vk_accounts(self, now: datetime) -> list[PlatformAccount]:
        return [
            row
            for row in self.tables.get(PlatformAccount, [])
            if row.platform == "vk"
            and row.status == "ok"
            and row.expires_at is not None
            and row.expires_at < now + RENEW_MARGIN
        ]

    def save(self, *items) -> None:
        for item in items:
            table = self.tables.setdefault(type(item), [])
            if item not in table:
                if hasattr(item, "id") and item.id is None:
                    item.id = max((row.id for row in table), default=0) + 1
                table.append(item)

    def remove(self, item) -> None:
        self.tables[type(item)].remove(item)

    @contextmanager
    def business_lock(self, business_id: int) -> Iterator[None]:
        # Виртуальный воркер выполняет шаги последовательно; конкуренцию PostgreSQL проверяем отдельно.
        yield

    def refresh(self, item) -> None:
        pass

    def fresh_business_state(self, business_id: int) -> tuple[str, dict]:
        # Объект в памяти один на всех: свежим он и есть (Worker.run сравнит его с самим собой).
        business = self.business(business_id)
        return business.status, business.settings

    def business(self, business_id: int) -> Business:
        business = self.find(Business, id=business_id)
        if business is None:
            raise ValueError("Бизнес не найден.")
        return business

    def get(self, change_id: int, business_id: int) -> ChangeLog | None:
        return self.find(ChangeLog, id=change_id, business_id=business_id)

    def pending(self, business_id: int | None = None) -> list[ChangeLog]:
        return [
            item
            for item in self.rows(ChangeLog, business_id, status="pending")
            if not item.evidence.get("_service", {}).get("dry_run")
        ]

    def change_by_key(self, business_id: int, key: str) -> ChangeLog | None:
        return self.find(ChangeLog, business_id=business_id, idempotency_key=key)

    def replaced_entities(self, business_id: int) -> set[tuple[str, str]]:
        ids = replaced_campaigns(self.rows(Plan, business_id))
        campaigns = self.rows(Campaign, business_id, id=ids)
        return {(item.platform, item.external_id) for item in campaigns if item.external_id}

    def status_changes(self, business_id: int, since: int = 0) -> list[ChangeLog]:
        return sorted(
            (
                row
                for row in self.rows(ChangeLog, business_id, status="applied")
                if row.id >= since and any(op.get("field") == "status" for op in row.operations)
            ),
            key=lambda row: row.id,
        )

    def campaign_names(self, business_id: int) -> dict[tuple[str, str], str]:
        return {
            (item.platform, item.external_id): item.name
            for item in self.rows(Campaign, business_id)
            if item.external_id
        }

    def local_entities(self, business_id: int, platform: str | None) -> dict:
        campaigns = self.rows(Campaign, business_id, platform=platform)
        groups = self.rows(AdGroup, business_id, campaign_id={item.id for item in campaigns})
        ads = self.rows(Ad, business_id, ad_group_id={item.id for item in groups})
        targets = self.rows(Target, business_id, ad_group_id={item.id for item in groups})
        return {
            (kind, item.external_id): item
            for kind, items in (("campaign", campaigns), ("group", groups), ("ad", ads), ("target", targets))
            for item in items
            if item.external_id
        }

    def recent_owner_changes(self, business_id: int, now: datetime) -> list[ChangeLog]:
        return [
            item
            for item in self.rows(ChangeLog, business_id)
            if item.actor == "owner"
            and item.status in {"applied", "reverted"}
            and item.created_at > now - timedelta(hours=24)
        ]

    def experiments(self, change: ChangeLog) -> list[Experiment]:
        return self.rows(Experiment, change.business_id, change_id=change.id)

    def reverts(self, change_id: int) -> list[ChangeLog]:
        return self.rows(ChangeLog, reverts_id=change_id)

    def start_run(self, business_id: int, kind: str, slot_key: str, now: datetime) -> WorkerRun | None:
        run = self.find(WorkerRun, business_id=business_id, kind=kind, slot_key=slot_key)
        if run is not None and (run.status == "ok" or run.status == "running" and run.started_at > now - RUN_STALE):
            return None
        log = run.log if run is not None else []
        run = run or WorkerRun(kind=kind, business_id=business_id, slot_key=slot_key, units_spent=0)
        run.status, run.started_at, run.finished_at, run.log = "running", now, None, log + [{"key": slot_key}]
        self.save(run)
        return run

    def interrupt_runs(self, business_id: int, now: datetime) -> None:
        for run in self.rows(WorkerRun, business_id, status="running"):
            run.status, run.finished_at, run.log = "failed", now, run.log + INTERRUPTED

    def notify(self, run: WorkerRun) -> None:
        # Как UNIQUE в PostgreSQL: ключ с NULL ни с чем не совпадает.
        keys = {name: getattr(run, name) for name in RUN_SLOT}
        if None in keys.values() or self.find(WorkerRun, **keys) is None:
            self.save(run)

    def unread_rows(self, business_id: int) -> list[WorkerRun]:
        rows = self.rows(WorkerRun, business_id, kind="notification", status="ok")
        unread = [row for row in rows if not any("read_at" in entry for entry in row.log)]
        return sorted(unread, key=lambda row: (row.started_at, row.id), reverse=True)

    def unread_notifications(
        self, business_id: int, codes: Collection[str], limit: int, now: datetime
    ) -> list[WorkerRun]:
        seen, result = set(), []
        for row in self.unread_rows(business_id):
            key = (row.log[0].get("reason_code"), notice_theme(row))
            expires_at = row.log[0].get("expires_at")
            if expires_at is not None and datetime.fromisoformat(expires_at) <= now:
                continue
            if key[0] in codes and key not in seen:
                seen.add(key)
                result.append(row)
        return result[:limit]

    def same_theme(self, run: WorkerRun) -> list[int]:
        key = (run.log[0].get("reason_code"), notice_theme(run))
        return [
            row.id
            for row in self.unread_rows(run.business_id)
            if (row.log[0].get("reason_code"), notice_theme(row)) == key
        ]

    def mark_read(self, business_id: int, ids: Collection[int], now: datetime) -> None:
        for row in self.rows(WorkerRun, business_id, kind="notification", id=set(ids)):
            row.log = row.log + [{"read_at": now.isoformat()}]

    def last_status_change(self, business_id: int, platform: str, entity_type: str, entity_id: str) -> ChangeLog | None:
        return max(
            (
                row
                for row in self.rows(ChangeLog, business_id, platform=platform, status="applied")
                if any(
                    (op.get("entity_type"), op.get("entity_id"), op.get("field")) == (entity_type, entity_id, "status")
                    for op in row.operations
                )
                and not settled_pause(row, entity_type, entity_id)
            ),
            key=lambda row: row.id,
            default=None,
        )

    def last_check(self, business_id: int) -> datetime | None:
        return max(
            (
                row.finished_at
                for row in self.rows(WorkerRun, kind={"heartbeat", "budget_guard"}, status="ok")
                if row.business_id in (None, business_id) and row.finished_at
            ),
            default=None,
        )

    def latest_run(self, business_id: int, kind: str) -> WorkerRun | None:
        return max(self.rows(WorkerRun, business_id, kind=kind), key=lambda row: (row.started_at, row.id), default=None)

    def applied_budget_contexts(self, business_id: int) -> list[ChangeLog]:
        return sorted(
            (row for row in self.rows(ChangeLog, business_id, status="applied") if budget_contexts(row)),
            key=lambda row: row.id,
        )

    def prune_runs(self, now: datetime) -> None:
        self.tables[WorkerRun] = [
            row
            for row in self.tables.get(WorkerRun, [])
            if row.kind == "notification"
            or (row.kind == "heartbeat" and now - HEARTBEAT_KEEP <= row.started_at <= now)
            or (row.kind != "heartbeat" and row.started_at >= now - RUNS_KEEP)
        ]

    def prune_auth(self, now: datetime) -> None:
        for model in (Session, OAuthState):
            self.tables[model] = [row for row in self.tables.get(model, []) if row.expires_at > now]


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
        self._rewound: set[tuple[int, str]] | None = None

    def proposed(self, business: Business) -> bool:
        return self.dry_run or business.settings.get("dry_run", False)

    def metadata(self, business: Business) -> dict:
        return deepcopy(business.settings.get("_worker", {}))

    def remember(self, business: Business, *items, **values) -> None:
        """items — строки, которые сохраняются тем же commit. Значения копируются: иначе словарь
        вызывающего (approvals в цикле sync_moderation) оставался бы в settings по ссылке, его
        мутации после commit меняли бы объект без записи — и объект расходился бы с базой."""
        business.settings = business.settings | {"_worker": self.metadata(business) | deepcopy(values)}
        self.repository.save(business, *items)

    def refs(self, campaign: Campaign, entities: dict | None = None) -> ExternalRefs:
        """entities — local_entities площадки кампании, если шаг их уже прочитал."""
        if entities is None:
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
            for change in self.repository.applied_budget_contexts(campaign.business_id):
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

    def pending(self, business_id: int) -> list[ChangeLog]:
        """Несверенные изменения площадок с рабочим доступом. Изменение кабинета, который требует входа заново, до
        нового входа сверить нечем: оно не должно останавливать шаги остальных площадок."""
        changes = self.repository.pending(business_id)
        # Доступ проверяется раз на площадку: на каждое изменение загрузчик кабинета открывал бы свою сессию БД.
        platforms = {change.platform for change in changes}
        reachable = {platform: self.reachable(business_id, platform) for platform in platforms}
        return [change for change in changes if reachable[change.platform]]

    def reachable(self, business_id: int, platform: str) -> bool:
        """Кабинет, который требует входа заново (CLOSED_ACCOUNT), сверить нечем. Временный сбой загрузчика кабинета
        (например, базы) изменение из сверки не выкидывает: шаги ждут его, как и при недоступной площадке."""
        try:
            self.resolve_adapter(business_id, platform)
        except CLOSED_ACCOUNT:
            return False
        except Exception:
            logger.warning("Кабинет %s бизнеса %s не открылся", platform, business_id)
        return True

    def demo_available(self, business: Business) -> bool:
        for campaign in self.campaigns(business):
            try:
                adapter = self.resolve_adapter(business.id, campaign.platform)
            except CLOSED_ACCOUNT:
                # Кабинет требует входа заново или не расшифровывается: шаги остальных площадок, включая страж
                # бюджета, всё равно идут.
                continue
            if isinstance(adapter, FakeAdapter) and campaign.external_id not in adapter._states:
                self.notice(
                    business,
                    "DEMO_STATE_LOST",
                    "Демо-данные пропали после перезапуска сервера. Войдите в демо заново.",
                    key=f"demo_state_lost:{business.id}",
                )
                return False
        return True

    @contextmanager
    def rewinding(self) -> Iterator[None]:
        """Перемотка демо: доступность демо-данных проверена под той же блокировкой (DemoService.check) и за перемотку
        не меняется, а пройденные и занятые слоты запоминаются в памяти — каждые полчаса их не спрашивают у базы."""
        self._rewound = set()
        try:
            yield
        finally:
            self._rewound = None

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
        """Все кампании бизнеса на площадках: их видят остановка, расход и сверка."""
        return [item for item in self.repository.rows(Campaign, business.id) if item.external_id]

    def current_campaigns(self, business: Business) -> list[Campaign]:
        """Кампании действующего плана: их шаги воркера включают, перераспределяют им бюджет и меняют, а экран
        «Реклама» показывает их объявления.

        Действующий план один — последний запущенный: запускается только последний план черновика
        (services.launch). Кампании плана, который так и не запустился и был заменён новым, остановлены до его сборки
        (PlanService.get_plan ждёт services.launch.recover_launch). Они показывают объявления устаревшего плана: их
        не включают и не дают им долю бюджета, но расход и остановка (campaigns) их по-прежнему видят. Признак берётся
        из плана, а не из статуса кампании: статус переписывает любое применённое изменение."""
        replaced = replaced_campaigns(self.repository.rows(Plan, business.id))
        return [item for item in self.campaigns(business) if item.id not in replaced]

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
        force: bool = False,
    ):
        if not operations:
            return None
        proposed = self.proposed(business) and actor == "autopilot" and not force
        key = f"{self.step_key}:{campaign.id}:{suffix}"
        existing = self.repository.change_by_key(business.id, key)
        if existing is not None:
            dry = existing.status == "proposed" or existing.evidence.get("_service", {}).get("dry_run")
            if existing.status == "pending" and not dry:
                # Сверка в начале шага не прочитала площадку: исход неизвестен, шаг повторится после сверки.
                raise ChangeConflict("Площадка не подтвердила изменение. Проверим ещё раз в ближайшие минуты.")
            if existing.status != "failed" and not (dry and not proposed):
                # Применённое, предложенное или отменённое владельцем действие слота повторно не выполняется:
                # после отмены объект и так защищён от автопилота на время frozen_until.
                return existing
            if dry:
                # Предложение пробного режима, из которого бизнес уже вышел, заменяется настоящим применением:
                # иначе в истории рядом висели бы «Предложено» и выполненное то же самое действие.
                self.repository.remove(existing)
            else:
                # Неудачная попытка остаётся в журнале, а ключ переходит к новой.
                existing.idempotency_key = None
                self.repository.save(existing)
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
            idempotency_key=key,
            now=self.clock.now,
            force=force,
        )
        if change.status != "applied" and not proposed:
            raise ChangeConflict("Площадка не подтвердила изменение. Проверим ещё раз в ближайшие минуты.")
        self.cache_context(change)
        return change

    def notice(self, business: Business, reason_code: str, text: str, *, key: str = "", **details) -> None:
        notice_key = key or f"{self.step_key}:{reason_code}"
        if details.get("target_id") is not None:
            notice_key = f"{notice_key}:{details['target_id']}"
        self.repository.notify(
            WorkerRun(
                kind="notification",
                business_id=business.id,
                slot_key=notice_key,
                started_at=self.clock.now,
                finished_at=self.clock.now,
                status="ok",
                units_spent=0,
                log=[{"key": notice_key, "reason_code": reason_code, "text": text, **details}],
            )
        )

    async def startup(self) -> None:
        from app.services.budget_guard import budget_guard
        from app.services.launch import recover_launch

        for business in self.repository.rows(Business):
            try:
                with self.repository.business_lock(business.id):
                    if not self.demo_available(business):
                        continue
                    # Блокировка взята: ни один живой процесс не выполняет шаг этого бизнеса.
                    self.repository.interrupt_runs(business.id, self.clock.now)
                    await self.changes.recover_pending(
                        self.resolve_adapter, business_id=business.id, now=self.clock.now
                    )
                    # Бизнес до запуска: шаги воркера его не трогают, но брошенный запуск мог оставить кампании.
                    if business.status in DRAFT_STATUSES:
                        await recover_launch(self, business)
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
            # бизнес могли загрузить в начале тика (identity map держит копию весь тик),
            # а пока шли шаги, владелец нажал «Остановить»/«Продолжить» или отменил запись об остановке.
            # Если свежая строка отличается от копии, перечитываем объект под блокировкой: иначе remember
            # шага вернул бы старые kill_switch/pending_stop поверх свежей записи. Перемотка демо коммитит
            # каждый шаг под этой же блокировкой: её копия совпадает с базой, refresh не случается.
            fresh = self.repository.fresh_business_state(business.id)
            if fresh is not None and tuple(fresh) != (business.status, business.settings):
                self.repository.refresh(business)
            if business.status not in {"running", "budget_stop", "paused"}:
                return
            slot = (business.id, key or schedule_key(kind, self.clock.now))
            if self._rewound is not None:
                if slot in self._rewound:
                    return
            elif not self.demo_available(business):
                return
            self.step_key = slot[1]
            run = self.repository.start_run(business.id, kind, self.step_key, self.clock.now)
            if run is None:
                if self._rewound is not None:
                    self._rewound.add(slot)
                return
            try:
                # Обычно несверенных изменений нет: тогда хватает одного запроса.
                if self.repository.pending(business.id):
                    recovered = await self.changes.recover_pending(
                        self.resolve_adapter, business_id=business.id, now=self.clock.now
                    )
                    for change in recovered:
                        self.cache_context(change)
                    blocking = self.pending(business.id) if kind != "budget_guard" else []
                    if blocking:
                        raise ChangeConflict(PENDING)
                await handlers()[kind](self, business)
                run.status = "ok"
                if self._rewound is not None:
                    self._rewound.add(slot)
            except Exception:
                run.status = "failed"
                # Ответы площадок могут содержать закрытые данные: сохраняем только безопасный код.
                run.log = run.log + [{"reason_code": "JOB_FAILED"}]
                raise
            finally:
                run.finished_at = self.clock.now
                self.repository.save(run)

    async def recover_launch(self, business_id: int) -> None:
        """Брошенный запуск черновика: см. services.launch.recover_launch. Блокировка занята — запуск или шаг мастера
        ещё идёт: проверим в следующий ход."""
        from app.services.launch import recover_launch
        from app.services.plan import launch_unsettled

        business = self.repository.business(business_id)
        # Блокировку ждут шаги мастера: без брошенного запуска или несверенного включения её не берём.
        if business.status not in DRAFT_STATUSES or not (
            any(map(launch_unsettled, self.repository.rows(Plan, business.id)))
            or self.pending(business.id)
            or any(item.review_since is not None for item in self.campaigns(business))
        ):
            return
        try:
            with self.repository.business_lock(business.id):
                self.repository.refresh(business)
                if business.status in DRAFT_STATUSES and self.demo_available(business):
                    await recover_launch(self, business)
        except ChangeConflict:
            return

    def prune(self) -> None:
        """Раз в сутки: пульс хранится сутки, прогоны шагов — 90 дней, уведомления не удаляются;
        просроченные сессии и запросы входа удаляются."""
        self.repository.prune_runs(self.clock.now)
        self.repository.prune_auth(self.clock.now)

    async def renew_vk_tokens(self) -> None:
        """Продление VK-токенов на истечении. Токен VK живёт ~24 часа; без этого шага кабинет регулярно
        падал бы в needs_reauth, а секрет для повторного подключения VK показывает один раз. Кабинеты
        Директа шаг не трогает, отказ одного кабинета не останавливает остальные."""
        from app.auth.vk import renew_account

        for account in self.repository.expiring_vk_accounts(self.clock.now):
            try:
                await renew_account(self.repository.db, account.business_id, force=True)
            except Exception as error:
                # Доступ, который VK отозвала, до нового подключения и так уйдёт в needs_reauth обычным путём.
                logger.warning(
                    "Token renewal failed: platform=%s business=%s code=%s",
                    account.platform,
                    account.business_id,
                    getattr(error, "status_code", None) or type(error).__name__,
                )

    async def run_due(self, skip: Callable[[Business], bool] | None = None) -> None:
        """Те же слоты расписания для APScheduler и виртуальных часов симулятора."""
        await self.run("heartbeat")
        await self.renew_vk_tokens()
        for business in self.repository.rows(Business):
            if not (skip and skip(business)):
                await self.run_business_due(business.id)

    async def run_business_due(self, business_id: int) -> None:
        """Шаги одного бизнеса в текущем слоте. Перемотка демо зовёт только их: пульс по её часам показал бы
        остальным бизнесам проверку из будущего."""
        self._contexts.clear()
        # Перемотка демо идёт только у запущенного бизнеса: брошенного запуска у него нет.
        if self._rewound is None:
            try:
                await self.recover_launch(business_id)
            except Exception:
                logger.error("Launch recovery failed for business %s", business_id)
        now = self.clock.now.astimezone(MOSCOW)
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
                await self.run(kind, business_id)
            except ChangeConflict as error:
                # Ожидание площадки (несверенное изменение) — не авария: шаг повторится.
                logger.info("Worker job %s waits for business %s: %s", kind, business_id, error)
            except Exception:
                logger.error("Worker job %s failed for business %s", kind, business_id)


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
    leads = sum(
        lead_count(row.conversions, business.lead_goal_ids)
        for row in worker.repository.rows(
            StatsDaily, business.id, date=Window(week_start, week_start + timedelta(days=6))
        )
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
