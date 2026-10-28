import asyncio
import hashlib
import json
import logging
import secrets
import shutil
from collections.abc import Awaitable, Callable
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

from sqlalchemy import select

from app.content.ad_text import ANGLES, SEGMENTS, generate_ad_text
from app.content.art import ART_COST_KOP, ArtBudget, ArtBudgetExceeded, ArtClient, ArtError
from app.content.art import ArtCall as ArtRecord
from app.content.ideas import generate_ideas
from app.content.images import DEFAULT_FONT, ImageRejected, accept_upload, check_image, run_decoding
from app.content.llm import LLMClient
from app.content.video import VideoError, build_video
from app.content.yandexgpt import YandexGPTError
from app.db.models import Asset, Business, MarkingProfile, Plan, PlatformAccount
from app.domain.budget import allocate, plan_budget, platform_shares, split_group_limits
from app.domain.settings import DomainSettings
from app.domain.types import PlatformLimit
from app.services.changes import LOCK_WAIT_SECONDS, ChangeConflict
from app.services.onboarding import (
    DESCRIPTION_KEYS,
    AiLimits,
    Blocked,
    NotFound,
    OnboardingError,
    OnboardingRepository,
    SpendLLM,
    ai_budget,
    description_draft,
    lines,
    waited_lock,
)

logger = logging.getLogger(__name__)
CAP_MULTIPLIER = Decimal("1.25")


def campaign_limits(daily: int, remaining: int, today: date, lifetime: int = 0) -> tuple[int, int]:
    """Кэп накопленный; запас применяется только к новым разрешённым тратам."""
    next_month = (today.replace(day=28) + timedelta(days=4)).replace(day=1)
    days = min(7 - today.weekday(), (next_month - today).days)
    additional = min(remaining, daily * days)
    return int(Decimal(daily) / CAP_MULTIPLIER), lifetime + int(Decimal(additional) / CAP_MULTIPLIER)


PLATFORMS = ("direct", "vk")
ANGLES_PER_GROUP = 2
IMAGE_CANDIDATES = 8
REGENERATE_COUNT = 2
MAX_ASSETS = 40
IMAGE_KINDS = ("image", "upload")
UNSETTLED = (
    "Прошлый запуск рекламы не завершился. Сначала остановим всё, что он успел создать на площадках, чтобы реклама не "
    "тратила деньги без присмотра, — обычно это занимает до получаса. Потом план соберётся по новым данным. Если "
    "площадка просит войти в кабинет заново, войдите: без этого рекламу не остановить. Если кабинет больше не нужен, "
    "остановите кампанию «Автопилот» в нём самом и отключите кабинет на шаге «Кабинеты»."
)

# Незавершённый запуск, неудача которого не записана: он идёт или брошен. Повтор под блокировкой отличит одно от
# другого (services.launch).
UNFINISHED = (
    "Запуск рекламы не завершился. Если он ещё идёт, подождите минуту и обновите страницу. Если нет, нажмите "
    "«Запустить рекламу» ещё раз: повтор продолжит с уже созданного, кампании не задвоятся."
)

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

    def asset(self, business_id: int, asset_id: int) -> Asset | None:
        return self.db.scalar(select(Asset).where(Asset.id == asset_id, Asset.business_id == business_id))


def launch_unsettled(plan: Plan) -> bool:
    """Запуск плана начат, но не завершён и не остановил созданное: кампании на площадках могут работать."""
    return plan.status != "launched" and bool(plan.payload.get("launch", {}).get("started_at"))


def launch_error(plan: Plan) -> str | None:
    """Объяснение прошлой попытки запуска на экране плана; у запущенного плана его нет."""
    if plan.status == "launched":
        return None
    return plan.payload.get("launch", {}).get("error") or (UNFINISHED if launch_unsettled(plan) else None)


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
    business: Business, platforms: list[str], today: date, domain: DomainSettings | None = None
) -> dict[str, PlatformLimit]:
    """Лимиты запуска: резерв и кэпы площадок считает domain/budget, сумма кэпов не превышает бюджет месяца."""
    if domain is None:
        from app.config import get_domain_settings

        domain = get_domain_settings()
    budget = plan_budget(business.month_budget_kop, 0, today, tuple(platforms), started_on=today, settings=domain)
    result = {}
    for limit in budget.limits:
        daily, cap = campaign_limits(limit.daily_limit, limit.remaining_cap, today)
        result[limit.platform] = PlatformLimit(limit.platform, cap, daily, cap)
    return result


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


