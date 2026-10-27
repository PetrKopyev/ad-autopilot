from __future__ import annotations

from calendar import monthrange
from datetime import datetime, time, timedelta
from typing import TYPE_CHECKING

from app.db.models import BudgetLedger, Business
from app.domain.explain import MONTHS
from app.platforms.base import Operation
from app.services.budget_guard import stop_all
from app.services.optimize import rebalance_budget
from app.worker.clock import MOSCOW, today

if TYPE_CHECKING:
    from app.worker.jobs import Worker


async def month_rollover(worker: Worker, business: Business) -> None:
    day = today(worker.clock)
    month = day.replace(day=1)
    meta = worker.metadata(business)
    previous = meta.get("month")
    if previous is None:
        months = [row.month for row in worker.repository.rows(BudgetLedger, business.id)]
        previous = min(months).isoformat() if months else month.isoformat()
    if monthrange(day.year, day.month)[1] - day.day == 3 and business.auto_continue:
        next_month = (month + timedelta(days=31)).replace(day=1)
        # Текст с датой, а не «через три дня»: уведомление читают и позже. С началом месяца оно уже не нужно.
        worker.notice(
            business,
            "MONTH_CONTINUE_NOTICE",
            f"{next_month.day} {MONTHS[next_month.month - 1]} начнётся новый месяц, и реклама продолжится с тем же "
            "бюджетом. Если продолжать не нужно, выключите «Продолжать в следующем месяце» в настройках.",
            key=f"month_notice:{month.isoformat()}",
            expires_at=datetime.combine(next_month, time(), MOSCOW).isoformat(),
        )
    if previous >= month.isoformat():
        worker.remember(business, month=month.isoformat())
        return
    if not business.auto_continue or meta.get("kill_switch") or business.status == "paused":
        await stop_all(business, worker=worker, reason_code="MONTH_STOP")
        worker.remember(business, month=month.isoformat())
        return
    was_stopped = business.status == "budget_stop"
    if not worker.proposed(business):
        business.status = "running"
        worker.repository.save(business)
    # Новые кэпы выставляются до возобновления кампаний.
    await rebalance_budget(worker, business)
    for campaign in worker.campaigns(business):
        if campaign.owner_paused or not campaign.daily_limit_kop:
            continue
        states = await worker.resolve_adapter(business.id, campaign.platform).get_state(worker.refs(campaign))
        state = states[campaign.external_id]
        if was_stopped and campaign.status == "paused_auto" and state.status in {"paused_auto", "paused_owner"}:
            await worker.apply(
                business,
                campaign,
                [Operation("set_status", "campaign", campaign.external_id, "status", state.status, "active")],
                actor="system",
                kind="resume",
                reason_code="BUDGET_RESUME",
                reason_params={"budget": business.month_budget_kop},
                suffix="resume",
            )
    worker.remember(business, month=month.isoformat())
