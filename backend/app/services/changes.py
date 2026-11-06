import asyncio
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import AbstractContextManager, ExitStack, contextmanager, nullcontext
from copy import deepcopy
from dataclasses import asdict, replace
from datetime import UTC, datetime, timedelta
from time import monotonic, sleep

from cryptography.fernet import InvalidToken
from sqlalchemy import Connection, and_, func, select, text
from sqlalchemy.exc import TimeoutError as PoolTimeout
from sqlalchemy.orm import Session, load_only

from app.db.models import (
    AD_STATUSES,
    ENTITY_STATUSES,
    Ad,
    AdGroup,
    Business,
    Campaign,
    ChangeLog,
    Experiment,
    Plan,
    Target,
)
from app.demo_flags import ADVANCING, advance_jobs
from app.domain.explain import TEMPLATES, explain
from app.platforms.base import AdPlatform, AwaitingModeration, EntityState, ExternalRefs, Operation, PlatformError

type AdapterResolver = Callable[[int, str], AdPlatform]

# Кабинет не откроется, пока владелец не войдёт заново: вход истёк, кабинет удалили, токен не расшифровать.
CLOSED_ACCOUNT = (PlatformError, LookupError, InvalidToken)


# Тексты конфликтов доходят до владельца (409): без слов «сверка» и «применение».
PENDING = "Площадка ещё не подтвердила предыдущее изменение. Попробуйте через несколько минут."
# Статусы площадки «ещё проверяет»: кампания-черновик ждёт принятия объявлений, объявление — модерации, у VK
# неизвестный статус. Статус такой сущности площадка подтвердит после проверки: изменение, упёршееся в проверку,
# применено, а локальная цель доводится шагом синхронизации (sync.settle_review).
REVIEW = frozenset({"pending", "moderation", "draft"})
PAUSES = frozenset({"paused_auto", "paused_owner"})


def awaiting_enable(campaign: Campaign, state: EntityState, row: Campaign | Ad | None = None) -> bool:
    """Площадка одобрила и выключила кампанию (или её объявление), а шаг модерации ещё не включил: локальная цель —
    работать, и остановка меняет её, хотя на площадке выключено. Иначе sync.settle_review включил бы остановленное."""
    return (
        campaign.review_since is not None
        and campaign.status == "active"
        and (row or campaign).status in {"active", "moderation"}
        and state.status in PAUSES
    )


def rejected_draft_pause(
    campaign: Campaign, entities: Mapping[tuple[str, str], object], reviewed: Sequence[Operation]
) -> bool:
    """Пауза черновика, все объявления которого отклонены: площадка не примет его, пока объявления не исправят, и
    ждать проверки нечего — отметка держала бы под кампанию бюджет и повторяла уведомление об отказе."""
    groups = {item.id for item in entities.values() if isinstance(item, AdGroup) and item.campaign_id == campaign.id}
    ads = [item for item in entities.values() if isinstance(item, Ad) and item.ad_group_id in groups]
    return (
        bool(ads)
        and all(ad.status == "rejected" for ad in ads)
        and all(op.entity_type == "campaign" and op.after in PAUSES for op in reviewed)
    )


# Паузу, которую площадка не применила к черновику, после проверки доводит шаг модерации (sync.settle_review): это не
# новая остановка, а исходная, и «почему остановлено» отвечает запись до неё.
SETTLE = "MODERATION_SETTLE"


def settled_pause(change, entity_type: str, entity_id: str) -> bool:
    return change.reason_code == SETTLE and any(
        (op.get("entity_type"), op.get("entity_id"), op.get("field")) == (entity_type, entity_id, "status")
        and op.get("after") in PAUSES
        for op in change.operations
    )


class ChangeConflict(ValueError):
    pass


class ServerBusy(ChangeConflict):
    """Все соединения для блокировок заняты: дело не в этом бизнесе, а в нагрузке на сервер."""

    def __init__(self) -> None:
        super().__init__("Сервер сейчас занят. Повторите через минуту.")


def load_operations(change: ChangeLog) -> list[Operation]:
    return [Operation(**item) for item in change.operations]


def load_refs(change: ChangeLog) -> ExternalRefs:
    return ExternalRefs(**change.evidence["_service"]["refs"])


