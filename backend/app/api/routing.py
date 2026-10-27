from collections.abc import Callable, Coroutine, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from functools import wraps
from inspect import iscoroutinefunction
from typing import Any

from fastapi import Request, Response
from fastapi.routing import APIRoute
from sqlalchemy.orm import Session

_sessions: ContextVar[list[Session]] = ContextVar("request_sessions")


@contextmanager
def request_session() -> Iterator[Session]:
    """Сессия БД для зависимости запроса: её закроет ReleasingRoute сразу после обработчика."""
    from app.db.session import SessionLocal

    with SessionLocal() as db:
        _sessions.get().append(db)
        yield db


def close_sessions() -> None:
    for db in _sessions.get():
        db.close()


def released(endpoint: Callable) -> Callable:
    if iscoroutinefunction(endpoint):

        @wraps(endpoint)
        async def run_async(*args, **kwargs):
            try:
                return await endpoint(*args, **kwargs)
            finally:
                close_sessions()

        return run_async

    @wraps(endpoint)
    def run(*args, **kwargs):
        try:
            return endpoint(*args, **kwargs)
        finally:
            close_sessions()

    return run


class ReleasingRoute(APIRoute):
    """Сессии запроса закрываются сразу после обработчика, в его же потоке, а не при выходе из зависимостей.

    Выход из зависимостей наступает после проверки ответа, а для синхронного обработчика она идёт в пуле потоков.
    Когда все потоки заняты запросами, которые ждут соединение с базой, выход не наступал, соединение не возвращалось
    в пул, и запросы падали по таймауту пула (так терялись картинки плана у двух пользователей сразу)."""

    def __init__(self, path: str, endpoint: Callable, **kwargs: Any) -> None:
        super().__init__(path, released(endpoint), **kwargs)

    def get_route_handler(self) -> Callable[[Request], Coroutine[Any, Any, Response]]:
        handler = super().get_route_handler()

        async def handle(request: Request) -> Response:
            token = _sessions.set([])
            try:
                return await handler(request)
            finally:
                _sessions.reset(token)

        return handle
