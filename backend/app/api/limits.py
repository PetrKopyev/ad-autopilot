from collections import deque
from html import escape
from ipaddress import ip_address, ip_network
from threading import Lock
from time import monotonic
from typing import Annotated

from fastapi import Depends, HTTPException, Request
from fastapi.responses import HTMLResponse

from app.auth.sessions import optional_session
from app.db.models import Session

MAX_KEYS = 10_000


def client_key(host: str) -> str:
    """Провайдер выдаёт клиенту IPv6 сетью /64: лимит по отдельному адресу обходился бы сменой адреса в своей сети."""
    try:
        address = ip_address(host)
    except ValueError:
        return host
    if address.version == 4:
        return str(address)
    # IPv4 в виде ::ffff:a.b.c.d — отдельный клиент: в /64 все такие адреса слились бы в один ключ.
    if address.ipv4_mapped:
        return str(address.ipv4_mapped)
    return str(ip_network(f"{address}/64", strict=False))


class RateLimit:
    """Скользящее окно в памяти процесса API (он один): отдельно по IP и по пользователю."""

    limits: list["RateLimit"] = []

    def __init__(self, message: str, *, per_ip: int, per_user: int | None = None, seconds: int = 60) -> None:
        self.message = message
        self.per_ip = per_ip
        self.per_user = per_user
        self.seconds = seconds
        self.hits: dict[str, deque[float]] = {}
        self.lock = Lock()
        RateLimit.limits.append(self)

    def __call__(self, request: Request, session: Annotated[Session | None, Depends(optional_session)]) -> None:
        keys = [(f"ip:{client_key(request.client.host if request.client else '')}", self.per_ip)]
        if session is not None and self.per_user is not None:
            keys.append((f"user:{session.user_id}", self.per_user))
        now = monotonic()
        with self.lock:
            for key, limit in keys:
                # Ключ переставляется в конец: в начале словаря остаются давно не приходившие, их и вытесняем.
                times = self.hits.pop(key, None) or deque()
                self.hits[key] = times
                while times and times[0] <= now - self.seconds:
                    times.popleft()
                if len(times) >= limit:
                    retry = max(1, int(times[0] + self.seconds - now) + 1)
                    raise HTTPException(429, self.message, headers={"Retry-After": str(retry)})
            for key, _ in keys:
                self.hits[key].append(now)
            while len(self.hits) > MAX_KEYS:
                del self.hits[next(iter(self.hits))]

    def page(self, request: Request, session: Session | None) -> HTMLResponse | None:
        """Для переходов браузера (вход через Яндекс и VK): вместо JSON — страница с текстом и ссылкой назад."""
        try:
            self(request, session)
        except HTTPException as error:
            body = PAGE.format(message=escape(error.detail))
            headers = error.headers | {"Cache-Control": "no-store"}
            return HTMLResponse(body, status_code=error.status_code, headers=headers)
        return None

    def reset(self) -> None:
        with self.lock:
            self.hits.clear()


PAGE = (
    '<!doctype html><html lang="ru"><head><meta charset="utf-8">'
    '<meta name="viewport" content="width=device-width, initial-scale=1"><title>Подождите минуту</title></head>'
    '<body><p>{message}</p><p><a href="/">Вернуться в Автопилот рекламы</a></p></body></html>'
)
LOGIN = "Слишком много попыток входа. Подождите минуту и попробуйте снова."
yandex_start_limit = RateLimit(LOGIN, per_ip=20, per_user=10)
vk_start_limit = RateLimit(LOGIN, per_ip=20, per_user=10)
demo_login_limit = RateLimit("Слишком много входов в демо. Подождите минуту.", per_ip=10)
site_check_limit = RateLimit(
    "Сайт проверяется слишком часто. Подождите пару минут и попробуйте снова.", per_ip=20, per_user=5, seconds=300
)
regenerate_limit = RateLimit(
    "Картинки обновляются слишком часто. Подождите минуту и попробуйте снова.", per_ip=20, per_user=6
)
assets_limit = RateLimit("Картинки меняются слишком часто. Подождите минуту.", per_ip=120, per_user=60)
upload_limit = RateLimit("Фото загружаются слишком часто. Подождите минуту и попробуйте снова.", per_ip=30, per_user=15)
vk_key_limit = RateLimit("Слишком много попыток подключить ключ VK. Подождите минуту.", per_ip=20, per_user=5)
