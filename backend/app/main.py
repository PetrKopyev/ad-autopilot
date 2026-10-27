import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import httpx
from fastapi import APIRouter, FastAPI
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from app.api.ads import router as ads_router
from app.api.auth import router as auth_router
from app.api.autopilot import router as autopilot_router
from app.api.body import BodyLimit
from app.api.business import router as business_router
from app.api.clients import router as clients_router
from app.api.experiments import router as experiments_router
from app.api.history import router as history_router
from app.api.notifications import router as notifications_router
from app.api.plan import MAX_UPLOAD, TOO_LARGE
from app.api.plan import router as plan_router
from app.api.routing import ReleasingRoute
from app.api.stats import router as stats_router
from app.api.vk import router as vk_router
from app.config import settings
from app.db.session import SessionLocal, engine
from app.services.registry import AdapterRegistry, db_loader
from app.worker.main import DemoRegistry, start_scheduler


class WithoutQuery(logging.Filter):
    """Строка запроса несёт код входа (callback OAuth) и другие закрытые данные: журнал доступа uvicorn пишет только
    адрес, метод, путь и код ответа."""

    def filter(self, record: logging.LogRecord) -> bool:
        client, method, path, *rest = record.args
        record.args = (client, method, path.split("?", 1)[0], *rest)
        return True


logging.getLogger("uvicorn.access").addFilter(WithoutQuery())


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    async with httpx.AsyncClient(timeout=30) as http:
        app.state.http = http
        registry_type = DemoRegistry if settings.platform_mode == "fake" else AdapterRegistry
        registry = registry_type(db_loader(SessionLocal), mode=settings.platform_mode, http=http)
        app.state.platform_adapters = registry
        scheduler = await start_scheduler(registry) if settings.platform_mode == "fake" else None
        try:
            yield
        finally:
            if scheduler:
                scheduler.shutdown(wait=True)


app = FastAPI(title="Автопилот рекламы", lifespan=lifespan)
app.add_middleware(BodyLimit, paths={"/api/plan/upload-photo": (MAX_UPLOAD, TOO_LARGE)})
router = APIRouter(prefix="/api", route_class=ReleasingRoute)


@router.get("/health")
def health() -> dict[str, bool | str]:
    db_ok = False
    try:
        with engine.connect() as connection:
            connection.execute(text("SELECT 1"))
        db_ok = True
    except SQLAlchemyError:
        pass

    return {"ok": True, "db": db_ok, "mode": settings.platform_mode}


app.include_router(router)
app.include_router(auth_router)
app.include_router(history_router)
app.include_router(notifications_router)
app.include_router(stats_router)
app.include_router(clients_router)
app.include_router(vk_router)
app.include_router(business_router)
app.include_router(plan_router)

app.include_router(autopilot_router)
app.include_router(ads_router)
app.include_router(experiments_router)
