from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager, nullcontext
from copy import deepcopy
from dataclasses import asdict, replace
from datetime import UTC, datetime, timedelta

from sqlalchemy import Connection, func, select, text
from sqlalchemy.orm import Session

from app.db.models import Ad, AdGroup, Business, Campaign, ChangeLog, Experiment, Target
from app.domain.explain import TEMPLATES, explain
from app.platforms.base import AdPlatform, EntityState, ExternalRefs, Operation, PlatformError

type AdapterResolver = Callable[[int, str], AdPlatform]


class ChangeConflict(ValueError):
    pass


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


def differences(operations: Sequence[Operation], states: Mapping[str, EntityState]) -> list[dict]:
    result = []
    for op in final_operations(operations):
        state = states.get(op.entity_id)
        if state is None or state.entity_type != op.entity_type or getattr(state, op.field) != op.after:
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


class ChangesRepository:
    def __init__(self, db: Session) -> None:
        self.db = db

    @contextmanager
    def business_lock(self, business_id: int) -> Iterator[None]:
        # Сессионная блокировка переживает commit записи pending; соединение закреплено до unlock.
        bind = self.db.get_bind()
        context = nullcontext(bind) if isinstance(bind, Connection) else bind.connect()
        with context as connection:
            acquired = connection.scalar(text("SELECT pg_try_advisory_lock(:id)"), {"id": business_id})
            if not acquired:
                raise ChangeConflict("Изменение этого бизнеса уже выполняется. Попробуйте позже.")
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
        statement = select(ChangeLog).where(ChangeLog.status == "pending")
        if business_id is not None:
            statement = statement.where(ChangeLog.business_id == business_id)
        return [
            item
            for item in self.db.scalars(statement.order_by(ChangeLog.created_at, ChangeLog.id))
            if not item.evidence.get("_service", {}).get("dry_run")
        ]

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

    def check_autopilot(self, business_id: int, platform: str, operations: Sequence[Operation], now: datetime) -> None:
        entities = self.local_entities(business_id, platform)
        affected = affected_entities(operations, entities)
        if any(
            isinstance(entity, Ad) and entity.frozen_until is not None and entity.frozen_until > now
            for key, entity in entities.items()
            if key in affected
        ):
            raise ChangeConflict("После отката владельцем объект временно защищён от изменений автопилота.")
        for change in self.recent_owner_changes(business_id, now):
            if change.platform == platform and affected & affected_entities(load_operations(change), entities):
                raise ChangeConflict("После правки владельца нужно подождать 24 часа.")

    def sync_local(self, change: ChangeLog, operations: Sequence[Operation]) -> None:
        entities = self.local_entities(change.business_id, change.platform)
        for op in final_operations(operations):
            entity = entities.get((op.entity_type, op.entity_id))
            if entity is None:
                continue
            if op.field in {"status", "title", "text", "daily_limit_kop", "period_cap_kop"}:
                value = op.after
                if isinstance(entity, Ad) and value == "paused":
                    value = "paused_owner" if change.actor == "owner" else "paused_auto"
                setattr(entity, op.field, value)


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
        now: datetime | None = None,
    ) -> ChangeLog:
        if not operations:
            raise ValueError("Нужна хотя бы одна операция.")
        business = self.repository.business(business_id)
        dry_run = self.dry_run
        if dry_run is None:
            from app.config import settings

            dry_run = settings.dry_run
        dry_run = (dry_run or business.settings.get("dry_run", False)) and actor != "external"
        if self.repository.pending(business_id):
            raise ChangeConflict("Сначала требуется сверить незавершённые изменения бизнеса.")
        if any(op.entity_id not in refs.entity_ids() for op in operations):
            raise ValueError("Операции должны относиться к переданному дереву кампании.")
        if actor == "autopilot" and not dry_run:
            self.repository.check_autopilot(business_id, platform, operations, now or datetime.now(UTC))
        params = deepcopy(reason_params or {})
        message = explain(reason_code, params) if reason_code in TEMPLATES else reason_text
        if not message:
            raise ValueError("Для неизвестного кода причины нужен готовый текст.")
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
            created_at=now or datetime.now(UTC),
        )
        self.repository.save(change)
        if dry_run:
            return change
        try:
            if actor != "external":
                for operation in operations:
                    await adapter.apply(operation)
        except PlatformError:
            self._metadata(change, error="Площадка сообщила об ошибке применения. Требуется сверка.")
        await self._reconcile(change, adapter, now=now)
        return change

    @staticmethod
    def _metadata(change: ChangeLog, **values) -> None:
        evidence = deepcopy(change.evidence)
        evidence["_service"].update(values)
        change.evidence = evidence

    async def _reconcile(self, change: ChangeLog, adapter: AdPlatform, *, now: datetime | None = None) -> None:
        try:
            mismatches = differences(load_operations(change), await adapter.get_state(load_refs(change)))
        except PlatformError:
            # При недоступном чтении исход неизвестен: pending будет проверен после рестарта.
            self._metadata(change, error="Не удалось сверить состояние площадки. Проверим повторно.")
            self.repository.save(change)
            return
        if mismatches:
            change.status = "failed"
            self._metadata(change, error="Состояние площадки не совпало с ожидаемым.", differences=mismatches)
        else:
            change.status = "applied"
            self._metadata(change, error=None, differences=[])
            self.repository.sync_local(change, load_operations(change))
            if change.kind == "revert":
                from app.services.rollback import finish_revert

                finish_revert(self.repository, change, now or datetime.now(UTC))
        self.repository.save(change)

    async def recover_pending(
        self, resolve_adapter: AdapterResolver, *, business_id: int | None = None, now: datetime | None = None
    ) -> list[ChangeLog]:
        result = []
        for item in self.repository.pending(business_id):
            try:
                with self.repository.business_lock(item.business_id):
                    change = self.repository.get(item.id, item.business_id)
                    if change is None or change.status != "pending":
                        continue
                    adapter = resolve_adapter(change.business_id, change.platform)
                    await self._reconcile(change, adapter, now=now)
                    result.append(change)
            except (ChangeConflict, PlatformError):
                continue
        return result
