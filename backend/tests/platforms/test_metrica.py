import json
from collections.abc import AsyncIterator, Iterator

import httpx
import pytest
import respx

from app.platforms.metrica import (
    BASE_URL,
    FakeMetrica,
    Metrica,
    MetricaAccessError,
    MetricaAuthError,
    MetricaClient,
    MetricaCounter,
    MetricaError,
    MetricaGoal,
    MetricaResponseError,
)


@pytest.fixture(autouse=True)
def mock_api() -> Iterator[respx.MockRouter]:
    with respx.mock(assert_all_mocked=True) as router:
        yield router


@pytest.fixture
async def metrica() -> AsyncIterator[MetricaClient]:
    async with MetricaClient("test-only") as client:
        yield client


async def test_list_counters_paginates(metrica: MetricaClient, mock_api: respx.MockRouter) -> None:
    first = mock_api.get(f"{BASE_URL}/management/v1/counters", params={"offset": 1, "per_page": 1000}).respond(
        json={"rows": 2, "counters": [{"id": 1, "name": "Первый", "site2": {"site": "first.example"}}]}
    )
    second = mock_api.get(f"{BASE_URL}/management/v1/counters", params={"offset": 2, "per_page": 1000}).respond(
        json={"rows": 2, "counters": [{"id": 2, "name": "Второй", "site": "second.example"}]}
    )

    assert await metrica.list_counters() == [
        MetricaCounter(1, "Первый", "first.example"),
        MetricaCounter(2, "Второй", "second.example"),
    ]
    assert first.call_count == second.call_count == 1
    for call in mock_api.calls:
        assert call.request.headers["Authorization"] == "OAuth test-only"
        assert call.request.headers["Accept"] == "application/json"
        assert call.request.extensions["timeout"]["read"] == 10.0
        assert "test-only" not in str(call.request.url)


async def test_list_counters_empty(metrica: MetricaClient, mock_api: respx.MockRouter) -> None:
    mock_api.get(f"{BASE_URL}/management/v1/counters").respond(json={"rows": 0, "counters": []})

    assert await metrica.list_counters() == []


@pytest.mark.parametrize("goals", [[], [{"id": 12, "name": "Заявка", "type": "action", "default_price": 10.5}]])
async def test_list_goals(metrica: MetricaClient, mock_api: respx.MockRouter, goals: list[dict]) -> None:
    route = mock_api.get(f"{BASE_URL}/management/v1/counter/1/goals").respond(json={"goals": goals})

    expected = [MetricaGoal(12, "Заявка", "action")] if goals else []
    assert await metrica.list_goals(1) == expected
    assert route.calls.last.request.headers["Authorization"] == "OAuth test-only"


@pytest.mark.parametrize("visits", [0, 0.0, 1, 23.0])
async def test_visits_and_liveness(metrica: MetricaClient, mock_api: respx.MockRouter, visits: int | float) -> None:
    route = mock_api.get(
        f"{BASE_URL}/stat/v1/data",
        params={"ids": 1, "metrics": "ym:s:visits", "date1": "6daysAgo", "date2": "today"},
    ).respond(json={"totals": [visits], "data": []})

    result = await metrica.get_visits(1)
    assert result == visits
    assert type(result) is int
    assert await metrica.is_alive(1) is (visits > 0)
    assert route.calls.last.request.headers["Authorization"] == "OAuth test-only"


@pytest.mark.parametrize(
    "method,path,args",
    [
        ("list_counters", "/management/v1/counters", ()),
        ("list_goals", "/management/v1/counter/1/goals", (1,)),
        ("is_alive", "/stat/v1/data", (1,)),
    ],
)
@pytest.mark.parametrize("status,error", [(401, MetricaAuthError), (403, MetricaAccessError)])
async def test_auth_errors(
    metrica: MetricaClient,
    mock_api: respx.MockRouter,
    method: str,
    path: str,
    args: tuple,
    status: int,
    error: type[MetricaError],
) -> None:
    mock_api.get(f"{BASE_URL}{path}").respond(status, json={"message": "test-only"})

    with pytest.raises(error) as caught:
        await getattr(metrica, method)(*args)

    assert caught.value.status_code == status
    assert "test-only" not in str(caught.value)
    assert "заново" in str(caught.value) if status == 401 else "metrika:read" in str(caught.value)


