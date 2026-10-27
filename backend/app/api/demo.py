import asyncio
import logging
from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager, contextmanager
from datetime import date
from functools import partial
from typing import Annotated

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request
from pydantic import BaseModel, Field, StrictInt

from app.api.auth import require_demo
from app.api.routing import ReleasingRoute
from app.api.stats import CurrentBusiness, Today
from app.auth.sessions import require_csrf
from app.db.models import Business
from app.demo_flags import ADVANCING, AdvanceProgress, advance_jobs, demo_now
from app.platforms.base import AdPlatform
from app.services.changes import ChangeConflict
from app.services.demo import DemoRepository, DemoService
from app.services.experiments import create_experiment
from app.worker.clock import SystemClock
from app.worker.jobs import Worker

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/demo", dependencies=[Depends(require_demo)], route_class=ReleasingRoute)

type OpenService = Callable[[int], AbstractContextManager[DemoService]]


class AdvanceIn(BaseModel):
    days: StrictInt = Field(ge=1, le=35)


@contextmanager
def open_demo_service(resolve: Callable[[int, str], AdPlatform], business_id: int) -> Iterator[DemoService]:
    from app.db.session import SessionLocal

    with SessionLocal() as db:
        yield DemoService(
            Worker(DemoRepository(db, business_id), resolve, dry_run=False, experiment_factory=create_experiment)
        )


def get_demo_service(request: Request) -> OpenService:
    # Перемотка идёт после ответа, поэтому сессию БД открывает сама задача, а не запрос.
    return partial(open_demo_service, request.app.state.platform_adapters.resolve)


Service = Annotated[OpenService, Depends(get_demo_service)]


def demo_status(business: Business, today: date) -> dict:
    progress = advance_jobs.get(business.id)
    return {
        "today": today,
        # Точное демо-время: по нему клиент считает «только что» и «N мин назад» у записей демо-истории.
        "now": demo_now(business, SystemClock().now).isoformat(),
        "total_days": business.settings.get("_demo", {}).get("days", 0),
        "advance": None
        if progress is None
        else {"state": progress.state, "days": progress.days, "done": progress.done, "error": progress.error},
    }


@router.get("/status")
def status(business: CurrentBusiness, today: Today) -> dict:
    return demo_status(business, today)


def run_advance(open_service: OpenService, business_id: int, progress: AdvanceProgress) -> None:
    try:
        with open_service(business_id) as service:
            asyncio.run(service.advance(business_id, progress))
    except ChangeConflict as error:
        progress.error = str(error)
    except Exception:
        logger.exception("Demo advance failed for business %s", business_id)
        progress.error = "Перемотка прервалась. Попробуйте ещё раз."
    progress.state = "error" if progress.error else "done"


@router.post("/advance", status_code=202, dependencies=[Depends(require_csrf)])
def advance(
    body: AdvanceIn, business: CurrentBusiness, today: Today, open_service: Service, background: BackgroundTasks
) -> dict:
    if advance_jobs.running(business.id):
        raise HTTPException(409, ADVANCING)
    try:
        with open_service(business.id) as service:
            service.check(business, body.days)
    except ChangeConflict as error:
        raise HTTPException(409, str(error)) from error
    progress = advance_jobs.start(business.id, body.days)
    if progress is None:
        raise HTTPException(409, ADVANCING)
    background.add_task(run_advance, open_service, business.id, progress)
    return demo_status(business, today)
