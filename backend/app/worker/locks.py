from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar

from sqlalchemy import Connection, text

from app.services.changes import ChangeConflict

_held: ContextVar[frozenset[tuple[int, int]]] = ContextVar("worker_business_locks", default=frozenset())


@contextmanager
def business_lock(bind, business_id: int) -> Iterator[None]:
    engine = bind.engine if isinstance(bind, Connection) else bind
    key = (id(engine), business_id)
    if key in _held.get():
        yield
        return
    # Отдельное соединение удерживает сессионную блокировку между commit pending/applied.
    with engine.connect() as connection:
        acquired = connection.scalar(text("SELECT pg_try_advisory_lock(:id)"), {"id": business_id})
        if not acquired:
            raise ChangeConflict("Этот бизнес уже обрабатывается.")
        token = _held.set(_held.get() | {key})
        try:
            yield
        finally:
            _held.reset(token)
            connection.execute(text("SELECT pg_advisory_unlock(:id)"), {"id": business_id})
