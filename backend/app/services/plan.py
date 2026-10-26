import hashlib
import json
import secrets
import shutil
from collections.abc import Awaitable, Callable
from copy import deepcopy
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

from sqlalchemy import select
from starlette.concurrency import run_in_threadpool

from app.content.ad_text import ANGLES, SEGMENTS, generate_ad_text
from app.content.art import ART_COST_KOP, ArtBudget, ArtBudgetExceeded, ArtClient, ArtError
from app.content.art import ArtCall as ArtRecord
from app.content.ideas import generate_ideas
from app.content.images import DEFAULT_FONT, ImageRejected, accept_upload, check_image
from app.content.llm import LLMClient
from app.content.video import VideoError, build_video
from app.content.yandexgpt import YandexGPTError
from app.db.models import Asset, Business, MarkingProfile, Plan, PlatformAccount
from app.domain.budget import allocate, plan_budget, platform_shares, split_group_limits
from app.domain.settings import DEFAULT_SETTINGS, DomainSettings
from app.domain.types import PlatformLimit
from app.services.onboarding import (
    DESCRIPTION_KEYS,
    AiLimits,
    Blocked,
    OnboardingError,
    OnboardingRepository,
    SpendLLM,
    ai_budget,
    description_draft,
    lines,
)

PLATFORMS = ("direct", "vk")
ANGLES_PER_GROUP = 2
IMAGE_CANDIDATES = 8
REGENERATE_COUNT = 2
MAX_ASSETS = 40
IMAGE_KINDS = ("image", "upload")

type ArtFactory = Callable[[ArtBudget, Callable[[ArtRecord], Awaitable[None]]], ArtClient]


class PlanRepository(OnboardingRepository):
    def latest_plan(self, business_id: int) -> Plan | None:
        return self.db.scalar(
            select(Plan).where(Plan.business_id == business_id).order_by(Plan.version.desc()).limit(1)
        )

    def get_plan(self, plan_id: int, business_id: int) -> Plan | None:
        return self.db.scalar(select(Plan).where(Plan.id == plan_id, Plan.business_id == business_id))

    def assets(self, business_id: int) -> list[Asset]:
        return list(self.db.scalars(select(Asset).where(Asset.business_id == business_id).order_by(Asset.id)))


def connected_platforms(accounts: list[PlatformAccount]) -> list[str]:
    ready = {account.platform for account in accounts if account.status == "ok"}
    return [platform for platform in PLATFORMS if platform in ready] or ["direct"]


