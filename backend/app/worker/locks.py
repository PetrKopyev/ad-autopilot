from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar

from sqlalchemy import Connection, text
from sqlalchemy.exc import TimeoutError as PoolTimeout

from app.demo_flags import ADVANCING, advance_jobs
from app.services.changes import ChangeConflict, ServerBusy, waiting

_held: ContextVar[frozenset[tuple[int, int]]] = ContextVar("worker_business_locks", default=frozenset())


def busy(business_id: int) -> ChangeConflict:
    return ChangeConflict(ADVANCING if advance_jobs.holds_lock(business_id) else "Этот бизнес уже обрабатывается.")


@contextmanager
def business_lock(bind, business_id: int, wait: float = 0) -> Iterator[None]:
    """wait — сколько секунд ждать, пока блокировку отпустят; ждёт поток, поэтому только вне цикла событий."""
    with waiting(lambda: acquire(bind, business_id), wait):
        yield


@contextmanager
def acquire(bind, business_id: int) -> Iterator[None]:
    from app.db.session import lock_bind

    engine = bind.engine if isinstance(bind, Connection) else bind
    key = (id(engine), business_id)
    if key in _held.get():
        yield
        return
    # Отдельное соединение удерживает сессионную блокировку между commit pending/applied.
    try:
        lock = lock_bind(engine).connect()
    except PoolTimeout as error:
        raise ServerBusy from error
    with lock as connection:
        if not connection.scalar(text("SELECT pg_try_advisory_lock(:id)"), {"id": business_id}):
            raise busy(business_id)
        token = _held.set(_held.get() | {key})
        try:
            yield
        finally:
            _held.reset(token)
            connection.execute(text("SELECT pg_advisory_unlock(:id)"), {"id": business_id})
