import json
import shutil
from io import BytesIO
from pathlib import Path

import pytest
from PIL import Image
from test_onboarding import (
    BUDGET_KOP,
    NOW,
    TODAY,
    ScriptedLLM,
    World,
    make_world,
    onboard,
    page_text,
    picture,
)

from app.content.ad_text import MEDICAL_DISCLAIMER, SEGMENTS
from app.content.art import ART_COST_KOP
from app.db.models import ArtCall, Asset, LLMCall
from app.services.onboarding import AiLimits, Blocked, OnboardingError
from app.services.plan import launch_blockers


@pytest.fixture
def world(tmp_path: Path) -> World:
    return make_world(tmp_path)


async def build(world: World) -> dict:
    await onboard(world)
    return await world.plans.get_plan(world.business.id, NOW, TODAY)


def assets_of(world: World, plan: dict, platform: str = "direct") -> list[dict]:
    return [asset for group in plan["groups"] if group["platform"] == platform for asset in group["assets"]]


async def test_plan_requires_approved_description(world: World) -> None:
    with pytest.raises(Blocked, match="описание"):
        await world.plans.get_plan(world.business.id, NOW, TODAY)


async def test_plan_groups_cover_segments_for_each_platform(world: World) -> None:
    plan = await build(world)
    segments = SEGMENTS["nails"]
    assert [(group["platform"], group["audience"].lower()) for group in plan["groups"]] == [
        (platform, segment) for platform in ("direct", "vk") for segment in segments
    ]
    assert plan["status"] == "draft" and plan["month_budget_kop"] == BUDGET_KOP
    assert world.business.status == "plan_ready"
    stored = world.repository.latest_plan(world.business.id)
    assert stored.version == 1 and stored.payload["groups"][0]["ads"][0]["title"] == "Маникюр классический"
    assert all(group["ads"] and group["ads"][0]["text"] for group in plan["groups"])


async def test_budget_split_follows_domain_rules(world: World) -> None:
    plan = await build(world)
    assert plan["direct_budget_kop"] + plan["vk_budget_kop"] == BUDGET_KOP
    assert plan["direct_budget_kop"] == BUDGET_KOP * 70 // 100
    stored = world.repository.latest_plan(world.business.id).payload
    daily = {
        platform: sum(g["daily_limit_kop"] for g in stored["groups"] if g["platform"] == platform)
        for platform in ("direct", "vk")
    }
    # первый месяц целиком: резерв 8 %, дневной лимит не выше 1,3 от среднего
    assert sum(daily.values()) <= int(BUDGET_KOP * 0.92 / 31 * 1.3)
    assert daily["direct"] > daily["vk"] > 0


async def test_single_platform_gets_whole_budget(tmp_path: Path) -> None:
    world = make_world(tmp_path, platforms=("direct",))
    plan = await build(world)
    assert plan["direct_budget_kop"] == BUDGET_KOP and plan["vk_budget_kop"] == 0
    assert {group["platform"] for group in plan["groups"]} == {"direct"}


async def test_images_are_generated_checked_and_accounted(world: World) -> None:
    plan = await build(world)
    stored = world.repository.assets(world.business.id)
    assert len(stored) == 8 and len({asset.phash for asset in stored}) == 8
    assert all((world.media / asset.file_path).is_file() and asset.cost_kop == ART_COST_KOP for asset in stored)
    assert world.repository.ai_spent(world.business.id) == 8 * ART_COST_KOP
    for group in plan["groups"]:
        assert 2 <= len(group["assets"]) <= 3 and sum(asset["selected"] for asset in group["assets"]) == 2
        assert all(asset["url"].endswith("/file") for asset in group["assets"])
    assert world.llm.calls.count("ideas") == 1


async def test_ai_limit_caps_generation_and_never_overspends(tmp_path: Path) -> None:
    limits = AiLimits(onboarding_kop=3 * ART_COST_KOP + 100, weekly_kop=2500)
    world = make_world(tmp_path, limits=limits)
    plan = await build(world)
    assert len(world.repository.assets(world.business.id)) == 3
    assert world.repository.ai_spent(world.business.id) <= limits.onboarding_kop
    assert plan["generations_remaining"] == 0


