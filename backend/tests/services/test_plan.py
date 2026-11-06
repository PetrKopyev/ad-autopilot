import asyncio
import json
import shutil
import threading
from contextlib import contextmanager
from copy import deepcopy
from dataclasses import replace
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace

import pytest
from PIL import Image

from app.content.ad_text import MEDICAL_DISCLAIMER, SEGMENTS
from app.content.art import ART_COST_KOP
from app.content.ideas import subject
from app.content.images import check_image, run_decoding
from app.content.verify import Service
from app.db.models import Ad, ArtCall, Asset, LLMCall, Plan
from app.domain.settings import DEFAULT_SETTINGS
from app.services import plan as plan_module
from app.services.changes import ChangeConflict
from app.services.onboarding import AiLimits, Blocked, NotFound, OnboardingError
from app.services.plan import direct_services, group_key, group_slots, launch_blockers, phrase
from tests.services.test_onboarding import (
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


@pytest.fixture
def world(tmp_path: Path) -> World:
    return make_world(tmp_path)


async def build(world: World) -> dict:
    """Картинки плана одни и те же при каждом запуске: зерно — счётчик, picture(seed) рисует по Random(seed); в первом
    сегменте их четыре и больше."""
    await onboard(world)
    return await world.plans.get_plan(world.business.id, NOW, TODAY)


def assets_of(world: World, plan: dict, platform: str = "direct") -> list[dict]:
    return [asset for group in plan["groups"] if group["platform"] == platform for asset in group["assets"]]


async def test_plan_requires_approved_description(world: World) -> None:
    with pytest.raises(Blocked, match="описание"):
        await world.plans.get_plan(world.business.id, NOW, TODAY)


async def test_direct_groups_are_services_and_vk_groups_cover_segments(world: World) -> None:
    plan = await build(world)
    segments = SEGMENTS["nails"]
    # Услуга одна: в Директе одна группа, вторая с теми же фразами спорила бы с ней за запрос.
    assert [(group["platform"], group["name"].split(" — ")[1]) for group in plan["groups"]] == [
        ("direct", segments[0]),
        *(("vk", segment) for segment in segments),
    ]
    assert plan["groups"][0]["audience"] == (
        "Тем, кто ищет в Яндексе «маникюр классический». Регион: Самара. Показываем в поиске и на других сайтах, "
        "где Яндекс размещает рекламу."
    )
    assert plan["groups"][-1]["audience"] == (
        "Регион: Самара. Пол, возраст и интересы не задаём: VK сам показывает рекламу тем, кто скорее оставит заявку "
        "на сайте."
    )
    assert plan["status"] == "draft" and plan["month_budget_kop"] == BUDGET_KOP
    assert world.business.status == "plan_ready"
    stored = world.repository.latest_plan(world.business.id)
    assert stored.version == 1 and stored.payload["groups"][0]["ads"][0]["title"] == "Маникюр классический"
    assert all(group["ads"] and group["ads"][0]["text"] for group in plan["groups"])


async def test_vk_is_off_when_its_share_misses_the_platform_minimum_and_joins_after_budget_raise(world: World) -> None:
    world.business.month_budget_kop = 3_000_000
    plan = await build(world)
    assert {group["platform"] for group in plan["groups"]} == {"direct"}
    assert (plan["direct_budget_kop"], plan["vk_budget_kop"]) == (3_000_000, 0)
    # Январь — 31 день: весь бюджет за вычетом резерва делится на дни и запас кэпа (×1,25) у Директа.
    assert plan["daily_kop"] == {"direct": 71_225}
    assert plan["off"] == [{"platform": "vk", "from_kop": 4_850_000, "min_daily_kop": 30_000}]
    first = plan["id"]
    # Бюджет подняли: план пересобирается с VK, и владелец видит его до запуска.
    world.business.month_budget_kop = 4_850_000
    plan = await world.plans.get_plan(world.business.id, NOW, TODAY)
    assert plan["id"] != first and plan["off"] == []
    assert {group["platform"] for group in plan["groups"]} == {"direct", "vk"}
    assert plan["daily_kop"]["vk"] >= 34_500
    assert world.repository.latest_plan(world.business.id).payload["platforms"] == ["direct", "vk"]


async def test_plan_with_vk_off_waits_for_new_plan_when_vk_is_disconnected(world: World) -> None:
    world.business.month_budget_kop = 3_000_000
    plan = await build(world)
    assert plan["off"] and plan["ready_to_launch"]
    for account in world.repository.accounts(world.business.id):
        if account.platform == "vk":
            account.status = "revoked"
    # Площадки плана те же, но выключенной VK больше нет: подпись о ней устарела, план собирается заново.
    plan = await world.plans.get_plan(world.business.id, NOW, TODAY)
    assert plan["off"] == [] and {group["platform"] for group in plan["groups"]} == {"direct"}


async def test_each_direct_group_answers_only_its_service_and_ads_of_group_differ(world: World) -> None:
    await onboard(world)
    services = ["Маникюр классический", "Педикюр", "Наращивание ресниц", "Снятие покрытия", "Дизайн ногтей"]
    fields = {key: {"value": item["value"]} for key, item in world.business.description["fields"].items()}
    fields["services"] = {"value": "\n".join(services)}
    fields["prices"] = {"value": "\n".join(f"{name} — 1 500 ₽" for name in services)}
    world.onboarding.save_description(world.business.id, fields, True, NOW)
    await world.plans.get_plan(world.business.id, NOW, TODAY)
    groups = world.repository.latest_plan(world.business.id).payload["groups"]
    direct = [group for group in groups if group["platform"] == "direct"]
    vk = [group for group in groups if group["platform"] == "vk"]
    segments = SEGMENTS["nails"]
    assert [(group["service"], group["segment"]) for group in direct] == [
        (name, segments[index % 3]) for index, name in enumerate(services)
    ]
    assert [group["phrases"] for group in direct] == [[f"{name.lower()} самара"] for name in services]
    assert [(group["service"], group["segment"]) for group in vk] == [
        (name, segment) for name, segment in zip(services, segments)
    ]
    assert all(group["phrases"] == [] for group in vk)
    plan = world.plans.view(world.business, world.repository.latest_plan(world.business.id), TODAY)
    assert plan["groups"][1]["audience"].startswith("Тем, кто ищет в Яндексе «педикюр». Регион: Самара.")
    assert plan["direct_services_total"] == len(services)
    assert plan["direct_campaigns"] == [{"services": [name]} for name in services]
    assert plan["direct_campaigns_note"] is None
    assert all(group["ads"][0]["text"] != group["ads"][1]["text"] for group in groups)


async def test_direct_groups_skip_repeated_phrases_and_stop_at_cap(world: World) -> None:
    await onboard(world)
    services = [
        "Маникюр классический",
        "Маникюр классический без покрытия",
        "Маникюр классический с покрытием гель-лак",
        *(f"Услуга {index}" for index in range(1, 6)),
    ]
    fields = {key: {"value": item["value"]} for key, item in world.business.description["fields"].items()}
    fields["services"] = {"value": "\n".join(services)}
    fields["prices"] = {"value": ""}
    world.onboarding.save_description(world.business.id, fields, True, NOW)
    await world.plans.get_plan(world.business.id, NOW, TODAY)
    groups = world.repository.latest_plan(world.business.id).payload["groups"]
    direct = [group for group in groups if group["platform"] == "direct"]
    # «Маникюр классический с покрытием гель-лак» даёт фразу «маникюр классический с покрытием», а не повтор.
    assert [group["service"] for group in direct] == services[:3] + ["Услуга 1", "Услуга 2"]
    phrases = [phrase for group in direct for phrase in group["phrases"]]
    assert len(phrases) == len(set(phrases)) == plan_module.MAX_SERVICE_GROUPS
    # Владелец видит, что часть услуг не вошла: без потолка групп было бы восемь.
    plan = world.plans.view(world.business, world.repository.latest_plan(world.business.id), TODAY)
    assert plan["direct_services_total"] == 8


async def test_images_follow_service_of_each_group(world: World) -> None:
    await onboard(world)
    services = [
        "Маникюр с гель-лаком",
        "Маникюр без покрытия",
        "Педикюр с покрытием",
        "Наращивание ногтей",
        "Снятие покрытия",
    ]
    fields = {key: {"value": item["value"]} for key, item in world.business.description["fields"].items()}
    fields["services"] = {"value": "\n".join(services)}
    fields["prices"] = {"value": "\n".join(f"{name} — 1 500 ₽" for name in services)}
    world.onboarding.save_description(world.business.id, fields, True, NOW)
    plan = await world.plans.get_plan(world.business.id, NOW, TODAY)
    stored = world.repository.latest_plan(world.business.id).payload["groups"]
    assets = {asset.id: asset for asset in world.repository.assets(world.business.id)}
    # Пять групп Директа — пять разных пар (услуга, сегмент), по две картинки на пару одним вызовом идей; три группы
    # VK — те же пары, что у первых трёх групп Директа, и те же картинки.
    assert len(stored) == 8 and len(assets) == 10 and world.llm.calls.count("ideas") == 1
    seen: dict[str, set[int]] = {}
    for group, shown in zip(stored, plan["groups"], strict=True):
        ids = {asset["id"] for asset in shown["assets"]}
        assert len(ids) == 2 and seen.setdefault(group_key(group), ids) == ids
        assert all(assets[id_].prompt.startswith(subject(group["service"], "nails")) for id_ in ids)
        assert all(asset["label"].startswith("Идея ") for asset in shown["assets"])
    assert len({id_ for ids in seen.values() for id_ in ids}) == 10
    pedicure = next(shown for group, shown in zip(stored, plan["groups"]) if group["service"] == "Педикюр с покрытием")
    assert all(assets[asset["id"]].prompt.startswith("Женские ступни") for asset in pedicure["assets"])
    assert all(asset.prompt.endswith("без текста и надписей.") for asset in assets.values())


def add_service(world: World, name: str) -> None:
    fields = {key: {"value": item["value"]} for key, item in world.business.description["fields"].items()}
    fields["services"] = {"value": f"Маникюр классический\n{name}"}
    fields["prices"] = {"value": f"Маникюр классический — 1 500 ₽\n{name} — 2 500 ₽"}
    world.onboarding.save_description(world.business.id, fields, True, NOW)


def keyed_assets(world: World) -> dict[str, list[int]]:
    return world.repository.latest_plan(world.business.id).payload["assets"]


async def test_rebuild_keeps_images_of_same_pair_and_generates_for_new_pairs(world: World) -> None:
    await build(world)
    before = deepcopy(keyed_assets(world))
    add_service(world, "Педикюр")
    plan = await world.plans.get_plan(world.business.id, NOW, TODAY)
    after = keyed_assets(world)
    # Педикюр получает свою группу Директа со вторым сегментом, и вторая группа VK переходит к той же паре.
    kept = [f"Маникюр классический — {SEGMENTS['nails'][index]}" for index in (0, 2)]
    assert all(after[key] == before[key] for key in kept)
    old_ids = {id_ for ids in before.values() for id_ in ids}
    new_keys = [key for key in after if key.startswith("Педикюр — ")]
    assert set(after) == {*kept, *new_keys} and new_keys == [f"Педикюр — {SEGMENTS['nails'][1]}"]
    assert all(len(after[key]) == 2 and not set(after[key]) & old_ids for key in new_keys)
    assets = {asset.id: asset for asset in world.repository.assets(world.business.id)}
    assert all(assets[id_].prompt.startswith("Женские ступни") for key in new_keys for id_ in after[key])
    assert world.llm.calls.count("ideas") == 2
    assert all(any(asset["selected"] for asset in group["assets"]) for group in plan["groups"])


async def test_old_plan_images_are_not_moved_to_service_pairs(world: World) -> None:
    await build(world)
    stored = world.repository.latest_plan(world.business.id)
    # План до разбивки по услугам: картинки по сегменту, у групп нет service.
    old = {group["segment"]: ids for group, ids in zip(stored.payload["groups"], stored.payload["assets"].values())}
    groups = [{key: value for key, value in group.items() if key != "service"} for group in stored.payload["groups"]]
    stored.payload = stored.payload | {"assets": old, "groups": groups, "inputs_hash": "old"}
    old_ids = {id_ for ids in old.values() for id_ in ids}
    await world.plans.get_plan(world.business.id, NOW, TODAY)
    after = keyed_assets(world)
    assert set(after) == {f"Маникюр классический — {segment}" for segment in SEGMENTS["nails"]}
    assert not {id_ for ids in after.values() for id_ in ids} & old_ids
    assert sum(len(ids) for ids in after.values()) == 8


async def test_rebuild_removes_generated_images_left_out_of_new_plan(world: World) -> None:
    await build(world)
    stored = world.repository.latest_plan(world.business.id)
    old = {group["segment"]: ids for group, ids in zip(stored.payload["groups"], stored.payload["assets"].values())}
    stored.payload = stored.payload | {"assets": old, "inputs_hash": "old"}
    images = world.repository.assets(world.business.id)
    assert any(asset.selected for asset in images)
    in_ad = images[-1]
    world.repository.add(Ad(ad_group_id=1, title="Т", text="Т", asset_id=in_ad.id))
    upload = Asset(business_id=world.business.id, kind="upload", file_path=f"{world.business.id}/upload.jpg")
    world.repository.add(upload)
    (world.media / upload.file_path).write_bytes(picture(777))
    gone = images[:-1]
    assert all((world.media / asset.file_path).is_file() for asset in gone)
    await world.plans.get_plan(world.business.id, NOW, TODAY)
    left = {asset.id for asset in world.repository.assets(world.business.id)}
    # Выбранные картинки тоже уходят: к новым парам они не переходят, и убрать их в интерфейсе было бы нельзя.
    assert {in_ad.id, upload.id} <= left
    assert not left & {asset.id for asset in gone}
    assert not any((world.media / asset.file_path).exists() for asset in gone)
    assert (world.media / upload.file_path).is_file() and (world.media / in_ad.file_path).is_file()


async def test_rebuild_removes_videos_of_removed_images_unless_ads_use_them(world: World) -> None:
    await build(world)
    stored = world.repository.latest_plan(world.business.id)
    stored.payload = stored.payload | {"assets": {}, "inputs_hash": "old"}
    images = world.repository.assets(world.business.id)
    videos = []
    for name in ("free", "in_ad"):
        video = Asset(
            business_id=world.business.id,
            kind="video",
            file_path=f"{world.business.id}/{name}.mp4",
            formats={"sources": [asset.id for asset in images[:3]]},
        )
        world.repository.add(video)
        (world.media / video.file_path).write_bytes(b"video")
        videos.append(video)
    free, in_ad = videos
    world.repository.add(Ad(ad_group_id=1, title="Т", text="Т", video_id=in_ad.id))
    await world.plans.get_plan(world.business.id, NOW, TODAY)
    left = {asset.id for asset in world.repository.assets(world.business.id)}
    assert free.id not in left and not (world.media / free.file_path).exists()
    assert in_ad.id in left and (world.media / in_ad.file_path).is_file()


async def test_rebuild_moves_uploads_to_group_of_same_segment(world: World) -> None:
    plan = await build(world)
    for group, seed in zip(plan["groups"], (777, 778, 779), strict=False):
        world.plans.upload_photo(world.business.id, group["id"], picture(seed), TODAY)
    uploads = [asset for asset in world.repository.assets(world.business.id) if asset.kind == "upload"]
    fields = {key: {"value": item["value"]} for key, item in world.business.description["fields"].items()}
    fields["services"] = {"value": "Педикюр"}
    fields["prices"] = {"value": "Педикюр — 2 500 ₽"}
    world.onboarding.save_description(world.business.id, fields, True, NOW)
    plan = await world.plans.get_plan(world.business.id, NOW, TODAY)
    assert all(group["name"].startswith("Педикюр — ") for group in plan["groups"])
    for group, upload in zip(plan["groups"][: len(uploads)], uploads, strict=True):
        assert upload.id in {asset["id"] for asset in group["assets"]}
    assert all(upload.selected for upload in uploads)


def test_uploads_without_their_segment_go_to_first_group_of_same_platform(world: World) -> None:
    previous = Plan(
        payload={
            "groups": [{"platform": "vk", "segment": "старый", "service": "Маникюр"}],
            "assets": {"Маникюр — старый": [1]},
        }
    )
    world.repository.add(Asset(business_id=world.business.id, kind="upload", file_path="u.jpg"))
    slots = [("direct", "Маникюр", "рядом"), ("vk", "Педикюр", "рядом"), ("vk", "Маникюр", "далеко")]
    assets: dict[str, list[int]] = {}
    world.plans.carry_uploads(world.business.id, previous, slots, assets)
    assert assets == {"Педикюр — рядом": [1]}


async def test_failed_rebuild_keeps_files_of_images_it_did_not_remove(world: World, monkeypatch) -> None:
    await build(world)
    stored = world.repository.latest_plan(world.business.id)
    stored.payload = stored.payload | {"assets": {}, "inputs_hash": "old"}
    unselected = [asset for asset in world.repository.assets(world.business.id) if not asset.selected]

    add = world.repository.add

    def fail(item: object) -> None:
        if isinstance(item, Plan):
            raise RuntimeError("commit failed")
        add(item)

    monkeypatch.setattr(world.repository, "add", fail)
    with pytest.raises(RuntimeError):
        await world.plans.get_plan(world.business.id, NOW, TODAY)
    assert all((world.media / asset.file_path).is_file() for asset in unselected)


async def test_upload_limit_counts_images_only(world: World, monkeypatch) -> None:
    plan = await build(world)
    images = len(world.repository.assets(world.business.id))
    monkeypatch.setattr(plan_module, "MAX_ASSETS", images + 1)
    world.repository.add(Asset(business_id=world.business.id, kind="video", file_path="v.mp4", selected=True))
    world.plans.upload_photo(world.business.id, plan["groups"][0]["id"], picture(777), TODAY)
    with pytest.raises(Blocked, match="слишком много"):
        world.plans.upload_photo(world.business.id, plan["groups"][0]["id"], picture(778), TODAY)


async def test_rebuild_without_ai_budget_leaves_new_pairs_without_images(tmp_path: Path) -> None:
    world = make_world(tmp_path, limits=AiLimits(onboarding_kop=9 * ART_COST_KOP + 100, weekly_kop=2500))
    await build(world)
    add_service(world, "Педикюр\nНаращивание ногтей")
    plan = await world.plans.get_plan(world.business.id, NOW, TODAY)
    after = keyed_assets(world)
    # Две новые пары, денег — на одну картинку: её получает первая пара, вторая остаётся без картинок.
    assert [len(after.get(key, [])) for key in after if not key.startswith("Маникюр")] == [1]
    assert any(not group["assets"] for group in plan["groups"])
    assert "В каждой группе отметьте хотя бы одну картинку." in plan["blockers"]
    assert world.repository.ai_spent(world.business.id) <= 9 * ART_COST_KOP + 100


async def test_group_of_repeated_service_name_writes_ads_about_first_of_them(world: World, monkeypatch) -> None:
    await onboard(world)
    real, write = plan_module.description_draft, plan_module.generate_ad_text
    seen = []

    def doubled(business):
        draft = real(business)
        first = draft.services[0]
        draft.services = [first, replace(first, price=99_900), *draft.services[1:]]
        return draft

    async def spy(llm, draft, **options):
        seen.append(options["service"])
        return await write(llm, draft, **options)

    monkeypatch.setattr(plan_module, "description_draft", doubled)
    monkeypatch.setattr(plan_module, "generate_ad_text", spy)
    await world.plans.get_plan(world.business.id, NOW, TODAY)
    first = real(world.business).services[0]
    named = [service for service in seen if service is not None and service.name == first.name]
    assert named and all(service.price == first.price for service in named)


def test_audience_of_old_direct_group_with_phrases_of_all_services_names_its_service_first() -> None:
    group = {
        "platform": "direct",
        "service": "Педикюр",
        "phrases": ["маникюр классический самара", "педикюр самара", "наращивание ресниц самара"],
    }
    assert plan_module.audience(group, "Самара").startswith(
        "Тем, кто ищет в Яндексе «педикюр», «маникюр классический», «наращивание ресниц». Регион: Самара."
    )


def test_old_plan_groups_keep_images_by_segment() -> None:
    assert group_key({"segment": "рядом с домом"}) == "рядом с домом"
    assert group_key({"segment": "рядом с домом", "service": ""}) == "рядом с домом"
    assert group_key({"segment": "рядом с домом", "service": "Педикюр"}) == "Педикюр — рядом с домом"


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


async def test_generated_images_are_checked_in_decode_queue(world: World, monkeypatch) -> None:
    calls = []

    async def spy(func, *args, **kwargs):
        calls.append(func)
        return await run_decoding(func, *args, **kwargs)

    monkeypatch.setattr(plan_module, "run_decoding", spy)
    await build(world)
    assert calls == [check_image] * 8


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


async def test_demo_regenerate_counts_down_and_labels_differ(world: World, monkeypatch) -> None:
    """В демо ИИ бесплатный: счётчик «осталось» всё равно убывает, а новые картинки подписаны разными сценами."""
    from app.content.art import ArtBudget
    from app.services.registry import DemoArt, DemoLLM

    await build(world)
    # Как в демо: траты на ИИ не пишутся и всегда ноль.
    monkeypatch.setattr(plan_module, "ai_budget", lambda *args: ArtBudget(world.plans.limits.onboarding_kop, 0))
    world.plans.llm = DemoLLM()
    world.plans.art_factory = lambda budget, record: DemoArt(budget)
    plan = await world.plans.get_plan(world.business.id, NOW, TODAY)
    group = plan["groups"][0]
    first = await world.plans.regenerate_images(world.business.id, group["id"], NOW, TODAY)
    second = await world.plans.regenerate_images(world.business.id, group["id"], NOW, TODAY)
    assert first["generations_remaining"] == plan["generations_remaining"] - 1
    assert second["generations_remaining"] == plan["generations_remaining"] - 2
    labels = [asset["label"] for asset in second["groups"][0]["assets"][len(group["assets"]) :]]
    assert len(labels) == 4 and len(set(labels)) == 4 and not any(":" in label for label in labels)


async def test_demo_regenerate_takes_scenes_not_yet_in_the_plan(world: World, monkeypatch) -> None:
    """В демо ИИ создаётся на каждый запрос: новые варианты всё равно берут сцены, которых в плане ещё нет."""
    from app.content.art import ArtBudget
    from app.services.registry import DemoArt, DemoLLM

    monkeypatch.setattr(plan_module, "ai_budget", lambda *args: ArtBudget(world.plans.limits.onboarding_kop, 0))
    world.plans.llm = DemoLLM()
    world.plans.art_factory = lambda budget, record: DemoArt(budget)
    plan = await build(world)
    group = plan["groups"][0]
    for _ in range(2):
        world.plans.llm = DemoLLM()
        plan = await world.plans.regenerate_images(world.business.id, group["id"], NOW, TODAY)
    labels = list({asset["id"]: asset["label"] for item in plan["groups"] for asset in item["assets"]}.values())
    assert len(labels) == len(set(labels)), labels


def test_labels_are_only_short_image_scenes(world: World) -> None:
    """Ключ видео «услуга — сегмент» и длинное описание кадра ранних картинок — не сцены: в used для ИИ они не идут."""
    scene = Asset(business_id=world.business.id, kind="image", idea_text="Френч с золотой фольгой", file_path="a.jpg")
    frame = Asset(business_id=world.business.id, kind="image", idea_text="Крупный план " * 10, file_path="b.jpg")
    video = Asset(business_id=world.business.id, kind="video", idea_text="Маникюр — Молодые мамы", file_path="c.mp4")
    for asset in (scene, frame, video):
        world.repository.add(asset)
    assert world.plans.labels(world.business.id, {"Маникюр — Молодые мамы": [scene.id, frame.id, video.id]}) == [
        "Френч с золотой фольгой"
    ]


async def test_demo_ideas_without_used() -> None:
    from app.services.registry import DemoLLM

    user = json.dumps({"items": [{"segment": "мамы"}]}, ensure_ascii=False)
    answer = json.loads(await DemoLLM().complete_json("визуальные идеи", user))
    assert len(answer["ideas"]) == 1


async def test_demo_llm_first_angle_title_is_phrase_not_bare_service_name() -> None:
    """Первое объявление группы в демо — тоже заголовок-фраза «услуга + повод» (шаг «Заголовки объявлений»),
    а не голое имя услуги; заходы группы получают разные заголовки."""
    from app.services.registry import DemoLLM

    llm = DemoLLM()
    user = json.dumps(
        {
            "facts": ["Маникюр", "Маникюр — 1 500 ₽"],
            "platform": "direct",
            "angle": "цена",
            "segment": "к празднику",
            "title_example": "Маникюр к празднику",
            "location": [],
        },
        ensure_ascii=False,
    )
    answer = json.loads(await llm.complete_json("Напиши объявление", user))
    assert answer["title"] == "Маникюр: праздничный образ"
    assert "1 500" in answer["text"]


async def test_demo_plan_ad_titles_are_phrases_and_differ(world: World) -> None:
    """В плане демо у групп Директа оба текста имеют разные заголовки-фразы — без голого имени услуги.
    У VK заголовок 40 знаков: длинное название услуги с поводом не влезает — там имя услуги допустимо."""
    from app.services.registry import DemoLLM

    world.plans.llm = DemoLLM()
    plan = await build(world)
    for group in plan["groups"]:
        titles = [ad["title"] for ad in group["ads"]]
        if group["platform"] == "direct":
            assert len(titles) == len(set(titles)), titles
            assert all(" " in title.strip() for title in titles), titles


async def test_regenerate_stops_at_limit_without_charging(tmp_path: Path) -> None:
    world = make_world(tmp_path, limits=AiLimits(onboarding_kop=8 * ART_COST_KOP + 100, weekly_kop=2500))
    plan = await build(world)
    spent = world.repository.ai_spent(world.business.id)
    with pytest.raises(Blocked, match="Лимит"):
        await world.plans.regenerate_images(world.business.id, plan["groups"][0]["id"], NOW, TODAY)
    assert world.repository.ai_spent(world.business.id) == spent


async def test_regenerate_without_new_images_is_blocked_and_keeps_plan(world: World, monkeypatch) -> None:
    plan = await build(world)
    stored = json.dumps(world.repository.latest_plan(world.business.id).payload, sort_keys=True)

    async def nothing(*args) -> list:
        return []

    monkeypatch.setattr(world.plans, "generate_images", nothing)
    with pytest.raises(Blocked, match="Не удалось получить новые картинки"):
        await world.plans.regenerate_images(world.business.id, plan["groups"][0]["id"], NOW, TODAY)
    assert json.dumps(world.repository.latest_plan(world.business.id).payload, sort_keys=True) == stored


async def test_regenerate_unknown_group(world: World) -> None:
    await build(world)
    with pytest.raises(NotFound):
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


async def test_upload_photo_returns_connection_before_decoding(world: World, monkeypatch) -> None:
    from app.services import plan as plan_module

    plan = await build(world)
    events = []
    monkeypatch.setattr(world.repository, "commit", lambda: events.append("commit"))
    accept = plan_module.accept_upload
    monkeypatch.setattr(
        plan_module, "accept_upload", lambda *args, **kwargs: events.append("decode") or accept(*args, **kwargs)
    )
    world.plans.upload_photo(world.business.id, plan["groups"][0]["id"], picture(777), TODAY)
    assert events[:2] == ["commit", "decode"]


async def test_select_assets_updates_flags_and_rejects_foreign_ids(world: World) -> None:
    plan = await build(world)
    ids = [asset["id"] for asset in plan["groups"][0]["assets"]][:1]
    world.plans.select_assets(world.business.id, ids)
    assert [asset.id for asset in world.repository.assets(world.business.id) if asset.selected] == ids
    with pytest.raises(NotFound):
        world.plans.select_assets(world.business.id, [9999])
    world.repository.add(Asset(business_id=99, kind="image", file_path="x.jpg"))
    with pytest.raises(NotFound):
        world.plans.select_assets(world.business.id, [world.repository.assets(99)[0].id])


async def test_selection_is_not_changed_while_business_is_locked(world: World, monkeypatch) -> None:
    """Блокировку держит запуск, пока загружает картинки на площадки: выбор посреди запуска не меняется."""
    monkeypatch.setattr("app.services.plan.LOCK_WAIT_SECONDS", 0)
    plan = await build(world)
    before = [asset.id for asset in world.repository.assets(world.business.id) if asset.selected]
    world.repository.locked = True
    with pytest.raises(ChangeConflict):
        world.plans.select_assets(world.business.id, [plan["groups"][0]["assets"][0]["id"]])
    assert [asset.id for asset in world.repository.assets(world.business.id) if asset.selected] == before


async def test_selection_waits_for_lock_like_wizard_steps(world: World, monkeypatch) -> None:
    """Выбор картинок ждёт блокировку, как шаги мастера: короткий шаг воркера или проверки сайта не даёт 409."""
    plan = await build(world)
    chosen = [plan["groups"][0]["assets"][0]["id"]]
    lock, attempts = world.repository.business_lock, []

    @contextmanager
    def released_on_second_try(business_id: int):
        attempts.append(business_id)
        if len(attempts) == 1:
            raise ChangeConflict("Этот бизнес уже обрабатывается.")
        with lock(business_id):
            yield

    monkeypatch.setattr(world.repository, "business_lock", released_on_second_try)
    assert world.plans.select_assets(world.business.id, chosen)
    assert len(attempts) == 2
    assert [asset.id for asset in world.repository.assets(world.business.id) if asset.selected] == chosen


@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="Видео собирает ffmpeg")
async def test_video_is_built_from_three_selected_images_and_rebuilt_only_on_change(world: World) -> None:
    plan = await build(world)
    segment_assets = plan["groups"][0]["assets"]
    assert len(segment_assets) >= 3
    ids = [asset["id"] for asset in segment_assets]
    assert world.plans.select_assets(world.business.id, ids)
    world.plans.build_videos(world.business.id)
    (video,) = [asset for asset in world.repository.assets(world.business.id) if asset.kind == "video"]
    first = video.file_path
    assert (world.media / first).stat().st_size > 0

    assert not world.plans.select_assets(world.business.id, ids)
    world.plans.build_videos(world.business.id)
    assert video.file_path == first

    world.plans.select_assets(world.business.id, ids[:3])
    world.plans.upload_photo(world.business.id, plan["groups"][0]["id"], picture(777), TODAY)
    world.plans.build_videos(world.business.id)
    assert video.file_path != first and video.selected
    assert not (world.media / first).exists() and (world.media / video.file_path).exists()

    world.plans.select_assets(world.business.id, ids[:2])
    world.plans.build_videos(world.business.id)
    assert not video.selected


