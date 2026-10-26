from collections.abc import Iterator
from dataclasses import dataclass
from datetime import date, datetime
from typing import Annotated
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException, Query

from app.auth.sessions import Repository, current_user
from app.db.models import Business, User
from app.services.demo import demo_now
from app.services.stats import Level, StatsRepository, StatsService

router = APIRouter(prefix="/api/stats")


@dataclass(frozen=True)
class Period:
    start: date
    end: date


def get_period(date_from: Annotated[date, Query(alias="from")], date_to: Annotated[date, Query(alias="to")]) -> Period:
    if date_from > date_to:
        raise HTTPException(422, "Начало периода должно быть не позже окончания.")
    return Period(date_from, date_to)


def get_business(
    user: Annotated[User, Depends(current_user)],
    repository: Repository,
    business_id: Annotated[int | None, Query(gt=0)] = None,
) -> Business:
    return repository.get_business(user.id, business_id)


def get_stats_service() -> Iterator[StatsService]:
    from app.db.session import SessionLocal

    with SessionLocal() as db:
        yield StatsService(StatsRepository(db))


def get_today(business: Annotated[Business, Depends(get_business)]) -> date:
    from app.config import settings

    zone = ZoneInfo(settings.tz_default)
    return demo_now(business, datetime.now(zone)).astimezone(zone).date()


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