def final_operations(operations: Sequence[Operation]) -> list[Operation]:
    result = {}
    for op in operations:
        key = (op.entity_type, op.entity_id, op.field)
        result[key] = replace(op, before=result[key].before) if key in result else op
    return list(result.values())


def differences(
    operations: Sequence[Operation], states: Mapping[str, EntityState], *, platform_pauses: bool = False
) -> list[dict]:
    result = []
    for op in final_operations(operations):
        state = states.get(op.entity_id)
        if (
            platform_pauses
            and state is not None
            and state.entity_type == op.entity_type
            and op.field == "status"
            and op.after in PAUSES
            and state.status in PAUSES
        ):
            # API площадок возвращает паузу без автора, а одобренную, но выключенную кампанию Директ показывает как
            # OFF: при любой паузе кампания не крутится, автора хранит наш журнал.
            continue
        after = op.after
        if op.field == "creative" and isinstance(after, dict) and "title2" not in after:
            # Записи add_ad до появления title2 (миграция 0006) не содержат ключа: сравниваем как None,
            # иначе каждый откат старой записи просил бы подтверждение (мелочь круга 3).
            after = after | {"title2": None}
        if state is None or state.entity_type != op.entity_type or getattr(state, op.field) != after:
            current = getattr(state, op.field) if state is not None and state.entity_type == op.entity_type else None
            if (
                op.field in {"daily_limit_kop", "period_cap_kop"}
                and isinstance(current, int)
                and isinstance(op.after, int)
                and abs(current - op.after) <= 1
            ):
                # Пересчёт НДС лимита VK (VK_AMOUNTS_INCLUDE_VAT=true) теряет копейку на округлении рублёвой
                # строки API до двух знаков: копейка не расхождение, на решения и бюджет не влияет.
                continue
            if in_review(op, state):
                # Площадка ещё проверяет объявление или кампанию: включение ждёт одобрения, а остановку черновика
                # она может отклонить — он и так не крутится. Не расхождение: цель доводит sync.settle_review.
                continue
            result.append(
                {
                    "entity_type": op.entity_type,
                    "entity_id": op.entity_id,
                    "field": op.field,
                    "expected": deepcopy(op.after),
                    "current": deepcopy(getattr(state, op.field)) if state else None,
                    "restore": deepcopy(op.before),
                }
            )
    return result


def without_rejected_ads(mismatches: list[dict], states: Mapping[str, EntityState]) -> list[dict]:
    """Отклонённое площадкой объявление не проваливает включение всей кампании: остальные одобренные объявления
    работают, а отклонённое переписывает сверка модерации."""
    return [
        item
        for item in mismatches
        if not (
            item["entity_type"] == "ad"
            and (state := states.get(item["entity_id"])) is not None
            and state.status == "rejected"
        )
    ]


def in_review(op: Operation, state: EntityState | None) -> bool:
    return (
        state is not None
        and state.entity_type == op.entity_type
        and op.entity_type in {"ad", "campaign"}
        and op.field == "status"
        # Остановку площадка «ещё проверяет» только у черновика и объявления на модерации: pending у паузы —
        # расхождение.
        and state.status in ({"draft", "moderation"} if op.after in PAUSES else REVIEW)
    )


def with_current_before(operations: Sequence[Operation], states: Mapping[str, EntityState]) -> list[Operation]:
    values = {}
    result = []
    for op in operations:
        state = states.get(op.entity_id)
        if state is None or state.entity_type != op.entity_type:
            raise ValueError("Объект изменения не найден на площадке.")
        key = (op.entity_id, op.field)
        before = values.get(key, getattr(state, op.field))
        result.append(replace(op, before=before))
        values[key] = op.after
    return result


def affected_entities(
    operations: Sequence[Operation], entities: Mapping[tuple[str, str], object]
) -> set[tuple[str, str]]:
    result = {(op.entity_type, op.entity_id) for op in operations}
    campaigns = {item.id for key, item in entities.items() if key in result and isinstance(item, Campaign)}
    groups = {
        item.id
        for key, item in entities.items()
        if isinstance(item, AdGroup) and (key in result or item.campaign_id in campaigns)
    }
    for key, item in entities.items():
        if isinstance(item, AdGroup) and item.id in groups:
            result.add(key)
        elif isinstance(item, Ad | Target) and item.ad_group_id in groups:
            result.add(key)
    return result


