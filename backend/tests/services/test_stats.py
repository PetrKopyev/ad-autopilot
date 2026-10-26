from collections.abc import Iterator
from dataclasses import asdict, replace
from datetime import UTC, date, datetime
from unittest.mock import MagicMock

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from app.db.models import BudgetLedger, Business, LeadsSurvey, User
from app.services.stats import (
    StatsRepository,
    StatsRow,
    StatsService,
    aggregate,
    aggregate_breakdown,
    aggregate_budget,
    aggregate_clients,
    aggregate_daily,
    client_window,
    money_per,
)

TODAY = date(2026, 10, 26)
FETCHED_AT = datetime(2026, 10, 26, 12, tzinfo=UTC)


def stats_row(**changes) -> StatsRow:
    row = StatsRow(
        business_id=1,
        platform="direct",
        campaign_id=10,
        ad_id=100,
        ad_group_id=20,
        date=date(2026, 10, 24),
        impressions=100,
        clicks=3,
        cost_kop=1001,
        conversions={"1": 2, "2": 1, "999": 70},
        fetched_at=FETCHED_AT,
        is_settled=True,
    )
    return replace(row, **changes)


@pytest.fixture
def rows() -> list[StatsRow]:
    return [
        stats_row(),
        stats_row(date=date(2026, 10, 25), cost_kop=2000),
        stats_row(platform="vk", campaign_id=11, ad_group_id=21, ad_id=101, cost_kop=500),
        stats_row(campaign_id=12, ad_group_id=None, ad_id=102, cost_kop=7, is_settled=False),
    ]


@pytest.mark.parametrize("level", ["business", "platform", "campaign", "group", "ad"])
def test_every_level_preserves_all_totals(rows: list[StatsRow], level: str) -> None:
    items = aggregate_breakdown(rows, level, [1, 2, 1], TODAY)
    for field in ("cost_kop", "impressions", "clicks"):
        assert sum(item[field] for item in items) == sum(getattr(row, field) for row in rows)
    assert sum(item["leads"] for item in items) == 12
    if level == "group":
        assert next(item for item in items if item["id"] is None)["cost_kop"] == 7


def test_derived_metrics_use_totals_and_selected_goals(rows: list[StatsRow]) -> None:
    metrics = aggregate(rows, [1, 2, 1], TODAY)
    assert metrics["cost_kop"] == 3508
    assert metrics["leads"] == 12
    assert metrics["ctr"] == 0.03
    assert type(metrics["ctr"]) is float
    assert metrics["cost_per_click_kop"] == 292
    assert metrics["cost_per_lead_kop"] == 292
    assert metrics["clients"] is None
    assert metrics["cost_per_client_kop"] is None
    assert aggregate(rows, [], TODAY)["leads"] == 0


@pytest.mark.parametrize("rows", [[], [stats_row(impressions=0, clicks=0, conversions={})]])
def test_zero_denominators_return_none(rows: list[StatsRow]) -> None:
    metrics = aggregate(rows, [1], TODAY)
    for field in ("ctr", "cost_per_click_kop", "cost_per_lead_kop", "cost_per_client_kop"):
        assert metrics[field] is None


@pytest.mark.parametrize("cost,count,expected", [(1, 2, 1), (1, 3, 0), (10**20 + 1, 2, 5 * 10**19 + 1), (1, 0, None)])
def test_money_rounds_half_up_without_floats(cost: int, count: int, expected: int | None) -> None:
    assert money_per(cost, count) == expected
    if expected is not None:
        assert type(money_per(cost, count)) is int


def test_ratios_are_computed_after_aggregation() -> None:
    rows = [stats_row(impressions=100, clicks=10), stats_row(impressions=900, clicks=9)]
    assert aggregate(rows, [1], TODAY)["ctr"] == 19 / 1000


def test_pure_aggregations_reject_invalid_parameters() -> None:
    with pytest.raises(ValueError):
        aggregate_breakdown([], "unknown", [], TODAY)
    with pytest.raises(ValueError):
        aggregate_daily([], TODAY, date(2026, 10, 1), [], TODAY)


