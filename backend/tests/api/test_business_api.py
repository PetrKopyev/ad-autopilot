from collections import deque

import pytest

from app.db.models import Campaign, PlatformAccount


def new_business(client, **changes):
    body = {"site_url": "https://salon.example/", "city": "Самара", "month_budget_kop": 1_500_000} | changes
    return client.post("/api/business", json=body)


@pytest.mark.parametrize(
    "budget", [1500.5, "1500000", None, True, 0, -100], ids=["float", "string", "null", "bool", "zero", "negative"]
)
def test_budget_must_be_whole_positive_kopecks(api, budget) -> None:
    client, context = api
    response = new_business(client, month_budget_kop=budget)
    assert response.status_code == 422
    assert context.world.business.month_budget_kop != budget


def test_budget_below_direct_minimum_is_rejected_with_the_minimum(api) -> None:
    client, context = api
    response = new_business(client, month_budget_kop=1_269_999)
    assert response.status_code == 422
    assert "Минимальный бюджет — 12 700 ₽ в месяц" in response.json()["detail"]
    assert context.world.business.month_budget_kop != 1_269_999


@pytest.mark.parametrize(
    "query,expected",
    [
        (
            "month_budget_kop=3000000&vk=true",
            {
                "daily_kop": {"direct": 71_225},
                "off": [{"platform": "vk", "from_kop": 4_850_000, "min_daily_kop": 30_000}],
            },
        ),
        ("month_budget_kop=3000000&vk=false", {"daily_kop": {"direct": 71_225}, "off": []}),
        ("month_budget_kop=4850000&vk=true", {"daily_kop": {"direct": 80_604, "vk": 34_544}, "off": []}),
    ],
)
def test_budget_hint_uses_plan_rules(api, query, expected) -> None:
    client, _ = api
    response = client.get(f"/api/business/budget-hint?{query}")
    assert response.status_code == 200, response.text
    assert response.json() == {"minimum_kop": 1_270_000} | expected


def test_budget_hint_requires_login_and_positive_amount(api) -> None:
    client, _ = api
    assert client.get("/api/business/budget-hint?month_budget_kop=0").status_code == 422
    client.cookies.clear()
    assert client.get("/api/business/budget-hint?month_budget_kop=3000000").status_code == 401


def test_budget_is_stored_in_kopecks(api) -> None:
    client, context = api
    response = new_business(client, month_budget_kop=2_234_567)
    assert response.status_code == 200, response.text
    assert response.json()["month_budget_kop"] == 2_234_567
    assert context.world.business.month_budget_kop == 2_234_567


@pytest.mark.parametrize(
    "method,path,body",
    [
        ("POST", "/api/business", {"site_url": "https://a.example/", "city": "Самара", "month_budget_kop": 100_000}),
        ("PUT", "/api/business/description", {"description": {}, "approved": False}),
        ("PUT", "/api/business/metrica", {"counter_id": 12345, "lead_goal_ids": [1]}),
        (
            "PUT",
            "/api/business/marking",
            {"subject_type": "ip", "inn": "526317984689", "name": "ИП", "phone": "89001234567"},
        ),
    ],
)
def test_business_changes_need_login_and_csrf(api, method, path, body) -> None:
    client, _ = api
    del client.headers["X-CSRF-Token"]
    assert client.request(method, path, json=body).status_code == 403
    client.cookies.clear()
    assert client.request(method, path, json=body).status_code == 401


def test_site_check_of_foreign_business_is_not_found(api) -> None:
    client, context = api
    response = client.get("/api/business/site-check", params={"business_id": context.other.id})
    assert response.status_code == 404
    assert client.get("/api/business/site-check").status_code == 200