def inputs_hash(business: Business, platforms: list[str]) -> str:
    fields = business.description.get("fields", {})
    data = {
        "fields": {key: fields.get(key, {}).get("value", "") for key in DESCRIPTION_KEYS},
        "budget": business.month_budget_kop,
        "platforms": platforms,
        "city": business.city,
        "site": business.site_url,
        "niche": business.niche_class,
        "medical": business.medical_level,
    }
    return hashlib.sha256(json.dumps(data, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def budget_limits(
    business: Business, platforms: list[str], today: date, domain: DomainSettings = DEFAULT_SETTINGS
) -> dict[str, PlatformLimit]:
    """Лимиты запуска: резерв и кэпы площадок считает domain/budget, сумма кэпов не превышает бюджет месяца."""
    budget = plan_budget(business.month_budget_kop, 0, today, tuple(platforms), started_on=today, settings=domain)
    return {limit.platform: limit for limit in budget.limits}


def phrases(business: Business) -> list[str]:
    names = lines(business.description.get("fields", {}).get("services", {}).get("value", ""))
    result = [f"{' '.join(name.split()[:4])} {business.city}".lower() for name in names[:5]]
    return list(dict.fromkeys(result))


def launch_blockers(
    business: Business,
    plan: Plan,
    accounts: list[PlatformAccount],
    marking: MarkingProfile | None,
    assets: list[Asset],
    medical_acknowledged: bool | None = None,
) -> list[str]:
    """Почему запускать нельзя; пустой список — можно. None в medical_acknowledged — подтверждение ещё не спрашивали."""
    payload = plan.payload
    platforms = payload["platforms"]
    ready = {account.platform for account in accounts if account.status == "ok"}
    selected = {asset.id for asset in assets if asset.selected}
    result = []
    if "direct" not in ready or not set(platforms) <= ready:
        result.append("Подключите кабинет Яндекс Директа: без него запуск невозможен.")
    if business.description_approved_at is None:
        result.append("Подтвердите описание бизнеса.")
    if not business.metrica_counter_id or not business.lead_goal_ids:
        result.append(
            "Без счётчика Метрики и цели обращений мы не запускаем рекламу: мы не увидим, приходят ли клиенты."
        )
    if marking is None or not all(marking.confirmed_in_platform.get(platform) is True for platform in platforms):
        result.append("Заполните данные рекламодателя и подтвердите маркировку на подключённых площадках.")
    if payload["inputs_hash"] != inputs_hash(business, connected_platforms(accounts)):
        result.append("Сведения о бизнесе изменились после подготовки плана. Обновите план.")
    if any(not selected & set(payload["assets"].get(group["segment"], ())) for group in payload["groups"]):
        result.append("В каждой группе отметьте хотя бы одну картинку.")
    if business.medical_level != "none" and medical_acknowledged is False:
        result.append("Подтвердите, что понимаете: для такой рекламы могут потребовать лицензию.")
    return result


class PlanService:
    def __init__(
        self,
        repository: PlanRepository,
        llm: LLMClient,
        art_factory: ArtFactory,
        media_root: Path,
        limits: AiLimits,
        domain: DomainSettings = DEFAULT_SETTINGS,
        font_path: str = DEFAULT_FONT,
        seeds: Callable[[], int] = lambda: secrets.randbits(31),
    ) -> None:
        self.font_path = font_path
        self.seeds = seeds
        self.repository = repository
        self.llm = llm
        self.art_factory = art_factory
        self.media_root = media_root
        self.limits = limits
        self.domain = domain

    async def get_plan(self, business_id: int, now: datetime, today: date) -> dict:
        business = self.repository.business(business_id)
        if business.description_approved_at is None:
            raise Blocked("Подтвердите описание бизнеса: без него мы не готовим план.")
        platforms = connected_platforms(self.repository.accounts(business_id))
        digest = inputs_hash(business, platforms)
        plan = self.repository.latest_plan(business_id)
        if plan is None or (plan.status != "launched" and plan.payload["inputs_hash"] != digest):
            with self.repository.business_lock(business_id):
                latest = self.repository.latest_plan(business_id)
                if latest is None or (latest.status != "launched" and latest.payload["inputs_hash"] != digest):
                    plan = await self.build(business, platforms, digest, latest, now, today)
                else:
                    plan = latest
        return self.view(business, plan, today)

    async def build(
        self, business: Business, platforms: list[str], digest: str, previous: Plan | None, now: datetime, today: date
    ) -> Plan:
        budget = ai_budget(self.repository, business, self.limits, today)
        llm = SpendLLM(self.llm, self.repository, business.id, budget, "ad_text")
        niche = business.niche_class or "other"
        segments = list(SEGMENTS.get(niche, SEGMENTS["other"]))
        assets = self.carry_assets(previous, segments)
        if not self.repository.assets(business.id):
            created = await self.generate_images(business, budget, IMAGE_CANDIDATES)
            for index, asset in enumerate(created):
                ids = assets.setdefault(segments[index % len(segments)], [])
                asset.selected = len(ids) < 2
                ids.append(asset.id)
            self.repository.commit()
        draft = description_draft(business)
        primary = (lines(business.description["fields"]["services"]["value"]) or ["Услуги"])[0]
        limits = budget_limits(business, platforms, today, self.domain)
        groups = []
        for platform in platforms:
            daily = split_group_limits(limits[platform], dict.fromkeys(segments, Decimal(1)))
            for segment, group_limit in zip(segments, daily, strict=True):
                try:
                    ads = [
                        await generate_ad_text(llm, draft, platform=platform, angle=angle, segment=segment)
                        for angle in ANGLES[:ANGLES_PER_GROUP]
                    ]
                except ValueError as error:
                    raise Blocked("Не удалось подготовить безопасные тексты. Уточните описание бизнеса.") from error
                groups.append(
                    {
                        "id": len(groups) + 1,
                        "platform": platform,
                        "segment": segment,
                        "name": f"{primary} — {segment}",
                        "audience": segment.capitalize(),
                        "daily_limit_kop": group_limit.daily_limit,
                        "phrases": phrases(business) if platform == "direct" else [],
                        "ads": [
                            {
                                "angle": ad.angle,
                                "title": ad.ad.title,
                                "title2": ad.ad.title2,
                                "text": ad.ad.text,
                                "fallback": ad.used_fallback,
                            }
                            for ad in ads
                        ],
                    }
                )
        payload = {
            "inputs_hash": digest,
            "built_at": now.isoformat(),
            "month_budget_kop": business.month_budget_kop,
            "platforms": platforms,
            "niche": niche,
            "segments": segments,
            "groups": groups,
            "assets": assets,
        }
        plan = Plan(
            business_id=business.id,
            version=previous.version + 1 if previous else 1,
            status="draft",
            payload=payload,
        )
        self.repository.add(plan)
        business.status = "plan_ready"
        self.repository.commit()
        return plan

    def carry_assets(self, previous: Plan | None, segments: list[str]) -> dict[str, list[int]]:
        if previous is None:
            return {}
        old = previous.payload["assets"]
        if previous.payload["segments"] == segments:
            return deepcopy(old)
        result: dict[str, list[int]] = {}
        for index, asset_id in enumerate(sorted(id_ for ids in old.values() for id_ in ids)):
            result.setdefault(segments[index % len(segments)], []).append(asset_id)
        return result

    async def record_art(self, record: ArtRecord) -> None:
        self.repository.save_art_call(record)

    async def generate_images(self, business: Business, budget: ArtBudget, count: int) -> list[Asset]:
        llm = SpendLLM(self.llm, self.repository, business.id, budget, "image_ideas")
        art = self.art_factory(budget, self.record_art)
        try:
            ideas = await generate_ideas(llm, business.niche_class or "other", count=count)
        except (YandexGPTError, ValueError):
            return []
        hashes = [asset.phash for asset in self.repository.assets(business.id) if asset.phash]
        created = []
        for idea in ideas:
            try:
                result = await art.generate(idea.prompt, seed=self.seeds(), business_id=business.id)
            except ArtBudgetExceeded:
                break
            except ArtError:
                continue
            try:
                checked = await run_in_threadpool(
                    check_image, result.image, existing_hashes=hashes, cost_kop=result.cost_kop
                )
            except ImageRejected:
                continue
            asset = Asset(
                business_id=business.id,
                kind="image",
                idea_text=idea.visual,
                prompt=idea.prompt,
                file_path=self.store(business.id, checked.phash, "jpg", checked.data),
                phash=checked.phash,
                cost_kop=checked.cost_kop,
            )
            self.repository.add(asset)
            hashes.append(checked.phash)
            created.append(asset)
        return created

    def store(self, business_id: int, name: str, suffix: str, data: bytes) -> str:
        directory = self.media_root / str(business_id)
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"{name}-{secrets.token_hex(4)}.{suffix}"
        path.write_bytes(data)
        return f"{business_id}/{path.name}"

    def draft_group(self, business_id: int, group_id: int) -> tuple[Plan, dict]:
        plan = self.repository.latest_plan(business_id)
        group = next((item for item in plan.payload["groups"] if item["id"] == group_id), None) if plan else None
        if group is None:
            raise LookupError("Группа не найдена.")
        if plan.status == "launched":
            raise Blocked("Реклама уже запущена. Картинки меняются в разделе «Реклама».")
        return plan, group

    def attach(self, plan: Plan, segment: str, assets: list[Asset]) -> None:
        payload = deepcopy(plan.payload)
        payload["assets"].setdefault(segment, []).extend(asset.id for asset in assets)
        plan.payload = payload
        self.repository.commit()

    async def regenerate_images(self, business_id: int, group_id: int, now: datetime, today: date) -> dict:
        business = self.repository.business(business_id)
        plan, group = self.draft_group(business_id, group_id)
        with self.repository.business_lock(business_id):
            budget = ai_budget(self.repository, business, self.limits, today)
            if self.generations_remaining(budget) == 0:
                raise Blocked("Лимит генерации картинок исчерпан. Загрузите своё фото.")
            created = await self.generate_images(business, budget, REGENERATE_COUNT)
        if not created:
            raise Blocked("Не удалось получить новые картинки. Попробуйте позже или загрузите своё фото.")
        self.attach(plan, group["segment"], created)
        return self.view(business, plan, today)

    def upload_photo(self, business_id: int, group_id: int, data: bytes, today: date) -> dict:
        business = self.repository.business(business_id)
        plan, group = self.draft_group(business_id, group_id)
        assets = self.repository.assets(business_id)
        if len(assets) >= MAX_ASSETS:
            raise Blocked("Загружено слишком много картинок. Уберите ненужные.")
        try:
            checked = accept_upload(data, existing_hashes=[asset.phash for asset in assets if asset.phash])
        except ImageRejected as error:
            raise OnboardingError(
                "Фото не подошло: нужен JPG или PNG от 256 пикселей по короткой стороне, до 10 МБ, без дублей."
            ) from error
        asset = Asset(
            business_id=business_id,
            kind="upload",
            file_path=self.store(business_id, checked.phash, "jpg", checked.data),
            phash=checked.phash,
            selected=True,
        )
        self.repository.add(asset)
        self.attach(plan, group["segment"], [asset])
        self.build_videos(business_id)
        return self.view(business, plan, today)

    def select_assets(self, business_id: int, asset_ids: list[int]) -> None:
        images = [asset for asset in self.repository.assets(business_id) if asset.kind in IMAGE_KINDS]
        if not set(asset_ids) <= {asset.id for asset in images}:
            raise OnboardingError("Картинка не найдена.")
        for asset in images:
            asset.selected = asset.id in asset_ids
        self.repository.commit()
        self.build_videos(business_id)

    def build_videos(self, business_id: int) -> None:
        """Видео из 3–4 выбранных картинок сегмента; без ffmpeg пропускаем: ролик загружается вручную."""
        plan = self.repository.latest_plan(business_id)
        if plan is None or shutil.which("ffmpeg") is None:
            return
        assets = self.repository.assets(business_id)
        by_id = {asset.id: asset for asset in assets}
        for segment, ids in plan.payload["assets"].items():
            chosen = [by_id[id_] for id_ in ids if id_ in by_id and by_id[id_].selected]
            video = next((asset for asset in assets if asset.kind == "video" and asset.idea_text == segment), None)
            if len(chosen) < 3:
                if video:
                    video.selected = False
                continue
            group = next(item for item in plan.payload["groups"] if item["segment"] == segment)
            try:
                built = build_video(
                    [self.media_root / asset.file_path for asset in chosen[:4]],
                    group["ads"][0]["title"],
                    font_path=self.font_path,
                )
            except (VideoError, ValueError):
                continue
            path = self.store(business_id, f"video-{segment.replace(' ', '-')}", "mp4", built.data)
            if video is None:
                video = Asset(business_id=business_id, kind="video", idea_text=segment, file_path=path)
                self.repository.add(video)
            video.file_path = path
            video.selected = True
        self.repository.commit()

    def generations_remaining(self, budget: ArtBudget) -> int:
        return max(0, (budget.limit_kop - budget.spent_kop) // (REGENERATE_COUNT * ART_COST_KOP))

    def view(self, business: Business, plan: Plan, today: date) -> dict:
        payload = plan.payload
        assets = self.repository.assets(business.id)
        by_id = {asset.id: asset for asset in assets}
        platforms = payload["platforms"]
        shares = allocate(business.month_budget_kop, platform_shares(platforms, self.domain))
        accounts = self.repository.accounts(business.id)
        marking = self.repository.marking(business.id)
        blockers = launch_blockers(business, plan, accounts, marking, assets)
        groups = [
            {
                "id": group["id"],
                "name": group["name"],
                "platform": group["platform"],
                "audience": group["audience"],
                "daily_limit_kop": group["daily_limit_kop"],
                "ads": [{"title": ad["title"], "text": ad["text"]} for ad in group["ads"]],
                "assets": [
                    {
                        "id": asset.id,
                        "url": f"/api/plan/assets/{asset.id}/file",
                        "kind": asset.kind,
                        "alt": asset.idea_text or "Ваше фото",
                        "selected": asset.selected,
                    }
                    for asset in (by_id[id_] for id_ in payload["assets"].get(group["segment"], ()) if id_ in by_id)
                ],
            }
            for group in payload["groups"]
        ]
        return {
            "id": plan.id,
            "month_budget_kop": payload["month_budget_kop"],
            "direct_budget_kop": shares.get("direct", 0),
            "vk_budget_kop": shares.get("vk", 0),
            "groups": groups,
            "generations_remaining": self.generations_remaining(
                ai_budget(self.repository, business, self.limits, today)
            ),
            "medical_level": business.medical_level,
            "marking_confirmed": marking is not None
            and all(marking.confirmed_in_platform.get(platform) is True for platform in platforms),
            "ready_to_launch": not blockers,
            "blockers": blockers,
            "status": "launched" if plan.status == "launched" else "draft",
        }