async def test_video_is_not_saved_when_selection_changed_during_build(world: World, monkeypatch) -> None:
    from app.services import plan as plan_module

    draft = await build(world)
    ids = [asset["id"] for asset in draft["groups"][0]["assets"]]
    assert len(ids) >= 3
    world.plans.select_assets(world.business.id, ids)
    monkeypatch.setattr(plan_module.shutil, "which", lambda name: "/usr/bin/ffmpeg")

    def build_video(images, title, font_path):
        world.plans.select_assets(world.business.id, ids[:2])
        return SimpleNamespace(data=b"video")

    monkeypatch.setattr(plan_module, "build_video", build_video)
    world.plans.build_videos(world.business.id)
    assert not [asset for asset in world.repository.assets(world.business.id) if asset.kind == "video"]
    assert not list((world.media / str(world.business.id)).glob("video-*"))


async def test_video_built_for_old_selection_is_not_saved_when_selection_changed_to_other_three(
    world: World, monkeypatch
) -> None:
    from app.services import plan as plan_module

    draft = await build(world)
    group = draft["groups"][0]
    for seed in (701, 702):
        world.plans.upload_photo(world.business.id, group["id"], picture(seed), TODAY)
    plan = world.repository.latest_plan(world.business.id)
    ids = plan.payload["assets"][group_key(plan.payload["groups"][0])]
    assert len(ids) >= 4
    world.plans.select_assets(world.business.id, ids[:3])
    monkeypatch.setattr(plan_module.shutil, "which", lambda name: "/usr/bin/ffmpeg")

    def build_video(images, title, font_path):
        # Пока ffmpeg собирал ролик из первых трёх, владелец выбрал другие три: старый ролик не сохраняем.
        world.plans.select_assets(world.business.id, ids[1:4])
        return SimpleNamespace(data=b"video")

    monkeypatch.setattr(plan_module, "build_video", build_video)
    assert world.plans.build_videos(world.business.id) is True
    assert not [asset for asset in world.repository.assets(world.business.id) if asset.kind == "video"]
    assert not list((world.media / str(world.business.id)).glob("video-*"))


