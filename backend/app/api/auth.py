import secrets
import shutil
from collections.abc import Awaitable, Callable, Iterator
from datetime import UTC, datetime, timedelta
from typing import Annotated
from uuid import uuid4

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, Request, Response
from fastapi.responses import RedirectResponse
from starlette.concurrency import run_in_threadpool

from app.api.limits import demo_login_limit, yandex_start_limit
from app.auth import yandex
from app.auth.revoke import revoke_tokens
from app.auth.sessions import (
    OAUTH_COOKIE,
    SESSION_COOKIE,
    STATE_TTL,
    Repository,
    clear_cookies,
    create_session,
    current_session,
    current_user,
    optional_session,
    require_csrf,
    set_session_cookies,
)
from app.db.models import Business, PlatformAccount, Session, User
from app.demo_flags import advance_jobs, demo_enabled, demo_now
from app.platforms.fake import VirtualClock
from app.services.budget_guard import stop_platform
from app.services.demo import DEMO_UID_PREFIX, DemoUsers
from app.worker.clock import SystemClock

router = APIRouter(prefix="/api")

type AdStopper = Callable[[int, int | None, str], Awaitable[bool]]


def remove_business_media(business_id: int) -> None:
    """Каталог бизнеса целиком: в нём и прежние ролики, на которые уже не ссылается ни одна строка assets."""
    from app.config import settings

    root = settings.media_dir.resolve()
    path = (root / str(business_id)).resolve()
    if path != root and path.is_relative_to(root) and path.is_dir():
        shutil.rmtree(path)


def get_ad_stopper(request: Request) -> AdStopper:
    async def stop(user_id: int, account_id: int | None, reason_text: str) -> bool:
        from sqlalchemy import select

        from app.db.session import SessionLocal
        from app.worker.jobs import Worker, WorkerRepository

        with SessionLocal() as db:
            repository = WorkerRepository(db)
            statement = (
                select(PlatformAccount)
                .join(Business)
                .where(Business.user_id == user_id, PlatformAccount.status == "ok")
                .order_by(PlatformAccount.id)
            )
            if account_id is not None:
                statement = statement.where(PlatformAccount.id == account_id)
            stopped = True
            for account in db.scalars(statement).all():
                business = repository.business(account.business_id)
                worker = Worker(
                    repository,
                    request.app.state.platform_adapters.resolve,
                    dry_run=False,
                    clock=VirtualClock(demo_now(business, SystemClock().now)),
                )
                worker.step_key = f"owner:{uuid4()}"
                done = await stop_platform(
                    business, worker=worker, platform=account.platform, reason_text=reason_text
                )
                stopped = stopped and done
            return stopped

    return stop


Stopper = Annotated[AdStopper, Depends(get_ad_stopper)]


@router.get("/auth/config")
def auth_config(response: Response) -> dict:
    response.headers["Cache-Control"] = "no-store"
    return {"demo": demo_enabled()}


def require_demo() -> None:
    if not demo_enabled():
        raise HTTPException(404, "Не найдено.")


MAX_DEMO_USERS = 50
DEMO_USER_TTL = timedelta(hours=24)


def require_same_origin(request: Request) -> None:
    # Свой заголовок нельзя отправить с чужого сайта без CORS: форма или ссылка не войдут в демо за пользователя.
    if (
        request.headers.get("X-Requested-With") != "XMLHttpRequest"
        or request.headers.get("Sec-Fetch-Site", "same-origin") != "same-origin"
    ):
        raise HTTPException(403, "Не удалось проверить запрос. Обновите страницу и попробуйте снова.")


def get_demo_users() -> Iterator[DemoUsers]:
    from app.db.session import SessionLocal

    with SessionLocal() as db:
        yield DemoUsers(db)


@router.post(
    "/auth/demo", dependencies=[Depends(require_demo), Depends(require_same_origin), Depends(demo_login_limit)]
)
def demo_login(
    request: Request, repository: Repository, demo_users: Annotated[DemoUsers, Depends(get_demo_users)]
) -> Response:
    for business_id in demo_users.expire(datetime.now(UTC) - DEMO_USER_TTL):
        advance_jobs.forget(business_id)
        for platform in ("direct", "vk"):
            request.app.state.platform_adapters.fakes.pop((business_id, platform), None)
        remove_business_media(business_id)
    if demo_users.count() >= MAX_DEMO_USERS:
        raise HTTPException(503, "Демо сейчас занято. Попробуйте через несколько часов.")
    user = repository.save_user(DEMO_UID_PREFIX + secrets.token_hex(16), "demo", None)
    session = create_session(user.id)
    repository.save_session(session, request.cookies.get(SESSION_COOKIE))
    response = Response(status_code=204, headers={"Cache-Control": "no-store"})
    set_session_cookies(response, session)
    return response


def _redirect() -> RedirectResponse:
    return RedirectResponse(
        yandex.settings.app_base_url.rstrip("/") + "/",
        status_code=303,
        headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"},
    )


@router.get("/auth/yandex/start")
def yandex_start(
    request: Request,
    repository: Repository,
    session: Annotated[Session | None, Depends(optional_session)],
    purpose: yandex.Purpose = "login",
    business_id: Annotated[int | None, Query(gt=0)] = None,
) -> Response:
    if limited := yandex_start_limit.page(request, session):
        return limited
    if purpose == "direct" and session is None:
        raise HTTPException(401, "Сначала войдите через Яндекс ID.")
    business = repository.get_business(session.user_id, business_id) if purpose == "direct" else None
    binding = session.id if session else request.cookies.get(OAUTH_COOKIE) or secrets.token_urlsafe(32)
    state = yandex.create_state(
        purpose, binding, session.user_id if session else None, business.id if business else None
    )
    try:
        url = yandex.authorization_url(state)
    except yandex.YandexError as error:
        raise HTTPException(503, str(error)) from None
    repository.save_state(state)
    response = RedirectResponse(url, status_code=303, headers={"Cache-Control": "no-store"})
    if session is None:
        response.set_cookie(
            OAUTH_COOKIE,
            binding,
            max_age=int(STATE_TTL.total_seconds()),
            secure=True,
            httponly=True,
            samesite="lax",
            path="/",
        )
    return response