# ffmpeg собирает ролик до 180 секунд: не больше двух сборок сразу и вне пула потоков, который обслуживает запросы.
VIDEO_EXECUTOR = ThreadPoolExecutor(max_workers=2, thread_name_prefix="video")


type Built = dict[str, tuple[list[int], bytes]]


class VideoQueue:
    """Очередь сборок роликов: у бизнеса не больше одной ожидающей сборки, всего бизнесов в очереди — не больше limit.

    Пока ролики бизнеса собираются, новый выбор картинок только отмечает, что нужна ещё одна сборка: она начнётся после
    текущей и прочитает выбор заново, поэтому побеждает последний выбор. Сборка, которая не сохранила результат из-за
    занятого бизнеса, повторяется через delay секунд, не больше retries раз. Собранные ролики (Built) переходят от
    попытки к попытке: ffmpeg запускается заново только для сегмента, у которого изменился набор картинок.

    Очередь живёт в цикле событий API, и между await её состояние никто не меняет: блокировка потоков не нужна."""

    def __init__(self, limit: int = 20, retries: int = 12, delay: float = 5) -> None:
        self.limit = limit
        self.retries = retries
        self.delay = delay
        self._active: set[int] = set()
        self._again: set[int] = set()

    async def request(self, business_id: int, build: Callable[[int, Built], bool]) -> None:
        if business_id in self._active:
            self._again.add(business_id)
            return
        if len(self._active) >= self.limit:
            logger.warning("Очередь сборки роликов заполнена, бизнес %s пропущен", business_id)
            return
        self._active.add(business_id)
        built: Built = {}
        try:
            retries = 0
            while True:
                self._again.discard(business_id)
                saved = await asyncio.get_running_loop().run_in_executor(VIDEO_EXECUTOR, build, business_id, built)
                if business_id in self._again:
                    continue
                if saved or retries >= self.retries:
                    return
                retries += 1
                await asyncio.sleep(self.delay)
        finally:
            self._active.discard(business_id)
            self._again.discard(business_id)


VIDEO_QUEUE = VideoQueue()


def build_videos(
    repository: PlanRepository,
    media_root: Path,
    business_id: int,
    font_path: str = DEFAULT_FONT,
    built: Built | None = None,
) -> bool:
    """Видео из 3–4 выбранных картинок сегмента; без ffmpeg пропускаем: ролик загружается вручную.

    Ролик пересобирается, только если изменился его набор картинок; прежний файл удаляется. ffmpeg работает без
    блокировки бизнеса (до 180 секунд), а результат сохраняется под ней и только если выбор за это время не
    изменился: иначе новый ролик соберёт задача, запущенная последним выбором. False — бизнес был занят и ничего не
    сохранено: сборку нужно повторить. built — ролики, собранные прошлыми попытками; дополняется этой."""
    if shutil.which("ffmpeg") is None:
        return True
    built = {} if built is None else built
    segments = video_segments(repository, business_id)
    plan = repository.latest_plan(business_id)
    # commit завершает чтение: ffmpeg работает без соединения с базой, а под блокировкой выбор перечитывается заново.
    repository.commit()
    for segment, chosen, video in segments:
        sources = [asset.id for asset in chosen]
        if (
            len(chosen) < 3
            or (video is not None and video.formats.get("sources") == sources)
            or (segment in built and built[segment][0] == sources)
        ):
            continue
        group = next(item for item in plan.payload["groups"] if item["segment"] == segment)
        try:
            images = [media_root / asset.file_path for asset in chosen]
            built[segment] = (sources, build_video(images, group["ads"][0]["title"], font_path=font_path).data)
        except (VideoError, ValueError):
            continue
    replaced, stored = [], []
    try:
        with repository.business_lock(business_id):
            for segment, chosen, video in video_segments(repository, business_id):
                sources = [asset.id for asset in chosen]
                if len(chosen) < 3:
                    if video is not None:
                        video.selected = False
                    continue
                if video is not None and video.formats.get("sources") == sources:
                    video.selected = True
                    continue
                if segment not in built or built[segment][0] != sources:
                    continue
                path = store(media_root, business_id, f"video-{segment.replace(' ', '-')}", "mp4", built[segment][1])
                stored.append(path)
                if video is None:
                    video = Asset(business_id=business_id, kind="video", idea_text=segment, file_path=path)
                    repository.add(video)
                else:
                    replaced.append(video.file_path)
                video.file_path = path
                video.formats = video.formats | {"sources": sources}
                video.selected = True
            repository.commit()
    except ChangeConflict:
        return False
    except BaseException:
        # Строки с новыми файлами не сохранились (например, бизнес удалили): файлы без строк не оставляем.
        for file_path in stored:
            remove_media(media_root, file_path)
        raise
    for file_path in replaced:
        remove_media(media_root, file_path)
    return True