async def test_video_build_is_skipped_without_ffmpeg(world: World, monkeypatch) -> None:
    from app.services import plan as plan_module

    draft = await build(world)
    world.plans.select_assets(world.business.id, [asset["id"] for asset in draft["groups"][0]["assets"]])
    monkeypatch.setattr(plan_module.shutil, "which", lambda name: None)
    monkeypatch.setattr(plan_module, "build_video", lambda *args, **kwargs: pytest.fail("ffmpeg не установлен"))
    assert world.plans.build_videos(world.business.id) is True


async def test_video_build_skips_busy_business(world: World, monkeypatch) -> None:
    from contextlib import contextmanager

    from app.services import plan as plan_module
    from app.services.changes import ChangeConflict

    draft = await build(world)
    ids = [asset["id"] for asset in draft["groups"][0]["assets"]]
    assert len(ids) >= 3
    world.plans.select_assets(world.business.id, ids)
    monkeypatch.setattr(plan_module.shutil, "which", lambda name: "/usr/bin/ffmpeg")
    monkeypatch.setattr(plan_module, "build_video", lambda images, title, font_path: SimpleNamespace(data=b"video"))

    @contextmanager
    def busy(business_id):
        raise ChangeConflict("Этот бизнес уже обрабатывается.")
        yield

    monkeypatch.setattr(world.repository, "business_lock", busy)
    assert world.plans.build_videos(world.business.id) is False
    assert not [asset for asset in world.repository.assets(world.business.id) if asset.kind == "video"]
    monkeypatch.undo()
    monkeypatch.setattr(plan_module.shutil, "which", lambda name: "/usr/bin/ffmpeg")
    monkeypatch.setattr(plan_module, "build_video", lambda images, title, font_path: SimpleNamespace(data=b"video"))
    assert world.plans.build_videos(world.business.id) is True
    assert [asset for asset in world.repository.assets(world.business.id) if asset.kind == "video"]