@router.get("/auth/yandex/callback")
async def yandex_callback(
    request: Request,
    repository: Repository,
    session: Annotated[Session | None, Depends(optional_session)],
    state: Annotated[str, Query(min_length=1, max_length=1024)],
    code: Annotated[str | None, Query(min_length=1, max_length=2048)] = None,
    error: Annotated[str | None, Query(max_length=256)] = None,
) -> Response:
    oauth_state = repository.get_state(state)
    if oauth_state is None or oauth_state.user_id != (session.user_id if session else None):
        raise HTTPException(400, "Не удалось проверить вход. Начните заново.")
    binding = session.id if session else request.cookies.get(OAUTH_COOKIE, "")
    try:
        business_id = yandex.verify_state(oauth_state, binding)
    except yandex.YandexError as failure:
        raise HTTPException(400, str(failure)) from None
    if code is None and error is None:
        raise HTTPException(400, "Яндекс не передал код входа. Начните заново.")
    oauth_state = repository.consume_state(state, code if error is None else None)
    if oauth_state is None:
        raise HTTPException(400, "Этот запрос входа уже использован. Начните заново.")
    if error is not None:
        raise HTTPException(400, "Вы не разрешили доступ. Попробуйте войти заново.")
    if oauth_state.purpose == "direct":
        repository.get_business(session.user_id, business_id)
    try:
        tokens = await yandex.exchange_code(code, oauth_state, business_id)
        profile = await yandex.get_profile(tokens["access_token"])
    except yandex.YandexError as failure:
        raise HTTPException(400, str(failure)) from None
    response = _redirect()
    if oauth_state.purpose == "login":
        user = repository.save_user(profile["id"], profile["login"], profile.get("default_email"))
        new_session = create_session(user.id)
        repository.save_session(new_session, request.cookies.get(SESSION_COOKIE))
        set_session_cookies(response, new_session)
        response.delete_cookie(OAUTH_COOKIE, path="/", secure=True, httponly=True, samesite="lax")
    else:
        repository.save_account(business_id, profile["login"], tokens)
    return response


@router.post("/auth/logout", status_code=204, dependencies=[Depends(require_csrf)])
def logout(session: Annotated[Session, Depends(current_session)], repository: Repository) -> Response:
    repository.delete_session(session.id)
    response = Response(status_code=204, headers={"Cache-Control": "no-store"})
    clear_cookies(response)
    return response


@router.delete("/me", dependencies=[Depends(require_csrf)])
async def delete_me(
    request: Request,
    user: Annotated[User, Depends(current_user)],
    repository: Repository,
    stop_ads: Stopper,
    background: BackgroundTasks,
    response: Response,
) -> dict:
    # Остановка до отзыва: потом управлять кампаниями будет нечем. Неудача не мешает удалению, но о ней сообщаем.
    stopped = await stop_ads(user.id, None, "Остановили рекламу перед удалением ваших данных.")
    revocations = await run_in_threadpool(repository.revocations, user.id)
    for business_id in await run_in_threadpool(repository.delete_user, user.id):
        remove_business_media(business_id)
    # Отзыв идёт после ответа: площадка может отвечать по 10 секунд, а данные у нас уже удалены.
    background.add_task(revoke_tokens, request.app.state.http, revocations)
    response.headers["Cache-Control"] = "no-store"
    clear_cookies(response)
    return {
        "stopped": stopped,
        "message": "Данные удалены. Рекламу перед удалением остановили."
        if stopped
        else "Данные удалены, но не все площадки подтвердили остановку рекламы. Проверьте в кабинетах "
        "Яндекс Директа и VK, что кампании «Автопилот» остановлены: управлять ими мы больше не можем.",
    }


@router.get("/me")
def me(user: Annotated[User, Depends(current_user)], repository: Repository, response: Response) -> dict:
    response.headers["Cache-Control"] = "no-store"
    return {
        "id": user.id,
        "login": user.login,
        "email": user.email,
        "accounts": [
            {
                "id": account.id,
                "business_id": account.business_id,
                "platform": account.platform,
                "external_login": account.external_login,
                "status": account.status,
                "scopes": account.scopes,
            }
            for account in repository.list_accounts(user.id)
        ],
    }


@router.delete("/accounts/{id}", dependencies=[Depends(require_csrf)])
async def disconnect_account(
    request: Request,
    id: int,
    user: Annotated[User, Depends(current_user)],
    repository: Repository,
    stop_ads: Stopper,
    background: BackgroundTasks,
    response: Response,
) -> dict:
    stopped = await stop_ads(user.id, id, "Остановили рекламу на площадке перед отключением кабинета.")
    revocations = await run_in_threadpool(repository.revocations, user.id, id)
    if not await run_in_threadpool(repository.disconnect_account, user.id, id):
        raise HTTPException(404, "Кабинет не найден.")
    background.add_task(revoke_tokens, request.app.state.http, revocations)
    response.headers["Cache-Control"] = "no-store"
    return {
        "stopped": stopped,
        "message": "Кабинет отключён. Рекламу на этой площадке перед отключением остановили."
        if stopped
        else "Кабинет отключён, но площадка не подтвердила остановку рекламы. Проверьте в её кабинете, что "
        "кампании «Автопилот» остановлены: управлять ими мы больше не можем.",
    }
