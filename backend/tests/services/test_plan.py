import asyncio
import json
import shutil
import threading
from contextlib import contextmanager
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace

import pytest
from PIL import Image

from app.content.ad_text import MEDICAL_DISCLAIMER, SEGMENTS
from app.content.art import ART_COST_KOP
from app.content.images import check_image, run_decoding
from app.db.models import ArtCall, Asset, LLMCall
from app.services import plan as plan_module
from app.services.changes import ChangeConflict
from app.services.onboarding import AiLimits, Blocked, NotFound, OnboardingError
from app.services.plan import launch_blockers
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
    ids = plan.payload["assets"][plan.payload["groups"][0]["segment"]]
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