async def test_video_is_built_once_while_selection_stays_the_same(world: World, monkeypatch) -> None:
    from app.services import plan as plan_module

    draft = await build(world)
    ids = [asset["id"] for asset in draft["groups"][0]["assets"]]
    assert len(ids) >= 3
    world.plans.select_assets(world.business.id, ids)
    monkeypatch.setattr(plan_module.shutil, "which", lambda name: "/usr/bin/ffmpeg")
    calls = []
    monkeypatch.setattr(
        plan_module, "build_video", lambda images, title, font_path: calls.append(images) or SimpleNamespace(data=b"v")
    )
    assert world.plans.build_videos(world.business.id) and world.plans.build_videos(world.business.id)
    assert len(calls) == 1


async def test_video_build_returns_connection_before_ffmpeg(world: World, monkeypatch) -> None:
    from app.services import plan as plan_module

    draft = await build(world)
    world.plans.select_assets(world.business.id, [asset["id"] for asset in draft["groups"][0]["assets"]])
    events = []
    monkeypatch.setattr(world.repository, "commit", lambda: events.append("commit"))
    monkeypatch.setattr(plan_module.shutil, "which", lambda name: "/usr/bin/ffmpeg")
    monkeypatch.setattr(
        plan_module,
        "build_video",
        lambda images, title, font_path: events.append("ffmpeg") or SimpleNamespace(data=b"v"),
    )
    assert world.plans.build_videos(world.business.id)
    assert events[:2] == ["commit", "ffmpeg"]


