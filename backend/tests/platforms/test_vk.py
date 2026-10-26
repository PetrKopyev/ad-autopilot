import json
from datetime import UTC, date, datetime, timedelta
from types import SimpleNamespace
from urllib.parse import parse_qs

import httpx
import pytest
import respx
from PIL import Image

from app.platforms.base import (
    AdGroupSpec,
    AdPlatform,
    AdSpec,
    AuthExpired,
    CampaignTreeSpec,
    ExternalRefs,
    ModerationRejected,
    Operation,
    PlatformError,
    RateLimited,
    TargetSpec,
)
from app.platforms.vk import VkAdapter, _kop, _rub

API = "https://ads.vk.com/api/v2"


def configuration() -> SimpleNamespace:
    return SimpleNamespace(
        vk_client_id="vk-app",
        vk_client_secret="vk-secret",
        app_base_url="https://autopilot.example",
        tz_default="Europe/Moscow",
    )


@pytest.fixture
def api():
    with respx.mock(assert_all_called=False) as router:
        yield router


@pytest.fixture
async def adapter(api):
    async with VkAdapter("tok", config=configuration(), href="https://shop.example/", marking_confirmed=True) as vk:
        yield vk


def tree(**changes) -> CampaignTreeSpec:
    values = {
        "id": "c1",
        "name": "Маникюр",
        "daily_limit_kop": 15050,
        "period_cap_kop": 450_000,
        "groups": (
            AdGroupSpec(
                "g1",
                "Ищут маникюр",
                (AdSpec("a1", "Маникюр рядом", "Запишитесь онлайн", image_ref="501"),),
                (TargetSpec("t1", "interest", "1234"),),
                {"regions": [2], "age_from": 25, "age_to": 40, "sex": ["female"]},
            ),
        ),
        **changes,
    }
    return CampaignTreeSpec(**values)


def created_plan() -> dict:
    return {
        "id": 10,
        "ad_groups": [{"id": 20, "name": "Ищут маникюр ap-g1", "banners": [{"id": 30, "name": "Маникюр рядом ap-a1"}]}],
    }


def mock_creation(api: respx.MockRouter) -> respx.Route:
    api.get(f"{API}/ad_plans.json").respond(json={"count": 0, "items": []})
    api.post(f"{API}/urls.json").respond(json={"id": 7})
    return api.post(f"{API}/ad_plans.json").respond(json=created_plan())


def sent(route: respx.Route, index: int = 0) -> dict:
    return json.loads(route.calls[index].request.content)


def test_adapter_implements_platform_protocol() -> None:
    assert isinstance(VkAdapter("tok", config=configuration()), AdPlatform)


@pytest.mark.parametrize(
    ("kop", "rub"), [(0, "0.00"), (1, "0.01"), (100, "1.00"), (15050, "150.50"), (123_456_789, "1234567.89")]
)
def test_money_is_exact(kop: int, rub: str) -> None:
    assert _rub(kop) == rub and _kop(rub) == kop


@pytest.mark.parametrize(("rub", "kop"), [("12.345", 1235), ("0.1", 10), (7, 700), ("19.99", 1999), ("0.285", 29)])
def test_money_from_vk_rounds_half_up_without_float_errors(rub, kop: int) -> None:
    assert _kop(rub) == kop
    with pytest.raises(ValueError):
        _kop("-1")


async def test_create_tree_posts_whole_plan_with_exact_budgets(adapter: VkAdapter, api) -> None:
    route = mock_creation(api)
    refs = await adapter.create_campaign_tree(tree())
    assert refs == ExternalRefs("10", {"g1": "20"}, {"a1": "30"}, {"t1": "20"})
    body = sent(route)
    assert body["name"] == "Маникюр ap-c1" and (body["budget_limit_day"], body["budget_limit"]) == ("150.50", "4500.00")
    assert body["status"] == "active" and "package_id" not in body["ad_groups"][0]
    group = body["ad_groups"][0]
    assert group["name"] == "Ищут маникюр ap-g1"
    assert group["targetings"] == {
        "geo": {"regions": [2]},
        "age": {"age_list": list(range(25, 41))},
        "sex": ["female"],
        "interests": [1234],
    }
    banner = group["banners"][0]
    assert banner["name"] == "Маникюр рядом ap-a1" and banner["urls"] == {"primary": {"id": 7}}
    assert banner["textblocks"] == {"title_25": {"text": "Маникюр рядом"}, "text_90": {"text": "Запишитесь онлайн"}}
    assert banner["content"] == {"image_600x600": {"id": 501}}
    assert (await adapter.create_campaign_tree(tree())) is refs
    assert route.call_count == 1


