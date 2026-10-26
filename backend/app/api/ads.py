from datetime import date, timedelta
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query

from app.api.autopilot import Screens
from app.api.business import human_errors
from app.api.stats import CurrentBusiness, Today
from app.auth.sessions import require_csrf

router = APIRouter(prefix="/api/ads")


@router.get("")
def ads(
    business: CurrentBusiness,
    service: Screens,
    today: Today,
    date_from: Annotated[date | None, Query(alias="from")] = None,
    date_to: Annotated[date | None, Query(alias="to")] = None,
) -> dict:
    end = date_to or today
    start = date_from or end - timedelta(days=13)
    if start > end:
        raise HTTPException(422, "Начало периода должно быть не позже окончания.")
    return service.ads(business, start, end)


@router.post("/{ad_id}/pause", dependencies=[Depends(require_csrf)])
async def pause(ad_id: int, business: CurrentBusiness, service: Screens) -> dict:
    with human_errors():
        return await service.ad_status(business, ad_id, False)


@router.post("/{ad_id}/resume", dependencies=[Depends(require_csrf)])
async def resume(ad_id: int, business: CurrentBusiness, service: Screens) -> dict:
    with human_errors():
        return await service.ad_status(business, ad_id, True)
