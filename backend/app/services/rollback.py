from collections.abc import Mapping, Sequence
from dataclasses import replace
from datetime import UTC, datetime, timedelta

from app.db.models import Ad, AdGroup, Campaign, ChangeLog
from app.platforms.base import AdPlatform, EntityState, Operation
from app.services.changes import (
    ChangeConflict,
    ChangesRepository,
    ChangesService,
    differences,
    load_operations,
    load_refs,
)


class ConfirmationRequired(ChangeConflict):
    def __init__(self, changes: list[dict]) -> None:
        super().__init__("С тех пор настройки изменились. Вернуть предыдущие значения всё равно?")
        self.changes = changes


def inverse_operations(operations: Sequence[Operation], states: Mapping[str, EntityState]) -> list[Operation]:
    result = []
    archived = set()
    for op in reversed(operations):
        if op.entity_id in archived:
            continue
        if op.invertible:
            result.append(op.inverse())
            continue
        if op.entity_type != "ad":
            raise ValueError("Для этой необратимой операции безопасный откат не поддерживается.")
        state = states.get(op.entity_id)
        if state is None:
            raise ValueError("Созданное объявление не найдено на площадке.")
        result = [item for item in result if item.entity_id != op.entity_id]
        result.extend(
            (
                Operation("set_status", "ad", op.entity_id, "status", state.status, "paused_owner"),
                Operation("set_status", "ad", op.entity_id, "status", "paused_owner", "archived"),
            )
        )
        archived.add(op.entity_id)
    return result


def finish_revert(repository: ChangesRepository, revert: ChangeLog, now: datetime) -> None:
    original = repository.get(revert.reverts_id, revert.business_id)
    if original is None or original.status != "applied":
        raise ChangeConflict("Исходное изменение уже отменено или недоступно для отката.")
    original.status = "reverted"
    entities = repository.local_entities(revert.business_id, revert.platform)
    frozen_until = now + timedelta(days=14)
    affected = set()
    group_ids = set()
    frozen_entities = {}
    for op in load_operations(revert):
        entity = entities.get((op.entity_type, op.entity_id))
        if isinstance(entity, Ad):
            affected.add(entity.id)
        elif isinstance(entity, AdGroup):
            frozen_entities[f"group:{op.entity_id}"] = frozen_until.isoformat()
        elif isinstance(entity, Campaign):
            frozen_entities[f"campaign:{op.entity_id}"] = frozen_until.isoformat()
        elif op.entity_type == "target" and entity is not None:
            frozen_entities[f"target:{op.entity_id}"] = frozen_until.isoformat()
    if original.entity_type == "group":
        frozen_entities = {f"group:{original.entity_id}": frozen_until.isoformat()}
    for experiment in repository.experiments(original):
        experiment.status = "cancelled"
        experiment.verdict = "control_wins"
        experiment.stats = dict(experiment.stats or {}) | {
            "manual_verdict": "variant_lost",
            "reverted_by_change_id": revert.id,
            "frozen_until": frozen_until.isoformat() if revert.actor == "owner" else None,
        }
        group_ids.add(experiment.ad_group_id)
        affected.update((experiment.control_ad_id, experiment.variant_ad_id))
    if revert.actor == "owner":
        if frozen_entities:
            business = repository.business(revert.business_id)
            meta = dict(business.settings.get("_worker", {}))
            meta["frozen_entities"] = meta.get("frozen_entities", {}) | frozen_entities
            business.settings = business.settings | {"_worker": meta}
        for entity in entities.values():
            if isinstance(entity, Ad) and (entity.id in affected or entity.ad_group_id in group_ids):
                entity.frozen_until = max(entity.frozen_until or frozen_until, frozen_until)