def test_incomplete_days_include_old_unsettled_rows_and_recent_settled_rows() -> None:
    rows = [
        stats_row(date=date(2026, 10, 20), is_settled=False),
        stats_row(date=date(2026, 10, 24)),
        stats_row(date=date(2026, 10, 25)),
        stats_row(date=TODAY),
    ]
    result = aggregate(rows, [1], TODAY)
    assert result["cost_kop"] == 4004
    assert result["incomplete_days"] == [date(2026, 10, 20), date(2026, 10, 25), TODAY]
    assert result["is_incomplete"] is True


def test_sources_show_latest_fetch_for_each_platform(rows: list[StatsRow]) -> None:
    rows.append(stats_row(fetched_at=datetime(2026, 10, 26, 13, tzinfo=UTC)))
    assert aggregate(rows, [1], TODAY)["sources"] == [
        {"platform": "direct", "fetched_at": datetime(2026, 10, 26, 13, tzinfo=UTC)},
        {"platform": "vk", "fetched_at": FETCHED_AT},
    ]


def test_daily_is_ordered_fills_gaps_and_preserves_totals(rows: list[StatsRow]) -> None:
    result = aggregate_daily(rows + [stats_row(date=date(2026, 10, 1))], date(2026, 10, 23), TODAY, [1, 2], TODAY)
    assert [item["date"] for item in result] == [date(2026, 10, day) for day in (23, 24, 25, 26)]
    assert sum(item["cost_kop"] for item in result) == 3508
    assert sum(item["leads"] for item in result) == 12
    assert result[0]["cost_kop"] == 0
    assert result[0]["cost_per_lead_kop"] is None
    assert result[-1]["is_incomplete"] is True


def survey(week_start: date, clients: int | None, answered_at: datetime | None = FETCHED_AT) -> LeadsSurvey:
    return LeadsSurvey(
        business_id=1, week_start=week_start, leads_count=10, clients_count=clients, answered_at=answered_at
    )


def test_clients_use_only_answered_weeks_and_matching_cost() -> None:
    assert client_window(TODAY) == (date(2026, 9, 28), date(2026, 10, 25))
    surveys = [
        survey(date(2026, 9, 21), 10),
        survey(date(2026, 9, 28), 2),
        survey(date(2026, 10, 5), 0),
        survey(date(2026, 10, 12), None),
        survey(date(2026, 10, 19), 3),
        survey(TODAY, 10),
    ]
    rows = [stats_row(date=item.week_start) for item in surveys]
    result = aggregate_clients(rows, surveys, TODAY)
    assert result["clients"] == 5
    assert result["cost_kop"] == 3003
    assert result["cost_per_client_kop"] == 601
    assert result["answered_weeks"] == 3
    assert result["unknown_weeks"] == 1


@pytest.mark.parametrize(
    "clients,answered_at,expected", [(None, FETCHED_AT, None), (3, None, None), (0, FETCHED_AT, 0)]
)
def test_unknown_clients_differ_from_zero(
    clients: int | None, answered_at: datetime | None, expected: int | None
) -> None:
    result = aggregate_clients([], [survey(date(2026, 10, 19), clients, answered_at)], TODAY)
    assert result["clients"] == expected
    assert result["cost_per_client_kop"] is None


def test_historical_clients_ignore_later_answers() -> None:
    result = aggregate_clients([], [survey(date(2026, 10, 12), 5)], date(2026, 10, 19))
    assert result["clients"] is None


def test_month_budget_uses_snapshots_without_double_counting(rows: list[StatsRow]) -> None:
    ledger = [
        BudgetLedger(month=date(2026, 10, 1), platform="direct", spent_kop=5000, spent_at=FETCHED_AT),
        BudgetLedger(month=date(2026, 9, 1), platform="vk", spent_kop=999999),
    ]
    result = aggregate_budget(10000, rows + [stats_row(date=date(2026, 9, 30))], ledger, TODAY)
    assert result["spent_kop"] == 5500
    assert result["remaining_kop"] == 4500
    assert result["days_remaining"] == 6
    assert [item["source"] for item in result["platforms"]] == ["budget_ledger", "stats_daily"]