def replaced_campaigns(plans: Sequence[Plan]) -> set[int]:
    """Кампании всех планов, кроме действующего — последнего запущенного (Worker.current_campaigns)."""
    current = max((plan for plan in plans if plan.status == "launched"), key=lambda plan: plan.version, default=None)
    return {
        campaign_id
        for plan in plans
        if plan is not current
        for campaign_id in plan.payload.get("launch", {}).get("campaigns", {}).values()
    }


LOCK_WAIT_SECONDS = 15


@contextmanager
def waiting(lock: Callable[[], AbstractContextManager], seconds: float) -> Iterator[None]:
    """Берёт блокировку lock(), дожидаясь до seconds секунд, пока её отпустят; ждёт поток (sleep), поэтому только вне
    цикла событий. Не дождались — ChangeConflict."""
    deadline = monotonic() + seconds
    with ExitStack() as stack:
        while True:
            try:
                stack.enter_context(lock())
                break
            except ChangeConflict:
                if monotonic() >= deadline:
                    raise
            sleep(0.5)
        yield


async def wait_for_lock(
    stack: ExitStack,
    repository: "ChangesRepository",
    business_id: int,
    seconds: float,
    *,
    advancing_aborts: bool = False,
) -> None:
    """Берёт блокировку бизнеса, дожидаясь, пока её отпустят (шаг воркера, другая запись); ждёт без блокировки цикла
    событий. Не дождались за seconds — ChangeConflict. advancing_aborts: выход сразу, когда бизнес заняла перемотка
    демо, — её блокировку ждать до конца срока бессмысленно."""
    deadline = monotonic() + seconds
    while True:
        try:
            stack.enter_context(repository.business_lock(business_id))
            return
        except ChangeConflict:
            if monotonic() >= deadline or (advancing_aborts and advance_jobs.holds_lock(business_id)):
                raise
        await asyncio.sleep(0.5)


