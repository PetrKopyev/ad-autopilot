import asyncio
import hashlib
import json
import logging
import re
import secrets
import shutil
from collections.abc import Awaitable, Callable, Sequence
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from textwrap import shorten

from sqlalchemy import or_, select

from app.content.ad_text import ANGLES, SEGMENTS, generate_ad_text
from app.content.art import ART_COST_KOP, ArtBudget, ArtBudgetExceeded, ArtClient, ArtError
from app.content.art import ArtCall as ArtRecord
from app.content.ideas import TITLE_LIMIT, generate_ideas
from app.content.images import DEFAULT_FONT, ImageRejected, accept_upload, check_image, run_decoding
from app.content.llm import LLMClient
from app.content.verify import Service
from app.content.video import VideoError, build_video
from app.content.yandexgpt import YandexGPTError
from app.db.models import Ad, Asset, Business, MarkingProfile, Plan, PlatformAccount
from app.domain.budget import (
    PLATFORM_MINIMUMS,
    allocate,
    budget_minimum,
    budget_platforms,
    campaign_batches,
    launch_limits,
    meets_minimum,
    platform_shares,
    split_campaign_limits,
    split_group_limits,
)
from app.domain.settings import DomainSettings
from app.domain.types import PlatformLimit
from app.platforms.direct import weekly_limit
from app.platforms.regions import phrase_city
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
    waited_lock,
)

logger = logging.getLogger(__name__)
PLATFORMS = ("direct", "vk")
ANGLES_PER_GROUP = 2
# Групп Директа не больше пяти: при минимальном бюджете (около 300 ₽ в день на Директ) на группу и так приходится
# по 60 ₽ в день, а каждая новая группа — ещё два текста и две картинки от ИИ.
MAX_SERVICE_GROUPS = 5
# Минус-фразы Директа: слова в начальной форме, Директ сам учитывает их словоформы.
NEGATIVE_PHRASES = (
    "обучение",
    "курсы",
    "вакансия",
    "резюме",
    "зарплата",
    "бесплатно",
    "своими руками",
    "домашних условиях",
    "видео",
    "скачать",
)
NICHE_NEGATIVE_PHRASES = {
    "nails": ("купить", "оптом", "материалы"),
    "beauty": ("купить", "оптом", "материалы"),
    "barbershop": ("купить", "машинка"),
    "yoga": ("коврик", "асаны"),
    "fitness": ("упражнения", "программа тренировок"),
    "medical": ("симптомы", "народные средства", "форум"),
}
IMAGE_CANDIDATES = 8
IMAGES_PER_KEY = 2
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

    def used_assets(self, business_id: int) -> set[int]:
        """Картинки и ролики, на которые ссылаются объявления (ads.asset_id, ads.video_id)."""
        used = select(Ad.id).where(or_(Ad.asset_id == Asset.id, Ad.video_id == Asset.id)).exists()
        return set(self.db.scalars(select(Asset.id).where(Asset.business_id == business_id, used)))

    def remove(self, item: object) -> None:
        self.db.delete(item)


def assets_key(service: str, segment: str) -> str:
    """Картинки плана делятся по паре (услуга, сегмент): группы одной пары на разных площадках делят картинки."""
    return f"{service} — {segment}" if service else segment


def group_key(group: dict) -> str:
    # В планах до разбивки по услугам у групп нет поля service: их картинки лежат по сегменту.
    return assets_key(group.get("service", ""), group["segment"])


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


def plan_platforms(
    business: Business, accounts: list[PlatformAccount], domain: DomainSettings
) -> tuple[list[str], dict[str, int]]:
    return budget_platforms(business.month_budget_kop, connected_platforms(accounts), domain)


