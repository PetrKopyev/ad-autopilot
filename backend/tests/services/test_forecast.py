import json
from collections.abc import AsyncIterator, Iterator
from datetime import timedelta
from decimal import Decimal
from types import SimpleNamespace

import httpx
import pytest
import respx

from app.content.yandexgpt import CompletionResult
from app.db.models import Business, LLMCall, Plan
from app.domain.settings import DomainSettings
from app.platforms.direct_forecast import LIVE_URL, PhraseForecast
from app.platforms.metrica import FakeMetrica, MetricaAccessError, MetricaCounter
from app.services import forecast as module
from app.services.forecast import (
    BENCHMARK,
    VK_NOTE,
    Clicks,
    ForecastService,
    ai_text,
    direct_clicks,
    leads,
    measured,
    numbers,
    template,
)
from app.services.registry import AccountInfo
from tests.platforms.test_direct_forecast import PHRASE, Direct
from tests.services.test_onboarding import LIMITS, NOW, TODAY, MemoryRepository

DOMAIN = DomainSettings(budget_reserve=Decimal("0.08"))
COUNTER = 777


def row(clicks: int, first: int, low: str, high: str) -> PhraseForecast:
    return PhraseForecast("фраза", clicks * 20, clicks, first, Decimal(low), Decimal(high))


@pytest.fixture(autouse=True)
def fresh_cache() -> Iterator[None]:
    yield
    module.clear()


def test_clicks_limited_by_budget() -> None:
    rows = [row(1000, 1500, "20", "40"), row(1000, 1500, "40", "60")]

    clicks = direct_clicks(rows, Decimal(30_000))

    # Вход: 30 000 / 30 = 1000 из 2000 возможных; первое место: 30 000 / 50 = 600 из 3000.
    assert clicks == Clicks(600, 1000, 3000, Decimal(30), Decimal(50))


def test_clicks_limited_by_potential() -> None:
    clicks = direct_clicks([row(100, 150, "20", "40")], Decimal(100_000))

    assert (clicks.min, clicks.max, clicks.potential) == (100, 150, 150)


def test_no_demand() -> None:
    clicks = direct_clicks([row(0, 0, "0", "0")], Decimal(30_000))

    assert clicks == Clicks(0, 0, 0, None, None)
    assert leads(clicks, BENCHMARK) == (0, 0)


def test_leads_range_rounds_outward() -> None:
    assert leads(Clicks(250, 410, 500, Decimal(30), Decimal(50)), BENCHMARK) == (2, 13)


def test_measured_conversion_needs_enough_visits() -> None:
    assert measured(99, 5) is None
    conversion = measured(400, 12)
    assert conversion.source == "metrica"
    assert conversion.min < Decimal("0.03") < conversion.max
    assert "12 обращений на 400 визитов" in conversion.label
    assert measured(150, 0).min == 0


def test_ai_text_without_new_numbers() -> None:
    clicks = Clicks(600, 1000, 3000, Decimal(30), Decimal(50))
    text = template(clicks, Decimal(27_600), BENCHMARK, leads(clicks, BENCHMARK))
    allowed = numbers(text)
    assert {Decimal(3000), Decimal(27600), Decimal(600), Decimal(1000), Decimal(6), Decimal(30)} <= allowed

    good = "Яндекс видит до 3 000 кликов в месяц, на ваш бюджет 27 600 ₽ — от 600 до 1 000. Это прогноз."
    assert ai_text(json.dumps({"text": good}), allowed) == good
    assert ai_text(json.dumps({"text": "Будет 45 обращений."}), allowed) is None
    assert ai_text(json.dumps({"text": "Конверсия 2,5%."}), allowed) is None
    assert ai_text("не JSON", allowed) is None
    assert ai_text(json.dumps({"text": "а" * 900}), allowed) is None


class Geo:
    async def geo_targeting(self, city: str) -> dict:
        return {"region_ids": [51]}


class Writer:
    def __init__(self, text: str) -> None:
        self.text = text
        self.calls = 0

    async def complete_json(self, system: str, user: str, temperature: float = 0.2) -> str:
        self.calls += 1
        text = json.dumps({"text": self.text}, ensure_ascii=False)
        return CompletionResult(
            text, model="yandexgpt", model_version="", tokens_in=100, tokens_out=50, cost_kop=12, status="ok"
        )


@pytest.fixture
def direct() -> Iterator[Direct]:
    handler = Direct()
    with respx.mock(assert_all_mocked=True, assert_all_called=False) as router:
        router.post(LIVE_URL).mock(side_effect=handler)
        yield handler


@pytest.fixture
async def http() -> AsyncIterator[httpx.AsyncClient]:
    async with httpx.AsyncClient() as client:
        yield client