class RollbackService:
    def __init__(self, repository: ChangesRepository, *, dry_run: bool | None = None) -> None:
        self.repository = repository
        self.changes = ChangesService(repository, dry_run=dry_run)

    async def revert(
        self,
        change_id: int,
        *,
        business_id: int,
        adapter: AdPlatform,
        confirm: bool = False,
        actor: str = "owner",
        now: datetime | None = None,
    ) -> ChangeLog:
        with self.repository.business_lock(business_id):
            original = self.repository.get(change_id, business_id)
            if original is None:
                raise ValueError("Изменение не найдено.")
            if original.status != "applied" or original.kind == "revert":
                raise ChangeConflict("Повторный откат или откат неприменённого изменения запрещён.")
            if original.actor == "system" and original.reason_code in {
                "BUDGET_REBALANCE",
                "BUDGET_STOP",
                "SPEND_MISMATCH",
                "MONTH_STOP",
            }:
                raise ChangeConflict("Это защита бюджета, отменить нельзя.")
            if original.actor == "external" and actor != "owner":
                raise ChangeConflict("Изменение в кабинете может отменить только владелец.")
            if any(
                item.status in {"pending", "applied"} and not item.evidence.get("_service", {}).get("dry_run")
                for item in self.repository.reverts(original.id)
            ):
                raise ChangeConflict("Откат этого изменения уже выполняется или выполнен.")
            refs = load_refs(original)
            states = await adapter.get_state(refs)
            operations = load_operations(original)
            entities = self.repository.local_entities(business_id, original.platform)
            group_limits = original.evidence.get("group_limits") if original.entity_type == "group" else None
            if group_limits is not None:
                current_limits = (
                    self.repository.business(business_id).settings.get("_worker", {}).get("group_limits", {})
                )
                mismatches = [
                    {
                        "entity_type": "group",
                        "entity_id": key,
                        "field": "daily_limit_kop",
                        "expected": value,
                        "current": current_limits.get(key),
                        "restore": group_limits["before"][key],
                    }
                    for key, value in group_limits["after"].items()
                    if current_limits.get(key) != value
                ]
            else:
                mismatches = differences(operations, states)
            if mismatches and not confirm:
                raise ConfirmationRequired(mismatches)
            inverse = inverse_operations(operations, states)
            if group_limits is not None:
                # SCALE меняет доли групп. Перебалансированный кэп кампании сохраняем.
                preserved = []
                for op in inverse:
                    entity = entities.get((op.entity_type, op.entity_id), states[op.entity_id])
                    value = getattr(entity, op.field)
                    preserved.append(replace(op, before=value, after=value))
                inverse = preserved
            ads = {entity.id: entity for entity in entities.values() if isinstance(entity, Ad)}
            for experiment in self.repository.experiments(original):
                for ad_id, status in ((experiment.control_ad_id, "active"), (experiment.variant_ad_id, "paused_owner")):
                    ad = ads.get(ad_id)
                    if ad is None or ad.external_id not in states:
                        raise ValueError("Для отката эксперимента нужны оба объявления на площадке.")
                    if ad_id == experiment.variant_ad_id and any(
                        op.entity_id == ad.external_id and op.field == "status" and op.after == "archived"
                        for op in inverse
                    ):
                        continue
                    inverse = [op for op in inverse if (op.entity_id, op.field) != (ad.external_id, "status")]
                    inverse.append(
                        Operation("set_status", "ad", ad.external_id, "status", states[ad.external_id].status, status)
                    )
            return await self.changes._apply_change(
                business_id=business_id,
                adapter=adapter,
                refs=refs,
                operations=inverse,
                actor=actor,
                kind="revert",
                platform=original.platform,
                entity_type=original.entity_type,
                entity_id=original.entity_id,
                reason_code="REVERT",
                reason_params={"change": original.reason_text},
                evidence={
                    "confirmed": confirm,
                    "conflicts": mismatches,
                    "warning": "Созданное объявление будет остановлено и убрано в архив."
                    if any(not op.invertible for op in operations)
                    else None,
                    **(
                        {
                            "group_limits": {
                                "before": {key: current_limits.get(key) for key in group_limits["before"]},
                                "after": original.evidence["group_limits"]["before"],
                            }
                        }
                        if group_limits is not None
                        else {}
                    ),
                },
                reverts_id=original.id,
                now=now or datetime.now(UTC),
            )