@pytest.mark.parametrize("status", [400, 404, 429, 500, 503])
async def test_other_http_errors(metrica: MetricaClient, mock_api: respx.MockRouter, status: int) -> None:
    mock_api.get(f"{BASE_URL}/stat/v1/data").respond(status, text="test-only")

    with pytest.raises(MetricaError) as caught:
        await metrica.is_alive(1)

    assert caught.value.status_code == status
    assert str(status) in str(caught.value)
    assert "test-only" not in str(caught.value)


@pytest.mark.parametrize("error", [httpx.ConnectError, httpx.ReadTimeout])
async def test_network_errors(
    metrica: MetricaClient, mock_api: respx.MockRouter, error: type[httpx.RequestError]
) -> None:
    mock_api.get(f"{BASE_URL}/stat/v1/data").mock(side_effect=error("test-only"))

    with pytest.raises(MetricaError, match="Не удалось связаться") as caught:
        await metrica.is_alive(1)

    assert caught.value.status_code is None
    assert caught.value.__suppress_context__


async def test_redirect_is_not_followed(metrica: MetricaClient, mock_api: respx.MockRouter) -> None:
    mock_api.get(f"{BASE_URL}/management/v1/counters").respond(302, headers={"Location": "https://other.example"})

    with pytest.raises(MetricaError) as caught:
        await metrica.list_counters()

    assert caught.value.status_code == 302
    assert len(mock_api.calls) == 1


async def test_invalid_json(metrica: MetricaClient, mock_api: respx.MockRouter) -> None:
    mock_api.get(f"{BASE_URL}/stat/v1/data").respond(text="not json")

    with pytest.raises(MetricaResponseError):
        await metrica.is_alive(1)


@pytest.mark.parametrize(
    "totals", [None, [], [1, 2], [None], [True], ["1"], [-1], [0.5], [float("nan")], [float("inf")]]
)
async def test_invalid_visits(metrica: MetricaClient, mock_api: respx.MockRouter, totals: object) -> None:
    mock_api.get(f"{BASE_URL}/stat/v1/data").respond(content=json.dumps({"totals": totals}))

    with pytest.raises(MetricaResponseError):
        await metrica.is_alive(1)


@pytest.mark.parametrize(
    "payload",
    [
        [],
        {},
        {"counters": [], "rows": 1},
        {"counters": [], "rows": True},
        {"counters": [None], "rows": 1},
        {"counters": [{"id": 0, "name": "Сайт", "site": "site.example"}], "rows": 1},
        {"counters": [{"id": 1, "name": "Сайт", "site2": {}}], "rows": 1},
    ],
)
async def test_invalid_counters(metrica: MetricaClient, mock_api: respx.MockRouter, payload: object) -> None:
    mock_api.get(f"{BASE_URL}/management/v1/counters").respond(json=payload)

    with pytest.raises(MetricaResponseError):
        await metrica.list_counters()


async def test_ad_conversions_count_direct_visits_reaching_any_goal(
    metrica: MetricaClient, mock_api: respx.MockRouter
) -> None:
    direct = "ym:s:lastsignTrafficSource=='ad' AND ym:s:lastsignAdvEngine=.('ya_direct','yandex_direct_star')"
    query = {
        "ids": 1,
        "metrics": "ym:s:visits",
        "date1": "90daysAgo",
        "date2": "yesterday",
        "accuracy": "full",
    }
    visits = mock_api.get(f"{BASE_URL}/stat/v1/data", params=query | {"filters": direct}).respond(
        json={"totals": [240.0]}
    )
    reached = mock_api.get(
        f"{BASE_URL}/stat/v1/data",
        params=query | {"filters": f"{direct} AND EXISTS(ym:s:goal=.('12','13'))"},
    ).respond(json={"totals": [6]})

    assert await metrica.get_ad_conversions(1, [12, 13]) == (240, 6)
    assert visits.called and reached.called


async def test_ad_conversions_never_exceed_visits(metrica: MetricaClient, mock_api: respx.MockRouter) -> None:
    mock_api.get(f"{BASE_URL}/stat/v1/data").mock(
        side_effect=[httpx.Response(200, json={"totals": [3]}), httpx.Response(200, json={"totals": [4]})]
    )

    assert await metrica.get_ad_conversions(1, [12, 13]) == (3, 3)


