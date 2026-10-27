import asyncio
import base64
import json
import os
import warnings
from dataclasses import replace
from datetime import UTC, date, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
import respx

from app.platforms.base import (
    AdGroupSpec,
    AdPlatform,
    AdSpec,
    AuthExpired,
    AuthSession,
    CampaignTreeSpec,
    ExternalRefs,
    ModerationRejected,
    Operation,
    PlatformError,
    RateLimited,
    TargetSpec,
)
from app.platforms.direct import DirectAdapter

V5 = "https://api.direct.yandex.com/json/v5"
V501 = "https://api.direct.yandex.com/v501"
TODAY = date(2026, 10, 26)


def configuration(**changes) -> SimpleNamespace:
    return SimpleNamespace(
        **{
            "direct_api_url": V5,
            "direct_api_v501_url": V501,
            "direct_sandbox": False,
            "tz_default": "Europe/Moscow",
            "yandex_client_id": "test-app",
            "yandex_client_secret": "test-only",
            "app_base_url": "https://autopilot.example",
            **changes,
        }
    )


@pytest.fixture
def api():
    with respx.mock(assert_all_called=False) as router:
        yield router


@pytest.fixture
async def adapter():
    async with DirectAdapter(
        "test-only",
        config=configuration(),
        client_login="advertiser",
        counter_ids=(12,),
        priority_goals={34: 12500},
        href="https://salon.example",
    ) as instance:
        instance._today = lambda: TODAY
        yield instance


@pytest.fixture
def plan() -> CampaignTreeSpec:
    return CampaignTreeSpec(
        "salon",
        "Салон",
        100000,
        500000,
        (
            AdGroupSpec(
                "nails",
                "Маникюр",
                (AdSpec("ad", "Маникюр", "Запишитесь", "image-hash"),),
                (TargetSpec("phrase", "phrase", "маникюр"),),
                {"region_ids": [51]},
            ),
        ),
    )


def request_body(route) -> dict:
    return json.loads(route.calls.last.request.content)


def result(api, service: str, **data):
    return api.post(f"{V501 if service in {'campaigns', 'adgroups', 'ads', 'keywords'} else V5}/{service}").respond(
        json={"result": data}
    )


class DirectServer:
    def __init__(self):
        self.rows = {"campaigns": [], "adgroups": [], "ads": [], "keywords": []}
        self.writes = []
        self.requests = []
        self.next_id = 100
        self.fail_keywords_once = False

    def __call__(self, request: httpx.Request) -> httpx.Response:
        service = request.url.path.rsplit("/", 1)[-1]
        body = json.loads(request.content)
        self.requests.append((service, body))
        method, params = body["method"], body["params"]
        key = {"campaigns": "Campaigns", "adgroups": "AdGroups", "ads": "Ads", "keywords": "Keywords"}.get(service)
        if method == "get":
            items = self.rows[service]
            selection = params.get("SelectionCriteria", {})
            for field, row_field in (("Ids", "Id"), ("CampaignIds", "CampaignId"), ("AdGroupIds", "AdGroupId")):
                if field in selection:
                    items = [item for item in items if item.get(row_field) in selection[field]]
            return httpx.Response(200, json={"result": {key: items}})
        self.writes.append((service, method, params))
        if service == "keywords" and self.fail_keywords_once:
            self.fail_keywords_once = False
            return httpx.Response(503, text="unavailable")
        if method == "add":
            self.next_id += 1
            item = {**params[key][0], "Id": self.next_id, "State": "ON", "Status": "ACCEPTED"}
            if service == "ads" and "ResponsiveAd" in item:
                content = item["ResponsiveAd"]
                item["ResponsiveAd"] = {
                    "Titles": [{"Title": title, "Status": "ACCEPTED"} for title in content["Titles"]],
                    "Texts": [{"Text": text, "Status": "ACCEPTED"} for text in content["Texts"]],
                    "Href": content["Href"],
                    "AdImages": {
                        "Items": [
                            {"ImageHash": image, "Status": "ACCEPTED"} for image in content.get("AdImageHashes", [])
                        ]
                    },
                }
            if service in {"ads", "keywords"}:
                item["CampaignId"] = next(
                    row["CampaignId"] for row in self.rows["adgroups"] if row["Id"] == item["AdGroupId"]
                )
            self.rows[service].append(item)
            return httpx.Response(200, json={"result": {"AddResults": [{"Id": item["Id"]}]}})
        if method == "update":
            for update in params[key]:
                next(row for row in self.rows[service] if row["Id"] == update["Id"]).update(update)
            return httpx.Response(200, json={"result": {"UpdateResults": [{"Id": 1}]}})
        for item in self.rows[service]:
            if item["Id"] in params.get("SelectionCriteria", {}).get("Ids", []):
                item["State"] = {"suspend": "SUSPENDED", "resume": "ON", "archive": "ARCHIVED"}[method]
        return httpx.Response(200, json={"result": {method.capitalize() + "Results": [{"Id": 1}]}})


async def test_protocol_headers_and_units(adapter, api):
    route = result(api, "clients", Clients=[{"ClientId": 7, "Login": "salon", "ClientInfo": "Салон"}])
    route.respond(
        json={"result": {"Clients": [{"ClientId": 7, "ClientInfo": "Салон"}]}}, headers={"Units": "10/890/1000"}
    )
    assert isinstance(adapter, AdPlatform)
    assert await adapter.whoami() == {"id": "7", "name": "Салон"}
    request = route.calls.last.request
    assert request.headers["Authorization"] == "Bearer test-only"
    assert request.headers["Accept-Language"] == "ru"
    assert request.headers["Client-Login"] == "advertiser"
    assert (await adapter.limits()).remaining_units == 890
    assert request_body(route)["method"] == "get"


