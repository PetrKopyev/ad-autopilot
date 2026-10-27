from fastapi import APIRouter

from app.api.autopilot import Screens
from app.api.routing import ReleasingRoute
from app.api.stats import CurrentBusiness

router = APIRouter(prefix="/api/experiments", route_class=ReleasingRoute)


@router.get("")
def experiments(business: CurrentBusiness, service: Screens) -> dict:
    return service.experiments(business)
