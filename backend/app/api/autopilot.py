from collections.abc import Iterator
from typing import Annotated

from fastapi import APIRouter, Depends, Request
from sqlalchemy import select
from sqlalchemy.orm import load_only

from app.api.stats import CurrentBusiness
from app.auth.sessions import require_csrf
from app.db.models import MarkingProfile, PlatformAccount
from app.db.query import condition
from app.demo_flags import demo_now
from app.platforms.fake import VirtualClock
from app.services.screens import ScreensService
from app.worker.clock import SystemClock
from app.worker.jobs import Worker, WorkerRepository

router = APIRouter(prefix="/api/autopilot")


class ScreensRepository(WorkerRepository):
    def rows(self, model, business_id: int | None = None, **filters) -> list:
        if model not in (MarkingProfile, PlatformAccount):
            return super().rows(model, business_id, **filters)
        fields = (
            (MarkingProfile.business_id, MarkingProfile.subject_type, MarkingProfile.confirmed_in_platform)
            if model is MarkingProfile
            else (PlatformAccount.id, PlatformAccount.business_id, PlatformAccount.platform, PlatformAccount.status)
        )
        statement = select(model).options(load_only(*fields, raiseload=True))
        if business_id is not None:
            statement = statement.where(model.business_id == business_id)
        for name, value in filters.items():
            statement = statement.where(condition(getattr(model, name), value))
        return list(self.db.scalars(statement))


def get_screens_service(request: Request, business: CurrentBusiness) -> Iterator[ScreensService]:
    from app.db.session import SessionLocal

    with SessionLocal() as db:
        yield ScreensService(
            Worker(
                ScreensRepository(db),
                request.app.state.platform_adapters.resolve,
                dry_run=False,
                clock=VirtualClock(demo_now(business, SystemClock().now)),
            )
        )


Screens = Annotated[ScreensService, Depends(get_screens_service)]


@router.get("/status")
def status(business: CurrentBusiness, service: Screens) -> dict:
    return service.status(business)


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
