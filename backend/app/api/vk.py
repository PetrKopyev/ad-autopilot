from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, SecretStr

from app.auth import vk
from app.auth.sessions import Repository, current_session, current_user, require_csrf
from app.db.models import Session, User
from app.platforms.base import AuthExpired, PlatformError

router = APIRouter(prefix="/api")


def get_vk_accounts(repository: Repository) -> vk.VkAccountRepository:
    return vk.VkAccountRepository(repository.db)


Accounts = Annotated[vk.VkAccountRepository, Depends(get_vk_accounts)]


class VkKey(BaseModel):
    client_id: str
    client_secret: SecretStr
    business_id: int | None = None


def _redirect() -> RedirectResponse:
    return RedirectResponse(
        vk.redirect_uri().removesuffix("api/auth/vk/callback"),
        status_code=303,
        headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"},
    )


@router.get("/auth/vk/start")
def vk_start(
    repository: Repository,
    session: Annotated[Session, Depends(current_session)],
    business_id: Annotated[int | None, Query(gt=0)] = None,
) -> Response:
    business = repository.get_business(session.user_id, business_id)
    state = vk.create_state(session.id, session.user_id, business.id)
    try:
        url = vk.VkClient().auth_url(state.state)
    except PlatformError as error:
        raise HTTPException(503, str(error)) from None
    repository.save_state(state)
    return RedirectResponse(url, status_code=303, headers={"Cache-Control": "no-store"})


@router.get("/auth/vk/callback")
async def vk_callback(
    repository: Repository,
    accounts: Accounts,
    session: Annotated[Session, Depends(current_session)],
    state: Annotated[str, Query(min_length=1, max_length=1024)],
    code: Annotated[str | None, Query(min_length=1, max_length=2048)] = None,
    error: Annotated[str | None, Query(max_length=256)] = None,
) -> Response:
    oauth_state = repository.get_state(state)
    if oauth_state is None or oauth_state.user_id != session.user_id:
        raise HTTPException(400, "Не удалось проверить вход. Начните заново.")
    try:
        business_id = vk.verify_state(oauth_state, session.id, session.user_id)
    except PlatformError as failure:
        raise HTTPException(400, str(failure)) from None
    if code is None and error is None:
        raise HTTPException(400, "VK не передал код входа. Начните заново.")
    if repository.consume_state(state, code if error is None else None) is None:
        raise HTTPException(400, "Этот запрос входа уже использован. Начните заново.")
    if error is not None:
        raise HTTPException(400, "Вы не разрешили доступ. Попробуйте подключить VK заново.")
    repository.get_business(session.user_id, business_id)
    try:
        async with vk.VkClient() as client:
            tokens = await client.exchange_code(code)
    except AuthExpired:
        raise HTTPException(400, "VK не подтвердил вход. Попробуйте подключить кабинет заново.") from None
    except PlatformError as failure:
        raise HTTPException(400, str(failure)) from None
    accounts.save(business_id, accounts.lock_account(business_id), tokens, auth_mode="oauth")
    return _redirect()


@router.post("/accounts/vk/key", dependencies=[Depends(require_csrf)])
async def vk_key(
    body: VkKey,
    user: Annotated[User, Depends(current_user)],
    repository: Repository,
    accounts: Accounts,
    response: Response,
) -> dict:
    response.headers["Cache-Control"] = "no-store"
    business = repository.get_business(user.id, body.business_id)
    client_id, client_secret = body.client_id.strip(), body.client_secret.get_secret_value().strip()
    if not client_id or not client_secret:
        raise HTTPException(400, "Введите client_id и client_secret из «Доступ к API».")
    try:
        async with vk.VkClient(client_id=client_id, client_secret=client_secret, manual_key=True) as client:
            tokens = await client.client_credentials()
    except AuthExpired:
        raise HTTPException(400, "VK не принял ключ. Проверьте client_id и client_secret.") from None
    except PlatformError as failure:
        raise HTTPException(400, str(failure)) from None
    account = accounts.save(
        business.id,
        accounts.lock_account(business.id),
        tokens,
        auth_mode="manual_key",
        client_id=client_id,
        client_secret=client_secret,
    )
    return {
        "id": account.id,
        "business_id": business.id,
        "external_login": account.external_login,
        "status": account.status,
    }
