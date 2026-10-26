import hashlib
import secrets
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Annotated

from fastapi import Depends, HTTPException, Request, Response
from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session as DBSession

from app.db.models import Asset, Business, OAuthState, PlatformAccount, Session, User
from app.db.session import SessionLocal

SESSION_COOKIE = "__Host-session"
CSRF_COOKIE = "__Host-csrf"
OAUTH_COOKIE = "__Host-oauth"
SESSION_TTL = timedelta(days=30)
STATE_TTL = timedelta(minutes=10)


def hash_session_id(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


class AuthRepository:
    def __init__(self, db: DBSession) -> None:
        self.db = db

    def get_session(self, token: str) -> Session | None:
        return self.db.scalar(
            select(Session).where(Session.id == hash_session_id(token), Session.expires_at > datetime.now(UTC))
        )

    def get_user(self, user_id: int) -> User | None:
        return self.db.get(User, user_id)

    def save_session(self, session: Session, previous_token: str | None = None) -> None:
        if previous_token:
            self.db.execute(delete(Session).where(Session.id == hash_session_id(previous_token)))
        self.db.add(session)
        self.db.commit()

    def delete_session(self, session_id: str) -> None:
        self.db.execute(delete(Session).where(Session.id == session_id))
        self.db.commit()

    def save_state(self, state: OAuthState) -> None:
        self.db.add(state)
        self.db.commit()

    def get_state(self, state: str) -> OAuthState | None:
        return self.db.get(OAuthState, state)

    def consume_state(self, state: str, code: str | None) -> OAuthState | None:
        now = datetime.now(UTC)
        consumed = self.db.scalar(
            delete(OAuthState).where(OAuthState.state == state, OAuthState.expires_at > now).returning(OAuthState)
        )
        if consumed is None:
            self.db.rollback()
            return None
        if code is not None:
            # A tombstone also rejects the same code submitted with a different valid state.
            fingerprint = "used-code:" + hashlib.sha256(code.encode()).hexdigest()
            self.db.execute(delete(OAuthState).where(OAuthState.state == fingerprint, OAuthState.expires_at <= now))
            self.db.add(OAuthState(state=fingerprint, purpose=consumed.purpose, expires_at=now + STATE_TTL))
        try:
            self.db.commit()
        except IntegrityError:
            self.db.rollback()
            return None
        return consumed

    def save_user(self, uid: str, login: str, email: str | None) -> User:
        statement = insert(User).values(yandex_uid=uid, login=login, email=email, consent_at=datetime.now(UTC))
        statement = statement.on_conflict_do_update(
            index_elements=[User.yandex_uid], set_={"login": login, "email": email}
        ).returning(User)
        user = self.db.scalar(statement, execution_options={"populate_existing": True})
        self.db.commit()
        return user

    def get_business(self, user_id: int, business_id: int | None) -> Business:
        statement = select(Business).where(Business.user_id == user_id)
        if business_id is not None:
            statement = statement.where(Business.id == business_id)
        businesses = list(self.db.scalars(statement.limit(2)))
        if not businesses:
            raise HTTPException(404, "Сначала заполните сведения о бизнесе.")
        if len(businesses) != 1:
            raise HTTPException(400, "Выберите бизнес для подключения кабинета.")
        return businesses[0]

    def save_account(self, business_id: int, login: str, tokens: dict) -> PlatformAccount:
        business = self.db.scalar(select(Business).where(Business.id == business_id).with_for_update())
        if business is None:
            raise HTTPException(404, "Бизнес не найден.")
        account = self.db.scalar(
            select(PlatformAccount).where(
                PlatformAccount.business_id == business_id, PlatformAccount.platform == "direct"
            )
        )
        if account is None:
            account = PlatformAccount(business_id=business_id, platform="direct", auth_mode="oauth")
            self.db.add(account)
        account.auth_mode = "oauth"
        account.external_login = login
        account.access_token_enc = tokens["access_token"]
        account.refresh_token_enc = tokens["refresh_token"]
        account.expires_at = tokens["expires_at"]
        account.scopes = tokens["scopes"]
        valid = {"direct:api", "metrika:read"} <= set(account.scopes) and (
            account.expires_at is None or account.expires_at > datetime.now(UTC)
        )
        account.status = "ok" if valid else "needs_reauth"
        account.last_ok_at = datetime.now(UTC) if account.status == "ok" else None
        self.db.commit()
        return account

    def list_accounts(self, user_id: int) -> list[PlatformAccount]:
        accounts = list(self.db.scalars(select(PlatformAccount).join(Business).where(Business.user_id == user_id)))
        for account in accounts:
            if account.status == "ok" and account.expires_at is not None and account.expires_at <= datetime.now(UTC):
                account.status = "needs_reauth"
        self.db.commit()
        return accounts

    def delete_user(self, user_id: int) -> list[str]:
        paths = list(self.db.scalars(select(Asset.file_path).join(Business).where(Business.user_id == user_id)))
        self.db.execute(delete(User).where(User.id == user_id))
        self.db.commit()
        return paths

    def disconnect_account(self, user_id: int, account_id: int) -> bool:
        account = self.db.scalar(
            select(PlatformAccount)
            .join(Business)
            .where(PlatformAccount.id == account_id, Business.user_id == user_id)
            .with_for_update(of=PlatformAccount)
        )
        if account is None:
            return False
        account.status = "revoked"
        account.access_token_enc = ""
        account.refresh_token_enc = None
        account.expires_at = None
        account.scopes = []
        account.last_ok_at = None
        self.db.commit()
        return True


def get_repository() -> Iterator[AuthRepository]:
    with SessionLocal() as db:
        yield AuthRepository(db)


Repository = Annotated[AuthRepository, Depends(get_repository)]


def optional_session(request: Request, repository: Repository) -> Session | None:
    session_id = request.cookies.get(SESSION_COOKIE)
    return repository.get_session(session_id) if session_id else None


def current_session(session: Annotated[Session | None, Depends(optional_session)]) -> Session:
    if session is None:
        raise HTTPException(401, "Войдите через Яндекс ID.")
    return session


def current_user(session: Annotated[Session, Depends(current_session)], repository: Repository) -> User:
    user = repository.get_user(session.user_id)
    if user is None:
        raise HTTPException(401, "Войдите через Яндекс ID.")
    return user


def require_csrf(request: Request, session: Annotated[Session, Depends(current_session)]) -> None:
    header = request.headers.get("X-CSRF-Token", "").encode()
    cookie = request.cookies.get(CSRF_COOKIE, "").encode()
    if (
        not header
        or not secrets.compare_digest(header, cookie)
        or not secrets.compare_digest(header, session.csrf_secret.encode())
    ):
        raise HTTPException(403, "Не удалось проверить запрос. Обновите страницу и попробуйте снова.")


def create_session(user_id: int) -> Session:
    token = secrets.token_urlsafe(32)
    session = Session(
        id=hash_session_id(token),
        user_id=user_id,
        expires_at=datetime.now(UTC) + SESSION_TTL,
        csrf_secret=secrets.token_urlsafe(32),
    )
    session.token = token  # only the hash is stored; the raw value goes to the cookie
    return session


def set_session_cookies(response: Response, session: Session) -> None:
    options = {"secure": True, "samesite": "lax", "path": "/", "max_age": int(SESSION_TTL.total_seconds())}
    response.set_cookie(SESSION_COOKIE, session.token, httponly=True, **options)
    response.set_cookie(CSRF_COOKIE, session.csrf_secret, httponly=False, **options)


def clear_cookies(response: Response) -> None:
    for name in (SESSION_COOKIE, CSRF_COOKIE, OAUTH_COOKIE):
        response.delete_cookie(name, path="/", secure=True, httponly=name != CSRF_COOKIE, samesite="lax")