def video_segments(repository: PlanRepository, business_id: int) -> list[tuple[str, list[Asset], Asset | None]]:
    plan = repository.latest_plan(business_id)
    if plan is None:
        return []
    assets = repository.assets(business_id)
    by_id = {asset.id: asset for asset in assets}
    return [
        (
            segment,
            [by_id[id_] for id_ in ids if id_ in by_id and by_id[id_].selected][:4],
            next((asset for asset in assets if asset.kind == "video" and asset.idea_text == segment), None),
        )
        for segment, ids in plan.payload["assets"].items()
    ]


def store(media_root: Path, business_id: int, name: str, suffix: str, data: bytes) -> str:
    directory = media_root / str(business_id)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{name}-{secrets.token_hex(4)}.{suffix}"
    path.write_bytes(data)
    return f"{business_id}/{path.name}"


def remove_media(media_root: Path, file_path: str) -> None:
    root = media_root.resolve()
    path = (root / file_path).resolve()
    if path.is_relative_to(root) and path.is_file():
        path.unlink()


class PlanService:
    def __init__(
        self,
        repository: PlanRepository,
        llm: LLMClient,
        art_factory: ArtFactory,
        media_root: Path,
        limits: AiLimits,
        domain: DomainSettings | None = None,
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
        if domain is None:
            from app.config import get_domain_settings

            domain = get_domain_settings()
        self.domain = domain

    async def get_plan(self, business_id: int, now: datetime, today: date) -> dict:
        business, platforms, digest, plan = self.inputs(self.repository.business(business_id))
        if plan is None or (plan.status != "launched" and plan.payload["inputs_hash"] != digest):
            with self.repository.business_lock(business_id):
                # Пока ждали блокировку, план мог собрать соседний запрос, а бизнес — измениться или удалиться.
                business, platforms, digest, plan = self.inputs(self.locked_business(business_id))
                if plan is None or (plan.status != "launched" and plan.payload["inputs_hash"] != digest):
                    # Новый план — новые кампании: кампании незавершённого запуска остались бы без плана и без
                    # присмотра. Сначала их останавливает воркер (services.launch.recover_launch).
                    if plan is not None and launch_unsettled(plan):
                        raise Blocked(UNSETTLED)
                    plan = await self.build(business, platforms, digest, plan, now, today)
        return self.view(business, plan, today)

    def inputs(self, business: Business) -> tuple[Business, list[str], str, Plan | None]:
        if business.description_approved_at is None:
            raise Blocked("Подтвердите описание бизнеса: без него мы не готовим план.")
        platforms = connected_platforms(self.repository.accounts(business.id))
        return business, platforms, inputs_hash(business, platforms), self.repository.latest_plan(business.id)

    def locked_business(self, business_id: int) -> Business:
        """Под блокировкой: удалённому бизнесу ИИ не генерирует и файлы не пишутся (удаление тоже идёт под ней)."""
        business = self.repository.fresh_business(business_id)
        if business is None:
            raise NotFound("Бизнес не найден.")
        return business

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
        # Тексты пишет ИИ, по секундам на каждый: соединение на это время возвращается в пул.
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
        hashes = [asset.phash for asset in self.repository.assets(business.id) if asset.phash]
        # Идеи и картинки ИИ готовит десятки секунд: соединение на это время возвращается в пул. Дальше каждая запись
        # (вызов ИИ, картинка) сама завершается commit.
        self.repository.commit()
        try:
            ideas = await generate_ideas(llm, business.niche_class or "other", count=count)
        except (YandexGPTError, ValueError):
            return []
        created = []
        for idea in ideas:
            try:
                result = await art.generate(idea.prompt, seed=self.seeds(), business_id=business.id)
            except ArtBudgetExceeded:
                break
            except ArtError:
                continue
            try:
                checked = await run_decoding(
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
            self.save_with_file(asset)
            hashes.append(checked.phash)
            created.append(asset)
        return created

    def store(self, business_id: int, name: str, suffix: str, data: bytes) -> str:
        return store(self.media_root, business_id, name, suffix, data)

    def save_with_file(self, asset: Asset) -> None:
        """Строка не сохранилась — её файл удаляется: файлов без строк не остаётся."""
        try:
            self.repository.add(asset)
        except BaseException:
            remove_media(self.media_root, asset.file_path)
            raise

    def draft_group(self, business_id: int, group_id: int) -> tuple[Plan, dict]:
        plan = self.repository.latest_plan(business_id)
        group = next((item for item in plan.payload["groups"] if item["id"] == group_id), None) if plan else None
        if group is None:
            raise NotFound("Группа не найдена.")
        if plan.status == "launched":
            raise Blocked("Реклама уже запущена. Картинки меняются в разделе «Реклама».")
        return plan, group

    def attach(self, plan: Plan, segment: str, assets: list[Asset]) -> None:
        payload = deepcopy(plan.payload)
        payload["assets"].setdefault(segment, []).extend(asset.id for asset in assets)
        plan.payload = payload
        self.repository.commit()

    async def regenerate_images(self, business_id: int, group_id: int, now: datetime, today: date) -> dict:
        self.draft_group(business_id, group_id)
        with self.repository.business_lock(business_id):
            business = self.locked_business(business_id)
            plan, group = self.draft_group(business_id, group_id)
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
        self.draft_group(business_id, group_id)
        assets = self.repository.assets(business_id)
        if len(assets) >= MAX_ASSETS:
            raise Blocked("Загружено слишком много картинок. Уберите ненужные.")
        # Разбор может ждать слот, пока картинки разбирает сборка ролика: соединение на это время возвращается в пул.
        self.repository.commit()
        try:
            checked = accept_upload(data, existing_hashes=[asset.phash for asset in assets if asset.phash])
        except ImageRejected as error:
            raise OnboardingError(
                "Фото не подошло: нужен JPG, PNG или WebP от 256 пикселей по короткой стороне, до 10 МБ, без дублей."
            ) from error
        # Файл пишется под блокировкой и после повторной проверки плана: удаление бизнеса не оставит его сиротой.
        with self.repository.business_lock(business_id):
            plan, group = self.draft_group(business_id, group_id)
            asset = Asset(
                business_id=business_id,
                kind="upload",
                file_path=self.store(business_id, checked.phash, "jpg", checked.data),
                phash=checked.phash,
                selected=True,
            )
            self.save_with_file(asset)
            self.attach(plan, group["segment"], [asset])
        return self.view(business, plan, today)

    def select_assets(self, business_id: int, asset_ids: list[int]) -> bool:
        """Отмечает выбранные картинки; True — выбор изменился и ролики нужно пересобрать.

        Под блокировкой: запуск держит её, пока загружает картинки на площадки, и выбор посреди запуска не меняется.
        Как и шаги мастера, ждёт её несколько секунд и только потом отвечает 409."""
        with waited_lock(self.repository, business_id, LOCK_WAIT_SECONDS):
            images = [asset for asset in self.repository.assets(business_id) if asset.kind in IMAGE_KINDS]
            if not set(asset_ids) <= {asset.id for asset in images}:
                raise NotFound("Картинка не найдена.")
            changed = {asset.id for asset in images if asset.selected} != set(asset_ids)
            for asset in images:
                asset.selected = asset.id in asset_ids
            self.repository.commit()
        return changed

    def build_videos(self, business_id: int, built: Built | None = None) -> bool:
        return build_videos(self.repository, self.media_root, business_id, self.font_path, built)

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
            "launch_error": launch_error(plan),
            "status": "launched" if plan.status == "launched" else "draft",
        }