class ChangesRepository:
    def __init__(self, db: Session) -> None:
        self.db = db

    @contextmanager
    def business_lock(self, business_id: int) -> Iterator[None]:
        # Сессионная блокировка переживает commit записи pending; соединение закреплено до unlock.
        from app.db.session import lock_bind

        bind = self.db.get_bind()
        try:
            context = nullcontext(bind) if isinstance(bind, Connection) else lock_bind(bind).connect()
        except PoolTimeout as error:
            raise ServerBusy from error
        with context as connection:
            acquired = connection.scalar(text("SELECT pg_try_advisory_lock(:id)"), {"id": business_id})
            if not acquired:
                raise ChangeConflict(
                    ADVANCING
                    if advance_jobs.holds_lock(business_id)
                    else "Изменение этого бизнеса уже выполняется. Попробуйте позже."
                )
            try:
                yield
            finally:
                connection.execute(text("SELECT pg_advisory_unlock(:id)"), {"id": business_id})

    def get(self, change_id: int, business_id: int) -> ChangeLog | None:
        return self.db.scalar(
            select(ChangeLog)
            .where(ChangeLog.id == change_id, ChangeLog.business_id == business_id)
            .execution_options(populate_existing=True)
        )

    def get_owned(self, change_id: int, user_id: int) -> ChangeLog | None:
        return self.db.scalar(
            select(ChangeLog).join(Business).where(ChangeLog.id == change_id, Business.user_id == user_id)
        )

    def business(self, business_id: int) -> Business:
        business = self.db.get(Business, business_id)
        if business is None:
            raise ValueError("Бизнес не найден.")
        return business

    def save(self, change: ChangeLog) -> None:
        self.db.add(change)
        self.db.commit()

    def pending(self, business_id: int | None = None) -> list[ChangeLog]:
        # Предложения пробного режима остаются pending навсегда; сверять их не нужно.
        statement = select(ChangeLog).where(
            ChangeLog.status == "pending", ChangeLog.evidence["_service"]["dry_run"].as_boolean().is_not(True)
        )
        if business_id is not None:
            statement = statement.where(ChangeLog.business_id == business_id)
        return list(self.db.scalars(statement.order_by(ChangeLog.created_at, ChangeLog.id)))

    def change_by_key(self, business_id: int, key: str) -> ChangeLog | None:
        return self.db.scalar(
            select(ChangeLog).where(ChangeLog.business_id == business_id, ChangeLog.idempotency_key == key)
        )

    def reverts(self, change_id: int) -> list[ChangeLog]:
        return list(self.db.scalars(select(ChangeLog).where(ChangeLog.reverts_id == change_id)))

    def history(
        self,
        user_id: int,
        *,
        business_id: int | None = None,
        platform: str | None = None,
        actor: str | None = None,
        page: int = 1,
        page_size: int = 20,
    ) -> tuple[list[ChangeLog], int]:
        statement = select(ChangeLog).join(Business).where(Business.user_id == user_id)
        if actor is None:
            # Ежедневный пересчёт лимитов в пределах бюджета владельца — бухгалтерия, а не событие: в общей ленте он
            # вытеснял всё остальное. Он остаётся в журнале и виден с фильтром «Система»; сбой и остановка видны всегда.
            statement = statement.where(
                ~and_(
                    ChangeLog.actor == "system",
                    ChangeLog.reason_code == "BUDGET_REBALANCE",
                    ChangeLog.kind == "budget",
                    ChangeLog.status == "applied",
                )
            )
        for column, value in (
            (ChangeLog.business_id, business_id),
            (ChangeLog.platform, platform),
            (ChangeLog.actor, actor),
        ):
            if value is not None:
                statement = statement.where(column == value)
        total = self.db.scalar(select(func.count()).select_from(statement.subquery()))
        rows = self.db.scalars(
            statement.order_by(ChangeLog.created_at.desc(), ChangeLog.id.desc())
            .offset((page - 1) * page_size)
            .limit(page_size)
        )
        return list(rows), total

    def local_entities(self, business_id: int, platform: str | None) -> dict[tuple[str, str], object]:
        campaigns = list(
            self.db.scalars(select(Campaign).where(Campaign.business_id == business_id, Campaign.platform == platform))
        )
        campaign_ids = [item.id for item in campaigns]
        groups = list(self.db.scalars(select(AdGroup).where(AdGroup.campaign_id.in_(campaign_ids))))
        group_ids = [item.id for item in groups]
        ads = list(self.db.scalars(select(Ad).where(Ad.ad_group_id.in_(group_ids))))
        targets = list(self.db.scalars(select(Target).where(Target.ad_group_id.in_(group_ids))))
        return {
            (kind, item.external_id): item
            for kind, items in (("campaign", campaigns), ("group", groups), ("ad", ads), ("target", targets))
            for item in items
            if item.external_id is not None
        }

    def replaced_entities(self, business_id: int) -> set[tuple[str, str]]:
        """Кампании прежних планов на площадках: (площадка, внешний id)."""
        ids = replaced_campaigns(list(self.db.scalars(select(Plan).where(Plan.business_id == business_id))))
        rows = self.db.execute(
            select(Campaign.platform, Campaign.external_id).where(
                Campaign.business_id == business_id, Campaign.id.in_(ids), Campaign.external_id.is_not(None)
            )
        )
        return {(platform, external_id) for platform, external_id in rows}

    def status_changes(self, business_id: int, since: int = 0) -> list[ChangeLog]:
        """Применённые изменения бизнеса с id не меньше since, которые меняли статус объектов, по порядку: superseded()
        смотрит только на более поздние. Без доказательств и пояснений — нужны лишь вид, площадка и операции."""
        return list(
            self.db.scalars(
                select(ChangeLog)
                .options(
                    load_only(
                        ChangeLog.id, ChangeLog.kind, ChangeLog.platform, ChangeLog.operations, ChangeLog.created_at
                    )
                )
                .where(
                    ChangeLog.business_id == business_id,
                    ChangeLog.status == "applied",
                    ChangeLog.id >= since,
                    ChangeLog.operations.contains([{"field": "status"}]),
                )
                .order_by(ChangeLog.id)
            )
        )

    def campaign_names(self, business_id: int) -> dict[tuple[str, str], str]:
        """Названия кампаний бизнеса по (площадка, внешний id) — для заголовков журнала."""
        rows = self.db.execute(
            select(Campaign.platform, Campaign.external_id, Campaign.name).where(
                Campaign.business_id == business_id, Campaign.external_id.is_not(None)
            )
        )
        return {(platform, external_id): name for platform, external_id, name in rows}

    def experiments(self, change: ChangeLog) -> list[Experiment]:
        condition = Experiment.change_id == change.id
        if change.entity_type == "experiment" and change.entity_id.isdecimal():
            condition |= Experiment.id == int(change.entity_id)
        return list(self.db.scalars(select(Experiment).where(Experiment.business_id == change.business_id, condition)))

    def recent_owner_changes(self, business_id: int, now: datetime) -> list[ChangeLog]:
        return list(
            self.db.scalars(
                select(ChangeLog).where(
                    ChangeLog.business_id == business_id,
                    ChangeLog.actor == "owner",
                    ChangeLog.status.in_(("applied", "reverted")),
                    ChangeLog.created_at > now - timedelta(hours=24),
                )
            )
        )

    def check_autopilot(
        self,
        business_id: int,
        platform: str,
        operations: Sequence[Operation],
        now: datetime,
        *,
        entity: tuple[str, str] | None = None,
    ) -> None:
        entities = self.local_entities(business_id, platform)
        affected = {entity} if entity else affected_entities(operations, entities)
        frozen = self.business(business_id).settings.get("_worker", {}).get("frozen_entities", {})
        if any(
            datetime.fromisoformat(frozen[f"{kind}:{id_}"]) > now for kind, id_ in affected if f"{kind}:{id_}" in frozen
        ):
            raise ChangeConflict("После отката владельцем объект временно защищён от изменений автопилота.")
        if any(
            isinstance(entity, Ad) and entity.frozen_until is not None and entity.frozen_until > now
            for key, entity in entities.items()
            if key in affected
        ):
            raise ChangeConflict("После отката владельцем объект временно защищён от изменений автопилота.")
        for change in self.recent_owner_changes(business_id, now):
            previous = (
                {(change.entity_type, change.entity_id)}
                if change.entity_type == "group"
                else affected_entities(load_operations(change), entities)
            )
            if change.platform == platform and affected & previous:
                raise ChangeConflict("После правки владельца нужно подождать 24 часа.")

    def sync_local(self, change: ChangeLog, operations: Sequence[Operation]) -> None:
        """Статус сущности, которую площадка ещё проверяет (_service.review), — наша цель: включённое объявление
        ждёт одобрения («moderation»), а кампания получает отметку review_since, по которой цель доводит
        sync.settle_review."""
        entities = self.local_entities(change.business_id, change.platform)
        review = set((change.evidence.get("_service") or {}).get("review", ()))
        if review:
            campaign = entities.get(("campaign", load_refs(change).campaign_id))
            if (
                isinstance(campaign, Campaign)
                and campaign.review_since is None
                and not rejected_draft_pause(campaign, entities, [op for op in operations if op.entity_id in review])
            ):
                campaign.review_since = change.created_at
        for op in final_operations(operations):
            entity = entities.get((op.entity_type, op.entity_id))
            if entity is None:
                continue
            if op.field in {"status", "title", "text", "daily_limit_kop", "period_cap_kop"}:
                value = op.after
                if isinstance(entity, Ad) and value == "paused":
                    value = "paused_owner" if change.actor == "owner" else "paused_auto"
                if op.field == "status" and value not in (AD_STATUSES if isinstance(entity, Ad) else ENTITY_STATUSES):
                    # Площадка отдаёт pending или mixed для состояний без нашего аналога (черновик, модерация,
                    # остановка мониторингом, смешанные статусы группы). Откат к такому before может совпасть при
                    # сверке; локальный статус тогда не меняется, иначе CHECK отклонит запись и изменение навсегда
                    # останется pending.
                    continue
                if isinstance(entity, Ad) and op.field == "status" and value == "active" and op.entity_id in review:
                    value = "moderation"
                setattr(entity, op.field, value)
        if "group_limits" in change.evidence:
            business = self.business(change.business_id)
            meta = deepcopy(business.settings.get("_worker", {}))
            meta["group_limits"] = meta.get("group_limits", {}) | change.evidence["group_limits"]["after"]
            business.settings = business.settings | {"_worker": meta}


