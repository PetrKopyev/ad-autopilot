import secrets
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from fastapi.responses import RedirectResponse

from app.auth import yandex
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
from app.db.models import Session, User

router = APIRouter(prefix="/api")


def _remove_media(file_path: str) -> None:
    from app.config import settings

    root = settings.media_dir.resolve()
    path = (root / file_path).resolve()
    if path.is_relative_to(root) and path.is_file():
        path.unlink()


def demo_enabled() -> bool:
    from app.config import settings

    return settings.platform_mode == "fake" and settings.demo_login


@router.get("/auth/config")
def auth_config(response: Response) -> dict:
    response.headers["Cache-Control"] = "no-store"
    return {"demo": demo_enabled()}


def require_demo() -> None:
    if not demo_enabled():
        raise HTTPException(404, "Не найдено.")


@router.post("/auth/demo", dependencies=[Depends(require_demo)])
def demo_login(request: Request, repository: Repository) -> Response:
    user = repository.save_user("demo-" + secrets.token_hex(16), "demo", None)
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
        tokens = await yandex.exchange_code(code, oauth_state)
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


@router.delete("/me", status_code=204, dependencies=[Depends(require_csrf)])
def delete_me(user: Annotated[User, Depends(current_user)], repository: Repository) -> Response:
    # Platform tokens are not revoked: the Yandex and VK clients have no revoke call; they are deleted with the rows.
    for path in repository.delete_user(user.id):
        _remove_media(path)
    response = Response(status_code=204, headers={"Cache-Control": "no-store"})
    clear_cookies(response)
    return response


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


@router.delete("/accounts/{id}", status_code=204, dependencies=[Depends(require_csrf)])
def disconnect_account(id: int, user: Annotated[User, Depends(current_user)], repository: Repository) -> Response:
    if not repository.disconnect_account(user.id, id):
        raise HTTPException(404, "Кабинет не найден.")
    return Response(status_code=204, headers={"Cache-Control": "no-store"})
