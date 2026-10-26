from fastapi import APIRouter, FastAPI
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from app.api.auth import router as auth_router
from app.api.history import router as history_router
from app.api.stats import router as stats_router
from app.config import settings
from app.db.session import engine

app = FastAPI(title="Автопилот рекламы")
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