async def test_video_file_is_removed_when_its_row_is_not_saved(world: World, monkeypatch) -> None:
    from app.services import plan as plan_module

    draft = await build(world)
    ids = [asset["id"] for asset in draft["groups"][0]["assets"]]
    assert len(ids) >= 3
    world.plans.select_assets(world.business.id, ids)
    monkeypatch.setattr(plan_module.shutil, "which", lambda name: "/usr/bin/ffmpeg")
    monkeypatch.setattr(plan_module, "build_video", lambda images, title, font_path: SimpleNamespace(data=b"video"))
    commits = iter([None])

    def commit():
        # Первый commit завершает чтение, второй падает: бизнес удалили, пока собирался ролик.
        if next(commits, "fail") == "fail":
            raise RuntimeError("business deleted")

    monkeypatch.setattr(world.repository, "commit", commit)
    with pytest.raises(RuntimeError):
        world.plans.build_videos(world.business.id)
    assert not list((world.media / str(world.business.id)).glob("video-*"))


async def test_video_queue_keeps_one_pending_build_per_business_and_limits_queue() -> None:
    from app.services.plan import VideoQueue

    queue = VideoQueue(limit=1, retries=0, delay=0)
    started, release = threading.Event(), threading.Event()
    calls = []

    def build(business_id, built):
        calls.append(business_id)
        started.set()
        release.wait(5)
        return True

    running = asyncio.create_task(queue.request(1, build))
    await asyncio.to_thread(started.wait, 5)
    # Пока ролики бизнеса собираются, новые выборы дают одну сборку после текущей, а не очередь из трёх.
    for _ in range(3):
        await queue.request(1, build)
    # В очереди уже есть бизнес, предел — один: второй бизнес не встаёт.
    await queue.request(2, build)
    release.set()
    await running
    assert calls == [1, 1]
    await queue.request(2, build)
    assert calls == [1, 1, 2]


