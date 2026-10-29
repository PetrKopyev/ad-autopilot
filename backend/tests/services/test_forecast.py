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
    assert "12 из 400 визитов с рекламы Яндекс Директа (поиск и сети)" in conversion.label
    assert measured(150, 0).min == 0


def test_ai_text_without_new_numbers() -> None:
    clicks = Clicks(600, 1000, 3000, Decimal(30), Decimal(50))
    text = template(clicks, Decimal(27_600), BENCHMARK, leads(clicks, BENCHMARK))
    allowed = numbers(text)
    assert {Decimal(3000), Decimal(27600), Decimal(600), Decimal(1000), Decimal(6), Decimal(30)} <= allowed

    range_ = leads(clicks, BENCHMARK)
    assert range_ == (6, 30)
    good = "Яндекс видит до 3 000 кликов в месяц, на ваш бюджет 27 600 ₽ — от 600 до 1 000. Обращений от 6 до 30."
    assert ai_text(json.dumps({"text": good}), allowed, range_) == good
    assert ai_text(json.dumps({"text": "Будет 45 обращений."}), allowed, range_) is None
    assert ai_text(json.dumps({"text": "Конверсия 2,5%."}), allowed, range_) is None
    assert ai_text("не JSON", allowed, range_) is None
    assert ai_text(json.dumps({"text": "а" * 900}), allowed, range_) is None


def test_ai_text_must_name_leads_range_and_no_other_number_of_leads() -> None:
    clicks = Clicks(600, 1000, 3000, Decimal(30), Decimal(50))
    allowed = numbers(template(clicks, Decimal(27_600), BENCHMARK, (6, 30)))

    def check(text: str) -> str | None:
        return ai_text(json.dumps({"text": text}), allowed, (6, 30))

    assert check("До 1 000 кликов в месяц, это от 6 до 30 обращений.") is not None
    assert check("До 1 000 кликов в месяц. Это прогноз.") is None
    assert check("От 6 до 30 кликов, а обращений 600.") is None
    assert check("Ждите 1 000 обращений, а в плохой месяц от 6 до 30.") is None


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
    accounts = {"direct": AccountInfo("ok", "", None, False, "test-only")}
    registry = SimpleNamespace(
        mode=mode,
        config=SimpleNamespace(direct_sandbox=False),
        http=http,
        load=lambda business_id, platform: accounts[platform],
        resolve=lambda business_id, platform: Geo(),
        metrica=lambda business_id: metrica or FakeMetrica(),
    )
    service = ForecastService(repository, registry, llm, LIMITS, DOMAIN)
    return SimpleNamespace(service=service, business=business, repository=repository, accounts=accounts)


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

    pending = await case.service.forecast(case.business, NOW, TODAY)
    # Первый запрос только заказывает отчёт и не ждёт его: готовность спросит следующий опрос экрана.
    assert pending["status"] == "pending" and direct.methods() == ["CreateNewForecast"]
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
    assert result["sources"][2]["source"] == (
        "ориентир по рынку услуг, не измерение, на визит. Своей доли нет: в Метрике не выбраны цели обращений."
    )
    assert "визитов на сайт бывает меньше, чем кликов" in result["explanation"]
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
    await case.service.forecast(case.business, NOW, TODAY)
    done(direct, 8)
    case.repository.rows(Plan)[0].payload["month_budget_kop"] = 9_000_000

    await case.service.forecast(case.business, NOW, TODAY)
    result = await case.service.forecast(case.business, NOW, TODAY)

    assert result["budget_rub"] == 57960.0
    assert direct.methods().count("CreateNewForecast") == 2


async def test_pending_then_ready(http: httpx.AsyncClient, direct: Direct) -> None:
    pending = {"data": [{"ForecastID": 7, "StatusForecast": "Pending"}]}
    direct.answers = {
        "CreateNewForecast": [{"data": 7}],
        "GetForecastList": [pending, {"data": [{"ForecastID": 7, "StatusForecast": "Done"}]}],
        "GetForecast": [{"data": {"Phrases": [PHRASE]}}],
        "DeleteForecastReport": [{"data": 1}],
    }
    case = world(http)

    first = await case.service.forecast(case.business, NOW, TODAY)
    second = await case.service.forecast(case.business, NOW + timedelta(seconds=5), TODAY)
    third = await case.service.forecast(case.business, NOW + timedelta(seconds=10), TODAY)

    assert first["status"] == second["status"] == "pending" and first["leads_min"] is None
    assert third["status"] == "ready"
    assert direct.methods() == [
        "CreateNewForecast",
        "GetForecastList",
        "GetForecastList",
        "GetForecast",
        "DeleteForecastReport",
    ]
    assert case.service.client(case.accounts["direct"], case.business.id)._timeout == module.DIRECT_TIMEOUT == 10.0