async def test_llm_spend_is_recorded_and_limit_forces_templates(tmp_path: Path) -> None:
    llm = ScriptedLLM(cost_kop=700)
    world = make_world(tmp_path, llm=llm, limits=AiLimits(onboarding_kop=2000, weekly_kop=2500))
    plan = await build(world)
    calls = world.repository.rows(LLMCall)
    assert calls and sum(call.cost_kop for call in calls) == world.repository.ai_spent(world.business.id) - sum(
        item.cost_kop for item in world.repository.rows(ArtCall)
    )
    assert llm.calls.count("ad") < 12
    # после исчерпания лимита тексты берутся из шаблонов, а не из ИИ
    stored = world.repository.latest_plan(world.business.id).payload
    assert any(ad["fallback"] for group in stored["groups"] for ad in group["ads"])
    assert all(ad["title"] and ad["text"] for group in plan["groups"] for ad in group["ads"])


async def test_same_inputs_do_not_rebuild_or_spend(world: World) -> None:
    first = await build(world)
    spent = world.repository.ai_spent(world.business.id)
    calls = list(world.llm.calls)
    second = await world.plans.get_plan(world.business.id, NOW, TODAY)
    assert second["id"] == first["id"] and world.llm.calls == calls
    assert world.repository.ai_spent(world.business.id) == spent


async def test_changed_description_builds_new_version_and_keeps_images(world: World) -> None:
    first = await build(world)
    fields = world.business.description["fields"]
    edited = {key: {"value": item["value"]} for key, item in fields.items()}
    edited["difference"] = {"value": "Работаем ежедневно и без очередей"}
    world.onboarding.save_description(world.business.id, edited, True, NOW)
    second = await world.plans.get_plan(world.business.id, NOW, TODAY)
    assert second["id"] != first["id"]
    assert world.repository.latest_plan(world.business.id).version == 2
    assert len(world.repository.assets(world.business.id)) == 8
    assert sorted(a["id"] for a in assets_of(world, first)) == sorted(a["id"] for a in assets_of(world, second))


async def test_medical_ads_carry_disclaimer(tmp_path: Path) -> None:
    world = make_world(tmp_path, pages=page_text("Услуги косметолога и ботокс."))
    plan = await build(world)
    assert plan["medical_level"] == "required"
    stored = world.repository.latest_plan(world.business.id).payload
    assert all(MEDICAL_DISCLAIMER in ad["text"] for group in stored["groups"] for ad in group["ads"])


async def test_ad_text_never_contains_unapproved_numbers(tmp_path: Path) -> None:
    bad = json.dumps({"title": "Скидка 50%", "title2": "", "text": "Звоните 8 800 555-35-35"})
    world = make_world(tmp_path, llm=ScriptedLLM(ad=bad))
    await build(world)
    stored = world.repository.latest_plan(world.business.id).payload
    assert all(ad["fallback"] for group in stored["groups"] for ad in group["ads"])
    assert not any("50" in ad["title"] or "800" in ad["text"] for group in stored["groups"] for ad in group["ads"])


async def test_regenerate_adds_two_images_and_spends_quota(world: World) -> None:
    plan = await build(world)
    group = plan["groups"][0]
    before = plan["generations_remaining"]
    result = await world.plans.regenerate_images(world.business.id, group["id"], NOW, TODAY)
    updated = next(item for item in result["groups"] if item["id"] == group["id"])
    assert len(updated["assets"]) == len(group["assets"]) + 2
    assert not any(asset["selected"] for asset in updated["assets"][len(group["assets"]) :])
    assert result["generations_remaining"] == before - 1
    assert world.repository.ai_spent(world.business.id) == 10 * ART_COST_KOP


async def test_regenerate_stops_at_limit_without_charging(tmp_path: Path) -> None:
    world = make_world(tmp_path, limits=AiLimits(onboarding_kop=8 * ART_COST_KOP + 100, weekly_kop=2500))
    plan = await build(world)
    spent = world.repository.ai_spent(world.business.id)
    with pytest.raises(Blocked, match="Лимит"):
        await world.plans.regenerate_images(world.business.id, plan["groups"][0]["id"], NOW, TODAY)
    assert world.repository.ai_spent(world.business.id) == spent


