from collections.abc import AsyncIterator
from typing import Annotated

from fastapi import APIRouter, Depends, Request
from sqlalchemy import select
from sqlalchemy.orm import Session as DBSession
from sqlalchemy.orm import load_only

from app.api.routing import ReleasingRoute, request_session
from app.api.stats import CurrentBusiness, OptionalBusiness
from app.auth.sessions import require_csrf
from app.db.models import Business, PlatformAccount
from app.db.query import condition
from app.demo_flags import demo_now
from app.platforms.fake import VirtualClock
from app.services.screens import ScreensService
from app.worker.clock import SystemClock
from app.worker.jobs import Worker, WorkerRepository

router = APIRouter(prefix="/api/autopilot", route_class=ReleasingRoute)


class ScreensRepository(WorkerRepository):
    def rows(self, model, business_id: int | None = None, **filters) -> list:
        if model is not PlatformAccount:
            return super().rows(model, business_id, **filters)
        # Экранам токены кабинетов не нужны: читается только состояние подключения.
        fields = (PlatformAccount.id, PlatformAccount.business_id, PlatformAccount.platform, PlatformAccount.status)
        statement = select(PlatformAccount).options(load_only(*fields, raiseload=True))
        if business_id is not None:
            statement = statement.where(PlatformAccount.business_id == business_id)
        for name, value in filters.items():
            statement = statement.where(condition(getattr(PlatformAccount, name), value))
        return list(self.db.scalars(statement))


# Пользователь ещё не прошёл мастер: бизнеса нет, показывать нечего.
NO_BUSINESS = {
    "status": "no_business",
    "last_check_at": None,
    "auto_continue": False,
    "resume_pending": False,
    "needs_attention": [],
    "has_plan": False,
}


def screens_service(request: Request, db: DBSession, business: Business) -> ScreensService:
    return ScreensService(
        Worker(
            ScreensRepository(db),
            request.app.state.platform_adapters.resolve,
            dry_run=False,
            clock=VirtualClock(demo_now(business, SystemClock().now)),
        )
    )


async def get_screens_service(request: Request, business: CurrentBusiness) -> AsyncIterator[ScreensService]:
    with request_session() as db:
        yield screens_service(request, db, business)


async def get_optional_screens(request: Request, business: OptionalBusiness) -> AsyncIterator[ScreensService | None]:
    if business is None:
        yield None
        return
    with request_session() as db:
        yield screens_service(request, db, business)


Screens = Annotated[ScreensService, Depends(get_screens_service)]
OptionalScreens = Annotated[ScreensService | None, Depends(get_optional_screens)]


@router.get("/status")
def status(business: OptionalBusiness, service: OptionalScreens) -> dict:
    return NO_BUSINESS if business is None else service.status(business)


@router.post("/pause", dependencies=[Depends(require_csrf)])
async def pause(business: CurrentBusiness, service: Screens) -> dict:
    from app.api.business import human_errors

    with human_errors():
        return await service.pause(business)


@router.post("/resume", dependencies=[Depends(require_csrf)])
async def resume(business: CurrentBusiness, service: Screens) -> dict:
    from app.api.business import human_errors

    with human_errors():
        return await service.resume(business)
