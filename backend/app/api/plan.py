from collections.abc import Iterator
from datetime import UTC, datetime
from email import policy
from email.parser import BytesParser
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field, StrictBool, StrictInt
from starlette.concurrency import run_in_threadpool

from app.api.business import human_errors
from app.api.stats import CurrentBusiness, Today
from app.auth.sessions import require_csrf
from app.content.images import MAX_BYTES
from app.services.launch import LaunchBlocked, LaunchFailed, LaunchRepository, LaunchService
from app.services.onboarding import AiLimits
from app.services.plan import PlanRepository, PlanService
from app.services.registry import build_art_factory, build_llm

router = APIRouter(prefix="/api/plan")
MAX_UPLOAD = MAX_BYTES + 64 * 1024


class RegenerateIn(BaseModel):
    group_id: StrictInt


class AssetsIn(BaseModel):
    asset_ids: list[StrictInt] = Field(max_length=100)


class LaunchIn(BaseModel):
    plan_id: StrictInt
    medical_acknowledged: StrictBool = False


def media_root() -> Path:
    from app.config import settings

    return settings.media_dir


def get_plan_service(request: Request) -> Iterator[PlanService]:
    from app.config import get_domain_settings, settings
    from app.db.session import SessionLocal

    http = request.app.state.http
    with SessionLocal() as db:
        yield PlanService(
            PlanRepository(db),
            build_llm(settings, http),
            build_art_factory(settings, http),
            media_root(),
            AiLimits.from_settings(settings),
            get_domain_settings(settings),
        )


def get_launch_service(request: Request) -> Iterator[LaunchService]:
    from app.config import get_domain_settings, settings
    from app.db.session import SessionLocal

    with SessionLocal() as db:
        yield LaunchService(
            LaunchRepository(db),
            request.app.state.platform_adapters.resolve,
            media_root(),
            domain=get_domain_settings(settings),
        )


Plans = Annotated[PlanService, Depends(get_plan_service)]
Launches = Annotated[LaunchService, Depends(get_launch_service)]


async def read_upload(request: Request) -> tuple[int, bytes]:
    """Разбор multipart без python-multipart: поля group_id и file, размер ограничен до чтения."""
    length = request.headers.get("content-length", "")
    content_type = request.headers.get("content-type", "")
    if not length.isdecimal() or int(length) > MAX_UPLOAD:
        raise HTTPException(413, "Файл слишком большой: не больше 10 МБ.")
    if not content_type.startswith("multipart/form-data"):
        raise HTTPException(422, "Загрузите фото как файл.")
    body = bytearray()
    async for chunk in request.stream():
        body += chunk
        if len(body) > MAX_UPLOAD:
            raise HTTPException(413, "Файл слишком большой: не больше 10 МБ.")
    message = BytesParser(policy=policy.HTTP).parsebytes(
        b"Content-Type: " + content_type.encode("latin-1") + b"\r\n\r\n" + bytes(body)
    )
    parts = {
        part.get_param("name", header="content-disposition"): part.get_payload(decode=True)
        for part in message.iter_parts()
    }
    try:
        return int(parts["group_id"]), parts["file"]
    except (KeyError, TypeError, ValueError) as error:
        raise HTTPException(422, "Выберите группу и файл.") from error


@router.get("")
async def get_plan(business: CurrentBusiness, service: Plans, today: Today) -> dict:
    with human_errors():
        return await service.get_plan(business.id, datetime.now(UTC), today)


@router.post("/regenerate-images", dependencies=[Depends(require_csrf)])
async def regenerate(body: RegenerateIn, business: CurrentBusiness, service: Plans, today: Today) -> dict:
    with human_errors():
        return await service.regenerate_images(business.id, body.group_id, datetime.now(UTC), today)


@router.post("/upload-photo", dependencies=[Depends(require_csrf)])
async def upload_photo(request: Request, business: CurrentBusiness, service: Plans, today: Today) -> dict:
    group_id, data = await read_upload(request)
    with human_errors():
        return await run_in_threadpool(service.upload_photo, business.id, group_id, data, today)


@router.put("/assets", dependencies=[Depends(require_csrf)])
def save_assets(body: AssetsIn, business: CurrentBusiness, service: Plans) -> dict:
    with human_errors():
        service.select_assets(business.id, body.asset_ids)
    return {"selected": body.asset_ids}


@router.post("/launch", dependencies=[Depends(require_csrf)])
async def launch(body: LaunchIn, business: CurrentBusiness, service: Launches, today: Today) -> dict:
    try:
        with human_errors():
            return await service.launch(business.id, body.plan_id, body.medical_acknowledged, datetime.now(UTC), today)
    except LaunchBlocked as error:
        raise HTTPException(409, " ".join(error.reasons)) from error
    except LaunchFailed as error:
        raise HTTPException(502, str(error)) from error


@router.get("/assets/{asset_id}/file")
def asset_file(asset_id: int, business: CurrentBusiness, service: Plans) -> FileResponse:
    asset = next((item for item in service.repository.assets(business.id) if item.id == asset_id), None)
    root = service.media_root.resolve()
    path = (root / asset.file_path).resolve() if asset else None
    if path is None or not path.is_relative_to(root) or not path.is_file():
        raise HTTPException(404, "Картинка не найдена.")
    return FileResponse(
        path,
        media_type="video/mp4" if asset.kind == "video" else "image/jpeg",
        headers={"Cache-Control": "private, max-age=3600"},
    )