async def test_regenerate_unknown_group(world: World) -> None:
    await build(world)
    with pytest.raises(LookupError):
        await world.plans.regenerate_images(world.business.id, 999, NOW, TODAY)


async def test_upload_photo_is_checked_and_selected(world: World) -> None:
    plan = await build(world)
    group = plan["groups"][0]
    data = picture(777)
    result = world.plans.upload_photo(world.business.id, group["id"], data, TODAY)
    uploaded = next(asset for asset in result["groups"][0]["assets"] if asset["kind"] == "upload")
    assert uploaded["selected"]
    with pytest.raises(OnboardingError, match="Фото не подошло"):
        world.plans.upload_photo(world.business.id, group["id"], data, TODAY)
    with pytest.raises(OnboardingError):
        world.plans.upload_photo(world.business.id, group["id"], b"not an image", TODAY)
    tiny = BytesIO()
    Image.new("RGB", (40, 40), "red").save(tiny, format="PNG")
    with pytest.raises(OnboardingError):
        world.plans.upload_photo(world.business.id, group["id"], tiny.getvalue(), TODAY)


async def test_select_assets_updates_flags_and_rejects_foreign_ids(world: World) -> None:
    plan = await build(world)
    ids = [asset["id"] for asset in plan["groups"][0]["assets"]][:1]
    world.plans.select_assets(world.business.id, ids)
    assert [asset.id for asset in world.repository.assets(world.business.id) if asset.selected] == ids
    with pytest.raises(OnboardingError):
        world.plans.select_assets(world.business.id, [9999])
    world.repository.add(Asset(business_id=99, kind="image", file_path="x.jpg"))
    with pytest.raises(OnboardingError):
        world.plans.select_assets(world.business.id, [world.repository.assets(99)[0].id])


@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="Видео собирает ffmpeg")
async def test_video_is_built_from_three_selected_images(world: World) -> None:
    plan = await build(world)
    segment_assets = plan["groups"][0]["assets"]
    world.plans.select_assets(world.business.id, [asset["id"] for asset in segment_assets])
    if len(segment_assets) < 3:
        pytest.skip("В сегменте меньше трёх картинок")
    videos = [asset for asset in world.repository.assets(world.business.id) if asset.kind == "video"]
    assert videos and (world.media / videos[0].file_path).stat().st_size > 0


async def test_readiness_reflects_blockers(world: World) -> None:
    await onboard(world, marking=False)
    plan = await world.plans.get_plan(world.business.id, NOW, TODAY)
    assert not plan["ready_to_launch"] and not plan["marking_confirmed"]
    assert any("маркировку" in reason for reason in plan["blockers"])
    world.onboarding.save_marking(
        world.business.id, "ip", "526317984689", "ИП Иванов", "89001234567", {"direct": True, "vk": True}
    )
    plan = await world.plans.get_plan(world.business.id, NOW, TODAY)
    assert plan["ready_to_launch"] and plan["marking_confirmed"] and plan["blockers"] == []


async def test_blockers_cover_every_launch_rule(world: World) -> None:
    await build(world)
    business = world.business
    plan = world.repository.latest_plan(business.id)

    def reasons(acknowledged: bool | None = None) -> list[str]:
        return launch_blockers(
            business,
            plan,
            world.repository.accounts(business.id),
            world.repository.marking(business.id),
            world.repository.assets(business.id),
            acknowledged,
        )

    assert reasons() == []
    business.metrica_counter_id = None
    assert any("счётчика" in text for text in reasons())
    business.metrica_counter_id = 12345
    business.description_approved_at = None
    assert any("описание" in text for text in reasons())
    business.description_approved_at = NOW
    world.repository.marking(business.id).confirmed_in_platform = {"direct": True, "vk": False}
    assert any("маркировку" in text for text in reasons())
    world.repository.marking(business.id).confirmed_in_platform = {"direct": True, "vk": True}
    business.month_budget_kop += 100
    assert any("изменились" in text for text in reasons())
    business.month_budget_kop -= 100
    for asset in world.repository.assets(business.id):
        asset.selected = False
    assert any("картинку" in text for text in reasons())
    business.medical_level = "maybe"
    assert reasons(False) and not any("лицензию" in text for text in reasons(None))
    assert any("лицензию" in text for text in reasons(False))
