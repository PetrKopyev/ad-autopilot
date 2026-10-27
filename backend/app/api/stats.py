from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import date, datetime
from typing import Annotated
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException, Query

from app.api.routing import ReleasingRoute, request_session
from app.auth.sessions import Repository, current_user
from app.db.models import Business, User
from app.demo_flags import demo_now
from app.services.stats import Level, StatsRepository, StatsService

router = APIRouter(prefix="/api/stats", route_class=ReleasingRoute)
MAX_PERIOD_DAYS = 400


@dataclass(frozen=True)
class Period:
    start: date
    end: date


def get_period(date_from: Annotated[date, Query(alias="from")], date_to: Annotated[date, Query(alias="to")]) -> Period:
    if date_from > date_to:
        raise HTTPException(422, "Начало периода должно быть не позже окончания.")
    if (date_to - date_from).days >= MAX_PERIOD_DAYS:
        raise HTTPException(422, f"Период не может быть длиннее {MAX_PERIOD_DAYS} дней.")
    return Period(date_from, date_to)


def find_business(
    user: Annotated[User, Depends(current_user)],
    repository: Repository,
    business_id: Annotated[int | None, Query(gt=0)] = None,
) -> Business | None:
    """Бизнес запроса. У пользователя без бизнеса — None: уведомления и статусы приложение запрашивает и до мастера.
    Чужой или несуществующий business_id — 404."""
    try:
        return repository.get_business(user.id, business_id)
    except HTTPException as error:
        if error.status_code != 404 or business_id is not None:
            raise
        return None


OptionalBusiness = Annotated[Business | None, Depends(find_business)]


def get_business(business: OptionalBusiness) -> Business:
    if business is None:
        raise HTTPException(404, "Сначала заполните сведения о бизнесе.")
    return business


async def get_stats_service() -> AsyncIterator[StatsService]:
    with request_session() as db:
        yield StatsService(StatsRepository(db))


def business_today(business: Business | None) -> date:
    from app.config import settings

    zone = ZoneInfo(settings.tz_default)
    now = datetime.now(zone)
    return (demo_now(business, now) if business else now).astimezone(zone).date()


def get_today(business: OptionalBusiness) -> date:
    return business_today(business)


CurrentBusiness = Annotated[Business, Depends(get_business)]
Service = Annotated[StatsService, Depends(get_stats_service)]
RequestedPeriod = Annotated[Period, Depends(get_period)]
Today = Annotated[date, Depends(get_today)]


@router.get("/summary")
def summary(business: CurrentBusiness, service: Service, period: RequestedPeriod, today: Today) -> dict:
    return service.summary(business, period.start, period.end, today)


@router.get("/breakdown")
def breakdown(business: CurrentBusiness, service: Service, period: RequestedPeriod, today: Today, level: Level) -> dict:
    return service.breakdown(business, period.start, period.end, level, today)


@router.get("/daily")
def daily(business: CurrentBusiness, service: Service, period: RequestedPeriod, today: Today) -> dict:
    return service.daily(business, period.start, period.end, today)
