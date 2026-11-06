from collections.abc import AsyncIterator, Iterator
from contextlib import contextmanager
from datetime import UTC, date, datetime
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field, StrictBool, StrictInt
from starlette.concurrency import run_in_threadpool

from app.api.autopilot import Screens
from app.api.demo import router as demo_router
from app.api.limits import description_limit, site_check_limit
from app.api.routing import ReleasingRoute, request_session
from app.api.stats import CurrentBusiness, Today
from app.auth.sessions import current_session, current_user, require_csrf
from app.content.site_reader import read_site
from app.db.models import Business, Session, User
from app.demo_flags import demo_enabled
from app.platforms.base import PlatformError
from app.platforms.metrica import MetricaError
from app.platforms.regions import CITY_REGIONS
from app.services.changes import ChangeConflict, ServerBusy
from app.services.demo import DEMO_SITE_URL, prepare_business, read_demo_site
from app.services.onboarding import (
    MAX_BUDGET_KOP,
    AiLimits,
    Blocked,
    NotFound,
    OnboardingError,
    OnboardingRepository,
    OnboardingService,
)
from app.services.plan import budget_view
from app.services.registry import build_llm, llm_mode

router = APIRouter(prefix="/api", route_class=ReleasingRoute)
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
    except ServerBusy as error:
        raise HTTPException(503, str(error)) from error
    except (Blocked, ChangeConflict) as error:
        raise HTTPException(409, str(error)) from error
    except NotFound as error:
        raise HTTPException(404, str(error)) from error
    except (PlatformError, MetricaError) as error:
        raise HTTPException(502, str(error)) from error


async def get_onboarding_service(request: Request) -> AsyncIterator[OnboardingService]:
    from app.config import settings

    with request_session() as db:
        registry = request.app.state.platform_adapters
        yield OnboardingService(
            OnboardingRepository(db),
            build_llm(settings, request.app.state.http),
            registry.metrica,
            AiLimits.from_settings(settings),
            reader=read_demo_site if settings.platform_mode == "fake" else read_site,
            ai_mode=llm_mode(settings),
            # У частичных заглушек реестра в тестах fresh может отсутствовать: передача тогда пропускается.
            fresh=getattr(registry, "fresh", None),
        )


Service = Annotated[OnboardingService, Depends(get_onboarding_service)]
CurrentUser = Annotated[User, Depends(current_user)]


@router.post("/business", dependencies=[Depends(require_csrf)])
def create_business(body: BusinessIn, user: CurrentUser, service: Service) -> dict:
    with human_errors():
        # Мастер демо предзаполняет сайт салоном-примером; если поле осталось пустым (сброс черновика, новая
        # вкладка), демо-бизнес всё равно создаём с этим адресом — иначе шаг 1 встаёт на пустом поле.
        site_url = body.site_url.strip() or (DEMO_SITE_URL if demo_enabled() else body.site_url)
        result = service.create_business(user.id, site_url, body.city, body.month_budget_kop, body.avg_check_kop)
        if demo_enabled():
            prepare_business(service.repository, service.repository.business(result["id"]))
        return result


@router.get("/cities", dependencies=[Depends(current_user)])
def cities() -> dict:
    return {"cities": list(CITY_REGIONS)}


@router.get("/business/budget-hint", dependencies=[Depends(current_user)])
def budget_hint(
    today: Today, month_budget_kop: Annotated[int, Query(gt=0, le=MAX_BUDGET_KOP)], vk: bool = False
) -> dict:
    """Подсказка мастера к бюджету до сохранения: бизнеса может ещё не быть, кабинет VK называет сам мастер."""
    from app.config import get_domain_settings

    return budget_view(month_budget_kop, ["direct", "vk"] if vk else ["direct"], today, get_domain_settings())


@router.get("/business/site-check")
async def site_check(
    request: Request,
    session: Annotated[Session, Depends(current_session)],
    business: CurrentBusiness,
    service: Service,
    today: Today,
) -> dict:
    # Шаг мастера запрашивает проверку при каждом входе: лимит считает только настоящее чтение сайта.
    return await check_site(request, session, business, service, today, refresh=False)


@router.post("/business/site-check", dependencies=[Depends(require_csrf)])
async def refresh_site_check(
    request: Request,
    session: Annotated[Session, Depends(current_session)],
    business: CurrentBusiness,
    service: Service,
    today: Today,
) -> dict:
    # Кнопка повтора: сайт читается заново мимо кэша — это меняет описание и тратит лимит ИИ, поэтому POST с CSRF.
    return await check_site(request, session, business, service, today, refresh=True)


async def check_site(
    request: Request, session: Session, business: Business, service: OnboardingService, today: date, *, refresh: bool
) -> dict:
    with human_errors():
        return await service.site_check(
            business.id,
            datetime.now(UTC),
            today,
            on_miss=lambda: site_check_limit(request, session),
            refresh=refresh,
        )


@router.put("/business/description", dependencies=[Depends(description_limit), Depends(require_csrf)])
def save_description(body: DescriptionIn, business: CurrentBusiness, service: Service) -> dict:
    # Синхронный обработчик: запись ждёт блокировку бизнеса в пуле потоков, а не в цикле событий.
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
async def save_marking(request: Request, body: MarkingIn, business: CurrentBusiness, service: Service) -> dict:
    with human_errors():
        # Сохранение ждёт блокировку бизнеса в пуле потоков, а передача на площадки идёт сразу после него:
        # результат передачи владелец видит в ответе и в настройках.
        result = await run_in_threadpool(
            service.save_marking,
            business.id,
            body.subject_type,
            body.inn,
            body.name,
            body.phone,
            body.confirmed_in_platform,
        )
        transmission = await service.transmit_marking(business.id, datetime.now(UTC))
        return result | {"transmission": transmission}


class BudgetIn(BaseModel):
    month_budget_kop: StrictInt = Field(gt=0, le=MAX_BUDGET_KOP)


class SettingsIn(BaseModel):
    auto_continue: StrictBool
    avg_check_kop: StrictInt | None = Field(default=None, gt=0)
    # Цель VK кабинета (пиксель Top@Mail): id источника и имя цели. Не задано — не менять (В8),
    # 0 — сбросить цель (В6).
    vk_priced_goal_source_id: StrictInt | None = Field(default=None, ge=0)
    vk_priced_goal_name: str = Field(default="", max_length=200)


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
        return service.save_settings(
            business,
            body.auto_continue,
            body.avg_check_kop,
            body.vk_priced_goal_source_id,
            body.vk_priced_goal_name,
        )
