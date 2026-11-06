import logging
from collections.abc import AsyncIterator
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request

from app.api.routing import ReleasingRoute, request_session
from app.auth.sessions import current_user, require_csrf
from app.db.models import ChangeLog, User
from app.demo_flags import demo_now
from app.platforms.base import PlatformError
from app.platforms.fake import VirtualClock
from app.services.changes import AdapterResolver, ChangeConflict, ChangesRepository, ServerBusy
from app.services.optimize import rebalance_budget
from app.services.rollback import ConfirmationRequired, RollbackService, can_revert, superseded, turns_on
from app.worker.clock import SystemClock
from app.worker.jobs import Worker

router = APIRouter(prefix="/api/history", route_class=ReleasingRoute)
logger = logging.getLogger(__name__)


async def get_history_repository() -> AsyncIterator[ChangesRepository]:
    with request_session() as db:
        yield ChangesRepository(db)


def get_adapter_resolver(request: Request) -> AdapterResolver:
    def resolve(business_id: int, platform: str):
        adapters = getattr(request.app.state, "platform_adapters", {})
        adapter = adapters.get((business_id, platform))
        if adapter is None:
            raise HTTPException(503, "Кабинет площадки пока недоступен для изменений.")
        return adapter

    return resolve


Repository = Annotated[ChangesRepository, Depends(get_history_repository)]
CurrentUser = Annotated[User, Depends(current_user)]
Adapters = Annotated[AdapterResolver, Depends(get_adapter_resolver)]


def history_item(change: ChangeLog, business=None, reverts=(), replaced=(), campaigns=None, overridden=()) -> dict:
    metadata = (change.evidence or {}).get("_service", {})
    # Объявление и группа называются по кампании из ссылок изменения: «Остановка рекламы» без названия не говорит, где.
    campaign_id = change.entity_id if change.entity_type == "campaign" else metadata.get("refs", {}).get("campaign_id")
    return {
        "id": change.id,
        "business_id": change.business_id,
        "created_at": change.created_at,
        "actor": change.actor,
        "kind": change.kind,
        "platform": change.platform,
        "entity_type": change.entity_type,
        "entity_id": change.entity_id,
        # Запуск и остановка пишутся по записи на кампанию: без названия в журнале N одинаковых строк.
        "campaign": (campaigns or {}).get((change.platform, campaign_id)),
        "operations": change.operations,
        "reason_code": change.reason_code,
        "reason_params": change.reason_params,
        "reason_text": change.reason_text,
        "evidence": {key: value for key, value in (change.evidence or {}).items() if key != "_service"},
        "status": "proposed" if metadata.get("dry_run") else change.status,
        "error": metadata.get("error"),
        "reverts_id": change.reverts_id,
        "can_revert": can_revert(change, business, reverts=reverts, replaced=replaced, overridden=overridden),
        # Включение рекламы не отменяется из журнала: фронт показывает вместо кнопки, как остановить рекламу.
        "turns_on": turns_on(change),
        "revert_warning": "Созданное объявление будет остановлено и убрано в архив."
        if any(not op.get("invertible", True) for op in change.operations)
        else None,
    }


@router.get("")
def history(
    user: CurrentUser,
    repository: Repository,
    platform: Literal["direct", "vk", "fake"] | None = None,
    actor: Literal["autopilot", "owner", "system", "external"] | None = None,
    business_id: Annotated[int | None, Query(gt=0)] = None,
    page: Annotated[int, Query(ge=1, le=100_000)] = 1,
    page_size: Annotated[int, Query(ge=1, le=100)] = 20,
) -> dict:
    items, total = repository.history(
        user.id, business_id=business_id, platform=platform, actor=actor, page=page, page_size=page_size
    )
    businesses = {business_id: repository.business(business_id) for business_id in {item.business_id for item in items}}
    replaced = {business_id: repository.replaced_entities(business_id) for business_id in businesses}
    campaigns = {business_id: repository.campaign_names(business_id) for business_id in businesses}
    overridden = {
        business_id: superseded(
            repository.status_changes(business_id, min(item.id for item in items if item.business_id == business_id))
        )
        for business_id in businesses
    }
    return {
        "items": [
            history_item(
                item,
                businesses[item.business_id],
                repository.reverts(item.id),
                replaced[item.business_id],
                campaigns[item.business_id],
                overridden[item.business_id],
            )
            for item in items
        ],
        "total": total,
        "page": page,
        "page_size": page_size,
    }


@router.post("/{change_id}/revert", dependencies=[Depends(require_csrf)])
async def revert(
    change_id: int,
    user: CurrentUser,
    repository: Repository,
    resolve_adapter: Adapters,
    confirm: bool = False,
) -> dict:
    original = repository.get_owned(change_id, user.id)
    if original is None:
        raise HTTPException(404, "Изменение не найдено.")
    try:
        change = await RollbackService(repository).revert(
            change_id,
            business_id=original.business_id,
            adapter=resolve_adapter(original.business_id, original.platform),
            confirm=confirm,
            now=demo_now(repository.business(original.business_id), SystemClock().now),
        )
        if original.entity_type == "group" and original.evidence.get("group_limits"):
            # Откат SCALE вернул лимиты групп (Н7): переносим их в лимиты кампаний пересчётом
            # при текущем бюджете — иначе на площадке остались бы устаревшие абсолютные значения.
            # Пересчёт только при работающей рекламе: force идёт мимо стража business.status/kill_switch
            # и возобновил бы кампании, которые «Остановить всю рекламу» пропустила.
            business = repository.business(original.business_id)
            meta = business.settings.get("_worker", {})
            if change.status == "applied" and business.status == "running" and not meta.get("kill_switch"):
                worker = Worker(
                    repository,
                    resolve_adapter,
                    dry_run=False,
                    clock=VirtualClock(demo_now(business, SystemClock().now)),
                )
                worker.step_key = f"owner:revert-scale:{change.id}"
                try:
                    await rebalance_budget(worker, business, force=True)
                except (ChangeConflict, PlatformError):
                    # Откат уже применён: сбой пересчёта не должен выглядеть как сбой отката.
                    # Лимиты кампаний досчитает штатный тик воркера.
                    logger.warning("Пересчёт лимитов кампаний после отката SCALE не выполнен", exc_info=True)
    except ConfirmationRequired as error:
        raise HTTPException(
            409, {"confirmation_required": True, "message": str(error), "changes": error.changes}
        ) from error
    except ServerBusy as error:
        raise HTTPException(503, str(error)) from error
    except ChangeConflict as error:
        raise HTTPException(409, str(error)) from error
    except (ValueError, KeyError) as error:
        raise HTTPException(422, "Не удалось подготовить безопасный откат изменения.") from error
    except PlatformError as error:
        raise HTTPException(502, "Не удалось прочитать состояние площадки. Попробуйте позже.") from error
    return history_item(change, campaigns=repository.campaign_names(change.business_id))