async def test_video_queue_repeats_build_that_found_business_busy() -> None:
    from app.services.plan import VideoQueue

    attempts = []
    await VideoQueue(retries=2, delay=0).request(5, lambda business_id, built: attempts.append(business_id) or False)
    assert attempts == [5, 5, 5]
    results = iter([False, True])
    attempts.clear()
    await VideoQueue(retries=5, delay=0).request(
        5, lambda business_id, built: attempts.append(business_id) or next(results)
    )
    assert attempts == [5, 5]


async def test_video_queue_lets_go_of_built_videos_once_business_is_deleted(world: World, monkeypatch) -> None:
    # Пока бизнес занят, собранные ролики ждут повтора в памяти. Бизнес удалили — следующая попытка ничего не
    # сохраняет и завершает очередь: байты роликов не держатся все 12 повторов.
    from contextlib import contextmanager

    from app.db.models import Plan
    from app.services import plan as plan_module
    from app.services.changes import ChangeConflict
    from app.services.plan import VideoQueue

    draft = await build(world)
    ids = [asset["id"] for asset in draft["groups"][0]["assets"]]
    assert len(ids) >= 3
    world.plans.select_assets(world.business.id, ids)
    monkeypatch.setattr(plan_module.shutil, "which", lambda name: "/usr/bin/ffmpeg")
    monkeypatch.setattr(plan_module, "build_video", lambda images, title, font_path: SimpleNamespace(data=b"video"))
    lock = world.repository.business_lock

    @contextmanager
    def busy(business_id):
        raise ChangeConflict("Этот бизнес уже обрабатывается.")
        yield

    monkeypatch.setattr(world.repository, "business_lock", busy)
    attempts = []

    def attempt(business_id, built):
        attempts.append(dict(built))
        if len(attempts) == 2:
            world.repository.tables[Plan] = []
            world.repository.business_lock = lock
        return world.plans.build_videos(business_id, built)

    await VideoQueue(retries=12, delay=0).request(world.business.id, attempt)
    assert len(attempts) == 2 and attempts[0] == {} and attempts[1]
    assert not list((world.media / str(world.business.id)).glob("video-*"))


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
            DEFAULT_SETTINGS,
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


