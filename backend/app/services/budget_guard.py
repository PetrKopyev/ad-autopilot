from __future__ import annotations

from contextlib import ExitStack
from math import ceil
from time import monotonic
from typing import TYPE_CHECKING

from app.db.models import BudgetLedger, Business, Campaign
from app.domain.budget import budget_guard as decide_guard
from app.domain.budget import plan_budget
from app.platforms.base import Operation
from app.services.changes import CLOSED_ACCOUNT, LOCK_WAIT_SECONDS, ChangeConflict, wait_for_lock
from app.worker.clock import today

if TYPE_CHECKING:
    from app.worker.jobs import Worker


async def read_spend(
    worker: Worker, business: Business, *, tolerate_errors: bool = False, campaigns: list[Campaign] | None = None
) -> tuple[dict[str, int], dict[str, int]]:
    """Расход по дате клика до сейчас, включая два неустоявшихся дня. campaigns — кампании бизнеса, если шаг их уже
    прочитал."""
    day = today(worker.clock)
    month = day.replace(day=1)
    spent, lifetime = {}, {}
    campaigns = worker.campaigns(business) if campaigns is None else campaigns
    platforms = {campaign.platform for campaign in campaigns}
    meta = worker.metadata(business)
    started_on = min(month, day.fromisoformat(meta.get("started_on", month.isoformat())))
    for platform in sorted(platforms):
        try:
            adapter = worker.resolve_adapter(business.id, platform)
            rows = await adapter.get_stats(started_on, day, "campaign")
            reported_today = await adapter.get_spend_today()
        except CLOSED_ACCOUNT:
            if not tolerate_errors:
                raise
            continue
        known = {campaign.external_id for campaign in campaigns if campaign.platform == platform}
        for row in rows:
            if row.campaign_id not in known:
                continue
            lifetime[row.campaign_id] = lifetime.get(row.campaign_id, 0) + row.cost_kop
        total = sum(row.cost_kop for row in rows if row.date >= month and row.campaign_id in known)
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
    actor: str = "system",
) -> None:
    with worker.repository.business_lock(business.id):
        if not worker.proposed(business):
            worker.remember(
                business,
                pending_stop={
                    "reason_code": reason_code,
                    "reason_params": reason_params,
                    "budget_stop": budget_stop,
                    "actor": actor,
                },
                **({"kill_switch": True} if not budget_stop else {}),
            )
        failed = False
        for campaign in worker.campaigns(business):
            try:
                states = await worker.resolve_adapter(business.id, campaign.platform).get_state(worker.refs(campaign))
                state = states[campaign.external_id]
                if state.status in {"paused_auto", "paused_owner", "archived"}:
                    continue
                if actor == "owner" and not worker.proposed(business):
                    stopped = worker.metadata(business).get("kill_switch_campaigns", [])
                    worker.remember(business, kill_switch_campaigns=sorted(set(stopped) | {campaign.id}))
                await worker.apply(
                    business,
                    campaign,
                    [
                        Operation(
                            "set_status",
                            "campaign",
                            campaign.external_id,
                            "status",
                            state.status,
                            "paused_owner" if actor == "owner" else "paused_auto",
                        )
                    ],
                    actor=actor,
                    kind="pause",
                    reason_code=reason_code,
                    reason_params=reason_params,
                    reason_text="Остановили рекламу на всех площадках.",
                    suffix="stop_all",
                )
            except (*CLOSED_ACCOUNT, ChangeConflict):
                # Кабинет, который требует входа заново или не расшифровывается, не мешает остановить остальные.
                failed = True
        if failed:
            raise ChangeConflict("Не все площадки подтвердили остановку. Повторим в следующем тике.")
        if not worker.proposed(business):
            business.status = "budget_stop" if budget_stop else "paused"
            worker.remember(business, pending_stop=None)


STOPPED = {"paused_auto", "paused_owner", "archived"}


