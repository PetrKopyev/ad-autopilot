import re
from datetime import date, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

from app.db.models import (
    Ad,
    AdGroup,
    BudgetLedger,
    Business,
    ChangeLog,
    Experiment,
    MarkingProfile,
    PlatformAccount,
    StatsDaily,
)
from app.db.query import Window
from app.domain.budget import PLATFORM_MINIMUMS, budget_minimum, daily_minimum, stop_margin
from app.domain.explain import PLURALS, explain_audience, explain_experiment, plural, rubles
from app.domain.rules import first_decision_days
from app.domain.types import ExperimentResult, Metrics, Verdict
from app.platforms.base import Operation, PlatformError, lead_count
from app.services.budget_guard import STOPPED, closed_platforms, read_spend, stop_all
from app.services.changes import ChangeConflict
from app.services.launch import TITLES
from app.services.onboarding import MAX_BUDGET_KOP, NotFound, OnboardingError, check_budget, owner_fields
from app.services.optimize import rebalance_budget
from app.services.stats import aggregate, aggregate_budget, aggregate_daily
from app.worker.clock import MOSCOW, today
from app.worker.jobs import Worker

AUDIENCE_RUNNING = (
    "Проверка идёт. Цену обращения сравним за весь срок, когда площадка уточнит обращения за последние дни."
)
SAME_EXCEPT = {
    "text": "Меняем только текст объявления, картинка та же.",
    "image": "Меняем только картинку, текст объявления тот же.",
    "audience": "Меняем только аудиторию: то же объявление показываем в отдельной группе другим людям.",
}
# Ярлык «Остановлено нами» уже говорит, что объявление остановили: причина рядом с ним не повторяет это последней
# фразой. В истории изменений текст остаётся целиком.
REPEATS_LABEL = re.compile(r" Остановили (?:объявление|его)\.$")


def transmission_view(marking: MarkingProfile) -> dict[str, str | None]:
    """Статус последней передачи данных рекламодателя по площадкам: marking_transmitted / marking_manual_required /
    None (передачи ещё не было или кабинет не подключён)."""
    stored = marking.transmission or {}
    return {platform: (stored.get(platform) or {}).get("status") for platform in ("direct", "vk")}