async def test_blocker_names_the_disconnected_platform_not_direct(world: World) -> None:
    await build(world)
    for account in world.repository.accounts(world.business.id):
        if account.platform == "vk":
            account.status = "needs_reauth"
    blockers = launch_blockers(
        world.business,
        world.repository.latest_plan(world.business.id),
        world.repository.accounts(world.business.id),
        world.repository.marking(world.business.id),
        world.repository.assets(world.business.id),
        DEFAULT_SETTINGS,
    )
    assert any(text.startswith("Подключите ВКонтакте заново") for text in blockers)
    assert not any("Яндекс Директа" in text for text in blockers)


async def test_launched_plan_does_not_show_launch_estimate_as_daily_limits(world: World) -> None:
    plan = await build(world)
    assert set(plan["daily_kop"]) == {"direct", "vk"}
    world.repository.latest_plan(world.business.id).status = "launched"
    plan = await world.plans.get_plan(world.business.id, NOW, TODAY)
    assert plan["status"] == "launched" and plan["daily_kop"] == {}


async def test_audience_of_old_plan_comes_from_its_targeting(world: World) -> None:
    await build(world)
    stored = world.repository.latest_plan(world.business.id)
    payload = deepcopy(stored.payload)
    del payload["city"]
    for group in payload["groups"]:
        group["audience"] = group["segment"].capitalize()
    stored.payload = payload
    plan = world.plans.view(world.business, stored, TODAY)
    assert all(group["audience"].startswith(("Тем, кто ищет в Яндексе", "Регион: Самара")) for group in plan["groups"])