async def test_sampled_ad_conversions_are_rounded(metrica: MetricaClient, mock_api: respx.MockRouter) -> None:
    mock_api.get(f"{BASE_URL}/stat/v1/data").mock(
        side_effect=[
            httpx.Response(200, json={"totals": [240.6], "sampled": True}),
            httpx.Response(200, json={"totals": [6.4], "sampled": True}),
        ]
    )

    assert await metrica.get_ad_conversions(1, [12]) == (241, 6)


@pytest.mark.parametrize("totals", [[], [10, 1], [-1], [1.5]])
async def test_invalid_ad_conversions(metrica: MetricaClient, mock_api: respx.MockRouter, totals: list) -> None:
    mock_api.get(f"{BASE_URL}/stat/v1/data").respond(json={"totals": totals})

    with pytest.raises(MetricaResponseError):
        await metrica.get_ad_conversions(1, [12])


@pytest.mark.parametrize("goal_ids", [[], list(range(1, 21))])
async def test_ad_conversions_goal_count(metrica: MetricaClient, goal_ids: list[int]) -> None:
    with pytest.raises(ValueError):
        await metrica.get_ad_conversions(1, goal_ids)


@pytest.mark.parametrize("goal", [{"id": 1, "name": "Заявка"}, {"id": True, "name": "Цель", "type": "action"}])
async def test_invalid_goals(metrica: MetricaClient, mock_api: respx.MockRouter, goal: dict) -> None:
    mock_api.get(f"{BASE_URL}/management/v1/counter/1/goals").respond(json={"goals": [goal]})

    with pytest.raises(MetricaResponseError):
        await metrica.list_goals(1)


@pytest.mark.parametrize("counter_id", [0, -1, True, "1"])
async def test_invalid_counter_id(metrica: MetricaClient, counter_id: object) -> None:
    for method in (metrica.list_goals, metrica.get_visits, metrica.is_alive):
        with pytest.raises(ValueError):
            await method(counter_id)


def test_empty_token() -> None:
    with pytest.raises(ValueError):
        MetricaClient("  ")


async def test_http_client_ownership() -> None:
    async with httpx.AsyncClient() as http_client:
        async with MetricaClient("test-only", client=http_client):
            pass
        assert not http_client.is_closed

    client = MetricaClient("test-only")
    await client.aclose()
    assert client._client.is_closed


@pytest.mark.parametrize("use_fake", [False, True])
@pytest.mark.parametrize("visits", [0, 5])
async def test_client_contract(metrica: MetricaClient, mock_api: respx.MockRouter, use_fake: bool, visits: int) -> None:
    counter = MetricaCounter(1, "Сайт", "site.example")
    goal = MetricaGoal(12, "Заявка", "action")
    if use_fake:
        client: Metrica = FakeMetrica(counters=[counter], goals={1: [goal]}, visits={1: visits})
    else:
        client = metrica
        mock_api.get(f"{BASE_URL}/management/v1/counters").respond(
            json={"rows": 1, "counters": [{"id": 1, "name": "Сайт", "site2": {"site": "site.example"}}]}
        )
        mock_api.get(f"{BASE_URL}/management/v1/counter/1/goals").respond(
            json={"goals": [{"id": 12, "name": "Заявка", "type": "action"}]}
        )
        mock_api.get(f"{BASE_URL}/stat/v1/data").respond(json={"totals": [float(visits)]})

    assert await client.list_counters() == [counter]
    assert await client.list_goals(1) == [goal]
    assert await client.get_visits(1) == visits
    assert await client.is_alive(1) is (visits > 0)
    if use_fake:
        assert not mock_api.calls


async def test_fake_empty_and_inactive() -> None:
    assert await FakeMetrica().list_counters() == []
    fake = FakeMetrica(counters=[MetricaCounter(1, "Сайт", "site.example")])

    assert await fake.list_goals(1) == []
    assert await fake.get_visits(1) == 0
    assert await fake.is_alive(1) is False
    for method in (fake.list_goals, fake.get_visits, fake.is_alive):
        with pytest.raises(MetricaAccessError):
            await method(2)
        with pytest.raises(ValueError):
            await method(0)


async def test_fake_returns_independent_lists() -> None:
    fake = FakeMetrica(
        counters=[MetricaCounter(1, "Сайт", "site.example")], goals={1: [MetricaGoal(12, "Заявка", "action")]}
    )
    counters = await fake.list_counters()
    goals = await fake.list_goals(1)
    counters.clear()
    goals.clear()

    assert len(await fake.list_counters()) == 1
    assert len(await fake.list_goals(1)) == 1
