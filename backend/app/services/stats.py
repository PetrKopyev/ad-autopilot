from collections import defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Literal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import Ad, BudgetLedger, Business, LeadsSurvey, StatsDaily
from app.platforms.base import lead_count
from app.worker.clock import MOSCOW

Level = Literal["business", "platform", "campaign", "group", "ad"]


@dataclass(frozen=True)
class StatsRow:
    business_id: int
    platform: str
    campaign_id: int
    ad_id: int
    ad_group_id: int | None
    date: date
    impressions: int
    clicks: int
    cost_kop: int
    conversions: dict[str, int]
    fetched_at: datetime
    is_settled: bool


def money_per(cost_kop: int, count: int) -> int | None:
    # Округление до копейки, половина вверх, без потери точности на больших суммах.
    return (2 * cost_kop + count) // (2 * count) if count else None


def aggregate(rows: Iterable[StatsRow], goal_ids: Sequence[int], today: date) -> dict:
    cost_kop = impressions = clicks = leads = 0
    incomplete_days: set[date] = set()
    sources: dict[str, datetime] = {}
    for row in rows:
        cost_kop += row.cost_kop
        impressions += row.impressions
        clicks += row.clicks
        leads += lead_count(row.conversions, goal_ids)
        if not row.is_settled or row.date > today - timedelta(days=2):
            incomplete_days.add(row.date)
        sources[row.platform] = max(sources.get(row.platform, row.fetched_at), row.fetched_at)
    return {
        "cost_kop": cost_kop,
        "impressions": impressions,
        "clicks": clicks,
        "leads": leads,
        "ctr": clicks / impressions if impressions else None,
        "cost_per_click_kop": money_per(cost_kop, clicks),
        "cost_per_lead_kop": money_per(cost_kop, leads),
        "clients": None,
        "cost_per_client_kop": None,
        "is_incomplete": bool(incomplete_days),
        "incomplete_days": sorted(incomplete_days),
        "sources": [{"platform": platform, "fetched_at": sources[platform]} for platform in sorted(sources)],
    }


def aggregate_breakdown(rows: Iterable[StatsRow], level: Level, goal_ids: Sequence[int], today: date) -> list[dict]:
    fields = {
        "business": "business_id",
        "platform": "platform",
        "campaign": "campaign_id",
        "group": "ad_group_id",
        "ad": "ad_id",
    }
    if level not in fields:
        raise ValueError("Неизвестный уровень статистики.")
    groups: dict[int | str | None, list[StatsRow]] = defaultdict(list)
    for row in rows:
        groups[getattr(row, fields[level])].append(row)
    return [
        {"id": key, **aggregate(groups[key], goal_ids, today)}
        for key in sorted(groups, key=lambda key: (key is None, key))
    ]


def aggregate_daily(
    rows: Iterable[StatsRow], date_from: date, date_to: date, goal_ids: Sequence[int], today: date
) -> list[dict]:
    if date_from > date_to:
        raise ValueError("Начало периода должно быть не позже окончания.")
    groups: dict[date, list[StatsRow]] = defaultdict(list)
    for row in rows:
        if date_from <= row.date <= date_to:
            groups[row.date].append(row)
    result = []
    for offset in range((date_to - date_from).days + 1):
        day = date_from + timedelta(days=offset)
        metrics = aggregate(groups[day], goal_ids, today)
        if day > today - timedelta(days=2):
            metrics.update(is_incomplete=True, incomplete_days=[day])
        result.append({"date": day, **metrics})
    return result


def client_window(as_of: date) -> tuple[date, date]:
    # Ответ относится ко всей неделе: берём четыре завершённые недели пн–вс.
    week_start = as_of - timedelta(days=as_of.weekday())
    return week_start - timedelta(weeks=4), week_start - timedelta(days=1)


def aggregate_clients(
    rows: Iterable[StatsRow], surveys: Iterable[LeadsSurvey], as_of: date, goal_ids: Sequence[int]
) -> dict:
    """Цена клиента за окно client_window — одна на «Клиентах» и «Статистике». Неделя без обращений известна точно:
    клиентов 0, её расход входит в цену. Неделя с обращениями без ответа (или с ответом «не знаю») неизвестна:
    её расход нельзя делить на клиентов отвеченных недель, она исключается и показывается в unknown_weeks. Пока нет
    ни одного ответа, клиентов не знаем (None)."""
    date_from, date_to = client_window(as_of)
    answers = {
        survey.week_start: survey.clients_count
        for survey in surveys
        if date_from <= survey.week_start <= date_to
        and survey.answered_at is not None
        and survey.clients_count is not None
        and survey.answered_at.astimezone(MOSCOW).date() <= as_of
    }
    spend: dict[date, int] = defaultdict(int)
    leads: dict[date, int] = defaultdict(int)
    for row in rows:
        if date_from <= row.date <= date_to:
            week = row.date - timedelta(days=row.date.weekday())
            spend[week] += row.cost_kop
            leads[week] += lead_count(row.conversions, goal_ids)
    quiet = {week for week in spend if not leads[week] and week not in answers}
    unknown = {week for week in leads if leads[week] and week not in answers}
    cost_kop = sum(spend[week] for week in answers.keys() | quiet)
    clients = sum(answers.values()) if answers else None
    return {
        "from": date_from,
        "to": date_to,
        "clients": clients,
        "cost_kop": cost_kop,
        "cost_per_client_kop": money_per(cost_kop, clients) if clients is not None else None,
        "answered_weeks": len(answers),
        "unknown_weeks": len(unknown),
    }