def world(
    http: httpx.AsyncClient,
    *,
    mode: str = "live",
    platforms: tuple[str, ...] = ("direct", "vk"),
    metrica: FakeMetrica | None = None,
    llm: Writer | None = None,
    goals: list[int] | None = None,
) -> SimpleNamespace:
    repository = MemoryRepository()
    business = Business(
        user_id=1,
        site_url="https://salon.example/",
        city="Самара",
        month_budget_kop=6_000_000,
        status="plan_ready",
        metrica_counter_id=COUNTER if goals else None,
        lead_goal_ids=goals or [],
    )
    repository.add(business)
    groups = [
        {"platform": "direct", "phrases": ["маникюр самара"]},
        {"platform": "direct", "phrases": ["маникюр самара"]},
        {"platform": "vk", "phrases": []},
    ]
    payload = {
        "city": "Самара",
        "month_budget_kop": 6_000_000,
        "platforms": list(platforms),
        "groups": [group for group in groups if group["platform"] in platforms],
    }
    repository.add(Plan(business_id=business.id, version=1, status="draft", payload=payload))
    registry = SimpleNamespace(
        mode=mode,
        config=SimpleNamespace(direct_sandbox=False),
        http=http,
        load=lambda business_id, platform: AccountInfo("ok", "", None, False, "test-only"),
        resolve=lambda business_id, platform: Geo(),
        metrica=lambda business_id: metrica or FakeMetrica(),
    )
    service = ForecastService(repository, registry, llm, LIMITS, DOMAIN)
    return SimpleNamespace(service=service, business=business, repository=repository)


def done(direct: Direct, forecast_id: int = 7, phrase: dict = PHRASE) -> None:
    direct.answers = {
        "CreateNewForecast": [{"data": forecast_id}],
        "GetForecastList": [{"data": [{"ForecastID": forecast_id, "StatusForecast": "Done"}]}],
        "GetForecast": [{"data": {"Phrases": [phrase]}}],
        "DeleteForecastReport": [{"data": 1}],
    }


async def test_ready_forecast_is_cached(http: httpx.AsyncClient, direct: Direct) -> None:
    done(direct)
    case = world(http)

    result = await case.service.forecast(case.business, NOW, TODAY)

    # Директу — 70% из 60 000 ₽, без запаса 8%: 38 640 ₽. Потенциал фразы меньше бюджета: 120 и 180 кликов.
    assert result["status"] == "ready"
    assert (result["clicks_min"], result["clicks_max"]) == (120, 180)
    assert (result["leads_min"], result["leads_max"]) == (1, 6)
    assert (result["cpc_rub"], result["cpc_rub_max"], result["budget_rub"]) == (21.5, 38.2, 38640.0)
    assert result["conversion"] == {
        "min": 0.01,
        "max": 0.03,
        "source": "benchmark",
        "label": "ориентир по рынку услуг, не измерение",
    }
    assert [source["what"] for source in result["sources"]] == ["Клики и цена клика", "Бюджет", "Доля обращений"]
    assert result["sources"][2]["url"] == BENCHMARK.url
    assert result["vk"] == {"status": "unavailable", "label": VK_NOTE}
    assert result["computed_at"] == NOW.isoformat() and result["demo"] is False
    assert "Это прогноз, он может ошибаться" in result["explanation"] and not result["explanation_by_ai"]
    assert direct.calls[0]["param"]["Phrases"] == ["маникюр самара"]
    assert direct.calls[0]["param"]["GeoID"] == [51]

    calls = len(direct.calls)
    assert await case.service.forecast(case.business, NOW + timedelta(hours=1), TODAY) == result
    assert len(direct.calls) == calls


async def test_changed_budget_asks_again(http: httpx.AsyncClient, direct: Direct) -> None:
    done(direct)
    case = world(http)
    await case.service.forecast(case.business, NOW, TODAY)
    done(direct, 8)
    case.repository.rows(Plan)[0].payload["month_budget_kop"] = 9_000_000

    result = await case.service.forecast(case.business, NOW, TODAY)

    assert result["budget_rub"] == 57960.0
    assert direct.methods().count("CreateNewForecast") == 2


async def test_pending_then_ready(http: httpx.AsyncClient, direct: Direct) -> None:
    pending = {"data": [{"ForecastID": 7, "StatusForecast": "Pending"}]}
    direct.answers = {
        "CreateNewForecast": [{"data": 7}],
        "GetForecastList": [pending] * 3 + [{"data": [{"ForecastID": 7, "StatusForecast": "Done"}]}],
        "GetForecast": [{"data": {"Phrases": [PHRASE]}}],
        "DeleteForecastReport": [{"data": 1}],
    }
    case = world(http)
    case.service.poll_delay = 0

    first = await case.service.forecast(case.business, NOW, TODAY)
    second = await case.service.forecast(case.business, NOW + timedelta(seconds=10), TODAY)

    assert first["status"] == "pending" and first["leads_min"] is None
    assert second["status"] == "ready"
    assert direct.methods().count("CreateNewForecast") == 1
    assert direct.methods()[-1] == "DeleteForecastReport"


