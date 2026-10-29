import pytest

from app.db.models import ChangeLog, Experiment
from tests.api.test_demo_api import demo, launch  # noqa: F401


@pytest.fixture
def with_check(demo, monkeypatch):  # noqa: F811
    """Демо-бизнес, у которого на первом шаге указан средний чек 2 500 ₽."""
    client, _, _ = demo
    post = client.post

    def create(url, json=None, **kwargs):
        if url == "/api/business":
            json = json | {"avg_check_kop": 250_000}
        return post(url, json=json, **kwargs)

    monkeypatch.setattr(client, "post", create)
    return demo


def test_demo_with_average_check_pauses_scales_and_tests(with_check):
    client, world, worker = with_check
    launch(client)
    assert world.business.avg_check_kop == 250_000
    for _ in range(2):
        assert client.post("/api/demo/advance", json={"days": 20}).status_code == 202
    codes = {row.reason_code for row in worker.repository.rows(ChangeLog) if row.status == "applied"}
    assert {"PAUSE_TOO_EXPENSIVE", "SCALE_UP", "EXPERIMENT_START"} <= codes
    assert worker.repository.rows(Experiment)


def month_spend(world, month) -> int:
    return sum(cost for (_, day), cost in world.adapters["direct"]._spent_daily.items() if day.replace(day=1) == month)


def test_demo_with_one_service_spends_most_of_the_month(demo, monkeypatch):  # noqa: F811
    """Одна услуга — одна группа: брать бюджет не у кого, её лимит и так весь дневной бюджет Директа. Демо-кабинет
    тратит этот лимит, а не упирается в свои показы: за январь уходит больше 80% бюджета."""
    client, world, worker = demo
    put = client.put

    def describe(url, json=None, **kwargs):
        if url == "/api/business/description":
            json["description"]["services"] = json["description"]["services"] | {"value": "Маникюр"}
        return put(url, json=json, **kwargs)

    monkeypatch.setattr(client, "put", describe)
    launch(client)
    assert len(worker.current_campaigns(world.business)) == 1
    assert client.post("/api/demo/advance", json={"days": 31}).status_code == 202
    january = worker.clock.now.date().replace(day=1, month=1)
    assert month_spend(world, january) > world.business.month_budget_kop * 80 // 100


def test_demo_forecast_matches_first_month_clicks(demo):  # noqa: F811
    """Прогноз демо считает клики так же, как их наберёт демо-кабинет: доля бюджета каждой группы по её цене клика."""
    from decimal import Decimal

    from app.db.models import StatsDaily
    from app.services.forecast import demo_forecast, direct_clicks

    client, world, worker = demo
    launch(client)
    groups = [
        group
        for group in world.repository.latest_plan(world.business.id).payload["groups"]
        if group["platform"] == "direct" and group["phrases"]
    ]
    budget = Decimal(world.business.month_budget_kop) * Decimal("0.92") / 100
    forecast = direct_clicks(demo_forecast(groups, budget)[0], budget)
    assert client.post("/api/demo/advance", json={"days": 30}).status_code == 202
    clicks = sum(row.clicks for row in worker.repository.rows(StatsDaily))
    assert abs(clicks - forecast.max) < forecast.max * Decimal("0.15")
