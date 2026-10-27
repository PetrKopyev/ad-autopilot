from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import sessionmaker

from app.config import settings

engine = create_engine(settings.database_url, pool_pre_ping=True)
# Зависимости FastAPI, открывающие сессию, — async-генераторы: сессия закрывается на цикле событий сразу после ответа.
# Синхронную зависимость FastAPI закрывает в пуле потоков; когда все потоки заняты запросами, ждущими соединения,
# закрытие ждало поток, соединение не возвращалось в пул, и запросы падали по таймауту пула.
SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)

# Блокировка бизнеса держит своё соединение всё время шага, перемотка демо — минуты. Из общего пула такие соединения
# забирали бы место у запросов, а запрос, который уже держит соединение сессии и ждёт второе для блокировки, мог ждать
# сам себя. Поэтому у блокировок свой пул; ожидание соединения в нём короткое и кончается отказом «бизнес занят».
lock_engine = create_engine(
    settings.database_url,
    pool_pre_ping=True,
    pool_size=5,
    max_overflow=20,
    pool_timeout=5,
    isolation_level="AUTOCOMMIT",
)
LOCK_ENGINES: dict[Engine, Engine] = {engine: lock_engine}


def lock_bind(bind: Engine) -> Engine:
    """Откуда брать соединение для блокировки бизнеса; у движков вне приложения (тесты) отдельного пула нет."""
    return LOCK_ENGINES.get(bind, bind)