@pytest.mark.parametrize(
    "code,error", [(53, AuthExpired), (152, RateLimited), (506, RateLimited), (56, RateLimited), (8000, PlatformError)]
)
@pytest.mark.parametrize("object_error", [False, True])
async def test_direct_errors(adapter, api, code, error, object_error):
    if object_error:
        api.post(f"{V501}/ads").mock(
            side_effect=[
                httpx.Response(
                    200,
                    json={
                        "result": {
                            "Ads": [
                                {
                                    "Id": 1,
                                    "ResponsiveAd": {
                                        "Titles": [{"Title": "a"}],
                                        "Texts": [{"Text": "text"}],
                                        "Href": "https://salon.example",
                                    },
                                }
                            ]
                        }
                    },
                ),
                httpx.Response(
                    200,
                    json={
                        "result": {
                            "UpdateResults": [{"Errors": [{"Code": code, "Message": "Ошибка", "Details": "Причина"}]}]
                        }
                    },
                ),
            ]
        )
        call = adapter.apply(Operation("update_ad_text", "ad", "1", "title", "a", "b"))
    else:
        api.post(f"{V5}/clients").respond(
            json={"error": {"error_code": code, "error_string": "Ошибка", "error_detail": "Причина"}}
        )
        call = adapter.whoami()
    with pytest.raises(error) as caught:
        await call
    if error is PlatformError:
        assert "Ошибка" in str(caught.value) and "Причина" in str(caught.value)


@pytest.mark.parametrize(
    "status,error", [(401, AuthExpired), (429, RateLimited), (503, PlatformError), (302, PlatformError)]
)
async def test_http_errors(adapter, api, status, error):
    api.post(f"{V5}/clients").respond(status, text="unavailable", headers={"Retry-After": "7"})
    with pytest.raises(error) as caught:
        await adapter.whoami()
    assert caught.value.status_code == status
    if status == 429:
        assert caught.value.retry_after == 7


@pytest.mark.parametrize("payload", [[], {}, {"result": []}, {"result": {"Clients": []}}])
async def test_invalid_response(adapter, api, payload):
    api.post(f"{V5}/clients").respond(json=payload)
    with pytest.raises(PlatformError):
        await adapter.whoami()


async def test_network_error(adapter, api):
    api.post(f"{V5}/clients").mock(side_effect=httpx.ReadTimeout("test-only"))
    with pytest.raises(PlatformError, match="Не удалось связаться"):
        await adapter.whoami()


async def test_sandbox_urls_and_optional_login(api):
    async with DirectAdapter("test-only", config=configuration(direct_sandbox=True)) as instance:
        clients = api.post("https://api-sandbox.direct.yandex.com/json/v5/clients").respond(
            json={"result": {"Clients": [{"ClientId": 1, "Login": "sandbox"}]}}
        )
        assert (await instance.whoami())["id"] == "1"
        assert "Client-Login" not in clients.calls.last.request.headers
        api.post("https://api-sandbox.direct.yandex.com/json/v5/adimages").respond(
            json={"result": {"AddResults": [{"AdImageHash": "hash"}]}}
        )
        assert instance._v501_url == "https://api-sandbox.direct.yandex.com/v501"


async def test_create_tree_requests_and_idempotency(adapter, api, plan):
    server = DirectServer()
    api.post(url__regex=r"https://api\.direct\.yandex\.com/v501/.*").mock(side_effect=server)
    first = await adapter.create_campaign_tree(plan)
    assert first == ExternalRefs(
        "101",
        {"nails": "102"},
        {"ad": "103"},
        {"phrase": "104"},
        {
            "daily_limit_kop": plan.daily_limit_kop,
            "period_cap_kop": plan.period_cap_kop,
            "weekly_limit_kop": 500_000,
        },
    )
    campaign = server.rows["campaigns"][0]
    assert campaign["Name"] == "Салон ap-salon"
    settings = campaign["UnifiedCampaign"]
    assert "TextCampaign" not in campaign
    assert settings["CounterIds"] == {"Items": [12]}
    assert settings["PriorityGoals"] == {"Items": [{"GoalId": 34, "Value": 125000000}]}
    assert settings["BiddingStrategy"]["Search"] == {
        "BiddingStrategyType": "WB_MAXIMUM_CLICKS",
        "WbMaximumClicks": {"WeeklySpendLimit": 5000000000},
    }
    assert settings["BiddingStrategy"]["Network"] == {"BiddingStrategyType": "NETWORK_DEFAULT"}
    group = server.rows["adgroups"][0]
    assert group["RegionIds"] == [51]
    assert group["UnifiedAdGroup"] == {"OfferRetargeting": "NO"}
    ad_request = next(params for service, method, params in server.writes if service == "ads" and method == "add")
    assert ad_request["Ads"][0]["ResponsiveAd"] == {
        "Titles": ["Маникюр"],
        "Texts": ["Запишитесь"],
        "Href": "https://salon.example",
        "AdImageHashes": ["image-hash"],
    }
    assert server.rows["keywords"][0]["Keyword"] == "маникюр"
    writes = len(server.writes)
    assert await adapter.create_campaign_tree(replace(plan, name="Другой", daily_limit_kop=1)) == first
    first.ads.clear()
    assert (await adapter.create_campaign_tree(plan)).ads == {"ad": "103"}
    assert len(server.writes) == writes
    async with DirectAdapter(
        "test-only", config=configuration(), counter_ids=(12,), href="https://salon.example"
    ) as another:
        another._today = lambda: TODAY
        assert await another.create_campaign_tree(plan) == ExternalRefs(
            "101",
            {"nails": "102"},
            {"ad": "103"},
            {"phrase": "104"},
            first.budget_context,
        )
    assert len(server.writes) == writes


async def test_recovers_partial_tree_and_concurrent_creation(adapter, api, plan):
    server = DirectServer()
    server.fail_keywords_once = True
    api.post(url__regex=r"https://api\.direct\.yandex\.com/v501/.*").mock(side_effect=server)
    with pytest.raises(PlatformError):
        await adapter.create_campaign_tree(plan)
    first, second = await asyncio.gather(adapter.create_campaign_tree(plan), adapter.create_campaign_tree(plan))
    assert first == second
    assert all(len(items) == 1 for items in server.rows.values())


@pytest.mark.parametrize(
    "today,expected", [(date(2026, 10, 26), 500000), (date(2026, 10, 30), 200000), (date(2026, 11, 1), 100000)]
)
async def test_weekly_limit_respects_month_and_period(adapter, today, expected):
    adapter._today = lambda: today
    assert adapter._weekly_limit(100000, 500000) == expected


