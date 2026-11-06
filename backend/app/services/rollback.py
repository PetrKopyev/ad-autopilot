from collections.abc import Collection, Mapping, Sequence
from datetime import UTC, datetime, timedelta

from app.db.models import Ad, AdGroup, Campaign, ChangeLog, Experiment
from app.domain.explain import undone
from app.platforms.base import AdPlatform, EntityState, Operation
from app.services.changes import (
    SETTLE,
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


def budget_protection(change: ChangeLog, business=None) -> bool:
    if change.actor != "system":
        return False
    if change.reason_code in {
        "BUDGET_REBALANCE",
        "BUDGET_STOP",
        "SPEND_MISMATCH",
        "MONTH_STOP",
        "BUDGET_UPDATE_FAILED",
        # Кампания брошенного запуска: бизнес не запущен, и за включённой рекламой никто бы не следил.
        "LAUNCH_INTERRUPTED",
    }:
        return True
    return (
        change.kind == "pause"
        and business is not None
        and (business.status == "budget_stop" or business.settings.get("_worker", {}).get("kill_switch", False))
    )


REPLACED = (
    "Эта кампания осталась от прежнего плана: реклама идёт по кампаниям нового, и включать её снова мы не будем. "
    "Её расход по-прежнему учитывается в бюджете."
)


def turns_on_replaced(change: ChangeLog, replaced: Collection[tuple[str, str]]) -> bool:
    """Откат вернул бы включённой кампанию прежнего плана: реклама с устаревшими объявлениями, без доли бюджета и без
    присмотра шагов воркера (Worker.current_campaigns). Откат к остановленной кампании разрешён. В старых записях
    журнала у операций есть не все ключи: такая запись не роняет журнал."""
    return any(
        op.get("entity_type") == "campaign"
        and op.get("field") == "status"
        and op.get("before") == "active"
        and (change.platform, op.get("entity_id")) in replaced
        for op in change.operations
    )


SUPERSEDED = "Статус этой рекламы с тех пор уже меняли: возобновить или остановить её можно на главной."

NOTHING_TO_REVERT = "Эта запись информационная: она ничего не меняла, откатывать нечего."

# Записи включения рекламы: откат одной из них остановил бы кампании, а бизнес остался бы «работает», и «Продолжить»
# их уже не включило бы. Остановить рекламу можно на главной. Новый код включения добавляется сюда; журнал
# получает признак в turns_on (api.history), фронт своего списка не держит.
TURNS_ON = {"LAUNCH", "OWNER_RESUME", "BUDGET_RESUME", "MONTH_RESUME"}
# Коды, которые включают кампанию только в части записей: включение объявлений откатывается как обычно.
TURNS_ON_CAMPAIGN = {"SERVICE_RETURNED"}
TURN_ON = (
    "Это включение рекламы, отдельно его не отменить. "
    "Остановить рекламу можно кнопкой «Остановить всю рекламу» на главной."
)


def turns_on(change: ChangeLog) -> bool:
    return (
        change.reason_code in TURNS_ON
        or (
            change.reason_code in TURNS_ON_CAMPAIGN
            and any(op.get("entity_type") == "campaign" and op.get("field") == "status" for op in change.operations)
        )
        or (
            # Доводка после проверки площадкой включает кампанию только в части записей; пауза откатывается как обычно.
            change.reason_code == SETTLE
            and any(
                (op.get("entity_type"), op.get("field"), op.get("after")) == ("campaign", "status", "active")
                for op in change.operations
            )
        )
    )


def reverts_by_experiment(change: ChangeLog) -> bool:
    """Записи проверок объявлений откатываются по связанному эксперименту, а не по собственным операциям:
    аудиторный запуск создавал группу отдельным вызовом (операций в записи нет), вердикт «разницы нет» тоже
    ничего не менял. Без ссылок на площадку такой откат не построить — это информационная запись чужого вида."""
    return (change.evidence or {}).get("_service", {}).get("refs") is not None and (
        change.kind == "experiment_verdict" or change.reason_code == "EXPERIMENT_START"
    )


def experiment_verdict_revert_text(
    operations: Sequence[Operation], ads: Mapping[int, Ad], skipped: Collection[str]
) -> str:
    """Текст отката вывода сравнения по факту: кого вернули к прежнему статусу и кого восстановить уже не вышло."""
    titles = {ad.external_id: ad.title for ad in ads.values() if ad.external_id}
    facts = []
    for op in operations:
        if op.entity_type != "ad" or op.field != "status":
            continue
        title = titles.get(op.entity_id, "объявление")
        if op.after == "active":
            facts.append(f"объявление «{title}» снова работает")
        elif op.after == "archived":
            facts.append(f"объявление «{title}» остановлено и убрано в архив")
        else:
            facts.append(f"объявлению «{title}» вернули прежний статус")
    for external_id in skipped:
        title = titles.get(external_id, "объявление")
        facts.append(f"объявление «{title}» уже удалено в кабинете площадки — восстанавливать не стали")
    if not facts:
        facts.append("настройки рекламы не меняли")
    return "Вы отменили вывод сравнения вариантов: " + "; ".join(facts) + "."


def superseded(changes: Sequence[ChangeLog]) -> set[int]:
    """Остановки и возобновления, после которых статус тех же объектов менялся ещё раз; changes — по порядку
    (ChangesRepository.status_changes). После «Продолжить» на главной старая остановка в журнале уже не отменяется."""
    later: set[tuple[str | None, str]] = set()
    result = set()
    for change in reversed(changes):
        # В старых записях журнала у операций есть не все ключи: такая запись не должна ронять журнал.
        entities = {(change.platform, op.get("entity_id")) for op in change.operations if op.get("field") == "status"}
        if change.kind in {"pause", "resume"} and entities & later:
            result.add(change.id)
        later |= entities
    return result


def can_revert(
    change: ChangeLog,
    business=None,
    *,
    actor: str = "owner",
    reverts: Sequence[ChangeLog] = (),
    replaced: Collection[tuple[str, str]] = (),
    overridden: Collection[int] = (),
) -> bool:
    """replaced — кампании прежних планов (ChangesRepository.replaced_entities), overridden — superseded()."""
    return (
        change.status == "applied"
        and change.kind != "revert"
        # Записи о маркировке информационные: операций нет, откатывать нечего.
        and change.kind != "marking"
        and not turns_on(change)
        # Информационные записи без операций (например, о пересозданном содержимом кабинета) не откатываются:
        # обратных операций не существует, и откат упал бы с ошибкой. Исключение — записи проверок объявлений.
        and (bool(change.operations) or reverts_by_experiment(change))
        and change.id not in overridden
        and not (change.evidence or {}).get("_service", {}).get("dry_run")
        and not budget_protection(change, business)
        and not turns_on_replaced(change, replaced)
        and (change.actor != "external" or actor == "owner")
        and not any(
            item.status in {"pending", "applied"} and not (item.evidence or {}).get("_service", {}).get("dry_run")
            for item in reverts
        )
    )


def inverse_operations(operations: Sequence[Operation], states: Mapping[str, EntityState]) -> list[Operation]:
    result = []
    archived = set()
    for op in reversed(operations):
        if op.entity_id in archived:
            continue
        if op.kind == "add_ad":
            state = states.get(op.entity_id)
            if state is None or state.entity_type != "ad":
                raise ValueError("Добавленное объявление не найдено на площадке.")
            result.append(Operation("set_status", "ad", state.entity_id, "status", state.status, "paused_owner"))
            archived.add(op.entity_id)
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


def related_experiments(repository: ChangesRepository, change: ChangeLog) -> list[Experiment]:
    result = repository.experiments(change)
    experiment_id = change.evidence.get("experiment_id")
    if experiment_id is not None and all(item.id != experiment_id for item in result):
        # change_id после вердикта указывает на вердикт; связь запуска сохраняется в evidence.
        if hasattr(repository, "rows"):
            result.extend(repository.rows(Experiment, change.business_id, id=experiment_id))
        else:
            item = repository.db.get(Experiment, experiment_id)
            if item is not None and item.business_id == change.business_id:
                result.append(item)
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
    for experiment in related_experiments(repository, original):
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
    if original.reason_code == "OWNER_STOP":
        # Отмена «Остановить всю рекламу»: кампании вернули к прежним статусам, и бизнес тоже возвращаем
        # в работу — иначе расход шёл бы при выключенном автопилоте, а главная показывала «на паузе»
        # (мелочь 3 круга 7).
        business = repository.business(revert.business_id)
        meta = dict(business.settings.get("_worker", {}))
        meta.pop("kill_switch", None)
        meta.pop("pending_stop", None)
        business.settings = business.settings | {"_worker": meta}
        if business.status == "paused":
            business.status = "running"
        repository.save(business)


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
            business = self.repository.business(business_id)
            replaced = self.repository.replaced_entities(business_id)
            reverts = self.repository.reverts(original.id)
            overridden = superseded(self.repository.status_changes(business_id, original.id))
            if not can_revert(
                original, business, actor=actor, reverts=reverts, replaced=replaced, overridden=overridden
            ):
                if not original.operations and not reverts_by_experiment(original):
                    raise ChangeConflict(NOTHING_TO_REVERT)
                if budget_protection(original, business):
                    raise ChangeConflict("Это защита бюджета, отменить нельзя.")
                if turns_on(original):
                    raise ChangeConflict(TURN_ON)
                if turns_on_replaced(original, replaced):
                    raise ChangeConflict(REPLACED)
                if original.id in overridden:
                    raise ChangeConflict(SUPERSEDED)
                if original.actor == "external" and actor != "owner":
                    raise ChangeConflict("Изменение в кабинете может отменить только владелец.")
                raise ChangeConflict("Изменение недоступно для отката или откат уже выполняется.")
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
                # Абсолютные лимиты кампании не восстанавливаем: после отката вызывающий код вернёт
                # group_limits и запустит rebalance_budget(force=True) — лимиты кампаний пересчитаются
                # из лимитов групп при текущем бюджете. Иначе откат возвращал «до» на момент SCALE
                # (устаревало после пересчёта → каждый откат упирался в подтверждение, а ночью лимит
                # выше бюджета держался часами), а накопленный кэп мог оказаться ниже уже потраченного.
                operations = [op for op in operations if op.kind != "set_budget"]
            else:
                mismatches = differences(operations, states)
            if mismatches and not confirm:
                raise ConfirmationRequired(mismatches)
            inverse = inverse_operations(operations, states)
            ads = {entity.id: entity for entity in entities.values() if isinstance(entity, Ad)}
            skipped: list[str] = []
            verdict = original.kind == "experiment_verdict"
            if verdict:
                # Откат вывода сравнения возвращает объявлениям статусы, записанные в самом изменении (какие были
                # до вывода); победитель не трогается. Объявление, которое владелец уже удалил на площадке, не
                # воскрешаем: запись об откате говорит о нём отдельно.
                restore_targets = {op.entity_id for op in inverse if op.entity_type == "ad" and op.field == "status"}
                for experiment in related_experiments(self.repository, original):
                    for ad_id in (experiment.control_ad_id, experiment.variant_ad_id):
                        ad = ads.get(ad_id)
                        if ad is None or ad.external_id not in restore_targets:
                            continue
                        state = states.get(ad.external_id)
                        if state is None or state.status == "archived":
                            skipped.append(ad.external_id)
                if skipped:
                    inverse = [
                        op
                        for op in inverse
                        if not (op.entity_type == "ad" and op.field == "status" and op.entity_id in skipped)
                    ]
            else:
                # Откат самого запуска проверки останавливает её новое плечо: для аудиторной проверки запись
                # запуска без операций — группу на площадке создавал отдельный вызов, и без этих операций
                # откату нечего было бы применить.
                for experiment in related_experiments(self.repository, original):
                    for ad_id, status in (
                        (experiment.control_ad_id, "active"),
                        (experiment.variant_ad_id, "paused_owner"),
                    ):
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
                            Operation(
                                "set_status", "ad", ad.external_id, "status", states[ad.external_id].status, status
                            )
                        )
            change = await self.changes._apply_change(
                business_id=business_id,
                adapter=adapter,
                refs=refs,
                operations=inverse,
                actor=actor,
                kind="revert",
                platform=original.platform,
                entity_type=original.entity_type,
                entity_id=original.entity_id,
                reason_code="EXPERIMENT_VERDICT_REVERT" if verdict else "REVERT",
                reason_params={"change": undone(original.kind, original.entity_type, original.reason_params)},
                # _apply_change берёт готовый текст, только если кода нет в шаблонах: REVERT там есть, свой код —
                # нет, текст составляем по факту отката.
                reason_text=experiment_verdict_revert_text(inverse, ads, skipped) if verdict else None,
                record_only=(verdict or group_limits is not None) and not inverse,
                evidence={
                    "confirmed": confirm,
                    "conflicts": mismatches,
                    "warning": "Добавленное объявление будет остановлено."
                    if any(op.kind == "add_ad" for op in operations)
                    else (
                        "Созданное объявление будет остановлено и убрано в архив."
                        if any(not op.invertible for op in operations)
                        else None
                    ),
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
            if group_limits is not None and change.status == "applied":
                # Возвращаем лимиты групп: rebalance_budget(force=True), который вызывает конечная точка,
                # перенесёт их в лимиты кампаний при текущем бюджете. Только применённый откат: при
                # pending (запись на площадке не подтверждена) или failed (площадка не приняла) локальные
                # лимиты не трогаем — «откатано» на словах, а деньги фактически пересчитаны бы не были (В2 круга 4).
                business = self.repository.business(business_id)
                meta = business.settings.get("_worker", {})
                restored = dict(meta.get("group_limits", {})) | dict(group_limits["before"])
                business.settings = business.settings | {"_worker": meta | {"group_limits": restored}}
                self.repository.save(business)
            return change
