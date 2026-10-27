from collections.abc import AsyncIterator
from datetime import date
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, StrictInt

from app.api.routing import ReleasingRoute, request_session
from app.api.stats import CurrentBusiness, Today
from app.auth.sessions import require_csrf
from app.services.clients import ClientsRepository, ClientsService
from app.services.onboarding import NotFound

router = APIRouter(prefix="/api/clients", route_class=ReleasingRoute)


class Answer(BaseModel):
    clients_count: StrictInt | Literal["не знаю"] | None


async def get_clients_service() -> AsyncIterator[ClientsService]:
    with request_session() as db:
        yield ClientsService(ClientsRepository(db))


Service = Annotated[ClientsService, Depends(get_clients_service)]


@router.get("/questions")
def questions(business: CurrentBusiness, service: Service, today: Today) -> dict:
    return service.questions(business, today)


@router.post("/questions/{id}/answer", dependencies=[Depends(require_csrf)])
def answer(id: date, body: Answer, business: CurrentBusiness, service: Service, today: Today) -> dict:
    try:
        return service.answer(business, id, body.clients_count, today)
    except NotFound as error:
        raise HTTPException(404, str(error)) from error
    except ValueError as error:
        raise HTTPException(422, str(error)) from error