async def test_conversion_strategy(adapter, api, plan):
    adapter._strategy = "WB_MAXIMUM_CONVERSION_RATE"
    server = DirectServer()
    api.post(url__regex=r"https://api\.direct\.yandex\.com/v501/.*").mock(side_effect=server)
    await adapter.create_campaign_tree(plan)
    strategy = server.rows["campaigns"][0]["UnifiedCampaign"]["BiddingStrategy"]["Search"]
    assert strategy == {
        "BiddingStrategyType": "WB_MAXIMUM_CONVERSION_RATE",
        "WbMaximumConversionRate": {"WeeklySpendLimit": 5000000000, "GoalId": 34},
    }
    adapter._goals[35] = 100
    assert adapter._campaign_settings(100)["BiddingStrategy"]["Search"]["WbMaximumConversionRate"]["GoalId"] == 13


async def test_text_campaign_fallback(adapter, api, plan):
    adapter._text_campaign = True
    server = DirectServer()
    api.post(url__regex=r"https://api\.direct\.yandex\.com/json/v5/.*").mock(side_effect=server)
    await adapter.create_campaign_tree(plan)
    assert "TextCampaign" in server.rows["campaigns"][0]
    assert "UnifiedAdGroup" not in server.rows["adgroups"][0]
    assert server.rows["ads"][0]["TextAd"]["AdImageHash"] == "image-hash"


async def test_missing_counter_fails_before_requests(api, plan):
    async with DirectAdapter("test-only", config=configuration(), href="https://salon.example") as instance:
        with pytest.raises(PlatformError, match="CounterIds"):
            await instance.create_campaign_tree(plan)
    assert not api.calls


async def test_campaign_pagination(adapter, api, plan):
    route = api.post(f"{V501}/campaigns").mock(
        side_effect=[
            httpx.Response(200, json={"result": {"Campaigns": [{"Id": 9, "Name": "Other"}], "LimitedBy": 1}}),
            httpx.Response(200, json={"result": {"Campaigns": [{"Id": 10, "Name": "Changed ap-salon"}]}}),
        ]
    )
    result(api, "adgroups", AdGroups=[])
    empty = replace(plan, groups=())
    assert (await adapter.create_campaign_tree(empty)).campaign_id == "10"
    assert request_body(route)["params"]["Page"]["Offset"] == 1
    assert request_body(route)["params"]["SelectionCriteria"] == {}


async def test_upload_image(adapter, api, tmp_path: Path):
    file = tmp_path / "photo.png"
    file.write_bytes(b"test-image")
    route = result(api, "adimages", AddResults=[{"AdImageHash": "hash"}])
    assert await adapter.upload_image(file) == "hash"
    assert request_body(route) == {
        "method": "add",
        "params": {"AdImages": [{"Name": "photo.png", "ImageData": base64.b64encode(b"test-image").decode()}]},
    }
    with pytest.raises(ValueError):
        await adapter.upload_image(tmp_path / "unsupported.txt")


@pytest.mark.parametrize(
    "field,kind,value,expected",
    [
        ("title", "update_ad_text", "Новый", {"Titles": ["Новый"]}),
        ("text", "update_ad_text", "Текст", {"Texts": ["Текст"]}),
        ("image_ref", "swap_ad_image", "new-hash", {"AdImageHashes": {"Items": ["new-hash"]}}),
        ("image_ref", "swap_ad_image", None, {"AdImageHashes": None}),
    ],
)
async def test_apply_ad_updates(adapter, api, field, kind, value, expected):
    def respond(request):
        method = json.loads(request.content)["method"]
        data = (
            {
                "Ads": [
                    {
                        "Id": 3,
                        "ResponsiveAd": {
                            "Titles": [{"Title": "Original"}],
                            "Texts": [{"Text": "Original text"}],
                            "Href": "https://salon.example",
                        },
                    }
                ]
            }
            if method == "get"
            else {"UpdateResults": [{"Id": 3}]}
        )
        return httpx.Response(200, json={"result": data})

    route = api.post(f"{V501}/ads").mock(side_effect=respond)
    op = Operation(kind, "ad", "3", field, "old", value)
    await adapter.apply(op)
    await adapter.apply(op)
    content = {"Titles": ["Original"], "Texts": ["Original text"], "Href": "https://salon.example", **expected}
    assert request_body(route) == {"method": "update", "params": {"Ads": [{"Id": 3, "ResponsiveAd": content}]}}


async def test_apply_targeting(adapter, api):
    route = result(api, "adgroups", UpdateResults=[{"Id": 2}])
    await adapter.apply(
        Operation(
            "set_targeting", "group", "2", "targeting", {}, {"region_ids": [51], "negative_keywords": ["бесплатно"]}
        )
    )
    assert request_body(route)["params"] == {
        "AdGroups": [{"Id": 2, "RegionIds": [51], "NegativeKeywords": {"Items": ["бесплатно"]}}]
    }
    with pytest.raises(PlatformError):
        await adapter.apply(Operation("set_targeting", "group", "2", "targeting", {}, {"city": "Самара"}))


@pytest.mark.parametrize(
    "entity,after,method",
    [
        ("campaign", "paused_owner", "suspend"),
        ("ad", "paused_auto", "suspend"),
        ("campaign", "archived", "archive"),
        ("target", "active", "resume"),
    ],
)
async def test_status_operations(adapter, api, entity, after, method):
    adapter._marking_confirmed = True
    service = {"campaign": "campaigns", "ad": "ads", "target": "keywords"}[entity]
    key = {"campaign": "Campaigns", "ad": "Ads", "target": "Keywords"}[entity]
    route = api.post(f"{V501}/{service}").mock(
        side_effect=[
            httpx.Response(
                200, json={"result": {key: [{"Id": 1, "State": "SUSPENDED" if after == "active" else "ON"}]}}
            ),
            httpx.Response(200, json={"result": {method.capitalize() + "Results": [{"Id": 1}]}}),
        ]
    )
    await adapter.apply(Operation("set_status", entity, "1", "status", "active", after))
    assert request_body(route) == {"method": method, "params": {"SelectionCriteria": {"Ids": [1]}}}


async def test_marking_blocks_activation(adapter, api):
    with pytest.raises(PlatformError, match="рекламодателя"):
        await adapter.apply(Operation("set_status", "campaign", "1", "status", "paused_owner", "active"))
    assert not api.calls