async def test_audience_names_city_of_the_plan_and_area_phrases_use_city_name(world: World) -> None:
    world.business.city = "Москва и Московская область"
    plan = await build(world)
    stored = world.repository.latest_plan(world.business.id)
    assert stored.payload["groups"][0]["phrases"] == ["маникюр классический москва"]
    assert plan["groups"][0]["audience"].startswith(
        "Тем, кто ищет в Яндексе «маникюр классический». Регион: Москва и Московская область."
    )
    # Запущенный план не пересобирается при смене города: его аудитория — по городу, с которым он собран.
    world.business.city = "Самара"
    assert world.plans.view(world.business, stored, TODAY)["groups"][-1]["audience"].startswith(
        "Регион: Москва и Московская область."
    )


def service(name: str) -> Service:
    return Service(name, None, "", "owner")


def test_phrase_takes_four_words_lowercase_and_city_of_area() -> None:
    assert phrase("Маникюр  Классический с покрытием гель-лак", "Самара") == "маникюр классический с покрытием самара"
    assert phrase("Стрижка", "Нижний Новгород") == "стрижка нижний новгород"
    assert phrase("Стрижка", "Москва и Московская область") == "стрижка москва"


def test_phrase_cleans_symbols_forbidden_by_direct_and_caps_words() -> None:
    """Знаки, которые API Директа отклоняет вместе с созданием всей кампании (запятые, /, скобки, %, №,
    операторы + ! [ ] ( ) " |), вычищаются; слов в фразе не больше семи."""
    assert phrase("Маникюр, педикюр", "Самара") == "маникюр педикюр самара"
    assert phrase("Стрижка/укладка", "Самара") == "стрижка укладка самара"
    assert phrase("Массаж (60 мин)", "Самара") == "массаж 60 мин самара"
    long_name = "Уход за волосами с кератином и ламинированием ботоксом"
    assert len(phrase(long_name, "Санкт-Петербург").split()) <= 7


def test_direct_services_skips_service_without_phrase_after_cleaning() -> None:
    services = [service("!!!"), service("Маникюр")]
    assert [item.name for item in direct_services(services, "Самара")] == ["Маникюр"]


def test_direct_services_skip_same_phrase_and_respect_limit() -> None:
    services = [service("Маникюр"), service("маникюр"), service("МАНИКЮР  "), service("Педикюр"), service("Стрижка")]

    assert [item.name for item in direct_services(services, "Самара")] == ["Маникюр", "Педикюр", "Стрижка"]
    assert direct_services(services, "Самара")[0] is services[0]
    assert len(direct_services([service(f"Услуга {index}") for index in range(8)], "Самара")) == 5
    assert len(direct_services([service(f"Услуга {index}") for index in range(8)], "Самара", None)) == 8
    assert direct_services(services, "Самара", 0) == []
    assert direct_services([], "Самара") == []


def test_group_slots_pair_services_and_segments_by_platform() -> None:
    assert group_slots("direct", ["Маникюр", "Педикюр", "Стрижка"], ["a", "b"]) == [
        ("Маникюр", "a"),
        ("Педикюр", "b"),
        ("Стрижка", "a"),
    ]
    assert group_slots("vk", ["Маникюр", "Педикюр", "Стрижка"], ["a", "b"]) == [("Маникюр", "a"), ("Педикюр", "b")]
    assert group_slots("vk", [""], ["a", "b"]) == [("", "a"), ("", "b")]
    assert group_slots("direct", [], ["a"]) == [] and group_slots("vk", ["Маникюр"], []) == []


def direct_plan(budget_kop: int, services: int, status: str = "ready", **values) -> Plan:
    groups = [{"id": index, "platform": "direct", "service": f"Услуга {index}"} for index in range(services)]
    payload = {"platforms": ["direct"], "groups": groups, "month_budget_kop": budget_kop} | values
    return Plan(business_id=1, status=status, payload=payload)


def services_of(plan: Plan) -> list[list[str]]:
    limits = plan_module.launch_limits(plan.payload["month_budget_kop"], ["direct"], TODAY, DEFAULT_SETTINGS)
    return [[group["service"] for group in batch] for batch in plan_module.direct_campaigns(plan, limits, TODAY)]


def test_plan_shows_campaign_per_service_when_budget_allows() -> None:
    plan = direct_plan(5_000_000, 3)
    limits = plan_module.launch_limits(5_000_000, ["direct"], TODAY, DEFAULT_SETTINGS)
    campaigns = plan_module.direct_campaigns(plan, limits, TODAY)
    assert services_of(plan) == [["Услуга 0"], ["Услуга 1"], ["Услуга 2"]]
    assert plan_module.campaigns_note(campaigns) is None


def test_plan_explains_services_sharing_campaign_on_small_budget() -> None:
    plan = direct_plan(250_000, 3)
    limits = plan_module.launch_limits(250_000, ["direct"], TODAY, DEFAULT_SETTINGS)
    campaigns = plan_module.direct_campaigns(plan, limits, TODAY)
    assert len(campaigns) < 3
    note = plan_module.campaigns_note(campaigns)
    assert note and f"кампаний: {len(campaigns)}, а услуг 3" in note and "366 ₽" in note


def test_launched_plan_shows_campaigns_created_by_launch() -> None:
    launch = {"campaigns": {"direct:1": 5, "direct:2": 6}, "batches": {"direct:1": [0, 2], "direct:2": [1]}}
    assert services_of(direct_plan(250_000, 3, status="launched", launch=launch)) == [
        ["Услуга 0", "Услуга 2"],
        ["Услуга 1"],
    ]
    legacy = direct_plan(250_000, 2, status="launched", launch={"campaigns": {"direct": 5}})
    assert services_of(legacy) == [["Услуга 0", "Услуга 1"]]
