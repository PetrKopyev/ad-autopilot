from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import httpx
from fastapi import APIRouter, FastAPI
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from app.api.auth import router as auth_router
from app.api.business import router as business_router
from app.api.clients import router as clients_router
from app.api.history import router as history_router
from app.api.plan import router as plan_router
from app.api.stats import router as stats_router
from app.api.vk import router as vk_router
from app.config import settings
from app.db.session import SessionLocal, engine
from app.services.registry import AdapterRegistry, db_loader


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    async with httpx.AsyncClient(timeout=30) as http:
        app.state.http = http
        app.state.platform_adapters = AdapterRegistry(db_loader(SessionLocal), mode=settings.platform_mode, http=http)
        yield


app = FastAPI(title="Автопилот рекламы", lifespan=lifespan)
router = APIRouter(prefix="/api")


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
app.include_router(stats_router)
app.include_router(clients_router)
app.include_router(vk_router)
app.include_router(business_router)
app.include_router(plan_router)