@pytest.mark.parametrize("field,value,expected", [("daily_limit_kop", 100, 6000000), ("period_cap_kop", 200, 2000000)])
async def test_budget_preserves_strategy_and_uses_micros(adapter, api, field, value, expected):
    settings = adapter._campaign_settings(1000)
    route = api.post(f"{V501}/campaigns").mock(
        side_effect=[
            httpx.Response(200, json={"result": {"Campaigns": [{"Id": 1, "UnifiedCampaign": settings}]}}),
            httpx.Response(200, json={"result": {"UpdateResults": [{"Id": 1}]}}),
        ]
    )
    await adapter.apply(Operation("set_budget", "campaign", "1", field, 10, value))
    updated = request_body(route)["params"]["Campaigns"][0]["UnifiedCampaign"]["BiddingStrategy"]
    assert updated["Search"]["WbMaximumClicks"]["WeeklySpendLimit"] == expected
    assert updated["Network"] == settings["BiddingStrategy"]["Network"]


async def test_unchanged_weekly_limit_does_not_update_campaign(adapter, api):
    server = DirectServer()
    server.rows["campaigns"] = [{"Id": 1, "UnifiedCampaign": adapter._campaign_settings(600)}]
    api.post(f"{V501}/campaigns").mock(side_effect=server)
    await adapter.apply(Operation("set_budget", "campaign", "1", "daily_limit_kop", 100, 100))
    assert not server.writes


@pytest.mark.parametrize("end,updates", [("2026-10-31", False), ("2026-10-28", True)])
async def test_end_date_extension_is_separate_from_budget_update(adapter, api, end, updates):
    server = DirectServer()
    server.rows["campaigns"] = [{"Id": 1, "EndDate": end}]
    api.post(f"{V501}/campaigns").mock(side_effect=server)
    await adapter.extend_end_date("1")
    assert bool(server.writes) is updates
    if updates:
        assert server.rows["campaigns"][0]["EndDate"] == "2026-10-31"
        assert server.writes == [("campaigns", "update", {"Campaigns": [{"Id": 1, "EndDate": "2026-10-31"}]})]


async def test_state_and_moderation(adapter, api, plan):
    server = DirectServer()
    api.post(url__regex=r"https://api\.direct\.yandex\.com/v501/.*").mock(side_effect=server)
    refs = await adapter.create_campaign_tree(plan)
    state = await adapter.get_state(refs)
    assert set(state) == set(refs.entity_ids())
    assert state[refs.campaign_id].status == "paused_owner"
    assert state[refs.campaign_id].daily_limit_kop == plan.daily_limit_kop
    assert state[refs.campaign_id].period_cap_kop == plan.period_cap_kop
    assert state[refs.campaign_id].targeting == {"weekly_limits_kop": {"Search": 500000}}
    assert state[refs.ads["ad"]].title == "Маникюр"
    assert state[refs.ads["ad"]].image_ref == "image-hash"
    assert state[refs.groups["nails"]].targeting["region_ids"] == [51]
    assert (await adapter.get_moderation(refs))[0].status == "approved"
    image = server.rows["ads"][0]["ResponsiveAd"]["AdImages"]["Items"][0]
    image.update(Status="REJECTED", StatusClarification="Недопустимое изображение")
    review = (await adapter.get_moderation(refs))[0]
    assert review.status == "rejected" and review.reason == "Недопустимое изображение"
    adapter._marking_confirmed = True
    with pytest.raises(ModerationRejected, match="Недопустимое изображение"):
        await adapter.apply(Operation("set_status", "ad", refs.ads["ad"], "status", "paused_owner", "active"))


@pytest.mark.parametrize(
    "status,expected",
    [
        ("ACCEPTED", "approved"),
        ("PREACCEPTED", "pending"),
        ("DRAFT", "pending"),
        ("MODERATION", "pending"),
        ("REJECTED", "rejected"),
    ],
)
async def test_moderation_statuses(adapter, api, status, expected):
    result(api, "ads", Ads=[{"Id": 3, "Status": status, "StatusClarification": "Причина"}])
    info = await adapter.get_moderation(ExternalRefs("1", {}, {"ad": "3"}, {}))
    assert info[0].status == expected
    assert info[0].reason == ("Причина" if expected == "rejected" else None)


def tsv(level="ad", cost="123456789", conversions="2", day="2026-10-23"):
    dims = (
        ["Date", "CampaignId"]
        + (["AdGroupId"] if level in {"group", "ad"} else [])
        + (["AdId"] if level == "ad" else [])
    )
    ids = [day, "1"] + (["2"] if level in {"group", "ad"} else []) + (["3"] if level == "ad" else [])
    return (
        "\t".join([*dims, "Impressions", "Clicks", "Cost", "Conversions_34_LSCCD"])
        + "\n"
        + "\t".join([*ids, "100", "7", cost, conversions])
        + "\n"
    )


async def test_offline_report(adapter, api, monkeypatch):
    sleep = AsyncMock()
    monkeypatch.setattr("app.platforms.direct.asyncio.sleep", sleep)
    route = api.post(f"{V5}/reports").mock(
        side_effect=[
            httpx.Response(201, headers={"retryIn": "2", "Units": "1/99/100"}),
            httpx.Response(202, headers={"retryIn": "3"}),
            httpx.Response(200, text=tsv()),
        ]
    )
    rows = await adapter.get_stats(date(2026, 10, 23), TODAY)
    assert len(rows) == 1
    row = rows[0]
    assert row.cost_kop == 12346 and type(row.cost_kop) is int
    assert row.conversions == {"34": 2} and row.entity_id == "3" and row.is_settled
    assert row.impressions == 100 and row.clicks == 7
    assert [call.args[0] for call in sleep.await_args_list] == [2, 3]
    bodies = [json.loads(call.request.content) for call in route.calls]
    assert bodies[0] == bodies[1] == bodies[2]
    params = bodies[0]["params"]
    assert "method" not in bodies[0]
    assert params["ReportType"] == "CUSTOM_REPORT" and params["Format"] == "TSV"
    assert params["Goals"] == ["34"] and params["IncludeVAT"] == "YES"
    assert params["FieldNames"] == [
        "Date",
        "CampaignId",
        "AdGroupId",
        "AdId",
        "Impressions",
        "Clicks",
        "Cost",
        "Conversions",
    ]
    assert route.calls.last.request.headers["processingMode"] == "offline"
    assert route.calls.last.request.headers["skipReportHeader"] == "true"
    assert "returnMoneyInMicros" not in route.calls.last.request.headers
    assert (await adapter.limits()).remaining_units == 99


