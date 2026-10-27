from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Response

from app.api.stats import CurrentBusiness
from app.auth.sessions import require_csrf
from app.db.models import WorkerRun
from app.worker.jobs import WorkerRepository

router = APIRouter(prefix="/api/notifications")
LIMIT = 20
# Только то, что владельцу стоит знать: служебные отметки шагов (мало данных для проверки и т. п.) не показываем.
NOTICES = {
    "BUDGET_STOP": ("Реклама остановлена: бюджет месяца почти израсходован", "danger"),
    "SPEND_MISMATCH": ("Площадка показывает больший расход, чем мы ждали", "danger"),
    "MODERATION_REJECTED": ("Площадка не пропустила объявление", "warning"),
    "ANOMALY": ("Перестали приходить обращения", "warning"),
    "NOT_SHOWING": ("Объявление не показывается", "warning"),
    "CAMPAIGN_EXTEND_FAILED": ("Не получилось продлить кампанию в Директе", "warning"),
    "DEMO_STATE_LOST": ("Демо нужно начать заново", "warning"),
    "EXPERIMENT_GENERATION_FAILED": ("Проверку двух вариантов не запустили", "warning"),
    "EXPERIMENT_AI_LIMIT": ("Проверку двух вариантов отложили", "info"),
    "EXPERIMENT_WIN": ("Проверка двух вариантов закончилась", "info"),
    "EXPERIMENT_NO_DIFF": ("Проверка двух вариантов закончилась", "info"),
    "EXPERIMENT_CLICKBAIT": ("Проверка двух вариантов закончилась", "info"),
    "BUDGET_OPPORTUNITY": ("Можно получить больше обращений", "info"),
    "MIN_ACTIVE_GROUPS": ("Объявление требует внимания", "info"),
    "MONTH_CONTINUE_NOTICE": ("Скоро новый месяц", "info"),
}


def get_notifications_repository() -> Iterator[WorkerRepository]:
    from app.db.session import SessionLocal

    with SessionLocal() as db:
        yield WorkerRepository(db)


Repository = Annotated[WorkerRepository, Depends(get_notifications_repository)]


def unread(repository: WorkerRepository, business_id: int) -> list[WorkerRun]:
    """Повтор того же сообщения в следующих шагах показывается один раз — самым новым."""
    seen, result = set(), []
    for row in repository.unread_notifications(business_id, NOTICES, LIMIT * 10):
        key = (row.log[0]["reason_code"], row.log[0]["text"])
        if key not in seen:
            seen.add(key)
            result.append(row)
    return result


@router.get("")
def notifications(business: CurrentBusiness, repository: Repository, response: Response) -> list[dict]:
    response.headers["Cache-Control"] = "no-store"
    return [
        {
            "id": row.id,
            "kind": row.log[0]["reason_code"],
            "created_at": row.started_at.isoformat(),
            "title": NOTICES[row.log[0]["reason_code"]][0],
            "text": row.log[0]["text"],
            "level": NOTICES[row.log[0]["reason_code"]][1],
        }
        for row in unread(repository, business.id)[:LIMIT]
    ]


@router.post("/{notification_id}/read", status_code=204, dependencies=[Depends(require_csrf)])
def read(notification_id: int, business: CurrentBusiness, repository: Repository) -> Response:
    rows = repository.unread_notifications(business.id, NOTICES, LIMIT * 10)
    target = next((row for row in rows if row.id == notification_id), None)
    if target is None:
        if not repository.rows(WorkerRun, business.id, kind="notification", id=notification_id):
            raise HTTPException(404, "Уведомление не найдено.")
        return Response(status_code=204, headers={"Cache-Control": "no-store"})
    # Вместе с ним прочитаны и его повторы: иначе после «Понятно» всплыл бы более старый такой же.
    key = (target.log[0]["reason_code"], target.log[0]["text"])
    repository.mark_read(
        business.id,
        [row.id for row in rows if (row.log[0]["reason_code"], row.log[0]["text"]) == key],
        datetime.now(UTC),
    )
    return Response(status_code=204, headers={"Cache-Control": "no-store"})