def test_site_check_is_rate_limited_per_user_and_ip(api, monkeypatch) -> None:
    client, context = api
    from app.api import limits

    now = [1000.0]
    monkeypatch.setattr(limits, "monotonic", lambda: now[0])
    calls = []

    async def site_check(*args, on_miss, refresh):
        on_miss()
        calls.append(args)
        return {}

    monkeypatch.setattr(context.world.onboarding, "site_check", site_check)
    statuses = [client.get("/api/business/site-check").status_code for _ in range(6)]
    assert statuses == [200] * 5 + [429] and len(calls) == 5
    response = client.get("/api/business/site-check")
    assert "Сайт проверяется слишком часто" in response.json()["detail"]
    client.cookies.set("__Host-session", "other-session")
    assert client.get("/api/business/site-check").status_code == 200
    now[0] += 300
    client.cookies.set("__Host-session", "test-session")
    assert client.get("/api/business/site-check").status_code == 200
    limits.site_check_limit.hits["ip:testclient"].extend([now[0]] * 20)
    client.cookies.set("__Host-session", "other-session")
    assert client.get("/api/business/site-check").status_code == 429


def test_site_check_answered_from_cache_does_not_spend_the_limit(api, monkeypatch) -> None:
    """Шаг 3 мастера запрашивает проверку при каждом входе; чтение сайта одно, остальное — из кэша."""
    client, context = api
    reads = []
    reader = context.world.onboarding.reader

    async def counting(url):
        reads.append(url)
        return await reader(url)

    monkeypatch.setattr(context.world.onboarding, "reader", counting)
    statuses = [client.get("/api/business/site-check").status_code for _ in range(10)]
    assert statuses == [200] * 10 and len(reads) <= 1


def test_site_check_retry_reads_site_again_and_spends_the_limit(api, monkeypatch) -> None:
    """Кнопка «Попробовать ещё раз» читает сайт мимо кэша, обычный вход на шаг берёт кэш."""
    client, context = api
    reads = []
    reader = context.world.onboarding.reader

    async def counting(url):
        reads.append(url)
        return await reader(url)

    monkeypatch.setattr(context.world.onboarding, "reader", counting)
    assert client.get("/api/business/site-check").status_code == 200
    assert client.get("/api/business/site-check").status_code == 200
    assert len(reads) == 1
    assert client.get("/api/business/site-check", params={"refresh": 1}).status_code == 200
    assert len(reads) == 2
    from app.api import limits

    assert len(limits.site_check_limit.hits["ip:testclient"]) == 2


def test_site_check_limit_is_per_ip_too(api, monkeypatch) -> None:
    client, context = api
    from app.api import limits

    async def site_check(*args, on_miss, refresh):
        on_miss()
        return {}

    monkeypatch.setattr(context.world.onboarding, "site_check", site_check)
    limits.site_check_limit.hits["ip:testclient"] = deque([limits.monotonic()] * 20)
    response = client.get("/api/business/site-check")
    assert response.status_code == 429 and int(response.headers["Retry-After"]) > 0


def test_launch_with_vk_only_is_blocked_until_direct_is_connected(api) -> None:
    client, context = api
    repository = context.world.repository
    direct = next(item for item in repository.rows(PlatformAccount) if item.platform == "direct")
    direct.status = "needs_reauth"
    response = client.post("/api/plan/launch", json={"plan_id": context.plan["id"]})
    assert response.status_code == 409 and "Директ" in response.json()["detail"]
    assert not repository.rows(Campaign) and context.world.business.status == "plan_ready"


def test_after_launch_wizard_explains_metrica_and_settings_keep_saving_description_and_marking(api) -> None:
    client, context = api
    business = context.world.business
    business.status = "running"
    response = client.put("/api/business/metrica", json={"counter_id": 12345, "lead_goal_ids": [1]})
    assert response.status_code == 409
    assert response.json()["detail"] == "Реклама уже запущена. Счётчик и цели Метрики после запуска не меняются."
    description = {"description": {"services": {"value": "Педикюр"}}, "approved": True}
    assert client.put("/api/business/description", json=description).status_code == 200
    marking = {"subject_type": "ooo", "inn": "7707083893", "name": "ООО Ромашка", "phone": "89001234567"}
    assert client.put("/api/business/marking", json=marking).status_code == 200
    assert business.description["fields"]["services"]["value"] == "Педикюр"
    assert context.world.repository.marking(business.id).name == "ООО Ромашка"