@pytest.mark.parametrize("level,id_", [("campaign", "1"), ("group", "2"), ("ad", "3")])
async def test_report_levels(adapter, api, level, id_):
    api.post(f"{V5}/reports").respond(text=tsv(level, day=TODAY.isoformat()))
    (row,) = await adapter.get_stats(TODAY, TODAY, level)
    assert row.entity_id == id_ and row.level == level and not row.is_settled


@pytest.mark.parametrize(
    "micros,kop",
    [("0", 0), ("4999", 0), ("5000", 1), ("10000", 1), ("1000000", 100), ("999999999999999999", 100000000000000)],
)
async def test_money_conversion(adapter, api, micros, kop):
    api.post(f"{V5}/reports").respond(text=tsv(cost=micros))
    assert (await adapter.get_stats(date(2026, 10, 23), TODAY))[0].cost_kop == kop


@pytest.mark.parametrize("conversion", ["--", "0", "2.00"])
async def test_conversion_counters(adapter, api, conversion):
    api.post(f"{V5}/reports").respond(text=tsv(conversions=conversion))
    assert (await adapter.get_stats(date(2026, 10, 23), TODAY))[0].conversions == {
        "34": 0 if conversion in {"--", "0"} else 2
    }


@pytest.mark.parametrize(
    "content",
    [
        "",
        "wrong\tcolumns\n",
        tsv(cost="nan"),
        tsv(cost="-1"),
        tsv(conversions="0.5"),
        tsv(conversions="NaN"),
        tsv(day="2027-01-01"),
    ],
)
async def test_invalid_reports(adapter, api, content):
    api.post(f"{V5}/reports").respond(text=content)
    with pytest.raises(PlatformError):
        await adapter.get_stats(date(2026, 10, 23), TODAY)


async def test_report_queue_timeout(adapter, api):
    adapter._report_attempts = 1
    api.post(f"{V5}/reports").respond(202, headers={"retryIn": "4"})
    with pytest.raises(RateLimited) as caught:
        await adapter.get_stats(TODAY, TODAY)
    assert caught.value.retry_after == 4


async def test_spend_today_and_minimums(adapter, api):
    api.post(f"{V5}/reports").respond(text=tsv("campaign", day=TODAY.isoformat()))
    assert await adapter.get_spend_today() == 12346
    result(api, "clients", Clients=[{"Currency": "RUB"}])
    route = result(
        api,
        "dictionaries",
        Currencies=[
            {
                "Currency": "RUB",
                "Properties": [
                    {"Name": "MinimumWeeklySpendLimit", "Value": "300000001"},
                    {"Name": "MinimumAverageCPC", "Value": "900000"},
                    # Минимум общего счёта к дневному лимиту кампании не относится.
                    {"Name": "MinimumAccountDailyBudget", "Value": "1000000000"},
                ],
            }
        ],
    )
    assert await adapter.min_budgets() == {"weekly_limit_kop": 30001}
    assert request_body(route) == {"method": "get", "params": {"DictionaryNames": ["Currencies"]}}
    assert await adapter.min_budgets() == {"weekly_limit_kop": 30001}
    assert route.call_count == 1
    assert await adapter.get_balance_hint() is None


PROFILE = {"type": "ip", "inn": "123456789012", "name": "ИП Пример", "phone": "+79990000000"}


async def test_marking_reads_before_update(adapter, api):
    route = api.post(f"{V5}/clients").mock(
        side_effect=[
            httpx.Response(200, json={"result": {"Clients": [{"ClientId": 1}]}}),
            httpx.Response(200, json={"result": {"UpdateResults": [{}]}}),
        ]
    )
    await adapter.set_marking(PROFILE)
    assert json.loads(route.calls[0].request.content)["method"] == "get"
    assert request_body(route) == {
        "method": "update",
        "params": {
            "Clients": [
                {
                    "TinInfo": {"TinType": "INDIVIDUAL", "Tin": PROFILE["inn"]},
                    "Phone": PROFILE["phone"],
                    "ErirAttributes": {"Organization": {"Name": PROFILE["name"]}},
                }
            ]
        },
    }
    assert adapter.marking_instruction() is None


async def test_marking_requires_explicit_overwrite_confirmation(adapter, api):
    route = result(
        api,
        "clients",
        Clients=[
            {
                "TinInfo": {"TinType": "LEGAL", "Tin": "old"},
                "ErirAttributes": {"Organization": {"Name": "Other", "Kpp": "123"}},
            }
        ],
    )
    with pytest.raises(PlatformError, match="confirm_overwrite"):
        await adapter.set_marking(PROFILE)
    assert len(route.calls) == 1
    route.mock(
        side_effect=[
            httpx.Response(
                200,
                json={
                    "result": {
                        "Clients": [
                            {
                                "TinInfo": {"TinType": "LEGAL", "Tin": "old"},
                                "ErirAttributes": {"Organization": {"Name": "Other", "Kpp": "123"}},
                            }
                        ]
                    }
                },
            ),
            httpx.Response(200, json={"result": {"UpdateResults": [{}]}}),
        ]
    )
    await adapter.set_marking({**PROFILE, "confirm_overwrite": "yes"})
    assert request_body(route)["params"]["Clients"][0]["ErirAttributes"]["Organization"]["Kpp"] == "123"


async def test_marking_unsupported_falls_back_but_auth_errors_propagate(adapter, api):
    route = api.post(f"{V5}/clients").respond(
        json={"error": {"error_code": 8000, "error_string": "Unsupported ErirAttributes"}}
    )
    await adapter.set_marking(PROFILE)
    assert "Данные рекламодателя" in adapter.marking_instruction()
    route.respond(json={"error": {"error_code": 53}})
    with pytest.raises(AuthExpired):
        await adapter.set_marking(PROFILE)


