from __future__ import annotations

from typing import TYPE_CHECKING

from app.db.models import BudgetLedger, Business
from app.domain.budget import budget_guard as decide_guard
from app.platforms.base import Operation
from app.worker.clock import today

if TYPE_CHECKING:
    from app.worker.jobs import Worker


async def read_spend(worker: Worker, business: Business) -> tuple[dict[str, int], dict[str, int]]:
    """Расход по дате клика до сейчас, включая два неустоявшихся дня."""
    day = today(worker.clock)
    month = day.replace(day=1)
    spent, lifetime = {}, {}
    platforms = {campaign.platform for campaign in worker.campaigns(business)}
    meta = worker.metadata(business)
    started_on = min(month, day.fromisoformat(meta.get("started_on", month.isoformat())))
    for platform in sorted(platforms):
        adapter = worker.resolve_adapter(business.id, platform)
        rows = await adapter.get_stats(started_on, day, "campaign")
        known = {campaign.external_id for campaign in worker.campaigns(business) if campaign.platform == platform}
        for row in rows:
            if row.campaign_id not in known:
                continue
            lifetime[row.campaign_id] = lifetime.get(row.campaign_id, 0) + row.cost_kop
        total = sum(row.cost_kop for row in rows if row.date >= month and row.campaign_id in known)
        reported_today = await adapter.get_spend_today()
        # День площадки может не совпадать с московским: сравниваем с последней датой отчёта.
        latest = max((row.date for row in rows if row.campaign_id in known), default=None)
        stats_today = sum(row.cost_kop for row in rows if row.date == latest and row.campaign_id in known)
        # Быстрый endpoint иногда свежее отчёта. Излишек консервативно относится к этому бизнесу.
        spent[platform] = total + max(0, reported_today - stats_today)
    return spent, lifetime


async def stop_all(
    business: Business,
    *,
    worker: Worker,
    reason_code: str = "OWNER_STOP",
    reason_params: dict | None = None,
    budget_stop: bool = False,
) -> None:
    with worker.repository.business_lock(business.id):
        if not worker.proposed(business):
            business.status = "budget_stop" if budget_stop else "paused"
            worker.repository.save(business)
            if not budget_stop:
                worker.remember(business, kill_switch=True)
        for campaign in worker.campaigns(business):
            states = await worker.resolve_adapter(business.id, campaign.platform).get_state(worker.refs(campaign))
            state = states[campaign.external_id]
            if state.status in {"paused", "paused_auto", "paused_owner", "archived"}:
                continue
            await worker.apply(
                business,
                campaign,
                [Operation("set_status", "campaign", campaign.external_id, "status", state.status, "paused")],
                actor="system",
                kind="pause",
                reason_code=reason_code,
                reason_params=reason_params,
                reason_text="Вся реклама остановлена по запросу владельца.",
                suffix="stop_all",
            )


async def budget_guard(worker: Worker, business: Business, *, reconcile: bool = False) -> None:
    month = today(worker.clock).replace(day=1)
    spent, _ = await read_spend(worker, business)
    ledgers = {row.platform: row for row in worker.repository.rows(BudgetLedger, business.id) if row.month == month}
    recorded = sum(row.spent_kop for row in ledgers.values())
    reported = sum(spent.values())
    daily = sum(campaign.daily_limit_kop for campaign in worker.campaigns(business))
    result = decide_guard(
        business.month_budget_kop, reported, daily, recorded_spent=recorded if reconcile and ledgers else None
    )
    for platform, value in spent.items():
        ledger = ledgers.get(platform) or BudgetLedger(
            business_id=business.id,
            platform=platform,
            month=month,
            allocated_kop=0,
            period_cap_kop=0,
            spent_kop=0,
        )
        ledger.spent_kop = max(ledger.spent_kop, value)
        ledger.spent_at = worker.clock.now
        if result.stop:
            ledger.hard_stop_at = worker.clock.now
        worker.repository.save(ledger)
    if result.stop:
        await stop_all(
            business,
            worker=worker,
            reason_code=result.reason_code,
            budget_stop=True,
            reason_params={"spent": result.spent, "budget": business.month_budget_kop, "margin": result.stop_margin},
        )
        worker.notice(business, result.reason_code, "Реклама остановлена. Проверьте расход и остаток бюджета.")