async def test_access_not_approved_is_honest_and_cached(http: httpx.AsyncClient, direct: Direct) -> None:
    direct.answers = {"CreateNewForecast": [{"error_code": 58, "error_str": "Незавершенная регистрация"}]}
    case = world(http)

    result = await case.service.forecast(case.business, NOW, TODAY)
    again = await case.service.forecast(case.business, NOW + timedelta(minutes=30), TODAY)

    assert result["status"] == "unavailable" and result["leads_min"] is None
    assert result["explanation"] == "Прогноз появится, когда Яндекс одобрит доступ к API Директа."
    assert again == result and len(direct.calls) == 1


async def test_failed_report_is_removed(http: httpx.AsyncClient, direct: Direct) -> None:
    direct.answers = {
        "CreateNewForecast": [{"data": 7}],
        "GetForecastList": [{"data": [{"ForecastID": 7, "StatusForecast": "Failed"}]}],
        "DeleteForecastReport": [{"data": 1}],
    }
    case = world(http)

    result = await case.service.forecast(case.business, NOW, TODAY)

    assert result["status"] == "unavailable" and "не смог посчитать" in result["explanation"]
    assert direct.methods()[-1] == "DeleteForecastReport"


async def test_conversion_from_metrica(http: httpx.AsyncClient, direct: Direct) -> None:
    done(direct, phrase=PHRASE | {"Clicks": 1000, "FirstPlaceClicks": 1200})
    metrica = FakeMetrica([MetricaCounter(COUNTER, "Сайт", "salon.example")], conversions={COUNTER: (400, 20)})
    case = world(http, metrica=metrica, goals=[5])

    result = await case.service.forecast(case.business, NOW, TODAY)

    conversion = result["conversion"]
    assert conversion["source"] == "metrica" and conversion["min"] < 0.05 < conversion["max"]
    assert "20 обращений на 400 визитов" in conversion["label"]
    assert result["leads_min"] == int(result["clicks_min"] * conversion["min"])


async def test_metrica_failure_falls_back_to_benchmark(http: httpx.AsyncClient, direct: Direct) -> None:
    done(direct)

    class Broken(FakeMetrica):
        async def get_ad_conversions(self, counter_id: int, goal_ids: list[int]) -> tuple[int, int]:
            raise MetricaAccessError()

    case = world(http, metrica=Broken(), goals=[5])

    assert (await case.service.forecast(case.business, NOW, TODAY))["conversion"]["source"] == "benchmark"


async def test_ai_explanation_is_used_only_without_new_numbers(http: httpx.AsyncClient, direct: Direct) -> None:
    done(direct)
    writer = Writer("Яндекс видит в Самаре до 180 кликов в месяц. Это прогноз, он может ошибаться.")
    case = world(http, llm=writer)

    result = await case.service.forecast(case.business, NOW, TODAY)

    assert result["explanation_by_ai"] and result["explanation"].startswith(writer.text)
    assert "может ошибаться" in result["explanation"]
    assert [call.purpose for call in case.repository.rows(LLMCall)] == ["forecast"]

    module.clear()
    done(direct, 8)
    writer.text = "Ждите 25 обращений в месяц."
    result = await case.service.forecast(case.business, NOW, TODAY)
    assert not result["explanation_by_ai"] and "25" not in result["explanation"]


async def test_demo_forecast_without_yandex(http: httpx.AsyncClient, direct: Direct) -> None:
    case = world(http, mode="fake")

    result = await case.service.forecast(case.business, NOW, TODAY)

    assert result["status"] == "ready" and result["demo"] is True
    assert result["clicks_max"] > 0 and result["leads_max"] > 0
    assert result["sources"][0]["source"].startswith("Демо") and result["sources"][0]["url"] is None
    assert not direct.calls


async def test_vk_only_plan(http: httpx.AsyncClient, direct: Direct) -> None:
    case = world(http, platforms=("vk",))

    result = await case.service.forecast(case.business, NOW, TODAY)

    assert result["status"] == "unavailable" and result["vk"]["label"] == VK_NOTE
    assert not direct.calls


async def test_no_plan(http: httpx.AsyncClient) -> None:
    case = world(http)
    case.repository.tables[Plan].clear()

    result = await case.service.forecast(case.business, NOW, TODAY)

    assert result["status"] == "unavailable" and result["vk"] is None