async def test_semaphore_limits_parallel_requests(adapter, api):
    active = peak = 0
    five_started = asyncio.Event()
    release = asyncio.Event()

    async def respond(request):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        if active == 5:
            five_started.set()
        await release.wait()
        active -= 1
        return httpx.Response(200, json={"result": {"Clients": [{"ClientId": 1, "Login": "salon"}]}})

    api.post(f"{V5}/clients").mock(side_effect=respond)
    tasks = [asyncio.create_task(adapter.whoami()) for _ in range(12)]
    await asyncio.wait_for(five_started.wait(), timeout=2)
    assert peak == 5
    release.set()
    await asyncio.gather(*tasks)
    assert peak == 5


async def test_auth_and_refresh(adapter, api):
    query = parse_qs(urlsplit(adapter.auth_url("state-value")).query)
    assert query["state"] == ["state-value"] and query["scope"] == ["direct:api"]
    oauth = api.post("https://oauth.yandex.ru/token").respond(
        json={"access_token": "test-access", "refresh_token": "test-refresh", "expires_in": 3600}
    )
    result(api, "clients", Clients=[{"ClientId": 1, "Login": "salon"}])
    session = await adapter.exchange_code("test-code")
    assert session.account_id == "1" and session.expires_at > datetime.now(UTC)
    data = parse_qs(oauth.calls.last.request.content.decode())
    assert data["grant_type"] == ["authorization_code"] and data["code"] == ["test-code"]
    refreshed = await adapter.refresh(session)
    assert refreshed == replace(session, expires_at=refreshed.expires_at)
    with pytest.raises(AuthExpired):
        await adapter.refresh(AuthSession("1", "test-access"))


async def test_client_ownership():
    async with httpx.AsyncClient() as client:
        async with DirectAdapter("test-only", config=configuration(), client=client):
            pass
        assert not client.is_closed
    instance = DirectAdapter("test-only", config=configuration())
    await instance.aclose()
    assert instance._client.is_closed


with warnings.catch_warnings():
    # pyproject.toml нельзя менять; sandbox остаётся локальным маркером этого набора.
    warnings.simplefilter("ignore", pytest.PytestUnknownMarkWarning)

    @pytest.mark.sandbox
    @pytest.mark.skipif("DIRECT_SANDBOX_TOKEN" not in os.environ, reason="DIRECT_SANDBOX_TOKEN не задан")
    async def test_live_sandbox_identity():
        async with DirectAdapter(
            os.environ["DIRECT_SANDBOX_TOKEN"], config=configuration(direct_sandbox=True)
        ) as instance:
            identity = await instance.whoami()
            assert identity["id"] and identity["name"]


async def test_group_pause_and_resume_are_idempotent(adapter, api, plan):
    server = DirectServer()
    api.post(url__regex=r"https://api\.direct\.yandex\.com/v501/.*").mock(side_effect=server)
    refs = await adapter.create_campaign_tree(plan)
    adapter._marking_confirmed = True
    operation = Operation("set_status", "group", refs.groups["nails"], "status", "active", "paused_auto")
    await adapter.apply(operation)
    writes = len(server.writes)
    await adapter.apply(operation)
    assert len(server.writes) == writes
    state = await adapter.get_state(refs)
    assert state[refs.groups["nails"]].status == "paused_owner"
    await adapter.apply(operation.inverse())
    writes = len(server.writes)
    await adapter.apply(operation.inverse())
    assert len(server.writes) == writes
    assert (await adapter.get_state(refs))[refs.groups["nails"]].status == "active"


async def test_archive_inverse_unarchives_then_resumes(adapter, api):
    adapter._marking_confirmed = True
    route = api.post(f"{V501}/campaigns").mock(
        side_effect=[
            httpx.Response(200, json={"result": {"Campaigns": [{"Id": 1, "State": "ARCHIVED"}]}}),
            httpx.Response(200, json={"result": {"UnarchiveResults": [{"Id": 1}]}}),
            httpx.Response(200, json={"result": {"ResumeResults": [{"Id": 1}]}}),
            httpx.Response(200, json={"result": {"Campaigns": [{"Id": 1, "State": "ON"}]}}),
        ]
    )
    operation = Operation("set_status", "campaign", "1", "status", "archived", "active")
    await adapter.apply(operation)
    await adapter.apply(operation)
    assert [json.loads(call.request.content)["method"] for call in route.calls] == ["get", "unarchive", "resume", "get"]


async def test_draft_activation_sends_to_moderation(adapter, api):
    adapter._marking_confirmed = True
    route = api.post(f"{V501}/ads").mock(
        side_effect=[
            httpx.Response(200, json={"result": {"Ads": [{"Id": 1, "State": "ON", "Status": "DRAFT"}]}}),
            httpx.Response(200, json={"result": {"ModerateResults": [{"Id": 1}]}}),
            httpx.Response(200, json={"result": {"ResumeResults": [{"Id": 1}]}}),
        ]
    )
    await adapter.apply(Operation("set_status", "ad", "1", "status", "pending", "active"))
    assert [json.loads(call.request.content)["method"] for call in route.calls] == ["get", "moderate", "resume"]


async def test_confirmed_campaign_can_start(adapter, api, plan):
    adapter._marking_confirmed = True
    server = DirectServer()
    api.post(url__regex=r"https://api\.direct\.yandex\.com/v501/.*").mock(side_effect=server)
    refs = await adapter.create_campaign_tree(plan)
    assert server.rows["campaigns"][0]["State"] == "ON"
    assert server.rows["campaigns"][0]["EndDate"] == "2026-10-31"
    assert (await adapter.get_state(refs))[refs.campaign_id].status == "active"


async def test_restarting_does_not_duplicate_edited_ads(adapter, api, plan):
    server = DirectServer()
    api.post(url__regex=r"https://api\.direct\.yandex\.com/v501/.*").mock(side_effect=server)
    original = await adapter.create_campaign_tree(plan)
    adapter._trees.clear()
    changed = replace(
        plan,
        name="Другое имя",
        groups=(replace(plan.groups[0], ads=(replace(plan.groups[0].ads[0], title="Другой заголовок"),)),),
    )
    writes = len(server.writes)
    assert await adapter.create_campaign_tree(changed) == original
    assert len(server.writes) == writes