def aggregate_budget(
    month_budget_kop: int, rows: Iterable[StatsRow], ledger: Iterable[BudgetLedger], today: date
) -> dict:
    """Расход месяца для бюджета. Отчёты (stats_daily) обновляются раз в три часа, а снимок стража бюджета
    (budget_ledger) — раз в полчаса и вместе со свежим расходом площадки за сегодня, поэтому обычно он больше.
    Бюджет считается по большему из двух на каждой площадке; reported_kop — расход по отчётам, тот же, что в
    статистике за месяц: разница — ещё не попавший в отчёты расход."""
    month = today.replace(day=1)
    next_month = (month.replace(day=28) + timedelta(days=4)).replace(day=1)
    reported: dict[str, int] = defaultdict(int)
    for row in rows:
        if month <= row.date <= today:
            reported[row.platform] += row.cost_kop
    snapshots = {item.platform: item for item in ledger if item.month == month}
    # Снимок включает расход площадки целиком; прибавлять к нему stats_daily нельзя.
    spent = dict(reported)
    for platform, snapshot in snapshots.items():
        spent[platform] = max(snapshot.spent_kop, reported.get(platform, 0))
    spent_kop = sum(spent.values())
    return {
        "month": month,
        "budget_kop": month_budget_kop,
        "spent_kop": spent_kop,
        "reported_kop": sum(reported.values()),
        "remaining_kop": max(0, month_budget_kop - spent_kop),
        "days_remaining": (next_month - today).days,
        "platforms": [
            {
                "platform": platform,
                "spent_kop": spent[platform],
                "source": "budget_ledger" if platform in snapshots else "stats_daily",
                "spent_at": snapshots[platform].spent_at if platform in snapshots else None,
            }
            for platform in sorted(spent)
        ],
    }


class StatsRepository:
    def __init__(self, db: Session) -> None:
        self.db = db

    def rows(self, business_id: int, date_from: date, date_to: date) -> list[StatsRow]:
        statement = (
            select(*StatsDaily.__table__.c, Ad.ad_group_id)
            .outerjoin(Ad, Ad.id == StatsDaily.ad_id)
            .where(
                StatsDaily.business_id == business_id,
                StatsDaily.date >= date_from,
                StatsDaily.date <= date_to,
            )
        )
        return [StatsRow(**row) for row in self.db.execute(statement).mappings()]

    def surveys(self, business_id: int, date_from: date, date_to: date) -> list[LeadsSurvey]:
        return list(
            self.db.scalars(
                select(LeadsSurvey).where(
                    LeadsSurvey.business_id == business_id,
                    LeadsSurvey.week_start >= date_from,
                    LeadsSurvey.week_start <= date_to,
                )
            )
        )

    def ledger(self, business_id: int, month: date) -> list[BudgetLedger]:
        return list(
            self.db.scalars(
                select(BudgetLedger).where(BudgetLedger.business_id == business_id, BudgetLedger.month == month)
            )
        )


class StatsService:
    def __init__(self, repository: StatsRepository) -> None:
        self.repository = repository

    @staticmethod
    def period(business: Business, date_from: date, date_to: date, today: date) -> dict:
        if date_from > date_to:
            raise ValueError("Начало периода должно быть не позже окончания.")
        return {
            "business_id": business.id,
            "from": date_from,
            "to": date_to,
            "incomplete_days": [day for day in (today - timedelta(days=1), today) if date_from <= day <= date_to],
        }

    def summary(self, business: Business, date_from: date, date_to: date, today: date) -> dict:
        period = self.period(business, date_from, date_to, today)
        rows = self.repository.rows(business.id, date_from, date_to)
        metrics = aggregate(rows, business.lead_goal_ids, today)
        metrics["incomplete_days"] = sorted(set(metrics["incomplete_days"]) | set(period["incomplete_days"]))
        metrics["is_incomplete"] = bool(metrics["incomplete_days"])
        as_of = min(date_to, today)
        clients_from, clients_to = client_window(as_of)
        clients = aggregate_clients(
            self.repository.rows(business.id, clients_from, clients_to),
            self.repository.surveys(business.id, clients_from, clients_to),
            as_of,
            business.lead_goal_ids,
        )
        metrics.update(clients=clients["clients"], cost_per_client_kop=clients["cost_per_client_kop"])
        budget = aggregate_budget(
            business.month_budget_kop,
            self.repository.rows(business.id, today.replace(day=1), today),
            self.repository.ledger(business.id, today.replace(day=1)),
            today,
        )
        return {**period, **metrics, "clients_window": clients, "budget": budget}

    def breakdown(self, business: Business, date_from: date, date_to: date, level: Level, today: date) -> dict:
        period = self.period(business, date_from, date_to, today)
        if level == "business":
            summary = self.summary(business, date_from, date_to, today)
            return {**self.with_items(period, [{"id": business.id, **summary}]), "level": level}
        items = aggregate_breakdown(
            self.repository.rows(business.id, date_from, date_to), level, business.lead_goal_ids, today
        )
        return {**self.with_items(period, items), "level": level}

    def daily(self, business: Business, date_from: date, date_to: date, today: date) -> dict:
        period = self.period(business, date_from, date_to, today)
        items = aggregate_daily(
            self.repository.rows(business.id, date_from, date_to), date_from, date_to, business.lead_goal_ids, today
        )
        return self.with_items(period, items)

    @staticmethod
    def with_items(period: dict, items: list[dict]) -> dict:
        incomplete_days = sorted(
            set(period["incomplete_days"]) | {day for item in items for day in item["incomplete_days"]}
        )
        return {**period, "is_incomplete": bool(incomplete_days), "incomplete_days": incomplete_days, "items": items}