async def test_create_tree_stays_blocked_until_marking_confirmed(api) -> None:
    route = mock_creation(api)
    async with VkAdapter("tok", config=configuration(), href="https://shop.example/") as vk:
        await vk.create_campaign_tree(tree())
        assert vk.marking_instruction()
        with pytest.raises(PlatformError, match="данные рекламодателя"):
            await vk.apply(Operation("set_status", "campaign", "10", "status", "paused_owner", "active"))
    body = sent(route)
    assert body["status"] == "blocked" and body["ad_groups"][0]["status"] == "blocked"
    assert body["ad_groups"][0]["banners"][0]["status"] == "blocked"


async def test_create_tree_is_idempotent_by_name_suffix(adapter: VkAdapter, api) -> None:
    api.get(f"{API}/ad_plans.json").respond(
        json={"count": 2, "items": [{"id": 10, "name": "Маникюр ap-c1"}, {"id": 99, "name": "Чужая ap-c10"}]}
    )
    api.get(f"{API}/ad_groups.json").respond(json={"count": 1, "items": [{"id": 20, "name": "Ищут маникюр ap-g1"}]})
    api.get(f"{API}/banners.json").respond(
        json={"count": 1, "items": [{"id": 30, "name": "Маникюр рядом ap-a1", "ad_group_id": 20}]}
    )
    create = api.post(f"{API}/ad_plans.json")
    refs = await adapter.create_campaign_tree(tree())
    assert refs.campaign_id == "10" and refs.ads == {"a1": "30"}
    assert not create.called
    params = parse_qs(api.calls[0].request.url.query.decode())
    assert params["_name__contains"] == ["ap-c1"]


async def test_create_tree_rejects_unsupported_input_before_requests(adapter: VkAdapter, api) -> None:
    long_title = AdSpec("a1", "x" * 26, "текст")
    with pytest.raises(PlatformError, match="до 25"):
        await adapter.create_campaign_tree(tree(groups=(AdGroupSpec("g1", "Группа", (long_title,)),)))
    phrase = (TargetSpec("t1", "phrase", "маникюр"),)
    with pytest.raises(PlatformError, match="интересы"):
        await adapter.create_campaign_tree(
            tree(groups=(AdGroupSpec("g1", "Группа", (AdSpec("a", "т", "т"),), phrase),))
        )
    with pytest.raises(PlatformError, match="положительными"):
        await adapter.create_campaign_tree(tree(daily_limit_kop=0))
    assert not api.calls


async def test_upload_image_is_multipart_with_dimensions(adapter: VkAdapter, api, tmp_path) -> None:
    file = tmp_path / "pic.png"
    Image.new("RGB", (600, 600)).save(file)
    route = api.post(f"{API}/content/static.json").respond(json={"id": 501})
    assert await adapter.upload_image(file) == "501"
    request = route.calls[0].request
    assert request.headers["content-type"].startswith("multipart/form-data")
    assert b'name="file"' in request.content and b'{"width": 600, "height": 600}' in request.content
    assert request.headers["authorization"] == "Bearer tok"
    broken = tmp_path / "broken.png"
    broken.write_bytes(b"not an image")
    with pytest.raises(PlatformError):
        await adapter.upload_image(broken)


async def test_upload_video(adapter: VkAdapter, api, tmp_path) -> None:
    route = api.post(f"{API}/content/video.json").respond(json={"id": 88})
    video = tmp_path / "clip.mp4"
    video.write_bytes(b"video")
    assert await adapter.upload_video(video) == "88"
    assert route.called


async def test_apply_updates_status_budget_text_image_targeting(adapter: VkAdapter, api) -> None:
    plan = api.post(f"{API}/ad_plans/10.json").respond(json={})
    group = api.post(f"{API}/ad_groups/20.json").respond(json={})
    banner = api.post(f"{API}/banners/30.json").respond(json={})
    api.get(f"{API}/banners/30.json").respond(json={"moderation_status": "allowed"})
    await adapter.apply(Operation("set_status", "campaign", "10", "status", "active", "paused_owner"))
    await adapter.apply(Operation("set_budget", "campaign", "10", "daily_limit_kop", 15050, 20001))
    await adapter.apply(Operation("set_budget", "campaign", "10", "period_cap_kop", 450_000, 600_005))
    await adapter.apply(Operation("set_status", "ad", "30", "status", "paused_owner", "active"))
    await adapter.apply(Operation("update_ad_text", "ad", "30", "title", "Старый", "Новый"))
    await adapter.apply(Operation("swap_ad_image", "ad", "30", "image_ref", "501", "502"))
    await adapter.apply(Operation("set_targeting", "group", "20", "targeting", {}, {"regions": [3], "age_from": 18}))
    assert [sent(plan, i) for i in range(3)] == [
        {"status": "blocked"},
        {"budget_limit_day": "200.01"},
        {"budget_limit": "6000.05"},
    ]
    assert [sent(banner, i) for i in range(3)] == [
        {"status": "active"},
        {"textblocks": {"title_25": {"text": "Новый"}}},
        {"content": {"image_600x600": {"id": 502}}},
    ]
    assert sent(group)["targetings"]["age"]["age_list"] == list(range(18, 76))
    await adapter.apply(Operation("set_status", "campaign", "10", "status", "active", "paused_owner"))
    assert plan.call_count == 4