class ScreensService:
    def __init__(self, worker: Worker) -> None:
        self.worker = worker
        self.repository = worker.repository
        # Каждое действие владельца — отдельная попытка, а не слот планировщика.
        worker.step_key = f"owner:{uuid4()}"

    def budget(self, business: Business) -> dict:
        day = today(self.worker.clock)
        month = self.repository.rows(StatsDaily, business.id, date=Window(day.replace(day=1), day))
        return aggregate_budget(
            business.month_budget_kop,
            month,
            self.repository.rows(BudgetLedger, business.id, month=day.replace(day=1)),
            day,
        )

    def status(self, business: Business) -> dict:
        last_check = self.repository.last_check(business.id)
        budget = self.budget(business)
        status = business.status if business.status in {"running", "paused", "budget_stop"} else "draft"
        accounts = self.repository.rows(PlatformAccount, business.id)
        revoked = [row for row in accounts if row.status == "revoked"]
        # Отключение кабинета останавливает его рекламу: без подключённых кабинетов реклама не работает.
        if status == "running" and len(revoked) == len(accounts):
            status = "paused"
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
        campaigns = self.worker.current_campaigns(business)
        for account in revoked:
            stopped = all(
                campaign.status in STOPPED or not campaign.external_id
                for campaign in campaigns
                if campaign.platform_account_id == account.id
            )
            attention.append(
                {
                    "id": f"revoked_{account.platform}",
                    "title": f"Кабинет {TITLES[account.platform]} отключён",
                    "destination": "settings",
                    "description": (
                        "Реклама там остановлена. Чтобы она снова работала, подключите кабинет в настройках."
                        if stopped
                        else "Мы не смогли подтвердить, что реклама там остановлена, — проверьте в кабинете площадки. "
                        "Чтобы реклама снова работала под присмотром автопилота, подключите кабинет в настройках."
                    ),
                }
            )
        latest = self.repository.latest_run(business.id, "budget_guard")
        if (latest and latest.status == "failed") or any(row.status == "needs_reauth" for row in accounts):
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
            # План собран (plan_ready) или уже запущен: до запуска экраны ведут в план, а не в мастер.
            "has_plan": business.status != "draft",
            "first_decisions": self.first_decisions(business) if status == "running" else None,
            # Маркировка: что подтверждено галочкой и что площадки приняли сами (инструкция — в настройках).
            "marking": None
            if (marking := next(iter(self.repository.rows(MarkingProfile, business.id)), None)) is None
            else {
                "confirmed": {
                    platform: marking.confirmed_in_platform.get(platform) is True for platform in ("direct", "vk")
                },
                "transmission": transmission_view(marking),
            },
        }

    def first_decisions(self, business: Business) -> str | None:
        """Срок до первых решений по цене — из порогов автопилота и того, сколько кликов объявления набирают сейчас."""
        from app.config import get_domain_settings

        domain = get_domain_settings()
        campaigns = {row.id for row in self.worker.current_campaigns(business)}
        rows = [
            row
            for row in self.repository.rows(StatsDaily, business.id, is_settled=True)
            if row.campaign_id in campaigns
        ]
        approvals = [
            datetime.fromisoformat(value) for value in self.worker.metadata(business).get("approvals", {}).values()
        ]
        age = (self.worker.clock.now - min(approvals)).days if approvals else None
        ads, days = len({row.ad_id for row in rows}), len({row.date for row in rows})
        rate = Decimal(sum(row.clicks for row in rows)) / (ads * days) if rows else None
        remaining = first_decision_days(age, rate, domain)
        why = (
            f"Срок зависит от бюджета: каждому объявлению нужно набрать {domain.min_price_clicks} кликов, "
            f"а цифры площадки уточняются ещё {plural(domain.settlement_days, PLURALS['days'])}."
        )
        if remaining is None:
            return (
                f"Объявления набирают слишком мало кликов: за {plural(domain.decision_window_days, PLURALS['days'])} "
                f"каждому не набрать {domain.min_price_clicks}, и автопилот не может сравнить их по цене обращения. "
                "Увеличьте бюджет — тогда появятся первые решения."
            )
        if not remaining:
            return None
        if age is None:
            return (
                f"Первые решения автопилота — не раньше чем через {plural(remaining, PLURALS['days'])} после того, "
                f"как площадка одобрит объявления. {why}"
            )
        return f"Первые решения автопилота — примерно через {plural(remaining, PLURALS['days'])}. {why}"

    async def pause(self, business: Business) -> dict:
        business = self.repository.business(business.id)
        await stop_all(business, worker=self.worker, actor="owner")
        return self.status(business)

    async def month_spent(self, business: Business) -> int:
        """Расход месяца, как в rebalance_budget: по каждой площадке большее из отчёта площадки и учёта бюджета.
        Кабинет отключённой площадки спросить нечем: её расход — из учёта бюджета."""
        closed = closed_platforms(self.worker, business)
        spent, _ = await read_spend(
            self.worker,
            business,
            campaigns=[item for item in self.worker.campaigns(business) if item.platform not in closed],
        )
        for item in self.budget(business)["platforms"]:
            spent[item["platform"]] = max(spent.get(item["platform"], 0), item["spent_kop"])
        return sum(spent.values())

    async def check_remaining(self, business: Business) -> None:
        from app.config import get_domain_settings

        # Запас считаем по кампаниям, которые заработают после возобновления: остановленные владельцем,
        # архивные и удерживаемые не тратят. Пауза автопилота (budget_stop, NO_ACTIVE_ADS) не исключается —
        # возобновление их снимает, и запас должен покрывать их трату, иначе кнопка «Продолжить» превращается
        # в мгновенный повторный останов стражем.
        held = set(self.worker.metadata(business).get("below_minimum", []))
        revoked = {row.id for row in self.repository.rows(PlatformAccount, business.id) if row.status == "revoked"}
        daily = sum(
            campaign.daily_limit_kop
            for campaign in self.worker.current_campaigns(business)
            if campaign.daily_limit_kop
            and campaign.status != "archived"
            and not campaign.owner_paused
            and campaign.id not in held
            and campaign.platform_account_id not in revoked
        )
        margin = stop_margin(daily, get_domain_settings())
        if await self.month_spent(business) >= max(0, business.month_budget_kop - margin):
            raise ChangeConflict(
                "Остатка бюджета не хватает с учётом защитного запаса. "
                "Рекламу можно возобновить с 1 числа или увеличить бюджет."
            )

    async def resume(self, business: Business) -> dict:
        business = self.repository.business(business.id)
        with self.repository.business_lock(business.id):
            if business.status == "running":
                raise ChangeConflict("Реклама уже работает")
            if business.status not in {"paused", "budget_stop"}:
                raise ChangeConflict("Сначала запустите рекламу")
            accounts = self.repository.rows(PlatformAccount, business.id)
            revoked = {row.id for row in accounts if row.status == "revoked"}
            if len(revoked) == len(accounts):
                raise ChangeConflict(
                    "Кабинеты площадок отключены. Подключите кабинет в настройках, и реклама снова сможет работать."
                )
            await self.check_remaining(business)
            meta = self.worker.metadata(business)
            if meta.get("pending_stop"):
                raise ChangeConflict("Площадки ещё подтверждают остановку. Повторите попытку после проверки.")
            await rebalance_budget(self.worker, business, force=True)
            stopped = set(meta.get("kill_switch_campaigns", []))
            held = set(self.worker.metadata(business).get("below_minimum", []))
            active = False
            campaigns = self.worker.current_campaigns(business)
            for campaign in campaigns:
                if (
                    campaign.owner_paused
                    or not campaign.daily_limit_kop
                    or campaign.id in held
                    or campaign.platform_account_id in revoked
                ):
                    continue
                last = self.repository.last_status_change(
                    business.id, campaign.platform, "campaign", campaign.external_id
                )
                state = (
                    await self.worker.resolve_adapter(business.id, campaign.platform).get_state(
                        self.worker.refs(campaign)
                    )
                )[campaign.external_id]
                if state.status == "active":
                    active = True
                    continue
                owner_stop = (
                    campaign.id in stopped
                    and last is not None
                    and last.reason_code == "OWNER_STOP"
                    and last.actor == "owner"
                )
                protection_stop = (
                    campaign.status == "paused_auto"
                    and last is not None
                    and last.actor == "system"
                    and last.reason_code in {"BUDGET_STOP", "MONTH_STOP", "SPEND_MISMATCH", "BUDGET_UPDATE_FAILED"}
                )
                # Кампанию остановили перед отключением кабинета; кабинет снова подключён — включаем.
                revoke_stop = last is not None and last.reason_code == "ACCOUNT_REVOKE"
                stopped_by_us = owner_stop or protection_stop or revoke_stop
                if not stopped_by_us or state.status not in {"paused_owner", "paused_auto"}:
                    continue
                await self.worker.apply(
                    business,
                    campaign,
                    [Operation("set_status", "campaign", campaign.external_id, "status", state.status, "active")],
                    actor="owner",
                    kind="resume",
                    reason_code="OWNER_RESUME",
                    reason_text="Вы возобновили рекламу после остановки.",
                    suffix="resume",
                )
                active = True
            if active:
                business.status = "running"
                self.worker.remember(business, kill_switch=False, kill_switch_campaigns=[])
        warning = None
        if not active and any(campaign.id in held for campaign in campaigns):
            warning = (
                "Реклама не включилась: при текущем бюджете площадкам не хватает минимума. "
                "Увеличьте бюджет в настройках."
            )
        return self.status(business) | {"warning": warning}

    def ads(self, business: Business, start: date, end: date) -> dict:
        # Только объявления действующего плана: у прежних планов статусы навсегда остались такими, какими их оставил
        # упавший запуск. Их расход входит в итоги статистики и бюджета, которые считаются по всем строкам.
        campaigns = {row.id: row for row in self.worker.current_campaigns(business)}
        groups = {row.id: row for row in self.repository.rows(AdGroup, business.id) if row.campaign_id in campaigns}
        stats = self.repository.rows(StatsDaily, business.id, date=Window(start, end))
        items = []
        for ad in self.repository.rows(Ad, business.id):
            if ad.ad_group_id not in groups:
                continue
            group = groups[ad.ad_group_id]
            campaign = campaigns[group.campaign_id]
            reason = None
            if ad.status in {"paused_owner", "paused_auto"}:
                change = (
                    self.repository.last_status_change(business.id, campaign.platform, "ad", ad.external_id)
                    if ad.external_id
                    else None
                )
                if change is None or (ad.status == "paused_owner" and change.reason_code == "OWNER_AD_STATUS"):
                    # Владелец остановил само объявление или причины в журнале нет: статус «Остановлено вами» или
                    # «Остановлено нами» уже говорит, кто остановил.
                    reason = None
                elif ad.status == "paused_auto":
                    reason = REPEATS_LABEL.sub("", change.reason_text)
                else:
                    reason = change.reason_text
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
        # Ряд для спарклайна — всегда последние 14 дней до сегодня, по тем же правилам, что /api/stats/daily.
        day = today(self.worker.clock)
        recent = self.repository.rows(StatsDaily, business.id, date=Window(day - timedelta(days=13), day))
        return {
            "items": sorted(items, key=lambda row: (row["platform"], row["campaign_id"], row["group_id"], row["id"])),
            "campaigns": [
                {
                    "id": campaign.id,
                    "platform": campaign.platform,
                    "name": campaign.name,
                    "status": campaign.status,
                    "daily": [
                        {key: point[key] for key in ("date", "cost_kop", "leads", "is_incomplete")}
                        for point in aggregate_daily(
                            [row for row in recent if row.campaign_id == campaign.id],
                            day - timedelta(days=13),
                            day,
                            business.lead_goal_ids,
                            day,
                        )
                    ],
                }
                for campaign in sorted(campaigns.values(), key=lambda row: (row.platform, row.id))
            ],
        }

    async def ad_status(self, business: Business, ad_id: int, active: bool) -> dict:
        business = self.repository.business(business.id)
        with self.repository.business_lock(business.id):
            ad = self.repository.find(Ad, business_id=business.id, id=ad_id)
            group = ad and self.repository.find(AdGroup, business_id=business.id, id=ad.ad_group_id)
            campaign = group and next(
                (row for row in self.worker.current_campaigns(business) if row.id == group.campaign_id), None
            )
            # Объявления прежних планов экран не показывает и не включает.
            if campaign is None:
                raise NotFound("Объявление не найдено.")
            if active:
                await self.check_remaining(business)
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
                    reason_text=f"Вы {'включили' if active else 'остановили'} объявление «{ad.title}».",
                    suffix=str(ad.id),
                )
        day = today(self.worker.clock)
        return next(row for row in self.ads(business, day - timedelta(days=13), day)["items"] if row["id"] == ad.id)

    def experiments(self, business: Business) -> dict:
        ads = {row.id: row for row in self.repository.rows(Ad, business.id)}
        experiments = [row for row in self.repository.rows(Experiment, business.id) if row.status != "proposed"]
        arm_ids = {ad_id for row in experiments for ad_id in (row.control_ad_id, row.variant_ad_id)}
        rows = self.repository.rows(StatsDaily, business.id, ad_id=arm_ids, is_settled=True)
        items = []
        for experiment in experiments:
            started = experiment.started_at.astimezone(MOSCOW).date()
            # Завершённая проверка показывает цифры, по которым её решили, а не накопленные после вывода.
            change = self.repository.find(ChangeLog, id=experiment.change_id) if experiment.change_id else None
            if experiment.status != "running" and change is not None:
                end = change.created_at.astimezone(MOSCOW).date() - timedelta(days=2)
            else:
                end = today(self.worker.clock) - timedelta(days=2)
            if change is not None and "control" in change.evidence:
                arms = [Metrics(**change.evidence["control"]), Metrics(**change.evidence["variant"])]
            else:
                arms = []
                for ad_id in (experiment.control_ad_id, experiment.variant_ad_id):
                    arm = [row for row in rows if row.ad_id == ad_id and started <= row.date <= end]
                    arms.append(
                        Metrics(
                            spend=sum(row.cost_kop for row in arm),
                            impressions=sum(row.impressions for row in arm),
                            clicks=sum(row.clicks for row in arm),
                            leads=sum(
                                min(row.clicks, lead_count(row.conversions, business.lead_goal_ids)) for row in arm
                            ),
                        )
                    )
            result = experiment.stats.get("result")
            audiences = experiment.stats.get("audiences") if experiment.metric == "cpl" else None
            conclusion = (
                (
                    explain_audience(
                        ExperimentResult(**result), *arms, audiences, overlap=experiment.stats.get("overlap", False)
                    )
                    if audiences
                    else explain_experiment(ExperimentResult(**result), *arms)
                )
                if result
                else None
            )
            if experiment.status == "cancelled":
                conclusion = "Проверку остановили. Победителя не выбирали."
            # Аудитории сравниваются за полный срок, а не по числу показов.
            remaining = 0 if audiences else sum(max(0, experiment.planned_n_per_arm - arm.impressions) for arm in arms)
            days = max(0, (experiment.deadline.astimezone(MOSCOW).date() - today(self.worker.clock)).days)
            hypothesis = {"text": "текст", "image": "картинку", "audience": "аудитории"}[experiment.hypothesis]
            variants = [ads.get(id_) for id_ in (experiment.control_ad_id, experiment.variant_ad_id)]
            # Варианты (А и Б) экран показывает сам, из arms; строка comparing говорит только, что между ними меняется.
            contents = [
                {
                    "title": ad.title if ad else "",
                    "text": ad.text if ad else "",
                    "image": f"/api/plan/assets/{ad.asset_id}/file" if ad and ad.asset_id else None,
                    "audience": audiences[index] if audiences else None,
                }
                for index, ad in enumerate(variants)
            ]
            comparing = SAME_EXCEPT[experiment.hypothesis]
            if experiment.status == "running":
                comparing += f" До конца проверки: {plural(days, PLURALS['days'])}."
            items.append(
                {
                    "id": experiment.id,
                    "title": f"Сравниваем {hypothesis}",
                    "hypothesis": experiment.hypothesis,
                    "comparing": comparing,
                    "arms": contents,
                    "remaining_impressions": remaining if experiment.status == "running" else 0,
                    "period": {"from": started, "to": end} if end >= started else None,
                    "status": experiment.status,
                    "conclusion": conclusion
                    or (
                        AUDIENCE_RUNNING
                        if audiences and experiment.status == "running"
                        else explain_experiment(ExperimentResult(Verdict.RUNNING, "EXPERIMENT_RUNNING"), *arms)
                        if experiment.status == "running"
                        else "Данных для уверенного выбора не хватило."
                    ),
                }
            )
        return {"items": sorted(items, key=lambda row: row["id"], reverse=True)}

    def business(self, business: Business) -> dict:
        marking = next(iter(self.repository.rows(MarkingProfile, business.id)), None)
        # ИНН и телефон целиком не возвращаем: только последние цифры, чтобы владелец видел, что данные сохранены.
        # Поля формы inn и phone пустые — чтобы изменить реквизиты, их вводят заново.
        safe_marking = (
            None
            if marking is None
            else {
                "subject_type": marking.subject_type,
                "inn": "",
                "name": marking.name,
                "phone": "",
                "confirmed_in_platform": marking.confirmed_in_platform,
                "transmission": transmission_view(marking),
                "saved": True,
                "inn_masked": f"•••{marking.inn_enc[-4:]}",
                "phone_masked": f"+7•••{marking.phone_enc[-4:]}" if marking.phone_enc else None,
            }
        )
        return {
            "id": business.id,
            "site_url": business.site_url,
            "city": business.city,
            "month_budget_kop": business.month_budget_kop,
            "avg_check_kop": business.avg_check_kop,
            # Служебная проверка сайта (check) и медицинская оценка клиенту не нужны: только поля описания.
            "description": (
                {"fields": owner_fields(business.description["fields"])} if business.description.get("fields") else None
            ),
            "auto_continue": business.auto_continue,
            "marking": safe_marking,
            # Мастер настройки восстанавливает по ним пройденные шаги в новой вкладке и на другом устройстве.
            "metrica_counter_id": business.metrica_counter_id,
            "lead_goal_ids": business.lead_goal_ids,
            "description_approved": business.description_approved_at is not None,
            "medical_level": business.medical_level,
        }

    async def save_budget(self, business: Business, amount: int) -> dict:
        from app.config import get_domain_settings

        business = self.repository.business(business.id)
        with self.repository.business_lock(business.id):
            if type(amount) is not int or not 0 < amount <= MAX_BUDGET_KOP:
                raise OnboardingError(f"Бюджет должен быть больше нуля и не превышать {rubles(MAX_BUDGET_KOP)} ₽.")
            check_budget(amount)
            if amount <= await self.month_spent(business):
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
                        f"Вы изменили бюджет с {rubles(old)} ₽ на {rubles(amount)} ₽ в месяц. "
                        f"Он действует с {effective:%d.%m.%Y}."
                    ),
                    evidence={},
                    status="applied",
                    created_at=self.worker.clock.now,
                )
            )
            try:
                minimums = await rebalance_budget(self.worker, business)
            except (ChangeConflict, PlatformError):
                if amount < old:
                    await stop_all(business, worker=self.worker, budget_stop=True, reason_code="BUDGET_UPDATE_FAILED")
                raise
            held = set(self.worker.metadata(business).get("below_minimum", []))
            vk_held = any(
                campaign.platform == "vk" and campaign.id in held
                for campaign in self.worker.current_campaigns(business)
            )
            warning = None
            if vk_held and amount < budget_minimum(("direct", "vk"), get_domain_settings()):
                minimum = daily_minimum(minimums.get("vk", PLATFORM_MINIMUMS["vk"]))
                warning = (
                    f"Площадка ВКонтакте остановлена: при таком бюджете ей не хватает минимума {rubles(minimum)} ₽ "
                    "в день."
                )
        return self.business(business) | {"warning": warning}

    def save_settings(
        self,
        business: Business,
        auto_continue: bool,
        avg_check_kop: int | None,
        vk_priced_goal_source_id: int | None = None,
        vk_priced_goal_name: str = "",
    ) -> dict:
        business = self.repository.business(business.id)
        with self.repository.business_lock(business.id):
            business.auto_continue = auto_continue
            business.avg_check_kop = avg_check_kop
            # Цель VK кабинета: None — не менять. Стратегия оплаты за конверсию будет опираться на неё (В8).
            settings = business.settings | (
                {"vk_priced_goal_source_id": vk_priced_goal_source_id, "vk_priced_goal_name": vk_priced_goal_name}
                if vk_priced_goal_source_id
                else {}
            )
            business.settings = settings
            self.repository.save(business)
        return self.business(business)