async def stop_platform(
    business: Business,
    *,
    worker: Worker,
    platform: str,
    reason_text: str,
    deadline: float,
    wait: float = LOCK_WAIT_SECONDS,
) -> bool:
    """Перед отзывом доступа к кабинету: без токена ни автопилот, ни страж бюджета рекламу уже не остановят.

    Защитное действие: выполняется и в пробном режиме. Если бизнес занят шагом воркера, ждём до wait секунд, затем
    ChangeConflict. Остановка кампании не прерывается посреди (иначе изменение осталось бы pending без ответа): после
    deadline (monotonic) следующая кампания не начинается, TimeoutError; блокировку после deadline тоже не ждём.
    Возвращает, подтвердила ли площадка остановку всех кампаний; остановка записывается в журнал от имени владельца.
    У бизнеса один кабинет на площадку (platform_accounts: business_id, platform), поэтому кампании площадки
    бизнеса — это кампании отключаемого кабинета.
    """
    with ExitStack() as stack:
        if monotonic() >= deadline:
            raise TimeoutError
        try:
            await wait_for_lock(stack, worker.repository, business.id, min(wait, deadline - monotonic()))
        except ChangeConflict:
            if monotonic() < deadline:
                raise
            raise TimeoutError from None
        stopped = True
        for campaign in worker.campaigns(business):
            if campaign.platform != platform:
                continue
            if monotonic() >= deadline:
                raise TimeoutError
            try:
                states = await worker.resolve_adapter(business.id, platform).get_state(worker.refs(campaign))
                state = states[campaign.external_id]
                if state.status in STOPPED:
                    continue
                change = await worker.apply(
                    business,
                    campaign,
                    [Operation("set_status", "campaign", campaign.external_id, "status", state.status, "paused_owner")],
                    actor="owner",
                    kind="pause",
                    reason_code="ACCOUNT_REVOKE",
                    reason_text=reason_text,
                    suffix="revoke",
                    force=True,
                )
                stopped = stopped and change.status == "applied"
            except (*CLOSED_ACCOUNT, ChangeConflict):
                stopped = False
        return stopped


async def budget_guard(worker: Worker, business: Business, *, reconcile: bool = False) -> None:
    from app.config import get_domain_settings

    domain = get_domain_settings()
    pending_stop = worker.metadata(business).get("pending_stop")
    if pending_stop:
        await stop_all(business, worker=worker, **pending_stop)
    month = today(worker.clock).replace(day=1)
    # Кампании читаются раз на шаг: остановка выше меняет их статус, а не состав и не дневные лимиты.
    campaigns = worker.campaigns(business)
    spent, _ = await read_spend(worker, business, tolerate_errors=True, campaigns=campaigns)
    ledgers = {row.platform: row for row in worker.repository.rows(BudgetLedger, business.id, month=month)}
    reported = sum(max(spent.get(platform, 0), row.spent_kop) for platform, row in ledgers.items())
    reported += sum(value for platform, value in spent.items() if platform not in ledgers)
    daily = sum(campaign.daily_limit_kop for campaign in campaigns)
    # При внешнем повышении кэпа фактический темп может превышать сохранённый лимит.
    # Увеличиваем защитный запас по приросту ledger, без запроса состояния кампаний.
    observed_daily = 0
    for platform, ledger in ledgers.items():
        if ledger.spent_at is None:
            continue
        seconds = (worker.clock.now - ledger.spent_at).total_seconds()
        if seconds > 0:
            seconds = max(seconds, domain.guard_poll_minutes * 60)
            observed_daily += ceil(max(0, spent.get(platform, 0) - ledger.spent_kop) * 86400 / seconds)
    # Ledger — снимок, а не разрешённый расход: во время простоя работают кэпы.
    # Верхняя граница сверки — authorized месяца (PLAN 2.5), включая резерв.
    day = today(worker.clock)
    allowed = plan_budget(
        business.month_budget_kop,
        0,
        day,
        ("all",),
        started_on=day.fromisoformat(worker.metadata(business).get("started_on", month.isoformat())),
        settings=domain,
    ).authorized
    mismatch = reconcile and reported > allowed
    result = decide_guard(
        business.month_budget_kop,
        reported,
        max(daily, observed_daily),
        recorded_spent=0 if mismatch else None,
        settings=domain,
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
        worker.notice(
            business, result.reason_code, "Реклама остановлена. Проверьте расход и остаток бюджета.", proposal=True
        )
