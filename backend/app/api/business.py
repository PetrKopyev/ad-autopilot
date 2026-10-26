from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field, StrictBool, StrictInt

from app.api.autopilot import Screens
from app.api.demo import router as demo_router
from app.api.stats import CurrentBusiness, Today
from app.auth.sessions import current_user, require_csrf
from app.content.site_reader import read_site
from app.db.models import User
from app.platforms.base import PlatformError
from app.platforms.metrica import MetricaError
from app.services.changes import ChangeConflict
from app.services.demo import prepare_business, read_demo_site
from app.services.onboarding import (
    MAX_BUDGET_KOP,
    AiLimits,
    Blocked,
    OnboardingError,
    OnboardingRepository,
    OnboardingService,
)
from app.services.registry import build_llm

router = APIRouter(prefix="/api")
router.include_router(demo_router)


class BusinessIn(BaseModel):
    site_url: str = Field(max_length=500)
    city: str = Field(max_length=100)
    month_budget_kop: StrictInt
    avg_check_kop: StrictInt | None = None


class DescriptionField(BaseModel):
    value: str = ""


class DescriptionIn(BaseModel):
    description: dict[str, DescriptionField]
    approved: bool


class MetricaIn(BaseModel):
    counter_id: StrictInt = Field(gt=0)
    lead_goal_ids: list[StrictInt] = Field(max_length=20)


class MarkingIn(BaseModel):
    subject_type: Literal["ip", "ooo", "selfemployed", "person"]
    inn: str = Field(max_length=12)
    name: str = Field(max_length=300)
    phone: str = Field(max_length=40)
    confirmed_in_platform: dict[str, bool] = Field(default_factory=dict)


@contextmanager
def human_errors() -> Iterator[None]:
    try:
        yield
    except OnboardingError as error:
        raise HTTPException(422, str(error)) from error
    except (Blocked, ChangeConflict) as error:
        raise HTTPException(409, str(error)) from error
    except LookupError as error:
        raise HTTPException(404, str(error)) from error
    except (PlatformError, MetricaError) as error:
        raise HTTPException(502, str(error)) from error


def get_onboarding_service(request: Request) -> Iterator[OnboardingService]:
    from app.config import settings
    from app.db.session import SessionLocal

    with SessionLocal() as db:
        yield OnboardingService(
            OnboardingRepository(db),
            build_llm(settings, request.app.state.http),
            request.app.state.platform_adapters.metrica,
            AiLimits.from_settings(settings),
            reader=read_demo_site if settings.platform_mode == "fake" else read_site,
        )


Service = Annotated[OnboardingService, Depends(get_onboarding_service)]
CurrentUser = Annotated[User, Depends(current_user)]


@router.post("/business", dependencies=[Depends(require_csrf)])
def create_business(body: BusinessIn, user: CurrentUser, service: Service) -> dict:
    from app.api.auth import demo_enabled

    with human_errors():
        result = service.create_business(user.id, body.site_url, body.city, body.month_budget_kop, body.avg_check_kop)
        if demo_enabled():
            prepare_business(service.repository, service.repository.business(result["id"]))
        return result


@router.get("/business/site-check")
async def site_check(business: CurrentBusiness, service: Service, today: Today) -> dict:
    with human_errors():
        return await service.site_check(business.id, datetime.now(UTC), today)


@router.put("/business/description", dependencies=[Depends(require_csrf)])
def save_description(body: DescriptionIn, business: CurrentBusiness, service: Service) -> dict:
    fields = {key: item.model_dump() for key, item in body.description.items()}
    with human_errors():
        return service.save_description(business.id, fields, body.approved, datetime.now(UTC))


@router.get("/metrica/counters")
async def counters(business: CurrentBusiness, service: Service) -> dict:
    with human_errors():
        return await service.counters(business.id)


@router.get("/metrica/goals")
async def goals(business: CurrentBusiness, service: Service, counter: Annotated[int, Query(gt=0)]) -> dict:
    with human_errors():
        return await service.goals(business.id, counter)


@router.put("/business/metrica", dependencies=[Depends(require_csrf)])
async def save_metrica(body: MetricaIn, business: CurrentBusiness, service: Service) -> dict:
    with human_errors():
        return await service.save_metrica(business.id, body.counter_id, body.lead_goal_ids)


@router.put("/business/marking", dependencies=[Depends(require_csrf)])
def save_marking(body: MarkingIn, business: CurrentBusiness, service: Service) -> dict:
    with human_errors():
        return service.save_marking(
            business.id, body.subject_type, body.inn, body.name, body.phone, body.confirmed_in_platform
        )


class BudgetIn(BaseModel):
    month_budget_kop: StrictInt = Field(gt=0, le=MAX_BUDGET_KOP)


class SettingsIn(BaseModel):
    auto_continue: StrictBool
    avg_check_kop: StrictInt | None = Field(default=None, gt=0)


@router.get("/business")
def get_business(business: CurrentBusiness, service: Screens) -> dict:
    return service.business(business)


@router.put("/business/budget", dependencies=[Depends(require_csrf)])
async def save_budget(body: BudgetIn, business: CurrentBusiness, service: Screens) -> dict:
    with human_errors():
        return await service.save_budget(business, body.month_budget_kop)


@router.put("/business/settings", dependencies=[Depends(require_csrf)])
def save_settings(body: SettingsIn, business: CurrentBusiness, service: Screens) -> dict:
    with human_errors():
        return service.save_settings(business, body.auto_continue, body.avg_check_kop)