async def budget_operations(repository, business, adapter, refs, operations, now):
    """Директ получает полную пару лимитов из Campaign, а не из памяти адаптера."""
    if not any(op.kind == "set_budget" for op in operations):
        return refs, operations
    entities = repository.local_entities(business.id, "direct")
    campaign = entities.get(("campaign", refs.campaign_id))
    context = dict(refs.budget_context)
    if campaign is not None:
        context.update(daily_limit_kop=campaign.daily_limit_kop, period_cap_kop=campaign.period_cap_kop)
    if not context:
        state = (await adapter.get_state(refs))[refs.campaign_id]
        context = {"daily_limit_kop": state.daily_limit_kop, "period_cap_kop": state.period_cap_kop}
    day = now.astimezone(adapter._tz).date()
    month = day.replace(day=1)
    start = min(month, day.fromisoformat(business.settings.get("_worker", {}).get("started_on", month.isoformat())))
    rows = await adapter.get_stats(start, day, "campaign", campaign_ids={refs.campaign_id})
    week = day - timedelta(days=day.weekday())
    context["spent_before_week_kop"] = sum(
        row.cost_kop for row in rows if row.campaign_id == refs.campaign_id and row.date < week
    )
    context["spent_week_kop"] = sum(
        row.cost_kop for row in rows if row.campaign_id == refs.campaign_id and row.date >= week
    )
    # Сегодняшний расход отдельно: недельный лимит считает остаток дня, а не полный дневной лимит сверх
    # уже потраченного (мелочь 1 круга 7 — иначе сегодня учитывался дважды).
    context["spent_today_kop"] = sum(
        row.cost_kop for row in rows if row.campaign_id == refs.campaign_id and row.date == day
    )
    refs = replace(refs, budget_context=context.copy())
    prepared = []
    for op in operations:
        if op.kind == "set_budget":
            context[op.field] = op.after
            context["weekly_limit_kop"] = adapter._context_weekly(context)
            op = replace(op, budget_context=context.copy())
        prepared.append(op)
    return refs, prepared