async def test_apply_refuses_unsafe_changes(adapter: VkAdapter, api) -> None:
    api.get(f"{API}/banners/30.json").respond(
        json={"moderation_status": "banned", "moderation_reasons": [{"description": "Запрещённый товар"}]}
    )
    with pytest.raises(ModerationRejected, match="Запрещённый товар"):
        await adapter.apply(Operation("set_status", "ad", "30", "status", "paused_owner", "active"))
    with pytest.raises(PlatformError, match="Нулевой"):
        await adapter.apply(Operation("set_budget", "campaign", "10", "daily_limit_kop", 100, 0))
    with pytest.raises(PlatformError, match="до 25"):
        await adapter.apply(Operation("update_ad_text", "ad", "30", "title", "a", "x" * 26))
    with pytest.raises(PlatformError, match="цели"):
        await adapter.apply(Operation("set_status", "target", "20", "status", "active", "paused_owner"))


async def test_get_state_maps_entities(adapter: VkAdapter, api) -> None:
    api.get(f"{API}/ad_plans.json").respond(
        json={
            "count": 1,
            "items": [
                {"id": 10, "name": "П", "status": "blocked", "budget_limit_day": "150.50", "budget_limit": "4500"}
            ],
        }
    )
    api.get(f"{API}/ad_groups.json").respond(
        json={
            "count": 1,
            "items": [
                {
                    "id": 20,
                    "name": "Г",
                    "status": "active",
                    "ad_plan_id": 10,
                    "targetings": {"geo": {"regions": [2]}, "interests": [5]},
                }
            ],
        }
    )
    api.get(f"{API}/banners.json").respond(
        json={
            "count": 1,
            "items": [
                {
                    "id": 30,
                    "name": "Б",
                    "status": "deleted",
                    "ad_group_id": 20,
                    "textblocks": {"title_25": {"text": "З"}, "text_90": {"text": "Т"}},
                    "content": {"image_600x600": {"id": 501}},
                }
            ],
        }
    )
    state = await adapter.get_state(ExternalRefs("10", {"g1": "20"}, {"a1": "30"}, {}))
    assert (state["10"].status, state["10"].daily_limit_kop, state["10"].period_cap_kop) == (
        "paused_owner",
        15050,
        450_000,
    )
    assert state["20"].parent_id == "10" and state["20"].targeting["regions"] == [2] and state["20"].status == "active"
    assert (state["30"].status, state["30"].title, state["30"].text, state["30"].image_ref) == (
        "archived",
        "З",
        "Т",
        "501",
    )


async def test_get_moderation_maps_status_and_reasons(adapter: VkAdapter, api) -> None:
    api.get(f"{API}/banners.json").respond(
        json={
            "count": 3,
            "items": [
                {"id": 1, "moderation_status": "allowed"},
                {
                    "id": 2,
                    "moderation_status": "banned",
                    "moderation_reasons": [{"description": "Нужна лицензия"}, "Текст"],
                },
                {"id": 3, "moderation_status": "pending"},
            ],
        }
    )
    result = await adapter.get_moderation(ExternalRefs("10", {}, {"a": "1", "b": "2", "c": "3"}, {}))
    assert [(item.entity_id, item.status, item.reason) for item in result] == [
        ("1", "approved", None),
        ("2", "rejected", "Нужна лицензия; Текст"),
        ("3", "pending", None),
    ]
    assert await adapter.get_moderation(ExternalRefs("10", {}, {}, {})) == []


async def test_get_stats_converts_rubles_to_kopecks_and_maps_campaign(adapter: VkAdapter, api) -> None:
    today = datetime.now(adapter._tz).date()
    api.get(f"{API}/ad_groups.json").respond(json={"count": 1, "items": [{"id": 20, "ad_plan_id": 10}]})
    api.get(f"{API}/banners.json").respond(json={"count": 1, "items": [{"id": 30, "ad_group_id": 20}]})
    route = api.get(f"{API}/statistics/banners/day.json").respond(
        json={
            "items": [
                {
                    "id": 30,
                    "rows": [
                        {"date": "2026-10-20", "base": {"shows": 1000, "clicks": 25, "spent": "12.345", "goals": 3}},
                        {"date": today.isoformat(), "base": {"shows": 10, "clicks": 1, "spent": "0.10"}},
                    ],
                }
            ]
        }
    )
    rows = await adapter.get_stats(date(2026, 10, 20), today, "ad")
    first, last = rows
    assert (first.entity_id, first.campaign_id, first.level) == ("30", "10", "ad")
    assert (first.impressions, first.clicks, first.cost_kop, first.conversions) == (1000, 25, 1235, {"all": 3})
    assert first.is_settled and not last.is_settled and last.conversions == {"all": 0}
    query = parse_qs(route.calls[0].request.url.query.decode())
    assert query["date_from"] == ["2026-10-20"] and query["metrics"] == ["base"]


