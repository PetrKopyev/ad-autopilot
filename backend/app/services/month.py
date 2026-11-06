from __future__ import annotations

from calendar import monthrange
from datetime import datetime, time, timedelta
from typing import TYPE_CHECKING

from app.db.models import BudgetLedger, Business
from app.domain.explain import MONTHS
from app.platforms.base import Operation, PlatformError
from app.services.budget_guard import campaign_state, stop_all
from app.services.changes import REVIEW
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
        # Запись называет причину остановки: владелец иначе не поймёт, почему реклама выключилась 1-го числа.
        # Пауза владельца важнее настройки: реклама стояла бы и с включённым продолжением.
        reason_text = (
            "Остановили рекламу на всех площадках. Реклама на паузе: вы остановили её вручную."
            if meta.get("kill_switch") or business.status == "paused"
            else "Остановили рекламу на всех площадках. Продолжение в следующем месяце выключено в настройках."
        )
        await stop_all(business, worker=worker, reason_code="MONTH_STOP", reason_text=reason_text)
        worker.remember(business, month=month.isoformat())
        return
    # Остановку по бюджету помним до конца прохода: прерванный проход (площадка не подтвердила включение) повторяется
    # уже при running, и без пометки кампании, остановленные по бюджету, не возобновились бы никогда.
    was_stopped = business.status == "budget_stop" or bool(meta.get("month_resume"))
    if was_stopped:
        worker.remember(business, month_resume=month.isoformat())
    business.status = "running"
    worker.repository.save(business)
    # Кампании, закончившиеся по дате окончания (Direct State=ENDED): продлеваем дату и возобновляем до
    # пересчёта лимитов. Иначе rebalance их не увидит (локальный статус ещё active, а площадка прислала
    # паузу), обнулит лимит, и месячный переход пропустил бы кампанию как недоступную.
    held_before = set(meta.get("below_minimum", []))
    for campaign in worker.current_campaigns(business):
        adapter = worker.resolve_adapter(business.id, campaign.platform)
        state = await campaign_state(worker, business, campaign)
        if state is None or not state.ended:
            continue
        if hasattr(adapter, "extend_end_date"):
            try:
                await adapter.extend_end_date(campaign.external_id)
            except PlatformError:
                worker.notice(
                    business,
                    "CAMPAIGN_EXTEND_FAILED",
                    "Не удалось продлить кампанию в Директе. Повторим при следующем месячном переходе.",
                    target_id=campaign.external_id,
                )
                continue
        if campaign.owner_paused or campaign.id in held_before:
            # владелец остановил кампанию сам, или она на паузе по минимуму площадки.
            # Дату продлеваем (иначе она останется ENDED), а включение оставляем владельцу и пересчёту.
            continue
        await worker.apply(
            business,
            campaign,
            [Operation("set_status", "campaign", campaign.external_id, "status", state.status, "active")],
            actor="system",
            kind="resume",
            reason_code="MONTH_RESUME",
            reason_text="Кампания закончилась по дате окончания: продлили дату и включили снова.",
            suffix="resume",
        )
    # Новые кэпы выставляются до возобновления кампаний.
    await rebalance_budget(worker, business)
    held = set(worker.metadata(business).get("below_minimum", []))
    for campaign in worker.current_campaigns(business):
        if (
            not was_stopped
            or campaign.status != "paused_auto"
            or campaign.owner_paused
            or not campaign.daily_limit_kop
            or campaign.id in held
        ):
            continue
        state = await campaign_state(worker, business, campaign)
        # Кампанию, остановленную, пока площадка её проверяла, включаем так же: включение снова дождётся одобрения.
        if state is not None and state.status in {"paused_auto", "paused_owner", *REVIEW}:
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
    worker.remember(business, month=month.isoformat(), month_resume=None)