class ChangesService:
    def __init__(self, repository: ChangesRepository, *, dry_run: bool | None = None) -> None:
        self.repository = repository
        self.dry_run = dry_run

    async def apply_change(self, *, business_id: int, **kwargs) -> ChangeLog:
        with self.repository.business_lock(business_id):
            return await self._apply_change(business_id=business_id, **kwargs)

    async def _apply_change(
        self,
        *,
        business_id: int,
        adapter: AdPlatform,
        refs: ExternalRefs,
        operations: Sequence[Operation],
        actor: str,
        kind: str,
        platform: str,
        reason_code: str,
        reason_params: dict | None = None,
        reason_text: str | None = None,
        evidence: dict | None = None,
        entity_type: str | None = None,
        entity_id: str | None = None,
        reverts_id: int | None = None,
        idempotency_key: str | None = None,
        now: datetime | None = None,
        force: bool = False,
        record_only: bool = False,
    ) -> ChangeLog:
        """Пробный режим (DRY_RUN) касается только решений автопилота: действия владельца, отката и служебные
        (лимиты бюджета, продление кампании, страж бюджета) применяются всегда. force — защитное действие автопилота,
        которое выполняется и в пробном режиме. record_only — на площадке нечего менять (откат вывода сравнения,
        чьё объявление владелец уже удалил): запись журнала и разбор эксперимента идут без операций."""
        if not operations and not record_only:
            raise ValueError("Нужна хотя бы одна операция.")
        business = self.repository.business(business_id)
        dry_run = self.dry_run
        if dry_run is None:
            from app.config import settings

            dry_run = settings.dry_run
        dry_run = (dry_run or business.settings.get("dry_run", False)) and actor == "autopilot" and not force
        protective_stop = actor in {"system", "owner"} and all(
            op.kind == "set_status" and op.after in {"paused_auto", "paused_owner"} for op in operations
        )
        blocking = [item for item in self.repository.pending(business_id) if item.platform == platform]
        if not protective_stop and blocking:
            raise ChangeConflict(PENDING)
        if any(op.entity_id not in refs.entity_ids() for op in operations):
            raise ValueError("Операции должны относиться к переданному дереву кампании.")
        if actor == "autopilot" and not dry_run:
            self.repository.check_autopilot(
                business_id,
                platform,
                operations,
                now or datetime.now(UTC),
                entity=(entity_type, entity_id) if entity_type == "group" else None,
            )
        params = deepcopy(reason_params or {})
        message = explain(reason_code, params) if reason_code in TEMPLATES else reason_text
        if not message:
            raise ValueError("Для неизвестного кода причины нужен готовый текст.")
        from app.platforms.direct import DirectAdapter

        if isinstance(adapter, DirectAdapter) and not dry_run:
            refs, operations = await budget_operations(
                self.repository, business, adapter, refs, operations, now or datetime.now(UTC)
            )
        if not dry_run and actor != "external":
            operations = with_current_before(operations, await adapter.get_state(refs))
        change = ChangeLog(
            business_id=business_id,
            actor=actor,
            kind=kind,
            platform=platform,
            entity_type=entity_type or operations[0].entity_type,
            entity_id=entity_id or operations[0].entity_id,
            operations=[asdict(op) for op in operations],
            reason_code=reason_code,
            reason_params=params,
            reason_text=message,
            evidence=deepcopy(evidence or {}) | {"_service": {"refs": asdict(refs), "dry_run": dry_run}},
            status="pending",
            reverts_id=reverts_id,
            idempotency_key=idempotency_key,
            created_at=now or datetime.now(UTC),
        )
        self.repository.save(change)
        if dry_run:
            return change
        if actor != "external":
            for operation in operations:
                try:
                    await adapter.apply(operation)
                except AwaitingModeration:
                    # Площадка ещё проверяет объявление: включение ждёт одобрения, остальные операции идут дальше.
                    continue
                except PlatformError:
                    self._metadata(change, error="Площадка ответила ошибкой.")
                    break
        await self._reconcile(change, adapter, now=now)
        return change

    @staticmethod
    def _metadata(change: ChangeLog, **values) -> None:
        evidence = deepcopy(change.evidence)
        evidence["_service"].update(values)
        change.evidence = evidence

    async def _reconcile(self, change: ChangeLog, adapter: AdPlatform, *, now: datetime | None = None) -> None:
        try:
            operations = load_operations(change)
            refs = load_refs(change)
            contexts = [op.budget_context for op in operations if op.budget_context]
            if contexts:
                refs = replace(refs, budget_context=contexts[-1])
            from app.platforms.direct import DirectAdapter
            from app.platforms.vk import VkAdapter

            platform_pauses = isinstance(adapter, (DirectAdapter, VkAdapter))
            states = await adapter.get_state(refs)
            mismatches = without_rejected_ads(differences(operations, states, platform_pauses=platform_pauses), states)
        except PlatformError:
            # При недоступном чтении исход неизвестен: pending сверят следующий ход воркера или повтор запуска.
            self._metadata(change, error="Площадка не ответила, прошло ли изменение. Узнаем, когда она ответит снова.")
            self.repository.save(change)
            return
        if mismatches:
            change.status = "failed"
            self._metadata(change, error="Состояние площадки не совпало с ожидаемым.", differences=mismatches)
        else:
            # Отклонённое объявление остаётся отклонённым: его переписывает сверка модерации.
            rejected = {item["entity_id"] for item in differences(operations, states) if item["current"] == "rejected"}
            review = sorted({op.entity_id for op in operations if in_review(op, states.get(op.entity_id))})
            change.status = "applied"
            self._metadata(change, error=None, differences=[], **({"review": review} if review else {}))
            self.repository.sync_local(
                change,
                [op for op in load_operations(change) if not (op.entity_type == "ad" and op.entity_id in rejected)],
            )
            if change.kind == "revert":
                from app.services.rollback import finish_revert

                finish_revert(self.repository, change, now or datetime.now(UTC))
        self.repository.save(change)

    async def recover_pending(
        self, resolve_adapter: AdapterResolver, *, business_id: int | None = None, now: datetime | None = None
    ) -> list[ChangeLog]:
        """Кабинет, который не открылся по любой причине, здесь пропускается: изменение остаётся pending, а ждут ли его
        шаги, решает Worker.pending."""
        result = []
        for item in self.repository.pending(business_id):
            try:
                with self.repository.business_lock(item.business_id):
                    change = self.repository.get(item.id, item.business_id)
                    if change is None or change.status != "pending":
                        continue
                    try:
                        adapter = resolve_adapter(change.business_id, change.platform)
                    except Exception:
                        continue
                    await self._reconcile(change, adapter, now=now)
                    result.append(change)
            except (ChangeConflict, PlatformError):
                continue
        return result
