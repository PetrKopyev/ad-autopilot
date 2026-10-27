"""Освобождает места демо-стенда сразу, не дожидаясь суток: удаляет всех демо-пользователей тем же путём, что и
истечение при входе в демо (DemoUsers.expire), вместе с бизнесами и файлами.

docker compose -p autopilot-demo --env-file .env.demo -f deploy/docker-compose.yml exec api python -m app.demo_reset
"""

from datetime import UTC, datetime

from app.api.auth import MAX_DEMO_USERS, remove_business_media
from app.demo_flags import demo_enabled
from app.services.demo import DemoUsers
from app.worker.jobs import WorkerRepository


def reset(db, now: datetime) -> int:
    """Возвращает число свободных мест; занятые перемоткой или сборкой ролика пользователи остаются. Заодно убирает
    пульс воркера «из будущего» и устаревшие прогоны — как ежедневная уборка воркера."""
    users = DemoUsers(db)
    for business_id in users.expire(now):
        remove_business_media(business_id)
    WorkerRepository(db).prune_runs(now)
    return MAX_DEMO_USERS - users.count()


def main() -> None:
    if not demo_enabled():
        raise SystemExit("Команда работает только на демо-стенде: PLATFORM_MODE=fake и DEMO_LOGIN=1.")
    from app.db.session import SessionLocal

    with SessionLocal() as db:
        print(f"Свободно мест в демо: {reset(db, datetime.now(UTC))} из {MAX_DEMO_USERS}.")


if __name__ == "__main__":
    main()