@pytest.mark.parametrize("today,days", [(date(2026, 12, 31), 1), (date(2028, 2, 28), 2)])
def test_budget_month_boundaries_and_overspend(today: date, days: int) -> None:
    result = aggregate_budget(1, [stats_row(date=today)], [], today)
    assert result["remaining_kop"] == 0
    assert result["days_remaining"] == days


def test_repository_keeps_rows_without_groups_and_scopes_all_queries() -> None:
    db = MagicMock()
    db.execute.return_value.mappings.return_value = [asdict(stats_row(ad_group_id=None))]
    repository = StatsRepository(db)
    assert repository.rows(1, date(2026, 10, 1), TODAY) == [stats_row(ad_group_id=None)]
    statement = db.execute.call_args.args[0]
    assert "LEFT OUTER JOIN ads" in str(statement)
    assert "stats_daily.business_id =" in str(statement)
    assert set(statement.compile().params.values()) == {1, date(2026, 10, 1), TODAY}
    repository.surveys(1, date(2026, 10, 1), TODAY)
    statement = db.scalars.call_args.args[0]
    assert "leads_survey.business_id =" in str(statement)
    assert set(statement.compile().params.values()) == {1, date(2026, 10, 1), TODAY}
    repository.ledger(1, date(2026, 10, 1))
    statement = db.scalars.call_args.args[0]
    assert "budget_ledger.business_id =" in str(statement)
    assert set(statement.compile().params.values()) == {1, date(2026, 10, 1)}


class MemoryRepository:
    def __init__(self, rows: list[StatsRow]) -> None:
        self.stats = rows + [stats_row(business_id=2, cost_kop=999999)]
        self.businesses = [
            Business(id=1, user_id=7, lead_goal_ids=[1, 2], month_budget_kop=10000),
            Business(id=2, user_id=8, lead_goal_ids=[1], month_budget_kop=20000),
        ]

    def get_business(self, user_id: int, business_id: int | None) -> Business:
        businesses = [item for item in self.businesses if item.user_id == user_id and business_id in (None, item.id)]
        if not businesses:
            raise HTTPException(404, "Бизнес не найден.")
        if len(businesses) > 1:
            raise HTTPException(400, "Выберите бизнес.")
        return businesses[0]

    def rows(self, business_id: int, date_from: date, date_to: date) -> list[StatsRow]:
        return [row for row in self.stats if row.business_id == business_id and date_from <= row.date <= date_to]

    def surveys(self, business_id: int, date_from: date, date_to: date) -> list[LeadsSurvey]:
        return [survey(date(2026, 10, 19), 3)] if business_id == 1 else []

    def ledger(self, business_id: int, month: date) -> list[BudgetLedger]:
        return []


@pytest.fixture
def repository(rows: list[StatsRow]) -> MemoryRepository:
    return MemoryRepository(rows)


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch, repository: MemoryRepository) -> Iterator[TestClient]:
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://test:test@invalid/test")
    monkeypatch.setenv("APP_BASE_URL", "https://testserver")
    monkeypatch.setenv("SECRET_KEY", "test-only-secret-key-0123456789abcdef")
    monkeypatch.setenv("TOKEN_ENCRYPTION_KEY", "test-only")
    monkeypatch.setenv("PLATFORM_MODE", "fake")

    from unittest.mock import AsyncMock

    from app.api.stats import get_stats_service, get_today
    from app.auth.sessions import current_user, get_repository
    from app.db.session import engine
    from app.main import app

    monkeypatch.setattr("app.main.start_scheduler", AsyncMock(return_value=None))

    def forbid_database(*args, **kwargs):
        pytest.fail("Stats unit tests must not connect to a database")

    monkeypatch.setattr(engine, "connect", forbid_database)
    overrides = app.dependency_overrides.copy()
    app.dependency_overrides.update(
        {
            current_user: lambda: User(id=7),
            get_repository: lambda: repository,
            get_stats_service: lambda: StatsService(repository),
            get_today: lambda: TODAY,
        }
    )
    try:
        with TestClient(app) as test_client:
            yield test_client
    finally:
        app.dependency_overrides.clear()
        app.dependency_overrides.update(overrides)


