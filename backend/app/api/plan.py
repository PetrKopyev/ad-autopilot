import asyncio
from collections.abc import Callable, Coroutine, Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Any

from fastapi import APIRouter, BackgroundTasks, Depends, File, Form, HTTPException, Request, Response, UploadFile
from fastapi.responses import FileResponse
from fastapi.routing import APIRoute
from pydantic import BaseModel, Field, StrictBool, StrictInt
from starlette.concurrency import run_in_threadpool
from starlette.types import Message

from app.api.business import human_errors
from app.api.limits import assets_limit, regenerate_limit, upload_limit
from app.api.stats import CurrentBusiness, Today
from app.auth.sessions import require_csrf
from app.content.images import MAX_BYTES
from app.services.launch import LaunchBlocked, LaunchFailed, LaunchRepository, LaunchService
from app.services.onboarding import AiLimits
from app.services.plan import VIDEO_EXECUTOR, PlanRepository, PlanService, build_videos
from app.services.registry import build_art_factory, build_llm

router = APIRouter(prefix="/api/plan")
MAX_UPLOAD = MAX_BYTES + 64 * 1024
TOO_LARGE = "Файл слишком большой: не больше 10 МБ."


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


def get_video_builder() -> Callable[[int], None]:
    """Сборка роликов идёт после ответа в своём сеансе БД: сеанс запроса к этому времени уже закрыт."""

    def build(business_id: int) -> None:
        from app.db.session import SessionLocal

        with SessionLocal() as db:
            build_videos(PlanRepository(db), media_root(), business_id)

    return build


Plans = Annotated[PlanService, Depends(get_plan_service)]
Launches = Annotated[LaunchService, Depends(get_launch_service)]
VideoBuilder = Annotated[Callable[[int], None], Depends(get_video_builder)]


async def rebuild_videos(build: Callable[[int], None], business_id: int) -> None:
    await asyncio.get_running_loop().run_in_executor(VIDEO_EXECUTOR, build, business_id)


class LimitedBody(APIRoute):
    """Тело больше MAX_UPLOAD отклоняется до разбора multipart: по Content-Length и при чтении потока."""

    def get_route_handler(self) -> Callable[[Request], Coroutine[Any, Any, Response]]:
        handler = super().get_route_handler()

        async def limited(request: Request) -> Response:
            length = request.headers.get("content-length")
            if length is not None and (not length.isdecimal() or int(length) > MAX_UPLOAD):
                raise HTTPException(413, TOO_LARGE)
            received = 0

            async def receive() -> Message:
                nonlocal received
                message = await request.receive()
                received += len(message.get("body", b""))
                if received > MAX_UPLOAD:
                    raise HTTPException(413, TOO_LARGE)
                return message

            return await handler(Request(request.scope, receive))

        return limited


@router.get("")
async def get_plan(business: CurrentBusiness, service: Plans, today: Today) -> dict:
    with human_errors():
        return await service.get_plan(business.id, datetime.now(UTC), today)


@router.post("/regenerate-images", dependencies=[Depends(regenerate_limit), Depends(require_csrf)])
async def regenerate(body: RegenerateIn, business: CurrentBusiness, service: Plans, today: Today) -> dict:
    with human_errors():
        return await service.regenerate_images(business.id, body.group_id, datetime.now(UTC), today)


async def upload_photo(
    group_id: Annotated[int, Form()],
    file: Annotated[UploadFile, File()],
    business: CurrentBusiness,
    service: Plans,
    today: Today,
    build: VideoBuilder,
    background: BackgroundTasks,
) -> dict:
    # Тип проверяет Pillow по содержимому (accept_upload); имя и Content-Type части не учитываются.
    data = await file.read(MAX_BYTES + 1)
    if len(data) > MAX_BYTES:
        raise HTTPException(413, TOO_LARGE)
    with human_errors():
        result = await run_in_threadpool(service.upload_photo, business.id, group_id, data, today)
    background.add_task(rebuild_videos, build, business.id)
    return result


router.add_api_route(
    "/upload-photo",
    upload_photo,
    methods=["POST"],
    dependencies=[Depends(upload_limit), Depends(require_csrf)],
    route_class_override=LimitedBody,
)


@router.put("/assets", dependencies=[Depends(assets_limit), Depends(require_csrf)])
def save_assets(
    body: AssetsIn, business: CurrentBusiness, service: Plans, build: VideoBuilder, background: BackgroundTasks
) -> dict:
    with human_errors():
        if service.select_assets(business.id, body.asset_ids):
            background.add_task(rebuild_videos, build, business.id)
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