async def test_sandbox_reports_are_requested_per_campaign(api):
    async with DirectAdapter(
        "test-only", config=configuration(direct_sandbox=True), priority_goals={34: 1}
    ) as instance:
        instance._today = lambda: TODAY
        api.post("https://api-sandbox.direct.yandex.com/v501/campaigns").respond(
            json={"result": {"Campaigns": [{"Id": 1}, {"Id": 4}]}}
        )
        route = api.post("https://api-sandbox.direct.yandex.com/json/v5/reports").mock(
            side_effect=[
                httpx.Response(200, text=tsv()),
                httpx.Response(200, text=tsv().replace("\t1\t2\t3\t", "\t4\t5\t6\t")),
            ]
        )
        rows = await instance.get_stats(date(2026, 10, 23), TODAY)
        assert {row.campaign_id for row in rows} == {"1", "4"}
        selections = [json.loads(call.request.content)["params"]["SelectionCriteria"] for call in route.calls]
        assert [selection["Filter"][0]["Values"] for selection in selections] == [["1"], ["4"]]


async def test_budget_rollback_preserves_other_limit(adapter, api):
    context = {"daily_limit_kop": 100, "period_cap_kop": 500}
    settings = adapter._campaign_settings(500)

    def respond(request):
        body = json.loads(request.content)
        if body["method"] == "get":
            return httpx.Response(200, json={"result": {"Campaigns": [{"Id": 1, "UnifiedCampaign": settings}]}})
        settings["BiddingStrategy"] = body["params"]["Campaigns"][0]["UnifiedCampaign"]["BiddingStrategy"]
        return httpx.Response(200, json={"result": {"UpdateResults": [{"Id": 1}]}})

    api.post(f"{V501}/campaigns").mock(side_effect=respond)
    operation = Operation("set_budget", "campaign", "1", "daily_limit_kop", 100, 20, budget_context=context)
    await adapter.apply(operation)
    assert settings["BiddingStrategy"]["Search"]["WbMaximumClicks"]["WeeklySpendLimit"] == 1200000
    await adapter.apply(operation.inverse())
    assert settings["BiddingStrategy"]["Search"]["WbMaximumClicks"]["WeeklySpendLimit"] == 5000000
    await adapter.apply(Operation("set_budget", "campaign", "1", "period_cap_kop", 500, 50, budget_context=context))
    assert settings["BiddingStrategy"]["Search"]["WbMaximumClicks"]["WeeklySpendLimit"] == 500000
    await adapter.apply(
        Operation(
            "set_budget", "campaign", "1", "daily_limit_kop", 100, 1000, budget_context=context | {"period_cap_kop": 50}
        )
    )
    assert settings["BiddingStrategy"]["Search"]["WbMaximumClicks"]["WeeklySpendLimit"] == 500000


async def test_unknown_budget_cannot_be_increased_without_plan(adapter, api):
    route = result(api, "campaigns", Campaigns=[{"Id": 1, "UnifiedCampaign": adapter._campaign_settings(10)}])
    with pytest.raises(PlatformError, match="нужен план"):
        await adapter.apply(Operation("set_budget", "campaign", "1", "daily_limit_kop", 1, 100))
    assert len(route.calls) == 1
    with pytest.raises(PlatformError, match="set_status"):
        await adapter.apply(Operation("set_budget", "campaign", "1", "daily_limit_kop", 1, 0))


async def test_service_confirms_direct_auto_pause_and_preserves_local_author(adapter, api, plan):
    from app.db.models import Campaign
    from app.services.changes import ChangesService
    from tests.services.test_changes import MemoryRepository

    server = DirectServer()
    api.post(url__regex=r"https://api\.direct\.yandex\.com/v501/.*").mock(side_effect=server)
    refs = await adapter.create_campaign_tree(plan)
    adapter._marking_confirmed = True
    await adapter.apply(Operation("set_status", "campaign", refs.campaign_id, "status", "paused_owner", "active"))
    repository = MemoryRepository()
    campaign = Campaign(id=1, external_id=refs.campaign_id, status="active")
    repository.entities = {("campaign", refs.campaign_id): campaign}
    change = await ChangesService(repository, dry_run=False).apply_change(
        business_id=1,
        adapter=adapter,
        refs=refs,
        actor="system",
        kind="pause",
        platform="direct",
        reason_code="MONTH_STOP",
        reason_text="Остановили рекламу.",
        operations=[Operation("set_status", "campaign", refs.campaign_id, "status", "active", "paused_auto")],
    )
    assert change.status == "applied" and campaign.status == "paused_auto"
    assert (await adapter.get_state(refs))[refs.campaign_id].status == "paused_owner"


async def test_service_budget_update_after_restart_converts_lifetime_cap_and_extends_end_date(api):
    from app.db.models import Campaign
    from app.platforms.base import StatRow
    from app.services.changes import ChangesService, load_operations, load_refs
    from tests.services.test_changes import MemoryRepository

    day = date(2026, 11, 2)
    now = datetime(2026, 11, 2, 12, tzinfo=UTC)
    server = DirectServer()
    repository = MemoryRepository()
    repository.current_business.settings = {"_worker": {"started_on": "2026-10-26"}}
    campaign = Campaign(id=1, external_id="1", daily_limit_kop=100_000, period_cap_kop=1_000_000)
    repository.entities = {("campaign", "1"): campaign}
    api.post(url__regex=r"https://api\.direct\.yandex\.com/v501/.*").mock(side_effect=server)
    async with DirectAdapter("test-only", config=configuration()) as restarted:
        restarted._today = lambda: day
        server.rows["campaigns"] = [
            {
                "Id": 1,
                "Name": "Салон",
                "State": "ON",
                "EndDate": "2026-10-31",
                "UnifiedCampaign": restarted._campaign_settings(500_000),
            }
        ]
        restarted.get_stats = AsyncMock(
            return_value=[
                StatRow("1", "1", row_day, "campaign", 10, 1, cost, {}, now, True)
                for row_day, cost in ((date(2026, 10, 28), 400_000), (day, 100_000))
            ]
        )
        change = await ChangesService(repository, dry_run=False).apply_change(
            business_id=1,
            adapter=restarted,
            refs=ExternalRefs("1", {}, {}, {}),
            actor="system",
            kind="budget",
            platform="direct",
            reason_code="BUDGET_REBALANCE",
            reason_text="Пересчитали бюджет.",
            now=now,
            operations=[
                Operation("set_budget", "campaign", "1", "period_cap_kop", 1_000_000, 1_300_000),
                Operation("set_budget", "campaign", "1", "daily_limit_kop", 100_000, 150_000),
            ],
        )
        await restarted.extend_end_date("1")
    assert change.status == "applied"
    assert campaign.daily_limit_kop == 150_000 and campaign.period_cap_kop == 1_300_000
    assert server.rows["campaigns"][0]["EndDate"] == "2026-11-08"
    weekly = server.rows["campaigns"][0]["UnifiedCampaign"]["BiddingStrategy"]["Search"]["WbMaximumClicks"]
    assert weekly["WeeklySpendLimit"] == 900_000 * 10_000
    context = load_operations(change)[-1].budget_context
    assert context["spent_before_week_kop"] == 400_000 and context["spent_week_kop"] == 100_000
    async with DirectAdapter("test-only", config=configuration()) as another:
        another._today = lambda: day
        refs = replace(load_refs(change), budget_context=context)
        state = (await another.get_state(refs))["1"]
        assert (state.daily_limit_kop, state.period_cap_kop) == (150_000, 1_300_000)