def params(**extra) -> dict[str, str]:
    return {"from": "2026-10-24", "to": "2026-10-26", **extra}


def test_summary_route(client: TestClient) -> None:
    response = client.get("/api/stats/summary", params=params())
    assert response.status_code == 200
    result = response.json()
    assert result["business_id"] == 1
    assert result["cost_kop"] == 3508
    assert result["leads"] == 12
    assert result["clients"] == 3
    assert result["cost_per_client_kop"] == 1169
    assert result["clients_window"]["from"] == "2026-09-28"
    assert result["budget"]["spent_kop"] == 3508
    assert result["incomplete_days"] == ["2026-10-24", "2026-10-25", "2026-10-26"]


@pytest.mark.parametrize("level", ["business", "platform", "campaign", "group", "ad"])
def test_breakdown_route(client: TestClient, level: str) -> None:
    response = client.get("/api/stats/breakdown", params=params(level=level))
    assert response.status_code == 200
    items = response.json()["items"]
    assert sum(item["cost_kop"] for item in items) == 3508
    assert sum(item["leads"] for item in items) == 12
    assert response.json()["is_incomplete"] is True
    if level != "business":
        assert all(item["clients"] is None and item["cost_per_client_kop"] is None for item in items)


def test_daily_route(client: TestClient) -> None:
    response = client.get("/api/stats/daily", params=params())
    assert response.status_code == 200
    items = response.json()["items"]
    assert [item["date"] for item in items] == ["2026-10-24", "2026-10-25", "2026-10-26"]
    assert sum(item["cost_kop"] for item in items) == 3508
    assert sum(item["leads"] for item in items) == 12
    assert response.json()["incomplete_days"] == ["2026-10-24", "2026-10-25", "2026-10-26"]


@pytest.mark.parametrize("path", ["summary", "breakdown", "daily"])
def test_routes_reject_foreign_business(client: TestClient, path: str) -> None:
    assert client.get(f"/api/stats/{path}", params=params(level="ad", business_id="2")).status_code == 404


@pytest.mark.parametrize("path", ["summary", "breakdown", "daily"])
def test_routes_require_authentication(client: TestClient, path: str) -> None:
    from app.auth.sessions import current_user
    from app.main import app

    app.dependency_overrides.pop(current_user)
    assert client.get(f"/api/stats/{path}", params=params(level="ad")).status_code == 401


@pytest.mark.parametrize("path", ["summary", "breakdown", "daily"])
@pytest.mark.parametrize(
    "query",
    [{}, {"from": "2026-10-24"}, {"to": "2026-10-26"}, params(**{"from": "bad"}), params(to="2026-10-01")],
)
def test_routes_validate_period(client: TestClient, path: str, query: dict) -> None:
    assert client.get(f"/api/stats/{path}", params={"level": "ad", **query}).status_code == 422


@pytest.mark.parametrize("level", [None, "unknown"])
def test_breakdown_requires_valid_level(client: TestClient, level: str | None) -> None:
    query = params() if level is None else params(level=level)
    assert client.get("/api/stats/breakdown", params=query).status_code == 422


def test_multiple_businesses_require_explicit_selection(client: TestClient, repository: MemoryRepository) -> None:
    repository.businesses.append(Business(id=3, user_id=7, lead_goal_ids=[], month_budget_kop=0))
    assert client.get("/api/stats/summary", params=params()).status_code == 400
    assert client.get("/api/stats/summary", params=params(business_id="1")).status_code == 200


def test_summary_marks_recent_empty_days(client: TestClient, repository: MemoryRepository) -> None:
    repository.stats.clear()
    result = client.get("/api/stats/summary", params=params()).json()
    assert result["cost_kop"] == 0
    assert result["is_incomplete"] is True
    assert result["incomplete_days"] == ["2026-10-25", "2026-10-26"]
    assert result["cost_per_lead_kop"] is None