async def test_report_no_longer_needed_is_deleted(http: httpx.AsyncClient, direct: Direct) -> None:
    direct.answers = {
        "CreateNewForecast": [{"data": 7}, {"data": 8}, {"data": 9}],
        "GetForecastList": [{"error_code": 500, "error_str": "Сбой"}],
        "DeleteForecastReport": [{"data": 1}] * 3,
    }
    case = world(http)

    await case.service.forecast(case.business, NOW, TODAY)
    case.repository.rows(Plan)[0].payload["month_budget_kop"] = 9_000_000
    await case.service.forecast(case.business, NOW, TODAY)
    await case.service.forecast(case.business, NOW + module.TTL["pending"], TODAY)
    failed = await case.service.forecast(case.business, NOW + module.TTL["pending"], TODAY)

    # Заменённый входом, истёкший и упавший на ошибке отчёты удалены — места в Директе не утекают.
    deleted = [call["param"] for call in direct.calls if call["method"] == "DeleteForecastReport"]
    assert deleted == [7, 8, 9] and not module.OWNED[case.business.id]
    assert failed["status"] == "unavailable" and "Сбой" in failed["explanation"]


async def test_report_of_old_connection_is_deleted_with_old_token(http: httpx.AsyncClient, direct: Direct) -> None:
    direct.answers = {"CreateNewForecast": [{"data": 7}, {"data": 8}], "DeleteForecastReport": [{"data": 1}]}
    case = world(http)
    await case.service.forecast(case.business, NOW, TODAY)

    case.accounts["direct"] = AccountInfo("ok", "", None, False, "new-token")
    result = await case.service.forecast(case.business, NOW, TODAY)

    delete = next(call for call in direct.calls if call["method"] == "DeleteForecastReport")
    assert (delete["param"], delete["token"]) == (7, "test-only")
    assert result["status"] == "pending" and direct.calls[-1]["token"] == "new-token"
    assert module.OWNED[case.business.id] == {8}


async def test_reports_limit_spares_neighbour_with_same_cabinet(http: httpx.AsyncClient, direct: Direct) -> None:
    direct.answers = {"CreateNewForecast": [{"data": 7}, {"error_code": 31}]}
    first, second = world(http), world(http)
    second.business.id = second.repository.rows(Plan)[0].business_id = first.business.id + 1
    await first.service.forecast(first.business, NOW, TODAY)
    reports = [{"ForecastID": id_, "StatusForecast": "Done"} for id_ in range(3, 8)]
    direct.answers["GetForecastList"] = [{"data": reports}]

    result = await second.service.forecast(second.business, NOW, TODAY)

    # Готовый отчёт 7 заказан для соседнего бизнеса: его ждёт следующий опрос соседа, удалять его нельзя.
    assert "DeleteForecastReport" not in direct.methods()
    assert result["status"] == "unavailable" and "все пять мест под отчёты прогноза" in result["explanation"]
    assert module.OWNED[first.business.id] == {7}


async def test_access_not_approved_is_honest_and_cached(http: httpx.AsyncClient, direct: Direct) -> None:
    direct.answers = {"CreateNewForecast": [{"error_code": 58, "error_str": "Незавершенная регистрация"}]}
    case = world(http)

    result = await case.service.forecast(case.business, NOW, TODAY)
    again = await case.service.forecast(case.business, NOW + timedelta(minutes=30), TODAY)

    assert result["status"] == "unavailable" and result["leads_min"] is None
    assert result["explanation"] == "Прогноз появится, когда Яндекс одобрит доступ к API Директа."
    assert again == result and len(direct.calls) == 1

    # Кабинет подключили заново: прежний отказ не ждёт истечения часа.
    case.accounts["direct"] = AccountInfo("ok", "", None, False, "new-token")
    done(direct)
    fresh = await case.service.forecast(case.business, NOW + timedelta(minutes=31), TODAY)
    assert fresh["status"] == "pending" and direct.calls[-1]["token"] == "new-token"


