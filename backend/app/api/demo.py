import asyncio
from collections.abc import Iterator
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field, StrictInt
from starlette.concurrency import run_in_threadpool

from app.api.auth import require_demo
from app.api.stats import CurrentBusiness
from app.auth.sessions import require_csrf
from app.services.changes import ChangeConflict
from app.services.demo import DemoRepository, DemoService, demo_now
from app.services.experiments import create_experiment
from app.worker.clock import MOSCOW, SystemClock
from app.worker.jobs import Worker
from app.worker.main import demo_tick_lock

router = APIRouter(prefix="/demo", dependencies=[Depends(require_demo)])


class AdvanceIn(BaseModel):
    days: StrictInt = Field(ge=1, le=35)


def get_demo_service(request: Request, business: CurrentBusiness) -> Iterator[DemoService]:
    from app.db.session import SessionLocal

    with SessionLocal() as db:
        yield DemoService(
            Worker(
                DemoRepository(db, business.id),
                request.app.state.platform_adapters.resolve,
                dry_run=False,
                experiment_factory=create_experiment,
            )
        )


Service = Annotated[DemoService, Depends(get_demo_service)]


@router.get("/status")
def status(business: CurrentBusiness) -> dict:
    now = demo_now(business, SystemClock().now)
    return {"today": now.astimezone(MOSCOW).date(), "total_days": business.settings.get("_demo", {}).get("days", 0)}


def run_advance(service: DemoService, business_id: int, days: int) -> dict:
    # Fake и фоновый tick используют один mutex, чтобы не менять симулятор одновременно.
    if not demo_tick_lock.acquire(blocking=False):
        raise HTTPException(409, "Демо уже обрабатывается. Повторите после завершения.")
    try:
        return asyncio.run(service.advance(business_id, days))
    except ChangeConflict as error:
        raise HTTPException(409, str(error)) from error
    finally:
        demo_tick_lock.release()


@router.post("/advance", dependencies=[Depends(require_csrf)])
async def advance(body: AdvanceIn, business: CurrentBusiness, service: Service) -> dict:
    return await run_in_threadpool(run_advance, service, business.id, body.days)
