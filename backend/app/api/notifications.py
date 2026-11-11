from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Response

from app.api.routing import ReleasingRoute, request_session
from app.api.stats import CurrentBusiness, OptionalBusiness
from app.auth.sessions import require_csrf
from app.db.models import WorkerRun
from app.demo_flags import demo_now
from app.worker.jobs import WorkerRepository

router = APIRouter(prefix="/api/notifications", route_class=ReleasingRoute)
LIMIT = 20
# Только то, что владельцу стоит знать: служебные отметки шагов (мало данных для проверки и т. п.) не показываем.
NOTICES = {
    "BUDGET_STOP": ("Реклама остановлена: бюджет месяца почти израсходован", "danger"),
    "SPEND_MISMATCH": ("Площадка показывает больший расход, чем мы ждали", "danger"),
    "CAMPAIGN_NOT_FOUND": ("Не получилось остановить кампанию", "warning"),
    "MODERATION_REJECTED": ("Площадка не пропустила объявление", "warning"),
    "MODERATION_ALL_REJECTED": ("Кампания не показывается: все объявления отклонены", "danger"),
    "MODERATION_LONG": ("Площадка долго проверяет объявления", "warning"),
    "CAMPAIGN_OFF_BY_PLATFORM": ("Площадка не включила кампанию", "warning"),
    "ANOMALY": ("Перестали приходить обращения", "warning"),
    "NOT_SHOWING": ("Объявление не показывается", "warning"),
    "CAMPAIGN_EXTEND_FAILED": ("Не получилось продлить кампанию в Директе", "warning"),
    "PLATFORM_MINIMUM_PAUSE": ("Реклама остановлена: бюджета не хватает на минимум площадки", "warning"),
    "DIRECT_REAUTH": ("Переподключите Яндекс Директ", "warning"),
    "BALANCE_LOW": ("На счёте площадки заканчиваются деньги", "danger"),
    "ACCOUNT_EMPTY": ("Средства на счёте площадки закончились", "danger"),
    "DEMO_STATE_LOST": ("Демо нужно начать заново", "warning"),
    "LAUNCH_INTERRUPTED": ("Запуск рекламы не завершился", "danger"),
    "EXPERIMENT_GENERATION_FAILED": ("Проверку двух вариантов не запустили", "warning"),
    "EXPERIMENT_AI_LIMIT": ("Проверку двух вариантов отложили", "info"),
    "EXPERIMENT_WIN": ("Проверка двух вариантов закончилась", "info"),
    "EXPERIMENT_NO_DIFF": ("Проверка двух вариантов закончилась", "info"),
    "EXPERIMENT_CLICKBAIT": ("Проверка двух вариантов закончилась", "info"),
    "EXPERIMENT_EXPIRED": ("Проверка двух вариантов закончилась", "info"),
    "BUDGET_OPPORTUNITY": ("Можно получить больше обращений", "info"),
    "MIN_ACTIVE_GROUPS": ("Объявление требует внимания", "info"),
    "MONTH_CONTINUE_NOTICE": ("Скоро новый месяц", "info"),
}


async def get_notifications_repository() -> AsyncIterator[WorkerRepository]:
    with request_session() as db:
        yield WorkerRepository(db)


Repository = Annotated[WorkerRepository, Depends(get_notifications_repository)]


@router.get("")
def notifications(business: OptionalBusiness, repository: Repository, response: Response) -> list[dict]:
    response.headers["Cache-Control"] = "no-store"
    if business is None:
        return []
    return [
        {
            "id": row.id,
            "kind": row.log[0]["reason_code"],
            "created_at": row.started_at.isoformat(),
            "title": NOTICES[row.log[0]["reason_code"]][0],
            "text": row.log[0]["text"],
            "level": NOTICES[row.log[0]["reason_code"]][1],
        }
        for row in repository.unread_notifications(business.id, NOTICES, LIMIT, demo_now(business, datetime.now(UTC)))
    ]


@router.post("/{notification_id}/read", status_code=204, dependencies=[Depends(require_csrf)])
def read(notification_id: int, business: CurrentBusiness, repository: Repository) -> Response:
    target = repository.find(WorkerRun, business_id=business.id, kind="notification", status="ok", id=notification_id)
    if target is None or target.log[0].get("reason_code") not in NOTICES:
        raise HTTPException(404, "Уведомление не найдено.")
    # Вместе с ним прочитаны и его повторы той же темы: иначе после «Понятно» всплыл бы более старый такой же.
    repository.mark_read(business.id, repository.same_theme(target), datetime.now(UTC))
    return Response(status_code=204, headers={"Cache-Control": "no-store"})
