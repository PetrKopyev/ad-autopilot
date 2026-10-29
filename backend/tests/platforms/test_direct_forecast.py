import json
from collections.abc import AsyncIterator, Iterator
from decimal import Decimal
from types import SimpleNamespace

import httpx
import pytest
import respx

from app.platforms.base import AuthExpired, PlatformError
from app.platforms.direct_forecast import (
    LIVE_URL,
    SANDBOX_URL,
    DirectForecastClient,
    ForecastAccessDenied,
    PhraseForecast,
)

PHRASE = {
    "Phrase": "маникюр самара",
    "Shows": 3000,
    "Clicks": 120,
    "FirstPlaceClicks": 180,
    "PremiumClicks": 400,
    "Min": 21.5,
    "Max": 38.2,
    "PremiumMin": 60,
    "PremiumMax": 90,
    "Currency": "RUB",
}


class Direct:
    """Live 4 по методу в теле: очередь ответов на каждый метод и журнал вызовов."""

    def __init__(self) -> None:
        self.answers: dict[str, list[dict]] = {}
        self.calls: list[dict] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        self.calls.append(body)
        return httpx.Response(200, json=self.answers[body["method"]].pop(0))

    def methods(self) -> list[str]:
        return [call["method"] for call in self.calls]


@pytest.fixture
def direct() -> Iterator[Direct]:
    handler = Direct()
    with respx.mock(assert_all_mocked=True) as router:
        router.post(LIVE_URL).mock(side_effect=handler)
        yield handler


@pytest.fixture
async def client() -> AsyncIterator[DirectForecastClient]:
    async with DirectForecastClient("test-only", config=SimpleNamespace(direct_sandbox=False), delay=0) as forecast:
        yield forecast


async def test_create_wait_get_delete(client: DirectForecastClient, direct: Direct) -> None:
    direct.answers = {
        "CreateNewForecast": [{"data": 7}],
        "GetForecastList": [
            {"data": [{"ForecastID": 7, "StatusForecast": "Pending"}]},
            {"data": [{"ForecastID": 7, "StatusForecast": "Done"}]},
        ],
        "GetForecast": [{"data": {"Phrases": [PHRASE], "Common": {"Clicks": 120}}}],
        "DeleteForecastReport": [{"data": 1}],
    }

    forecast_id = await client.create(["маникюр самара", "маникюр самара"], [51])
    assert await client.wait(forecast_id) == "Done"
    rows = await client.get(forecast_id)
    await client.delete(forecast_id)

    assert rows == [PhraseForecast("маникюр самара", 3000, 120, 180, Decimal("21.5"), Decimal("38.2"))]
    create = direct.calls[0]
    assert create["param"] == {"Phrases": ["маникюр самара"], "GeoID": [51], "Currency": "RUB"}
    assert create["token"] == "test-only" and create["locale"] == "ru"
    assert direct.methods() == [
        "CreateNewForecast",
        "GetForecastList",
        "GetForecastList",
        "GetForecast",
        "DeleteForecastReport",
    ]
    assert direct.calls[-1]["param"] == 7


async def test_wait_gives_up_while_pending(client: DirectForecastClient, direct: Direct) -> None:
    direct.answers = {"GetForecastList": [{"data": [{"ForecastID": 7, "StatusForecast": "Pending"}]}] * 3}

    assert await client.wait(7) == "Pending"
    assert direct.methods() == ["GetForecastList"] * 3


async def test_missing_report_is_failed(client: DirectForecastClient, direct: Direct) -> None:
    direct.answers = {"GetForecastList": [{"data": [{"ForecastID": 3, "StatusForecast": "Done"}]}]}

    assert await client.status(7) == "Failed"


async def test_access_not_approved(client: DirectForecastClient, direct: Direct) -> None:
    direct.answers = {"CreateNewForecast": [{"error_code": 58, "error_str": "Незавершенная регистрация"}]}

    with pytest.raises(ForecastAccessDenied, match="когда Яндекс одобрит доступ"):
        await client.create(["маникюр"], [51])


async def test_reports_limit_frees_finished_reports(client: DirectForecastClient, direct: Direct) -> None:
    direct.answers = {
        "CreateNewForecast": [{"error_code": 31, "error_str": "Превышен лимит"}, {"data": 9}],
        "GetForecastList": [
            {
                "data": [
                    {"ForecastID": 1, "StatusForecast": "Done"},
                    {"ForecastID": 2, "StatusForecast": "Pending"},
                    {"ForecastID": 3, "StatusForecast": "Failed"},
                ]
            }
        ],
        "DeleteForecastReport": [{"data": 1}, {"data": 1}],
    }

    assert await client.create(["маникюр"], [51]) == 9
    deleted = [call["param"] for call in direct.calls if call["method"] == "DeleteForecastReport"]
    assert deleted == [1, 3]


async def test_reports_limit_all_pending_frees_oldest(client: DirectForecastClient, direct: Direct) -> None:
    pending = [{"ForecastID": id_, "StatusForecast": "Pending"} for id_ in (8, 4, 6, 5, 7)]
    direct.answers = {
        "CreateNewForecast": [{"error_code": 31}, {"data": 9}],
        "GetForecastList": [{"data": pending}],
        "DeleteForecastReport": [{"data": 1}],
    }

    assert await client.create(["маникюр"], [51]) == 9
    assert [call["param"] for call in direct.calls if call["method"] == "DeleteForecastReport"] == [4]


@pytest.mark.parametrize(
    ("answer", "error"),
    [({"error_code": 53}, AuthExpired), ({"error_code": 71, "error_str": "Неверные параметры"}, PlatformError)],
)
async def test_errors(client: DirectForecastClient, direct: Direct, answer: dict, error: type) -> None:
    direct.answers = {"CreateNewForecast": [answer]}

    with pytest.raises(error):
        await client.create(["маникюр"], [51])


@pytest.mark.parametrize(
    "phrase", [PHRASE | {"Currency": "YND_FIXED"}, PHRASE | {"Clicks": -1}, PHRASE | {"Min": "много"}]
)
async def test_invalid_forecast(client: DirectForecastClient, direct: Direct, phrase: dict) -> None:
    direct.answers = {"GetForecast": [{"data": {"Phrases": [phrase]}}]}

    with pytest.raises(PlatformError, match="некорректный прогноз"):
        await client.get(7)


async def test_sandbox_and_network_failure() -> None:
    with respx.mock(assert_all_mocked=True) as router:
        route = router.post(SANDBOX_URL).mock(side_effect=httpx.ConnectError("down"))
        async with DirectForecastClient("test-only", config=SimpleNamespace(direct_sandbox=True)) as client:
            with pytest.raises(PlatformError, match="Не удалось связаться"):
                await client.statuses()
    assert route.called