async def test_get_spend_today_sums_campaigns(adapter: VkAdapter, api) -> None:
    today = datetime.now(adapter._tz).date().isoformat()
    api.get(f"{API}/statistics/ad_plans/day.json").respond(
        json={
            "items": [
                {"id": 10, "rows": [{"date": today, "base": {"shows": 5, "clicks": 1, "spent": "10.10"}}]},
                {"id": 11, "rows": [{"date": today, "base": {"shows": 5, "clicks": 1, "spent": "5.05"}}]},
            ]
        }
    )
    assert await adapter.get_spend_today() == 1515


async def test_get_stats_rejects_broken_answers(adapter: VkAdapter, api) -> None:
    api.get(f"{API}/statistics/ad_plans/day.json").respond(json={"items": [{"id": 1, "rows": [{"date": "bad"}]}]})
    with pytest.raises(PlatformError):
        await adapter.get_stats(date(2026, 10, 1), date(2026, 10, 2), "campaign")
    with pytest.raises(ValueError):
        await adapter.get_stats(date(2026, 10, 3), date(2026, 10, 2))


async def test_unauthorized_means_auth_expired(adapter: VkAdapter, api) -> None:
    api.get(f"{API}/user.json").respond(401, json={"error": {"code": "invalid_token"}})
    with pytest.raises(AuthExpired):
        await adapter.whoami()
    async with VkAdapter(config=configuration()) as anonymous:
        with pytest.raises(AuthExpired):
            await anonymous.whoami()


async def test_rate_limit_headers_block_next_request(adapter: VkAdapter, api) -> None:
    reset = int((datetime.now(UTC) + timedelta(seconds=30)).timestamp())
    route = api.get(f"{API}/user.json").respond(
        json={"id": 5}, headers={"X-RateLimit-Remaining-Sec": "0", "X-RateLimit-Sec-Reset": str(reset)}
    )
    await adapter.whoami()
    limits = await adapter.limits()
    assert limits.rate_limit_remaining == 0 and limits.resets_at is not None
    with pytest.raises(RateLimited) as error:
        await adapter.whoami()
    assert 1 <= error.value.retry_after <= 31 and route.call_count == 1


async def test_http_429_and_server_errors(adapter: VkAdapter, api) -> None:
    api.get(f"{API}/user.json").respond(429, headers={"Retry-After": "7"})
    with pytest.raises(RateLimited) as error:
        await adapter.whoami()
    assert error.value.retry_after == 7
    api.get(f"{API}/user.json").respond(500)
    async with VkAdapter("tok", config=configuration()) as other:
        with pytest.raises(PlatformError) as failure:
            await other.whoami()
    assert failure.value.status_code == 500
    api.get(f"{API}/user.json").mock(side_effect=httpx.ConnectError("secret-host"))
    async with VkAdapter("tok", config=configuration()) as other:
        with pytest.raises(PlatformError) as failure:
            await other.whoami()
    assert "secret-host" not in str(failure.value)


async def test_marking_for_individual_goes_to_ord_user(api) -> None:
    route = api.post(f"{API}/ord_user.json").respond(json={})
    profile = {"type": "self_employed", "inn": "500100732259", "name": "Иванова Анна", "phone": "+79990000000"}
    async with VkAdapter("tok", config=configuration()) as vk:
        assert vk.marking_instruction()
        await vk.set_marking(profile)
        assert vk.marking_instruction() is None
        with pytest.raises(ValueError):
            await vk.set_marking({"type": "ip"})
    assert sent(route) == {"name": "Иванова Анна", "phone": "+79990000000", "inn": "500100732259"}


async def test_marking_for_company_gives_instruction_without_request(api) -> None:
    route = api.post(f"{API}/ord_user.json")
    async with VkAdapter("tok", config=configuration()) as vk:
        await vk.set_marking({"type": "ooo", "inn": "7707083893", "name": "ООО Ромашка", "phone": "+79990000000"})
        assert "Маркировка" in vk.marking_instruction()
    assert not route.called


async def test_balance_hint_and_min_budgets(adapter: VkAdapter, api) -> None:
    api.get(f"{API}/user.json").respond(json={"id": 5, "account": {"balance": "250.75"}})
    assert await adapter.get_balance_hint() == 25075
    api.get(f"{API}/user.json").respond(json={"id": 5})
    assert await adapter.get_balance_hint() is None
    assert (await adapter.min_budgets())["daily_limit_kop"] > 0
