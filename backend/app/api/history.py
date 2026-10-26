from collections.abc import Iterator
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request

from app.auth.sessions import current_user, require_csrf
from app.db.models import ChangeLog, User
from app.platforms.base import PlatformError
from app.services.changes import AdapterResolver, ChangeConflict, ChangesRepository
from app.services.rollback import ConfirmationRequired, RollbackService, can_revert

router = APIRouter(prefix="/api/history")


def get_history_repository() -> Iterator[ChangesRepository]:
    from app.db.session import SessionLocal

    with SessionLocal() as db:
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


def history_item(change: ChangeLog, business=None, reverts=()) -> dict:
    metadata = (change.evidence or {}).get("_service", {})
    return {
        "id": change.id,
        "business_id": change.business_id,
        "created_at": change.created_at,
        "actor": change.actor,
        "kind": change.kind,
        "platform": change.platform,
        "entity_type": change.entity_type,
        "entity_id": change.entity_id,
        "operations": change.operations,
        "reason_code": change.reason_code,
        "reason_params": change.reason_params,
        "reason_text": change.reason_text,
        "evidence": {key: value for key, value in (change.evidence or {}).items() if key != "_service"},
        "status": "proposed" if metadata.get("dry_run") else change.status,
        "error": metadata.get("error"),
        "reverts_id": change.reverts_id,
        "can_revert": can_revert(change, business, reverts=reverts),
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
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=100)] = 20,
) -> dict:
    items, total = repository.history(
        user.id, business_id=business_id, platform=platform, actor=actor, page=page, page_size=page_size
    )
    businesses = {business_id: repository.business(business_id) for business_id in {item.business_id for item in items}}
    return {
        "items": [history_item(item, businesses[item.business_id], repository.reverts(item.id)) for item in items],
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
        )
    except ConfirmationRequired as error:
        raise HTTPException(
            409, {"confirmation_required": True, "message": str(error), "changes": error.changes}
        ) from error
    except ChangeConflict as error:
        raise HTTPException(409, str(error)) from error
    except (ValueError, KeyError) as error:
        raise HTTPException(422, "Не удалось подготовить безопасный откат изменения.") from error
    except PlatformError as error:
        raise HTTPException(502, "Не удалось прочитать состояние площадки. Попробуйте позже.") from error
    return history_item(change)
