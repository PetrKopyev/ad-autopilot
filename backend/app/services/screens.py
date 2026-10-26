from datetime import date, timedelta
from uuid import uuid4

from app.db.models import (
    Ad,
    AdGroup,
    BudgetLedger,
    Business,
    Campaign,
    ChangeLog,
    Experiment,
    MarkingProfile,
    PlatformAccount,
    StatsDaily,
    WorkerRun,
)
from app.domain.explain import explain_experiment, rubles
from app.domain.types import ExperimentResult, Metrics, Verdict
from app.platforms.base import Operation, PlatformError
from app.services.budget_guard import read_spend, stop_all
from app.services.changes import ChangeConflict
from app.services.optimize import rebalance_budget
from app.services.stats import aggregate, aggregate_budget
from app.worker.clock import today
from app.worker.jobs import Worker


class ScreensService:
    def __init__(self, worker: Worker) -> None:
        self.worker = worker
        self.repository = worker.repository
        # Каждое действие владельца — отдельная попытка, а не слот планировщика.
        worker.step_key = f"owner:{uuid4()}"

    def budget(self, business: Business) -> dict:
        day = today(self.worker.clock)
        budget = aggregate_budget(
            business.month_budget_kop,
            self.repository.rows(StatsDaily, business.id),
            self.repository.rows(BudgetLedger, business.id),
            day,
        )
        stats_spend = {}
        for row in self.repository.rows(StatsDaily, business.id):
            if day.replace(day=1) <= row.date <= day:
                stats_spend[row.platform] = stats_spend.get(row.platform, 0) + row.cost_kop
        for platform in budget["platforms"]:
            platform["spent_kop"] = max(platform["spent_kop"], stats_spend.get(platform["platform"], 0))
        budget["spent_kop"] = sum(platform["spent_kop"] for platform in budget["platforms"])
        budget["remaining_kop"] = max(0, business.month_budget_kop - budget["spent_kop"])
        return budget

    def status(self, business: Business) -> dict:
        runs = [
            row
            for row in self.repository.rows(WorkerRun)
            if row.business_id in (None, business.id) and row.kind in {"heartbeat", "budget_guard"}
        ]
        last_check = max((row.finished_at for row in runs if row.status == "ok" and row.finished_at), default=None)
        budget = self.budget(business)
        status = business.status if business.status in {"running", "paused", "budget_stop"} else "draft"
        if status == "running" and not budget["remaining_kop"]:
            status = "budget_stop"
        titles = {
            "running": "Реклама работает",
            "paused": "Реклама на паузе",
            "budget_stop": "Бюджет месяца израсходован — снова 1 числа",
            "draft": "Реклама ещё не запущена",
        }
        attention = [
            {
                "id": "budget",
                "title": titles[status],
                "destination": "settings",
                "description": (
                    f"Бюджет месяца — {rubles(budget['budget_kop'])} ₽. "
                    f"Потрачено — {rubles(budget['spent_kop'])} ₽. "
                    f"До конца месяца дней: {budget['days_remaining']}."
                ),
            }
        ]
        latest = max(
            (row for row in runs if row.kind == "budget_guard" and row.business_id == business.id),
            key=lambda row: row.started_at,
            default=None,
        )
        accounts = self.repository.rows(PlatformAccount, business.id)
        if (latest and latest.status == "failed") or any(row.status != "ok" for row in accounts):
            attention.append(
                {
                    "id": "platform",
                    "title": "Ошибка площадки",
                    "destination": "settings",
                    "description": "Не удалось проверить рекламу. Проверьте подключение кабинета; повторим проверку.",
                }
            )
        return {
            "status": status,
            "last_check_at": last_check,
            "auto_continue": business.auto_continue,
            "needs_attention": attention,
        }

    async def pause(self, business: Business) -> dict:
        business = self.repository.business(business.id)
        await stop_all(business, worker=self.worker, actor="owner")
        return self.status(business)

    async def check_remaining(self, business: Business) -> None:
        spent, _ = await read_spend(self.worker, business)
        budget = self.budget(business)
        if (
            business.status == "budget_stop"
            or max(sum(spent.values()), budget["spent_kop"]) >= business.month_budget_kop
        ):
            raise ChangeConflict(
                "Бюджет месяца израсходован. Рекламу можно возобновить с 1 числа или увеличить бюджет."
            )

    async def resume(self, business: Business) -> dict:
        business = self.repository.business(business.id)
        with self.repository.business_lock(business.id):
            await self.check_remaining(business)
            meta = self.worker.metadata(business)
            if not meta.get("kill_switch"):
                return self.status(business)
            if meta.get("pending_stop"):
                raise ChangeConflict("Площадки ещё подтверждают остановку. Повторите попытку после проверки.")
            await rebalance_budget(self.worker, business, force=True)
            stopped = set(meta.get("kill_switch_campaigns", []))
            for campaign in self.worker.campaigns(business):
                if campaign.id not in stopped or not campaign.daily_limit_kop:
                    continue
                history = sorted(self.repository.rows(ChangeLog, business.id), key=lambda row: row.id, reverse=True)
                last = next(
                    (
                        row
                        for row in history
                        if row.status == "applied"
                        and row.platform == campaign.platform
                        and any(
                            op["entity_type"] == "campaign"
                            and op["entity_id"] == campaign.external_id
                            and op["field"] == "status"
                            for op in row.operations
                        )
                    ),
                    None,
                )
                if last is None or last.reason_code != "OWNER_STOP" or last.actor != "owner":
                    continue
                state = (
                    await self.worker.resolve_adapter(business.id, campaign.platform).get_state(
                        self.worker.refs(campaign)
                    )
                )[campaign.external_id]
                if campaign.status != "paused_owner" or state.status not in {"paused_owner", "paused_auto"}:
                    continue
                await self.worker.apply(
                    business,
                    campaign,
                    [Operation("set_status", "campaign", campaign.external_id, "status", state.status, "active")],
                    actor="owner",
                    kind="resume",
                    reason_code="OWNER_RESUME",
                    reason_text="Вы возобновили рекламу, которую остановили общей кнопкой.",
                    suffix="resume",
                )
            business.status = "running"
            self.worker.remember(business, kill_switch=False, kill_switch_campaigns=[])
        return self.status(business)

    def ads(self, business: Business, start: date, end: date) -> dict:
        campaigns = {row.id: row for row in self.repository.rows(Campaign, business.id)}
        groups = {row.id: row for row in self.repository.rows(AdGroup, business.id)}
        stats = [row for row in self.repository.rows(StatsDaily, business.id) if start <= row.date <= end]
        history = sorted(self.repository.rows(ChangeLog, business.id), key=lambda row: row.id, reverse=True)
        items = []
        for ad in self.repository.rows(Ad, business.id):
            group = groups[ad.ad_group_id]
            campaign = campaigns[group.campaign_id]
            reason = None
            if ad.status in {"paused_owner", "paused_auto"}:
                change = next(
                    (
                        row
                        for row in history
                        if row.status == "applied"
                        and row.platform == campaign.platform
                        and any(
                            op["entity_type"] == "ad" and op["entity_id"] == ad.external_id and op["field"] == "status"
                            for op in row.operations
                        )
                    ),
                    None,
                )
                reason = (
                    change.reason_text
                    if change
                    else (
                        "Вы остановили объявление."
                        if ad.status == "paused_owner"
                        else "Автопилот остановил объявление."
                    )
                )
            elif ad.status == "rejected":
                reason = ad.moderation_reason or "Площадка отклонила объявление. Проверьте его содержание."
            items.append(
                {
                    "id": ad.id,
                    "platform": campaign.platform,
                    "campaign_id": campaign.id,
                    "campaign_name": campaign.name,
                    "group_id": group.id,
                    "group_name": group.theme,
                    "title": ad.title,
                    "text": ad.text,
                    "image_url": f"/api/plan/assets/{ad.asset_id}/file" if ad.asset_id else None,
                    "status": ad.status,
                    "reason": reason,
                    "metrics": aggregate(
                        [row for row in stats if row.ad_id == ad.id], business.lead_goal_ids, today(self.worker.clock)
                    ),
                }
            )
        return {
            "items": sorted(items, key=lambda row: (row["platform"], row["campaign_id"], row["group_id"], row["id"]))
        }

    async def ad_status(self, business: Business, ad_id: int, active: bool) -> dict:
        business = self.repository.business(business.id)
        with self.repository.business_lock(business.id):
            ad = next((row for row in self.repository.rows(Ad, business.id) if row.id == ad_id), None)
            if ad is None:
                raise LookupError("Объявление не найдено.")
            if active:
                await self.check_remaining(business)
            group = next(row for row in self.repository.rows(AdGroup, business.id) if row.id == ad.ad_group_id)
            campaign = next(row for row in self.worker.campaigns(business) if row.id == group.campaign_id)
            status = "active" if active else "paused_owner"
            if ad.status != status:
                if not ad.external_id:
                    raise ChangeConflict("Объявление ещё не отправлено на площадку.")
                await self.worker.apply(
                    business,
                    campaign,
                    [Operation("set_status", "ad", ad.external_id, "status", ad.status, status)],
                    actor="owner",
                    kind="resume" if active else "pause",
                    reason_code="OWNER_AD_STATUS",
                    reason_text="Вы включили объявление." if active else "Вы остановили объявление.",
                    suffix=str(ad.id),
                )
        day = today(self.worker.clock)
        return next(row for row in self.ads(business, day - timedelta(days=13), day)["items"] if row["id"] == ad.id)

    def experiments(self, business: Business) -> dict:
        rows = self.repository.rows(StatsDaily, business.id)
        ads = {row.id: row for row in self.repository.rows(Ad, business.id)}
        items = []
        for experiment in self.repository.rows(Experiment, business.id):
            if experiment.status == "proposed":
                continue
            arms = []
            for ad_id in (experiment.control_ad_id, experiment.variant_ad_id):
                arm = [
                    row
                    for row in rows
                    if row.ad_id == ad_id
                    and row.is_settled
                    and experiment.started_at.date() <= row.date <= today(self.worker.clock) - timedelta(days=2)
                ]
                arms.append(
                    Metrics(impressions=sum(row.impressions for row in arm), clicks=sum(row.clicks for row in arm))
                )
            result = experiment.stats.get("result")
            conclusion = explain_experiment(ExperimentResult(**result), *arms) if result else None
            if experiment.status == "cancelled":
                conclusion = "Проверку остановили. Победителя не выбирали."
            remaining = sum(max(0, experiment.planned_n_per_arm - arm.impressions) for arm in arms)
            days = max(0, (experiment.deadline.date() - today(self.worker.clock)).days)
            hypothesis = {"text": "текст", "image": "картинку", "audience": "аудиторию"}[experiment.hypothesis]
            links = " и ".join(
                f"«{ads[id_].title}» (/ads#{id_})" for id_ in (experiment.control_ad_id, experiment.variant_ad_id)
            )
            items.append(
                {
                    "id": experiment.id,
                    "title": f"Сравниваем {hypothesis}",
                    "comparing": f"Сравниваем {links}. До конца проверки дней: {days}.",
                    "remaining_impressions": remaining if experiment.status == "running" else 0,
                    "status": experiment.status,
                    "conclusion": conclusion
                    or (
                        explain_experiment(ExperimentResult(Verdict.RUNNING, "EXPERIMENT_RUNNING"), *arms)
                        if experiment.status == "running"
                        else "Данных для уверенного выбора не хватило."
                    ),
                }
            )
        return {"items": sorted(items, key=lambda row: row["id"], reverse=True)}

    def business(self, business: Business) -> dict:
        marking = next(iter(self.repository.rows(MarkingProfile, business.id)), None)
        # Закрытые реквизиты не читаем и не возвращаем. Для формы оставляем пустые значения.
        safe_marking = (
            None
            if marking is None
            else {
                "subject_type": marking.subject_type,
                "inn": "",
                "name": "",
                "phone": "",
                "confirmed_in_platform": marking.confirmed_in_platform,
            }
        )
        return {
            "id": business.id,
            "site_url": business.site_url,
            "city": business.city,
            "month_budget_kop": business.month_budget_kop,
            "avg_check_kop": business.avg_check_kop,
            "description": business.description or None,
            "auto_continue": business.auto_continue,
            "marking": safe_marking,
        }

    async def save_budget(self, business: Business, amount: int) -> dict:
        business = self.repository.business(business.id)
        with self.repository.business_lock(business.id):
            spent, _ = await read_spend(self.worker, business)
            if amount <= max(sum(spent.values()), self.budget(business)["spent_kop"]):
                raise ChangeConflict("Этот бюджет уже потрачен. Укажите сумму больше расхода за месяц.")
            old = business.month_budget_kop
            effective = today(self.worker.clock) + timedelta(days=1)
            previous = self.worker.metadata(business).get("budget_change", {})
            current = (
                previous.get("before", old)
                if previous.get("effective_on", "") > today(self.worker.clock).isoformat()
                else old
            )
            business.month_budget_kop = amount
            self.worker.remember(
                business,
                budget_change={
                    "before": current,
                    "effective_on": effective.isoformat(),
                },
            )
            self.repository.save(
                ChangeLog(
                    business_id=business.id,
                    actor="owner",
                    kind="budget",
                    platform=None,
                    entity_type="business",
                    entity_id=str(business.id),
                    operations=[],
                    reason_code="OWNER_BUDGET",
                    reason_params={"before": old, "after": amount, "effective_on": effective.isoformat()},
                    reason_text=(
                        f"Вы изменили бюджет на {rubles(amount)} ₽ в месяц. Он действует с {effective:%d.%m.%Y}."
                    ),
                    evidence={},
                    status="applied",
                    created_at=self.worker.clock.now,
                )
            )
            try:
                await rebalance_budget(self.worker, business)
            except (ChangeConflict, PlatformError):
                if amount < old:
                    await stop_all(business, worker=self.worker, budget_stop=True, reason_code="BUDGET_UPDATE_FAILED")
                raise
        return self.business(business)

    def save_settings(self, business: Business, auto_continue: bool, avg_check_kop: int | None) -> dict:
        business = self.repository.business(business.id)
        with self.repository.business_lock(business.id):
            business.auto_continue = auto_continue
            business.avg_check_kop = avg_check_kop
            self.repository.save(business)
        return self.business(business)