def inputs_hash(business: Business, platforms: list[str], off: dict[str, int]) -> str:
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
    if off:
        data["off"] = sorted(off)
    return hashlib.sha256(json.dumps(data, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def budget_limits(
    business: Business, platforms: list[str], today: date, domain: DomainSettings | None = None
) -> dict[str, PlatformLimit]:
    """Лимиты запуска: резерв и кэпы площадок считает domain/budget, сумма кэпов не превышает бюджет месяца."""
    if domain is None:
        from app.config import get_domain_settings

        domain = get_domain_settings()
    return launch_limits(business.month_budget_kop, platforms, today, domain)


def off_view(off: dict[str, int]) -> list[dict]:
    return [
        {"platform": platform, "from_kop": amount, "min_daily_kop": PLATFORM_MINIMUMS[platform]["daily_limit_kop"]}
        for platform, amount in off.items()
    ]


def budget_view(monthly_budget: int, connected: list[str], today: date, domain: DomainSettings) -> dict:
    """Подсказка мастера к бюджету: те же площадки и лимиты, что у плана и запуска."""
    platforms, off = budget_platforms(monthly_budget, connected, domain)
    limits = launch_limits(monthly_budget, platforms, today, domain)
    return {
        "minimum_kop": budget_minimum(("direct",), domain),
        "month_kop": allocate(monthly_budget, platform_shares(platforms, domain)),
        "daily_kop": {platform: limits[platform].daily_limit for platform in platforms},
        "off": off_view(off),
    }


def phrase_words(text: str) -> list[str]:
    """Слова фразы Директа: только буквы, цифры и дефис внутри слова. Остальные знаки (запятые, скобки, /, %, №,
    операторы + ! [ ] ( ) " |) API отклоняет вместе с созданием всей кампании — чистим до отправки."""
    return re.findall(r"[0-9a-zа-яё]+(?:-[0-9a-zа-яё]+)*", text.lower())


def phrase(service: str, city: str) -> str:
    return " ".join((phrase_words(service)[:4] + phrase_words(phrase_city(city)))[:7])


def direct_services(services: list[Service], city: str, limit: int | None = MAX_SERVICE_GROUPS) -> list[Service]:
    """Услуги групп Директа: у каждой своя ключевая фраза, и группы кампании не спорят за один запрос. Услуги с той
    же фразой, что у прежней, пропускаются; услуга без фразы после чистки знаков API не запустилась бы — не берём;
    групп не больше limit — первые услуги описания."""
    chosen: dict[str, Service] = {}
    for service in services:
        if not phrase_words(service.name):
            continue
        chosen.setdefault(phrase(service.name, city), service)
    return list(chosen.values())[:limit]


def group_slots(platform: str, services: list[str], segments: list[str]) -> list[tuple[str, str]]:
    """Пары (услуга, сегмент) групп площадки. В Директе группа — услуга со своей фразой, сегмент даёт повод текстам и
    картинкам; во VK, где фраз нет, группа — сегмент. Первые пары площадок совпадают и делят картинки."""
    if platform == "direct":
        return [(service, segments[index % len(segments)]) for index, service in enumerate(services)]
    return [(services[index % len(services)], segment) for index, segment in enumerate(segments)]


def audience(group: dict, city: str) -> str:
    """Кому покажут группу — по тому, что уходит на площадку при запуске (services.launch): Директу — город и
    ключевые фразы группы, VK — только город, без пола, возраста и интересов. Считается при показе плана: в старых
    планах в поле audience лежит повод сегмента."""
    if group["platform"] == "vk":
        return (
            f"Регион: {city}. Пол, возраст и интересы не задаём: VK сам показывает рекламу тем, кто скорее оставит "
            "заявку на сайте."
        )
    own = " ".join(group.get("service", "").split()[:4]).lower()
    themes = [item.removesuffix(f" {phrase_city(city).lower()}") for item in group["phrases"]]
    # В планах до групп по услугам у каждой группы Директа фразы всех услуг: своя услуга — первой.
    themes.sort(key=lambda theme: theme != own)
    if not themes:
        return f"Регион: {city}. Ключевых фраз пока нет: добавьте услуги в описание бизнеса."
    quoted = ", ".join(f"«{theme}»" for theme in themes)
    return (
        f"Тем, кто ищет в Яндексе {quoted}. Регион: {city}. "
        "Показываем в поиске и на других сайтах, где Яндекс размещает рекламу."
    )


def campaign_platform(key: str) -> str:
    """Ключ кампании плана — площадка или «площадка:номер»: у Директа своя кампания на услугу."""
    return key.split(":")[0]


def direct_batches(groups: list[dict], limit: PlatformLimit, minimum: dict[str, int], today: date) -> list[list[dict]]:
    """Группы Директа по кампаниям: кампания на услугу, если лимит Директа даёт каждой не меньше недельного минимума
    площадки, иначе кампаний меньше и в одной идут несколько услуг."""

    def fits(part: PlatformLimit) -> bool:
        weekly = weekly_limit(part.daily_limit, part.remaining_cap, today)
        return meets_minimum(part, minimum) and weekly >= minimum.get("weekly_limit_kop", 0)

    by_id = {str(group["id"]): group for group in groups}
    return [[by_id[id_] for id_ in batch] for batch in campaign_batches(limit, list(by_id), fits)]


def campaign_units(
    plan: Plan,
    platforms: list[str],
    limits: dict[str, PlatformLimit],
    minimums: dict[str, dict[str, int]],
    today: date,
) -> dict[str, tuple[list[dict], PlatformLimit]]:
    """Кампании запуска: ключ, группы плана и лимиты. VK — одна кампания на площадку; Директ — «direct:номер» по
    кампании на услугу (direct_batches). Повтор запуска берёт раскладку прошлой попытки (launch.batches), запуск,
    начатый до кампаний по услугам, и план до групп по услугам (у групп нет service) — одну кампанию «direct»."""
    launch = plan.payload.get("launch", {})
    saved = launch.get("batches") or ({"direct": []} if "direct" in launch.get("campaigns", {}) else {})
    units = {}
    for platform in platforms:
        groups = [group for group in plan.payload["groups"] if group["platform"] == platform]
        if platform != "direct" or "direct" in saved or any("service" not in group for group in groups):
            units[platform] = (groups, limits[platform])
            continue
        keys = sorted(key for key in saved if campaign_platform(key) == "direct")
        batches = (
            [[group for group in groups if group["id"] in saved[key]] for key in keys]
            if keys
            else direct_batches(groups, limits[platform], minimums[platform], today)
        )
        keys = keys or [f"direct:{index + 1}" for index in range(len(batches))]
        parts = split_campaign_limits(limits[platform], [[group["id"] for group in batch] for batch in batches])
        units |= {key: (batch, part) for key, batch, part in zip(keys, batches, parts, strict=True)}
    return units


def direct_campaigns(plan: Plan, limits: dict[str, PlatformLimit], today: date) -> list[list[dict]]:
    """Группы Директа по кампаниям для экрана плана: запущенный план — как создал запуск, черновик — раскладка по
    минимуму Директа без сети (запуск сверит её с живым минимумом)."""
    if "direct" not in plan.payload["platforms"]:
        return []
    if plan.status != "launched":
        units = campaign_units(plan, ["direct"], limits, {"direct": PLATFORM_MINIMUMS["direct"]}, today)
        return [groups for groups, _ in units.values()]
    groups = [group for group in plan.payload["groups"] if group["platform"] == "direct"]
    batches = plan.payload.get("launch", {}).get("batches", {})
    keys = sorted(key for key in batches if key.startswith("direct:"))
    return [[group for group in groups if group["id"] in batches[key]] for key in keys] if keys else [groups]


def campaigns_note(campaigns: list[list[dict]]) -> str | None:
    """Почему услуг Директа больше, чем кампаний: недельный минимум Директа ставится на каждую кампанию."""
    services = sum(1 for batch in campaigns for group in batch if group.get("service"))
    if len(campaigns) >= services:
        return None
    minimum = PLATFORM_MINIMUMS["direct"]["weekly_limit_kop"] // 100
    return (
        f"Бюджета Директа хватает на кампаний: {len(campaigns)}, а услуг {services}. У каждой кампании Директа есть "
        f"недельный минимум — {minimum} ₽, поэтому несколько услуг идут в одной кампании. С бюджетом побольше "
        "у каждой услуги будет своя кампания."
    )


def negative_phrases(niche: str) -> list[str]:
    """Минус-фразы Директа: запросы тех, кто ищет не услугу, а работу, обучение или бесплатную инструкцию."""
    return [*NEGATIVE_PHRASES, *NICHE_NEGATIVE_PHRASES.get(niche, ())]


def stem(word: str) -> str:
    return word.lower()[:4]


def targeting(plan: Plan, platform: str, group: dict) -> dict:
    """Настройки группы сверх региона: Директу — минус-фразы плана без слов из фраз самой группы (у услуги «Курсы
    маникюра» минус-слово «курсы» отрезало бы её же запросы)."""
    if platform != "direct":
        return {}
    own = {stem(word) for phrase in group.get("phrases", []) for word in phrase.split()}
    phrases = plan.payload.get("negative_phrases", negative_phrases(plan.payload.get("niche", "other")))
    return {"negative_keywords": [phrase for phrase in phrases if not own & {stem(word) for word in phrase.split()}]}


def launch_blockers(
    business: Business,
    plan: Plan,
    accounts: list[PlatformAccount],
    marking: MarkingProfile | None,
    assets: list[Asset],
    domain: DomainSettings,
    medical_acknowledged: bool | None = None,
) -> list[str]:
    """Почему запускать нельзя; пустой список — можно. None в medical_acknowledged — подтверждение ещё не спрашивали."""
    payload = plan.payload
    platforms = payload["platforms"]
    ready = {account.platform for account in accounts if account.status == "ok"}
    selected = {asset.id for asset in assets if asset.selected}
    result = []
    if "direct" not in ready:
        result.append("Подключите кабинет Яндекс Директа: без него запуск невозможен.")
    if "vk" in platforms and "vk" not in ready:
        result.append(
            "Подключите ВКонтакте заново: план готовили с этой площадкой, а доступа к её кабинету сейчас нет."
        )
    if business.description_approved_at is None:
        result.append("Подтвердите описание бизнеса.")
    if not business.metrica_counter_id or not business.lead_goal_ids:
        result.append(
            "Без счётчика Метрики и цели обращений мы не запускаем рекламу: мы не увидим, приходят ли клиенты."
        )
    if marking is None or not all(marking.confirmed_in_platform.get(platform) is True for platform in platforms):
        result.append("Заполните данные рекламодателя и подтвердите маркировку на подключённых площадках.")
    if payload["inputs_hash"] != inputs_hash(business, *plan_platforms(business, accounts, domain)):
        result.append("Сведения о бизнесе изменились после подготовки плана. Обновите план.")
    if any(not selected & set(payload["assets"].get(group_key(group), ())) for group in payload["groups"]):
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
    """Видео из 3–4 выбранных картинок пары (услуга, сегмент); без ffmpeg пропускаем: ролик загружается вручную.

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
    for key, chosen, video in segments:
        sources = [asset.id for asset in chosen]
        if (
            len(chosen) < 3
            or (video is not None and video.formats.get("sources") == sources)
            or (key in built and built[key][0] == sources)
        ):
            continue
        group = next(item for item in plan.payload["groups"] if group_key(item) == key)
        try:
            images = [media_root / asset.file_path for asset in chosen]
            built[key] = (sources, build_video(images, group["ads"][0]["title"], font_path=font_path).data)
        except (VideoError, ValueError):
            continue
    replaced, stored = [], []
    try:
        with repository.business_lock(business_id):
            for key, chosen, video in video_segments(repository, business_id):
                sources = [asset.id for asset in chosen]
                if len(chosen) < 3:
                    if video is not None:
                        video.selected = False
                    continue
                if video is not None and video.formats.get("sources") == sources:
                    video.selected = True
                    continue
                if key not in built or built[key][0] != sources:
                    continue
                path = store(media_root, business_id, "video", "mp4", built[key][1])
                stored.append(path)
                if video is None:
                    video = Asset(business_id=business_id, kind="video", idea_text=key, file_path=path)
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
            key,
            [by_id[id_] for id_ in ids if id_ in by_id and by_id[id_].selected][:4],
            next((asset for asset in assets if asset.kind == "video" and asset.idea_text == key), None),
        )
        for key, ids in plan.payload["assets"].items()
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
        business, platforms, off, digest, plan = self.inputs(self.repository.business(business_id))
        if plan is None or (plan.status != "launched" and plan.payload["inputs_hash"] != digest):
            with self.repository.business_lock(business_id):
                # Пока ждали блокировку, план мог собрать соседний запрос, а бизнес — измениться или удалиться.
                business, platforms, off, digest, plan = self.inputs(self.locked_business(business_id))
                if plan is None or (plan.status != "launched" and plan.payload["inputs_hash"] != digest):
                    # Новый план — новые кампании: кампании незавершённого запуска остались бы без плана и без
                    # присмотра. Сначала их останавливает воркер (services.launch.recover_launch).
                    if plan is not None and launch_unsettled(plan):
                        raise Blocked(UNSETTLED)
                    plan = await self.build(business, platforms, off, digest, plan, now, today)
        return self.view(business, plan, today)

    def inputs(self, business: Business) -> tuple[Business, list[str], dict[str, int], str, Plan | None]:
        if business.description_approved_at is None:
            raise Blocked("Подтвердите описание бизнеса: без него мы не готовим план.")
        platforms, off = plan_platforms(business, self.repository.accounts(business.id), self.domain)
        digest = inputs_hash(business, platforms, off)
        return business, platforms, off, digest, self.repository.latest_plan(business.id)

    def locked_business(self, business_id: int) -> Business:
        """Под блокировкой: удалённому бизнесу ИИ не генерирует и файлы не пишутся (удаление тоже идёт под ней)."""
        business = self.repository.fresh_business(business_id)
        if business is None:
            raise NotFound("Бизнес не найден.")
        return business

    async def build(
        self,
        business: Business,
        platforms: list[str],
        off: dict[str, int],
        digest: str,
        previous: Plan | None,
        now: datetime,
        today: date,
    ) -> Plan:
        budget = ai_budget(self.repository, business, self.limits, today)
        llm = SpendLLM(self.llm, self.repository, business.id, budget, "ad_text")
        niche = business.niche_class or "other"
        segments = list(SEGMENTS.get(niche, SEGMENTS["other"]))
        draft = description_draft(business)
        # Одноимённые услуги дают одну фразу: группе достаётся та же первая услуга, что выбрал direct_services.
        chosen = direct_services(draft.services, business.city)
        by_name = {service.name: service for service in chosen}
        services = [service.name for service in chosen] or [""]
        # Площадка и пара (услуга, сегмент) каждой группы в её порядке: группы идут по площадкам.
        slots = [
            (platform, service, segment)
            for platform in platforms
            for service, segment in group_slots(platform, services, segments)
        ]
        pairs = list(dict.fromkeys((service, segment) for _, service, segment in slots))
        assets = self.carry_assets(previous, [assets_key(*pair) for pair in pairs])
        # Тексты пишет ИИ, по секундам на каждый: соединение на это время возвращается в пул.
        self.repository.commit()
        limits = budget_limits(business, platforms, today, self.domain)
        groups = []
        for platform in platforms:
            # Пары внутри площадки не повторяются: в Директе разные услуги, во VK разные сегменты.
            names = {
                f"{service or 'Услуги'} — {segment}": (service, segment)
                for slot_platform, service, segment in slots
                if slot_platform == platform
            }
            daily = split_group_limits(limits[platform], dict.fromkeys(names, Decimal(1)))
            for (name, (service, segment)), group_limit in zip(names.items(), daily, strict=True):
                ads = []
                try:
                    for angle in ANGLES[:ANGLES_PER_GROUP]:
                        ads.append(
                            await generate_ad_text(
                                llm,
                                draft,
                                platform=platform,
                                angle=angle,
                                segment=segment,
                                service=by_name.get(service),
                                other=ads[-1].ad if ads else None,
                            )
                        )
                except ValueError as error:
                    raise Blocked("Не удалось подготовить безопасные тексты. Уточните описание бизнеса.") from error
                groups.append(
                    {
                        "id": len(groups) + 1,
                        "platform": platform,
                        "segment": segment,
                        "service": service,
                        "name": name,
                        "daily_limit_kop": group_limit.daily_limit,
                        # Группа Директа отвечает только на запросы своей услуги.
                        "phrases": [phrase(service, business.city)] if platform == "direct" and service else [],
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
        # Картинки — после текстов: бюджет ИИ сначала идёт на тексты. Своих картинок нет у новых пар и у всех пар
        # первого плана; первый план получает не меньше IMAGE_CANDIDATES картинок, новая пара — по IMAGES_PER_KEY.
        # Пары идут по кругу: если бюджет кончится, картинку получит сначала каждая пара, остальные останутся без
        # картинок до «Попросить ещё варианты» или своего фото.
        missing = [pair for pair in pairs if assets_key(*pair) not in assets]
        if missing:
            count = IMAGES_PER_KEY * len(missing) if assets else max(IMAGE_CANDIDATES, IMAGES_PER_KEY * len(missing))
            targets = [missing[index % len(missing)] for index in range(count)]
            for key, asset in await self.generate_images(business, budget, targets, self.labels(business.id, assets)):
                ids = assets.setdefault(key, [])
                asset.selected = len(ids) < 2
                ids.append(asset.id)
        self.carry_uploads(business.id, previous, slots, assets)
        payload = {
            "inputs_hash": digest,
            "built_at": now.isoformat(),
            "city": business.city,
            "month_budget_kop": business.month_budget_kop,
            "platforms": platforms,
            "off": off,
            "niche": niche,
            "segments": segments,
            "negative_phrases": negative_phrases(niche),
            "groups": groups,
            "assets": assets,
        }
        orphans = self.orphans(business.id, assets)
        for asset in orphans:
            self.repository.remove(asset)
        plan = Plan(
            business_id=business.id,
            version=previous.version + 1 if previous else 1,
            status="draft",
            payload=payload,
        )
        self.repository.add(plan)
        business.status = "plan_ready"
        self.repository.commit()
        # Файлы — после commit: откат не оставит строку без файла.
        for asset in orphans:
            remove_media(self.media_root, asset.file_path)
        return plan

    def orphans(self, business_id: int, assets: dict[str, list[int]]) -> list[Asset]:
        """Сгенерированные картинки прошлых планов, которых нет в новом, и ролики из них: их нигде не видно и не убрать,
        а место в MAX_ASSETS картинки занимают. Загруженные пользователем и те, на которые ссылаются объявления,
        остаются."""
        kept = {id_ for ids in assets.values() for id_ in ids} | self.repository.used_assets(business_id)
        rows = self.repository.assets(business_id)
        images = [asset for asset in rows if asset.kind == "image" and asset.id not in kept]
        gone = {asset.id for asset in images}
        videos = [
            asset
            for asset in rows
            if asset.kind == "video" and asset.id not in kept and gone & set(asset.formats.get("sources", ()))
        ]
        return images + videos

    def carry_assets(self, previous: Plan | None, keys: list[str]) -> dict[str, list[int]]:
        """Картинки переходят только к той же паре (услуга, сегмент): чужая услуга на картинке хуже новой генерации.
        Картинки старых планов лежат по сегменту и к парам с услугой не переходят."""
        if previous is None:
            return {}
        old = previous.payload["assets"]
        return {key: list(old[key]) for key in keys if key in old}

    def carry_uploads(
        self, business_id: int, previous: Plan | None, slots: list[tuple[str, str, str]], assets: dict[str, list[int]]
    ) -> None:
        """Свои фото не теряются, когда пара сменилась: переходят к первой группе с тем же сегментом, иначе — к первой
        группе той же площадки, иначе — к первой группе."""
        if previous is None:
            return
        uploads = {asset.id for asset in self.repository.assets(business_id) if asset.kind == "upload"}
        carried = {id_ for ids in assets.values() for id_ in ids}
        for group in previous.payload["groups"]:
            moved = [
                id_
                for id_ in previous.payload["assets"].get(group_key(group), ())
                if id_ in uploads and id_ not in carried
            ]
            if not moved:
                continue
            _, service, segment = next(
                (slot for slot in slots if slot[2] == group["segment"]),
                next((slot for slot in slots if slot[0] == group["platform"]), slots[0]),
            )
            assets.setdefault(assets_key(service, segment), []).extend(moved)
            carried.update(moved)

    async def record_art(self, record: ArtRecord) -> None:
        self.repository.save_art_call(record)

    async def generate_images(
        self, business: Business, budget: ArtBudget, targets: list[tuple[str, str]], used: Sequence[str] = ()
    ) -> list[tuple[str, Asset]]:
        """По картинке на каждую пару (услуга, сегмент) из targets; возвращает созданные с ключом их пары. used —
        подписи картинок плана: новые сцены их не повторяют."""
        llm = SpendLLM(self.llm, self.repository, business.id, budget, "image_ideas")
        art = self.art_factory(budget, self.record_art)
        hashes = [asset.phash for asset in self.repository.assets(business.id) if asset.phash]
        # Идеи и картинки ИИ готовит десятки секунд: соединение на это время возвращается в пул. Дальше каждая запись
        # (вызов ИИ, картинка) сама завершается commit.
        self.repository.commit()
        try:
            ideas = await generate_ideas(llm, business.niche_class or "other", targets, used)
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
                idea_text=idea.label,
                prompt=idea.prompt,
                file_path=self.store(business.id, checked.phash, "jpg", checked.data),
                phash=checked.phash,
                cost_kop=checked.cost_kop,
            )
            self.save_with_file(asset)
            hashes.append(checked.phash)
            created.append((assets_key(idea.service, idea.segment), asset))
        return created

    def labels(self, business_id: int, assets: dict[str, list[int]]) -> list[str]:
        """Подписи картинок плана. У видео в idea_text ключ «услуга — сегмент», у ранних картинок — длинное описание
        кадра: это не сцены, и ИИ их не получает."""
        ids = {id_ for items in assets.values() for id_ in items}
        return [
            asset.idea_text
            for asset in self.repository.assets(business_id)
            if asset.id in ids and asset.kind == "image" and asset.idea_text and len(asset.idea_text) <= TITLE_LIMIT
        ]

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

    def attach(self, plan: Plan, key: str, assets: list[Asset]) -> None:
        payload = deepcopy(plan.payload)
        payload["assets"].setdefault(key, []).extend(asset.id for asset in assets)
        plan.payload = payload
        self.repository.commit()

    async def regenerate_images(self, business_id: int, group_id: int, now: datetime, today: date) -> dict:
        self.draft_group(business_id, group_id)
        with self.repository.business_lock(business_id):
            business = self.locked_business(business_id)
            plan, group = self.draft_group(business_id, group_id)
            budget = ai_budget(self.repository, business, self.limits, today)
            if self.generations_remaining(budget, plan) == 0:
                raise Blocked("Лимит генерации картинок исчерпан. Загрузите своё фото.")
            pair = (group.get("service", ""), group["segment"])
            used = self.labels(business_id, plan.payload["assets"])
            created = await self.generate_images(business, budget, [pair] * REGENERATE_COUNT, used)
            if not created:
                raise Blocked("Не удалось получить новые картинки. Попробуйте позже или загрузите своё фото.")
            plan.payload = plan.payload | {"regenerated": plan.payload.get("regenerated", 0) + len(created)}
            self.attach(plan, group_key(group), [asset for _, asset in created])
        return self.view(business, plan, today)

    def upload_photo(self, business_id: int, group_id: int, data: bytes, today: date) -> dict:
        business = self.repository.business(business_id)
        self.draft_group(business_id, group_id)
        # Ролики собираются из картинок, по одному на пару: в лимит идут только картинки.
        assets = [asset for asset in self.repository.assets(business_id) if asset.kind in IMAGE_KINDS]
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
            self.attach(plan, group_key(group), [asset])
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

    def generations_remaining(self, budget: ArtBudget, plan: Plan) -> int:
        """Сколько раз ещё можно попросить картинки. По тратам на ИИ; в демо ИИ бесплатный и траты не растут —
        поэтому считаем и сами картинки, попрошенные к этому плану: счётчик под кнопкой убывает и там."""
        left = budget.limit_kop - max(budget.spent_kop, plan.payload.get("regenerated", 0) * ART_COST_KOP)
        return max(0, left // (REGENERATE_COUNT * ART_COST_KOP))

    def view(self, business: Business, plan: Plan, today: date) -> dict:
        payload = plan.payload
        assets = self.repository.assets(business.id)
        by_id = {asset.id: asset for asset in assets}
        platforms = payload["platforms"]
        shares = allocate(business.month_budget_kop, platform_shares(platforms, self.domain))
        accounts = self.repository.accounts(business.id)
        marking = self.repository.marking(business.id)
        blockers = launch_blockers(business, plan, accounts, marking, assets, self.domain)
        limits = (
            {}
            if plan.status == "launched"
            else launch_limits(payload["month_budget_kop"], platforms, today, self.domain)
        )
        groups = [
            {
                "id": group["id"],
                "name": group["name"],
                "platform": group["platform"],
                "audience": audience(group, payload.get("city", business.city)),
                "daily_limit_kop": group["daily_limit_kop"],
                "ads": [{"title": ad["title"], "text": ad["text"]} for ad in group["ads"]],
                "assets": [
                    {
                        "id": asset.id,
                        "url": f"/api/plan/assets/{asset.id}/file",
                        "kind": asset.kind,
                        "alt": asset.idea_text or "Ваше фото",
                        # Подпись — суть идеи картинки; у ранних картинок в idea_text лежит длинное описание кадра.
                        "label": shorten(asset.idea_text, TITLE_LIMIT, placeholder="…") if asset.idea_text else "",
                        "selected": asset.selected,
                    }
                    for asset in (by_id[id_] for id_ in payload["assets"].get(group_key(group), ()) if id_ in by_id)
                ],
            }
            for group in payload["groups"]
        ]
        campaigns = direct_campaigns(plan, limits, today)
        return {
            "id": plan.id,
            "month_budget_kop": payload["month_budget_kop"],
            "direct_budget_kop": shares.get("direct", 0),
            "vk_budget_kop": shares.get("vk", 0),
            "daily_kop": {platform: limit.daily_limit for platform, limit in limits.items()},
            "off": off_view(payload.get("off", {})),
            "groups": groups,
            # Сколько групп Директа дало бы описание без потолка: больше MAX_SERVICE_GROUPS — часть услуг не вошла.
            # Кампании Директа: у каждой услуги своя, если бюджет даёт каждой недельный минимум площадки.
            "direct_campaigns": [
                {"services": [group.get("service") or group["name"] for group in batch]} for batch in campaigns
            ],
            "direct_campaigns_note": campaigns_note(campaigns),
            "negative_phrases": payload.get("negative_phrases", negative_phrases(payload.get("niche", "other")))
            if "direct" in platforms
            else [],
            "direct_services_total": len(direct_services(description_draft(business).services, business.city, None)),
            "generations_remaining": self.generations_remaining(
                ai_budget(self.repository, business, self.limits, today), plan
            ),
            "medical_level": business.medical_level,
            "marking_confirmed": marking is not None
            and all(marking.confirmed_in_platform.get(platform) is True for platform in platforms),
            "ready_to_launch": not blockers,
            "blockers": blockers,
            "launch_error": launch_error(plan),
            # Частичный запуск: причины незапустившихся площадок. Экран запущенного плана показывает их
            # с предложением повторить (повтор доводит только эти площадки, работающие не трогает).
            "platform_errors": payload.get("launch", {}).get("platform_errors", {}),
            # Площадки, где запуск упёрся в проверку объявлений: после одобрения кампании включит шаг модерации,
            # кнопка не нужна.
            "awaiting_moderation": payload.get("launch", {}).get("awaiting_moderation", []),
            "status": "launched" if plan.status == "launched" else "draft",
        }