async def test_failed_report_is_removed(http: httpx.AsyncClient, direct: Direct) -> None:
    direct.answers = {
        "CreateNewForecast": [{"data": 7}],
        "GetForecastList": [{"data": [{"ForecastID": 7, "StatusForecast": "Failed"}]}],
        "DeleteForecastReport": [{"data": 1}],
    }
    case = world(http)

    await case.service.forecast(case.business, NOW, TODAY)
    result = await case.service.forecast(case.business, NOW, TODAY)

    assert result["status"] == "unavailable" and "не смог посчитать" in result["explanation"]
    assert direct.methods()[-1] == "DeleteForecastReport"


async def test_conversion_from_metrica(http: httpx.AsyncClient, direct: Direct) -> None:
    done(direct, phrase=PHRASE | {"Clicks": 1000, "FirstPlaceClicks": 1200})
    metrica = FakeMetrica([MetricaCounter(COUNTER, "Сайт", "salon.example")], conversions={COUNTER: (400, 20)})
    case = world(http, metrica=metrica, goals=[5])

    await case.service.forecast(case.business, NOW, TODAY)
    result = await case.service.forecast(case.business, NOW, TODAY)

    conversion = result["conversion"]
    assert conversion["source"] == "metrica" and conversion["min"] < 0.05 < conversion["max"]
    assert "20 из 400 визитов с рекламы Яндекс Директа" in conversion["label"]
    assert result["sources"][2]["source"] == f"{conversion['label']}, на визит."
    assert result["leads_min"] == int(result["clicks_min"] * conversion["min"])


async def test_metrica_failure_falls_back_to_benchmark(
    http: httpx.AsyncClient, direct: Direct, caplog: pytest.LogCaptureFixture
) -> None:
    done(direct)

    class Broken(FakeMetrica):
        async def get_ad_conversions(self, counter_id: int, goal_ids: list[int]) -> tuple[int, int]:
            raise MetricaAccessError()

    case = world(http, metrica=Broken(), goals=[5])

    await case.service.forecast(case.business, NOW, TODAY)
    result = await case.service.forecast(case.business, NOW, TODAY)

    assert result["conversion"]["source"] == "benchmark"
    reason = "Свою долю по Метрике посчитать не удалось: Метрика ответила ошибкой."
    assert reason in result["sources"][2]["source"] and reason in result["explanation"]
    # Текст ошибки Метрики пользователю не показывается — только в журнале.
    assert "Нет доступа" not in result["explanation"] and "Нет доступа" not in result["sources"][2]["source"]
    assert "Нет доступа к данным Яндекс Метрики" in caplog.text


async def test_few_metrica_visits_say_why_benchmark(http: httpx.AsyncClient, direct: Direct) -> None:
    done(direct)
    metrica = FakeMetrica([MetricaCounter(COUNTER, "Сайт", "salon.example")], conversions={COUNTER: (57, 3)})
    case = world(http, metrica=metrica, goals=[5])

    await case.service.forecast(case.business, NOW, TODAY)
    result = await case.service.forecast(case.business, NOW, TODAY)

    assert result["conversion"]["source"] == "benchmark"
    assert "за 90 дней 57 визитов с рекламы Яндекс Директа, а нужно не меньше 100" in result["sources"][2]["source"]


async def test_ai_explanation_is_used_only_without_new_numbers(http: httpx.AsyncClient, direct: Direct) -> None:
    done(direct)
    writer = Writer("В Самаре до 180 кликов в месяц, обращений от 1 до 6. Это прогноз, он может ошибаться.")
    case = world(http, llm=writer)

    await case.service.forecast(case.business, NOW, TODAY)
    result = await case.service.forecast(case.business, NOW, TODAY)

    assert result["explanation_by_ai"] and result["explanation"].startswith(writer.text)
    assert "может ошибаться" in result["explanation"]
    assert [call.purpose for call in case.repository.rows(LLMCall)] == ["forecast"]

    module.clear()
    done(direct, 8)
    writer.text = "Ждите 25 обращений в месяц."
    await case.service.forecast(case.business, NOW, TODAY)
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