async def test_invalid_mutation_result_does_not_confirm_marking(adapter, api):
    api.post(f"{V5}/clients").mock(
        side_effect=[
            httpx.Response(200, json={"result": {"Clients": [{}]}}),
            httpx.Response(200, json={"result": {}}),
        ]
    )
    with pytest.raises(PlatformError):
        await adapter.set_marking(PROFILE)
    assert adapter.marking_instruction()


@pytest.mark.parametrize("units", ["invalid", "1/2", "-1/2/3", "1/a/3"])
async def test_malformed_units_preserve_previous_limits(adapter, api, units):
    route = result(api, "clients", Clients=[{"ClientId": 1, "Login": "salon"}])
    data = {"result": {"Clients": [{"ClientId": 1, "Login": "salon"}]}}
    route.respond(json=data, headers={"Units": "1/5/10"})
    await adapter.whoami()
    route.respond(json=data, headers={"Units": units})
    await adapter.whoami()
    assert (await adapter.limits()).remaining_units == 5


async def test_invalid_marking_data_does_not_become_manual_fallback(adapter, api):
    api.post(f"{V5}/clients").respond(json={"error": {"error_code": 8000, "error_string": "Неверный ИНН"}})
    with pytest.raises(PlatformError, match="ИНН"):
        await adapter.set_marking(PROFILE)


async def test_end_date_known_far_skips_get(adapter, api):
    server = DirectServer()
    server.rows["campaigns"] = [{"Id": 1, "EndDate": "2026-10-31"}]
    route = api.post(f"{V501}/campaigns").mock(side_effect=server)
    await adapter.extend_end_date("1")
    calls = route.call_count
    await adapter.extend_end_date("1")
    assert route.call_count == calls


def geo_regions(*items: tuple[int, str, int | None, str]) -> dict:
    return {
        "result": {
            "GeoRegions": [
                {"GeoRegionId": id_, "GeoRegionName": name, "ParentId": parent, "GeoRegionType": kind}
                for id_, name, parent, kind in items
            ]
        }
    }


GEO_TREE = (
    (225, "Россия", 3, "Country"),
    (3, "Центр", 225, "District"),
    (10174, "Санкт-Петербург и Ленинградская область", 17, "Administrative area"),
    (17, "Северо-Запад", 225, "District"),
    (10897, "Мурманская область", 17, "Administrative area"),
    (10176, "Кировск", 10174, "City"),
    (20171, "Кировск", 10897, "City"),
    (11079, "Нижегородская область", 40, "Administrative area"),
    (40, "Поволжье", 225, "District"),
    (20044, "Арзамас", 11079, "City"),
    (149, "Беларусь", 166, "Country"),
    (166, "СНГ", 10001, "Region"),
    (26034, "Арзамас", 149, "City"),
    (10002, "Белгородская область", 3, "Administrative area"),
    (4, "Белгород", 10002, "City"),
)


@pytest.mark.parametrize(
    "city,region",
    [
        ("Москва", 213),
        ("  МОСКВА\t", 213),
        ("  москва и Московская   область ", 1),
        ("Санкт-Петербург", 2),
        ("Казань", 43),
    ],
)
async def test_geo_targeting_known_city_needs_no_request(adapter, api, city, region) -> None:
    assert await adapter.geo_targeting(city) == {"region_ids": [region]}
    assert not api.calls


async def test_geo_targeting_finds_other_city_in_russia_once_and_caches(adapter, api) -> None:
    route = api.post(f"{V5}/dictionaries").respond(json=geo_regions(*GEO_TREE))
    assert await adapter.geo_targeting("Арзамас") == {"region_ids": [20044]}
    assert await adapter.geo_targeting("белгород") == {"region_ids": [4]}
    assert route.call_count == 1
    assert json.loads(route.calls.last.request.content) == {
        "method": "get",
        "params": {"DictionaryNames": ["GeoRegions"]},
    }


async def test_geo_targeting_explains_unknown_and_ambiguous_city(adapter, api) -> None:
    api.post(f"{V5}/dictionaries").respond(json=geo_regions(*GEO_TREE))
    with pytest.raises(PlatformError, match="«Урюпинск» не найден в справочнике регионов Яндекс Директа"):
        await adapter.geo_targeting("Урюпинск")
    with pytest.raises(PlatformError, match="несколько мест с названием «Кировск»"):
        await adapter.geo_targeting("Кировск")


async def test_geo_targeting_unavailable_dictionary_is_platform_error(adapter, api) -> None:
    api.post(f"{V5}/dictionaries").respond(500)
    with pytest.raises(PlatformError):
        await adapter.geo_targeting("Арзамас")
    api.post(f"{V5}/dictionaries").respond(json={"result": {"GeoRegions": [{"GeoRegionId": "x"}]}})
    with pytest.raises(PlatformError, match="некорректные"):
        await adapter.geo_targeting("Арзамас")
    assert await adapter.geo_targeting("Самара") == {"region_ids": [51]}


async def test_group_without_regions_is_rejected_instead_of_targeting_whole_world(adapter, api, plan):
    """Без region_ids Директ показывал бы рекламу по всему миру (RegionIds [0]): адаптер не подставляет умолчание."""
    group = replace(plan.groups[0], targeting={})
    with pytest.raises(ValueError, match="RegionIds"):
        await adapter.create_campaign_tree(replace(plan, groups=(group,)))
    assert not api.calls
