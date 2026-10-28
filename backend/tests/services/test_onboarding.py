import asyncio
import itertools
import json
from collections.abc import Awaitable, Callable
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from io import BytesIO
from pathlib import Path
from random import Random

import pytest
from PIL import Image, ImageDraw, ImageFont

from app.content.art import ArtBudget, FakeArt
from app.content.art import ArtCall as ArtRecord
from app.content.images import DEFAULT_FONT
from app.content.site_reader import Page, SiteReadResult
from app.content.verify import DescriptionDraft, OwnerQuestion, Service
from app.content.yandexgpt import CompletionResult, YandexGPTError
from app.db.models import (
    Ad,
    AdGroup,
    ArtCall,
    Asset,
    Business,
    Campaign,
    ChangeLog,
    LLMCall,
    MarkingProfile,
    Plan,
    PlatformAccount,
    Target,
    WorkerRun,
)
from app.platforms.fake import FakeAdapter
from app.platforms.metrica import FakeMetrica, MetricaCounter, MetricaGoal
from app.services.changes import ChangeConflict
from app.services.launch import LaunchRepository, LaunchService
from app.services.onboarding import (
    LOCK_WAITERS,
    SAVING,
    SITE_CHECKS,
    AiLimits,
    Blocked,
    NotFound,
    OnboardingError,
    OnboardingService,
    SpendLLM,
    ai_budget,
    description_draft,
    description_fields,
    detect_niche,
    launch_time,
    normalize_phone,
    service_names,
    valid_inn,
)
from app.services.plan import PlanService

NOW = datetime(2026, 1, 1, 9, tzinfo=UTC)
TODAY = date(2026, 1, 1)
BUDGET_KOP = 5_000_000
LIMITS = AiLimits(onboarding_kop=6000, weekly_kop=2500)
COUNTER = 12345
SITE = "https://salon.example/"
DESCRIPTION = json.dumps(
    {
        "services": [{"name": "Маникюр классический", "price": 150000, "quote": "Маникюр классический — 1 500 ₽."}],
        "address": {"value": "Самара, ул. Полевая, д. 7", "quote": "Адрес: г. Самара, ул. Полевая, д. 7."},
        "advantages": [{"value": "Работаем ежедневно", "quote": "Работаем ежедневно с 09:00 до 21:00."}],
    },
    ensure_ascii=False,
)
AD = json.dumps({"title": "Маникюр классический", "title2": "", "text": "Запишитесь на маникюр"}, ensure_ascii=False)


def page_text(extra: str = "") -> list[Page]:
    return [
        Page(
            SITE,
            f"Салон красоты. Маникюр классический — 1 500 ₽. Работаем ежедневно с 09:00 до 21:00. {extra}",
            counter_ids=[COUNTER],
        ),
        Page(SITE + "contacts", "Адрес: г. Самара, ул. Полевая, д. 7. Записывайтесь заранее."),
    ]


class MemoryRepository(LaunchRepository):
    """Те же методы, что у репозитория БД, но поверх списков: сервисы проверяются без PostgreSQL."""

    def __init__(self) -> None:
        self.tables: dict[type, list] = {}
        self.locked = False

    def rows(self, model: type, **keys: object) -> list:
        return [
            item
            for item in self.tables.get(model, [])
            if all(getattr(item, name) == value for name, value in keys.items())
        ]

    def put(self, item: object) -> None:
        table = self.tables.setdefault(type(item), [])
        for column in type(item).__table__.columns:
            if getattr(item, column.key) is None and column.default is not None:
                arg = column.default.arg
                setattr(item, column.key, arg(None) if column.default.is_callable else arg)
        if hasattr(item, "id") and item.id is None:
            item.id = max((row.id for row in table), default=0) + 1
        if item not in table:
            table.append(item)

    @contextmanager
    def business_lock(self, business_id: int):
        if self.locked:
            raise ChangeConflict("Изменение этого бизнеса уже выполняется. Попробуйте позже.")
        yield

    def commit(self) -> None:
        pass

    def refresh(self, item: object) -> None:
        pass

    def add(self, item: object) -> None:
        self.put(item)

    def stage(self, *items: object) -> None:
        for item in items:
            self.put(item)

    def save(self, change: ChangeLog) -> None:
        self.put(change)

    def get(self, change_id: int, business_id: int) -> ChangeLog | None:
        return next(iter(self.rows(ChangeLog, id=change_id, business_id=business_id)), None)

    def pending(self, business_id: int | None = None) -> list[ChangeLog]:
        return [item for item in self.rows(ChangeLog, status="pending") if business_id in (None, item.business_id)]

    def user_businesses(self, user_id: int) -> list[Business]:
        return self.rows(Business, user_id=user_id)

    def business(self, business_id: int) -> Business:
        business = self.fresh_business(business_id)
        if business is None:
            raise ValueError("Бизнес не найден.")
        return business

    def marking(self, business_id: int) -> MarkingProfile | None:
        return next(iter(self.rows(MarkingProfile, business_id=business_id)), None)

    def accounts(self, business_id: int) -> list[PlatformAccount]:
        return self.rows(PlatformAccount, business_id=business_id)

    def ai_spent(self, business_id: int, since: datetime | None = None) -> int:
        # В памяти created_at не проставляется сервером: такой вызов считается только что сделанным.
        return sum(
            item.cost_kop
            for model in (LLMCall, ArtCall)
            for item in self.rows(model, business_id=business_id)
            if since is None or item.created_at is None or item.created_at >= since
        )

    def launched_at(self, business_id: int) -> datetime | None:
        plans = self.rows(Plan, business_id=business_id, status="launched")
        latest = max(plans, key=lambda item: item.version, default=None)
        return launch_time(latest.payload["launch"]["launched_at"]) if latest else None

    def record_llm(self, business_id: int, purpose: str, result: CompletionResult) -> None:
        self.put(
            LLMCall(
                business_id=business_id,
                purpose=purpose,
                model=result.model,
                tokens_in=result.tokens_in,
                tokens_out=result.tokens_out,
                cost_kop=result.cost_kop,
                status=result.status,
            )
        )

    def save_art_call(self, record) -> None:
        row = next(iter(self.rows(ArtCall, id=record.id)), None) if record.id else None
        if row is None:
            row = ArtCall(business_id=record.business_id)
            self.put(row)
        row.cost_kop, row.status, row.operation_id, row.seed = (
            record.cost_kop,
            record.status,
            record.operation_id,
            record.seed,
        )
        record.id = row.id

    def latest_plan(self, business_id: int) -> Plan | None:
        return max(self.rows(Plan, business_id=business_id), key=lambda item: item.version, default=None)

    def get_plan(self, plan_id: int, business_id: int) -> Plan | None:
        return next(iter(self.rows(Plan, id=plan_id, business_id=business_id)), None)

    def assets(self, business_id: int) -> list[Asset]:
        return self.rows(Asset, business_id=business_id)

    def asset(self, business_id: int, asset_id: int) -> Asset | None:
        return next(iter(self.rows(Asset, id=asset_id, business_id=business_id)), None)

    def used_assets(self, business_id: int) -> set[int]:
        ids = {asset.id for asset in self.assets(business_id)}
        return {id_ for ad in self.tables.get(Ad, []) for id_ in (ad.asset_id, ad.video_id) if id_ in ids}

    def remove(self, item: object) -> None:
        self.tables[type(item)].remove(item)

    def fresh_business(self, business_id: int) -> Business | None:
        return next(iter(self.rows(Business, id=business_id)), None)

    def campaign(self, campaign_id: int) -> Campaign | None:
        return next(iter(self.rows(Campaign, id=campaign_id)), None)

    def tree_rows(self, campaign_id: int):
        groups = self.rows(AdGroup, campaign_id=campaign_id)
        ids = {group.id for group in groups}
        return (
            groups,
            [item for item in self.tables.get(Ad, []) if item.ad_group_id in ids],
            [item for item in self.tables.get(Target, []) if item.ad_group_id in ids],
        )

    def save_ledger(self, ledger) -> None:
        self.put(ledger)

    def read_notices(self, business_id: int, now: datetime, plan_id: int | None = None) -> None:
        for row in self.rows(WorkerRun, business_id=business_id, kind="notification", status="ok"):
            entry = row.log[0]
            if (
                entry.get("reason_code") == "LAUNCH_INTERRUPTED"
                and plan_id in (None, entry.get("target_id"))
                and not any("read_at" in item for item in row.log)
            ):
                row.log = row.log + [{"read_at": now.isoformat()}]

    def local_entities(self, business_id: int, platform: str | None) -> dict:
        campaigns = [item for item in self.rows(Campaign, business_id=business_id) if item.platform == platform]
        groups, ads, targets = [], [], []
        for campaign in campaigns:
            found = self.tree_rows(campaign.id)
            groups += found[0]
            ads += found[1]
            targets += found[2]
        return {
            (kind, item.external_id): item
            for kind, items in (("campaign", campaigns), ("group", groups), ("ad", ads), ("target", targets))
            for item in items
            if item.external_id
        }


class ScriptedLLM:
    """Отвечает по виду запроса: описание сайта, идеи картинок или текст объявления."""

    def __init__(self, description: str = DESCRIPTION, ad: str = AD, cost_kop: int = 0) -> None:
        self.description = description
        self.ad = ad
        self.cost_kop = cost_kop
        self.calls: list[str] = []

    async def complete_json(self, system: str, user: str, temperature: float = 0.2) -> str:
        if "Извлеки" in system:
            kind, text = "description", self.description
        elif "визуальные идеи" in system:
            kind = "ideas"
            items = json.loads(user)["items"]
            text = json.dumps(
                {"ideas": [{"title": f"Идея {number}", "visual": f"Кадр {number}"} for number in range(len(items))]}
            )
        else:
            kind, text = "ad", self.ad
            location = json.loads(user).get("location")
            if location and text == AD:
                text = json.dumps({**json.loads(AD), "text": location[0]}, ensure_ascii=False)
        self.calls.append(kind)
        if not self.cost_kop:
            return text
        return CompletionResult(
            text, model="test", model_version="1", tokens_in=100, tokens_out=100, cost_kop=self.cost_kop, status="DONE"
        )


def picture(seed: int) -> bytes:
    rng = Random(seed)
    image = Image.new("RGB", (512, 512), "#f4f0e8")
    draw = ImageDraw.Draw(image)
    for _ in range(7):
        x, y = rng.randrange(450), rng.randrange(450)
        draw.rectangle(
            (x, y, x + rng.randrange(60, 300), y + rng.randrange(60, 300)),
            fill=tuple(rng.randrange(256) for _ in range(3)),
        )
    output = BytesIO()
    image.save(output, format="JPEG")
    return output.getvalue()


@dataclass
class VariedArt(FakeArt):
    """Разные картинки на каждый вызов; учёт трат — как у YandexArt, через record_call."""

    record: Callable[[ArtRecord], Awaitable[None]] | None = None

    async def generate(self, prompt: str, *, seed: int, business_id: int):
        self.image = picture(seed)
        try:
            return await super().generate(prompt, seed=seed, business_id=business_id)
        finally:
            await self.record(self.calls[-1])


def font_path() -> str:
    for candidate in (DEFAULT_FONT, "/System/Library/Fonts/Supplemental/Arial.ttf", "DejaVuSans.ttf"):
        try:
            return str(ImageFont.truetype(candidate, size=24).path)
        except OSError:
            continue
    pytest.skip("Для наложения оффера нужен TrueType-шрифт с кириллицей")


@dataclass
class World:
    repository: MemoryRepository
    llm: ScriptedLLM
    arts: list[VariedArt]
    onboarding: OnboardingService
    plans: PlanService
    launches: LaunchService
    adapters: dict[str, FakeAdapter]
    business: Business
    media: Path


def make_world(
    tmp_path: Path,
    *,
    platforms: tuple[str, ...] = ("direct", "vk"),
    pages: list[Page] | None = None,
    llm: ScriptedLLM | None = None,
    limits: AiLimits = LIMITS,
    adapters: dict[str, FakeAdapter] | None = None,
) -> World:
    repository = MemoryRepository()
    llm = llm or ScriptedLLM()
    adapters = adapters or {platform: FakeAdapter() for platform in ("direct", "vk")}
    sources = pages if pages is not None else page_text()

    async def reader(url: str) -> SiteReadResult:
        return SiteReadResult(sources)

    metrica = FakeMetrica(
        [MetricaCounter(COUNTER, "Счётчик", "salon.example")], {COUNTER: [MetricaGoal(1, "Заявка", "action")]}
    )
    onboarding = OnboardingService(repository, llm, lambda business_id: metrica, limits, reader=reader)
    business = Business(user_id=1, site_url=SITE, city="Самара", month_budget_kop=BUDGET_KOP)
    repository.add(business)
    for platform in platforms:
        repository.add(
            PlatformAccount(business_id=business.id, platform=platform, auth_mode="oauth", access_token_enc="t")
        )

    art_holder: list[VariedArt] = []

    def art_factory(budget: ArtBudget, record):
        art = VariedArt(b"", budget, record=record)
        art_holder.append(art)
        return art

    plans = PlanService(
        repository,
        llm,
        art_factory,
        tmp_path / "media",
        limits,
        font_path=font_path(),
        seeds=itertools.count(1).__next__,
    )
    launches = LaunchService(
        repository, lambda business_id, platform: adapters[platform], tmp_path / "media", font_path=font_path()
    )
    return World(repository, llm, art_holder, onboarding, plans, launches, adapters, business, tmp_path / "media")


async def onboard(world: World, *, marking: bool = True, confirmed: dict | None = None) -> None:
    business_id = world.business.id
    await world.onboarding.site_check(business_id, NOW, TODAY)
    fields = {
        key: {"value": item["value"]}
        for key, item in (await world.onboarding.site_check(business_id, NOW, TODAY))["description"].items()
    }
    world.onboarding.save_description(business_id, fields, True, NOW)
    await world.onboarding.save_metrica(business_id, COUNTER, [1])
    if marking:
        world.onboarding.save_marking(
            business_id, "ip", "526317984689", "ИП Иванов", "8 900 123-45-67", confirmed or {"direct": True, "vk": True}
        )


@pytest.fixture
def world(tmp_path: Path) -> World:
    return make_world(tmp_path)


@pytest.mark.parametrize("inn", ["7707083893", "526317984689"])
def test_valid_inn(inn: str) -> None:
    assert valid_inn(inn)


@pytest.mark.parametrize("inn", ["7707083894", "526317984688", "000000000000", "123", "77070838931", "77070838a3", ""])
def test_invalid_inn(inn: str) -> None:
    assert not valid_inn(inn)


def test_phone_is_normalized_and_validated() -> None:
    assert normalize_phone("8 (900) 123-45-67") == "+79001234567"
    assert normalize_phone("+7 900 123 45 67") == "+79001234567"
    with pytest.raises(OnboardingError):
        normalize_phone("12345")


def test_niche_detection() -> None:
    assert detect_niche("Маникюр и педикюр", "none") == "nails"
    assert detect_niche("Что-то своё", "none") == "other"
    assert detect_niche("Что-то своё", "required") == "medical"


def test_business_budget_is_stored_in_kopecks_and_draft_is_updated(world: World) -> None:
    created = world.onboarding.create_business(2, "salon2.example", " Казань ", 1_500_000, None)
    assert created["site_url"] == "https://salon2.example/"
    assert created["month_budget_kop"] == 1_500_000 and type(created["month_budget_kop"]) is int
    again = world.onboarding.create_business(2, "https://salon2.example/", "Казань", 2_000_000, 300_000)
    assert again["id"] == created["id"] and again["avg_check_kop"] == 300_000


def test_business_budget_below_direct_minimum_names_the_minimum_and_why(world: World) -> None:
    with pytest.raises(OnboardingError) as error:
        world.onboarding.create_business(2, "salon2.example", "Казань", 1_269_999, None)
    assert str(error.value) == (
        "Минимальный бюджет — 12 700 ₽ в месяц. Яндекс Директ требует не меньше 300 ₽ на кампанию в неделю, а мы "
        "считаем с запасом, чтобы реклама не вставала на паузу в конце недели и месяца."
    )
    assert world.repository.user_businesses(2) == []
    assert world.onboarding.create_business(2, "salon2.example", "Казань", 1_270_000, None)["month_budget_kop"]


@pytest.mark.parametrize(
    ("site", "city", "budget"),
    [
        ("http://127.0.0.1/", "Самара", 100),
        ("javascript:alert(1)", "Самара", 100),
        ("salon.example", " ", 100),
        ("salon.example", "Урюпинск", 2_000_000),
        ("salon.example", "Самара", 0),
        ("salon.example", "Самара", -5),
        ("salon.example", "Самара", 1.5),
    ],
)
def test_invalid_business_is_rejected(world: World, site: str, city: str, budget: object) -> None:
    with pytest.raises(OnboardingError):
        world.onboarding.create_business(2, site, city, budget, None)
    assert len(world.repository.user_businesses(2)) == 0


def test_launched_business_cannot_be_recreated(world: World) -> None:
    world.business.status = "running"
    with pytest.raises(Blocked):
        world.onboarding.create_business(1, "salon.example", "Самара", BUDGET_KOP, None)
    # Повторное «Далее» без правок отвечает без блокировки только черновику: запущенный бизнес так не пересоздать.
    with pytest.raises(Blocked):
        world.onboarding.create_business(1, SITE, "Самара", BUDGET_KOP, None)


@pytest.mark.parametrize(("city", "avg_check"), [("Казань", None), ("Самара", 250_000)])
def test_draft_saves_city_or_average_check_changed_alone(world: World, city: str, avg_check: int | None) -> None:
    """Повторное «Далее» без правок ничего не пишет, но правка одного города или одного среднего чека — правка."""
    view = world.onboarding.create_business(1, SITE, city, BUDGET_KOP, avg_check)
    assert (view["city"], view["avg_check_kop"]) == (city, avg_check)
    assert (world.business.city, world.business.avg_check_kop) == (city, avg_check)


async def test_site_check_builds_verified_draft(world: World) -> None:
    result = await world.onboarding.site_check(world.business.id, NOW, TODAY)
    assert result["status"] == "ready" and result["counter_id"] == COUNTER and result["services_found"] == 1
    fields = result["description"]
    assert fields["services"]["value"] == "Маникюр классический" and fields["services"]["verified"]
    assert fields["prices"]["value"] == "Маникюр классический — 1 500 ₽"
    assert fields["address"]["quote"] == "Адрес: г. Самара, ул. Полевая, д. 7."
    assert world.business.niche_class == "nails"


async def test_site_check_is_cached_and_does_not_repeat_paid_calls(world: World) -> None:
    await world.onboarding.site_check(world.business.id, NOW, TODAY)
    await world.onboarding.site_check(world.business.id, NOW + timedelta(minutes=5), TODAY)
    assert world.llm.calls == ["description"]
    await world.onboarding.site_check(world.business.id, NOW + timedelta(minutes=11), TODAY)
    assert world.llm.calls == ["description", "description"]


async def test_site_check_counts_rate_limit_only_for_real_reading(world: World) -> None:
    misses = []
    for minutes in (0, 5, 11):
        now = NOW + timedelta(minutes=minutes)
        await world.onboarding.site_check(world.business.id, now, TODAY, on_miss=lambda m=minutes: misses.append(m))
    assert misses == [0, 11]


async def test_site_check_refresh_reads_site_past_fresh_cache(world: World) -> None:
    misses = []
    await world.onboarding.site_check(world.business.id, NOW, TODAY, on_miss=lambda: misses.append("first"))
    await world.onboarding.site_check(world.business.id, NOW, TODAY, on_miss=lambda: misses.append("cached"))
    assert world.llm.calls == ["description"] and misses == ["first"]
    later = NOW + timedelta(minutes=1)
    await world.onboarding.site_check(
        world.business.id, later, TODAY, on_miss=lambda: misses.append("refresh"), refresh=True
    )
    assert world.llm.calls == ["description", "description"] and misses == ["first", "refresh"]
    assert world.business.description["check"]["checked_at"] == later.isoformat()


async def test_site_check_cache_is_dropped_when_ai_mode_changes(world: World) -> None:
    """Подключили YandexGPT вместо заглушки: проверка из кэша с ответом заглушки больше не отдаётся."""
    misses = []
    await world.onboarding.site_check(world.business.id, NOW, TODAY, on_miss=lambda: misses.append("demo"))
    world.onboarding.ai_mode = "yandexgpt/latest"
    await world.onboarding.site_check(world.business.id, NOW, TODAY, on_miss=lambda: misses.append("gpt"))
    await world.onboarding.site_check(world.business.id, NOW, TODAY, on_miss=lambda: misses.append("cached"))
    assert misses == ["demo", "gpt"] and world.llm.calls == ["description", "description"]
    assert world.business.description["check"]["ai_mode"] == "yandexgpt/latest"


async def test_site_check_cache_written_before_ai_mode_is_reused(world: World) -> None:
    misses = []
    world.onboarding.ai_mode = "yandexgpt/latest"
    await world.onboarding.site_check(world.business.id, NOW, TODAY, on_miss=lambda: misses.append("first"))
    del world.business.description["check"]["ai_mode"]
    await world.onboarding.site_check(world.business.id, NOW, TODAY, on_miss=lambda: misses.append("cached"))
    assert misses == ["first"] and world.llm.calls == ["description"]


EMPTY_DESCRIPTION = json.dumps({"services": [], "address": {"value": "", "quote": ""}, "advantages": []})


async def test_site_check_refresh_fills_only_empty_fields_of_unapproved_description(world: World) -> None:
    world.llm.description = EMPTY_DESCRIPTION
    await world.onboarding.site_check(world.business.id, NOW, TODAY)
    world.onboarding.save_description(world.business.id, {"address": {"value": "Самара, у вокзала"}}, False, NOW)
    world.llm.description = DESCRIPTION
    fields = (await world.onboarding.site_check(world.business.id, NOW, TODAY, refresh=True))["description"]
    assert fields["services"]["value"] == "Маникюр классический" and fields["services"]["verified"]
    assert fields["address"]["value"] == "Самара, у вокзала" and not fields["address"]["verified"]
    assert world.business.description["fields"] == fields


async def test_site_reread_after_cache_expiry_fills_empty_fields(world: World) -> None:
    world.llm.description = EMPTY_DESCRIPTION
    await world.onboarding.site_check(world.business.id, NOW, TODAY)
    world.onboarding.save_description(world.business.id, {"address": {"value": "Самара, у вокзала"}}, False, NOW)
    world.llm.description = DESCRIPTION
    fields = (await world.onboarding.site_check(world.business.id, NOW + timedelta(minutes=11), TODAY))["description"]
    assert world.llm.calls == ["description", "description"]
    assert fields["services"]["value"] == "Маникюр классический" and fields["services"]["verified"]
    assert fields["address"]["value"] == "Самара, у вокзала"


async def test_site_check_refresh_keeps_approved_description(world: World) -> None:
    world.llm.description = EMPTY_DESCRIPTION
    await world.onboarding.site_check(world.business.id, NOW, TODAY)
    world.onboarding.save_description(world.business.id, {"services": {"value": "Стрижки"}}, True, NOW)
    approved = world.business.description["fields"]
    world.llm.description = DESCRIPTION
    result = await world.onboarding.site_check(world.business.id, NOW, TODAY, refresh=True)
    assert result["services_found"] == 1
    assert result["description"] == approved == world.business.description["fields"]


class SlowLLM(ScriptedLLM):
    """Отдаёт управление циклу событий, как настоящий ИИ, пока готовит ответ."""

    def __init__(self, during: Callable[[], None] = lambda: None) -> None:
        super().__init__()
        self.during = during

    async def complete_json(self, system: str, user: str, temperature: float = 0.2) -> str:
        await asyncio.sleep(0.01)
        self.during()
        await asyncio.sleep(0.01)
        return await super().complete_json(system, user, temperature)


async def test_parallel_site_check_waits_for_running_one_and_takes_its_result(world: World) -> None:
    reads, misses = [], []
    original = world.onboarding.reader

    async def slow_reader(url: str) -> SiteReadResult:
        reads.append(url)
        await asyncio.sleep(0.01)
        return await original(url)

    world.onboarding.llm = world.llm = SlowLLM()
    world.onboarding.reader = slow_reader
    # Ждущая проверка без события ведущей висела бы вечно: срок превращает такую поломку в падение теста.
    first, second = await asyncio.wait_for(
        asyncio.gather(
            world.onboarding.site_check(world.business.id, NOW, TODAY, on_miss=lambda: misses.append("first")),
            world.onboarding.site_check(world.business.id, NOW, TODAY, on_miss=lambda: misses.append("second")),
        ),
        5,
    )
    assert first == second and first["services_found"] == 1
    assert reads == [SITE] and world.llm.calls == ["description"] and misses == ["first"]
    assert SITE_CHECKS == {}


async def test_refresh_during_running_site_check_takes_its_result_without_second_read(world: World) -> None:
    reads, misses = [], []
    original = world.onboarding.reader

    async def slow_reader(url: str) -> SiteReadResult:
        reads.append(url)
        await asyncio.sleep(0.01)
        return await original(url)

    world.onboarding.llm = world.llm = SlowLLM()
    world.onboarding.reader = slow_reader
    first, second = await asyncio.wait_for(
        asyncio.gather(
            world.onboarding.site_check(world.business.id, NOW, TODAY, on_miss=lambda: misses.append("first")),
            world.onboarding.site_check(
                world.business.id, NOW, TODAY, on_miss=lambda: misses.append("refresh"), refresh=True
            ),
        ),
        5,
    )
    assert first == second and reads == [SITE] and world.llm.calls == ["description"] and misses == ["first"]
    assert SITE_CHECKS == {}


async def test_after_failed_site_check_one_waiting_check_reads_site_and_others_wait_for_it(world: World) -> None:
    reads, misses = [], []
    original = world.onboarding.reader

    async def failing_once(url: str) -> SiteReadResult:
        reads.append(url)
        await asyncio.sleep(0.01)
        if len(reads) == 1:
            raise RuntimeError("Проверка сорвалась")
        return await original(url)

    world.onboarding.reader = failing_once
    results = await asyncio.wait_for(
        asyncio.gather(
            *(
                world.onboarding.site_check(
                    world.business.id, NOW, TODAY, on_miss=lambda name=name: misses.append(name)
                )
                for name in ("first", "second", "third", "fourth")
            ),
            return_exceptions=True,
        ),
        5,
    )
    assert isinstance(results[0], RuntimeError)
    assert results[1] == results[2] == results[3] and results[1]["services_found"] == 1
    assert reads == [SITE, SITE] and misses == ["first", "second"] and world.llm.calls == ["description"]
    assert SITE_CHECKS == {}


async def test_finished_site_check_does_not_remove_running_check_with_owner_text(world: World) -> None:
    """Проверка с текстом владельца не ждёт идущую и ставит своё событие: закончившаяся раньше обычная проверка его не
    снимает, и следующая обычная ждёт проверку с текстом владельца, а не читает сайт рядом с ней."""
    reads, gates = [], [asyncio.Event(), asyncio.Event()]
    original = world.onboarding.reader

    async def reader(url: str) -> SiteReadResult:
        gate = gates[len(reads)]
        reads.append(url)
        await gate.wait()
        return await original(url)

    world.onboarding.reader = reader
    business_id = world.business.id
    ordinary = asyncio.create_task(world.onboarding.site_check(business_id, NOW, TODAY))
    await asyncio.sleep(0.01)
    owner = asyncio.create_task(world.onboarding.site_check(business_id, NOW, TODAY, owner_text="Маникюр и педикюр"))
    await asyncio.sleep(0.01)
    gates[0].set()
    await asyncio.wait_for(ordinary, 5)
    assert business_id in SITE_CHECKS
    following = asyncio.create_task(world.onboarding.site_check(business_id, NOW, TODAY))
    await asyncio.sleep(0.01)
    assert not following.done()
    gates[1].set()
    assert await asyncio.wait_for(following, 5) == await owner
    assert reads == [SITE, SITE] and world.llm.calls == ["description", "description"]
    assert SITE_CHECKS == {}


async def test_site_check_reads_site_outside_lock_and_waits_for_it(world: World) -> None:
    held = True
    seen = []

    @contextmanager
    def lock(business_id: int):
        if held:
            raise ChangeConflict("Этот бизнес уже обрабатывается.")
        yield

    async def release() -> None:
        nonlocal held
        await asyncio.sleep(0.6)
        held = False

    original = world.onboarding.reader

    async def reader(url: str) -> SiteReadResult:
        seen.append(held)
        return await original(url)

    world.repository.business_lock = lock
    world.onboarding.reader = reader
    result, _ = await asyncio.gather(world.onboarding.site_check(world.business.id, NOW, TODAY), release())
    assert seen == [True] and result["status"] == "ready" and world.llm.calls == ["description"]


async def test_site_changed_during_read_is_not_paid_or_saved(world: World) -> None:
    original = world.onboarding.reader

    async def reader(url: str) -> SiteReadResult:
        world.business.site_url = "https://other.example/"
        return await original(url)

    world.onboarding.reader = reader
    with pytest.raises(ChangeConflict, match="Адрес сайта изменился"):
        await world.onboarding.site_check(world.business.id, NOW, TODAY)
    assert world.llm.calls == [] and "check" not in world.business.description


async def test_site_changed_while_ai_answers_is_not_saved(world: World) -> None:
    def change_site() -> None:
        world.business.site_url = "https://other.example/"
        world.business.description = {}

    world.onboarding.llm = world.llm = SlowLLM(change_site)
    with pytest.raises(ChangeConflict, match="Адрес сайта изменился"):
        await world.onboarding.site_check(world.business.id, NOW, TODAY)
    assert world.llm.calls == ["description"]
    assert world.business.description == {} and world.business.medical_level == "none"
    assert world.business.niche_class is None and world.business.niche_text is None
    assert SITE_CHECKS == {}


async def test_business_deleted_during_site_check_is_not_found(world: World) -> None:
    original = world.onboarding.reader

    async def reader(url: str) -> SiteReadResult:
        world.repository.tables[Business].remove(world.business)
        return await original(url)

    world.onboarding.reader = reader
    with pytest.raises(NotFound):
        await world.onboarding.site_check(world.business.id, NOW, TODAY)
    assert world.llm.calls == []


def test_business_is_not_changed_while_it_is_locked(world: World, monkeypatch) -> None:
    """Блокировку держат проверка сайта (ответ ИИ) и запуск (лимиты кампаний по бюджету): пока они идут — 409, и для
    того же сайта тоже."""
    monkeypatch.setattr("app.services.onboarding.LOCK_WAIT_SECONDS", 0)
    world.repository.locked = True
    with pytest.raises(ChangeConflict):
        world.onboarding.create_business(1, "other.example", "Самара", BUDGET_KOP, None)
    with pytest.raises(ChangeConflict):
        world.onboarding.create_business(1, SITE, "Самара", 2 * BUDGET_KOP, None)
    assert (world.business.site_url, world.business.month_budget_kop) == (SITE, BUDGET_KOP)
    world.repository.locked = False
    same_site = world.onboarding.create_business(1, SITE, "Самара", 2 * BUDGET_KOP, None)
    assert same_site["month_budget_kop"] == 2 * BUDGET_KOP
    world.onboarding.create_business(1, "other.example", "Самара", BUDGET_KOP, None)
    assert world.business.site_url == "https://other.example/"


def test_unchanged_business_is_not_locked(world: World) -> None:
    """Повторное «Далее» без правок ничего не пишет: пока проверка сайта держит блокировку, оно не получает 409."""
    world.repository.locked = True
    view = world.onboarding.create_business(1, "salon.example", " Самара ", BUDGET_KOP, None)
    assert view["id"] == world.business.id and view["month_budget_kop"] == BUDGET_KOP


async def test_same_site_keeps_description_and_other_site_resets_it(world: World) -> None:
    await world.onboarding.site_check(world.business.id, NOW, TODAY)
    description = world.business.description
    assert description["fields"]
    world.onboarding.create_business(1, SITE, "Самара", 2 * BUDGET_KOP, None)
    assert world.business.description == description and world.business.month_budget_kop == 2 * BUDGET_KOP
    world.onboarding.create_business(1, "other.example", "Самара", 2 * BUDGET_KOP, None)
    assert world.business.description == {} and world.business.description_approved_at is None


@pytest.mark.parametrize("site", ["other.example", SITE])
def test_business_is_not_changed_when_launch_finished_before_lock(world: World, monkeypatch, site: str) -> None:
    lock = world.repository.business_lock

    @contextmanager
    def launched_meanwhile(business_id: int):
        world.business.status = "running"
        with lock(business_id):
            yield

    monkeypatch.setattr(world.repository, "business_lock", launched_meanwhile)
    with pytest.raises(Blocked, match="уже запущена"):
        world.onboarding.create_business(1, site, "Самара", 2 * BUDGET_KOP, None)
    assert (world.business.site_url, world.business.month_budget_kop) == (SITE, BUDGET_KOP)


async def test_metrica_is_not_changed_when_launch_finished_before_lock(world: World, monkeypatch) -> None:
    """По целям считаются заявки в статистике и автопилоте: запуск, завершившийся, пока спрашивали Метрику, их уже не
    меняет, а в настройках их не поменять."""
    await world.onboarding.site_check(world.business.id, NOW, TODAY)
    lock = world.repository.business_lock

    @contextmanager
    def launched_meanwhile(business_id: int):
        world.business.status = "running"
        with lock(business_id):
            yield

    monkeypatch.setattr(world.repository, "business_lock", launched_meanwhile)
    with pytest.raises(Blocked, match="Метрики после запуска не меняются"):
        await world.onboarding.save_metrica(world.business.id, COUNTER, [1])
    assert world.business.metrica_counter_id is None and world.business.lead_goal_ids == []


async def test_wizard_steps_are_not_saved_while_business_is_locked(world: World, monkeypatch) -> None:
    """Блокировку держат проверка сайта, запуск и шаг автопилота: пока они идут — 409, и ничего не записано."""
    monkeypatch.setattr("app.services.onboarding.LOCK_WAIT_SECONDS", 0)
    business_id = world.business.id
    await world.onboarding.site_check(business_id, NOW, TODAY)
    fields = world.business.description["fields"]
    world.repository.locked = True
    with pytest.raises(ChangeConflict):
        world.onboarding.save_description(business_id, {"services": {"value": "Педикюр"}}, True, NOW)
    with pytest.raises(ChangeConflict):
        await world.onboarding.save_metrica(business_id, COUNTER, [1])
    with pytest.raises(ChangeConflict):
        world.onboarding.save_marking(business_id, "ip", "526317984689", "ИП Иванов", "89001234567", {"direct": True})
    assert world.business.description["fields"] == fields and world.business.description_approved_at is None
    assert world.business.metrica_counter_id is None and world.repository.marking(business_id) is None


async def test_description_and_marking_wait_for_lock(world: World, monkeypatch) -> None:
    """Блокировку на секунды держат шаг автопилота и перемотка демо: сохранение из настроек ждёт её, а не отвечает
    409."""
    business_id = world.business.id
    await world.onboarding.site_check(business_id, NOW, TODAY)
    lock, attempts = world.repository.business_lock, []

    @contextmanager
    def released_on_second_try(business_id: int):
        attempts.append(business_id)
        if len(attempts) % 2:
            raise ChangeConflict("Этот бизнес уже обрабатывается.")
        with lock(business_id):
            yield

    monkeypatch.setattr(world.repository, "business_lock", released_on_second_try)
    world.onboarding.save_description(business_id, {"services": {"value": "Педикюр"}}, True, NOW)
    world.onboarding.save_marking(business_id, "ip", "526317984689", "ИП Иванов", "89001234567", {"direct": True})
    assert len(attempts) == 4 and world.business.description["fields"]["services"]["value"] == "Педикюр"
    assert world.repository.marking(business_id).name == "ИП Иванов"


def test_business_step_waits_for_lock_like_description(world: World, monkeypatch) -> None:
    """Сведения о бизнесе ждут блокировку, как описание: короткий шаг автопилота или проверки сайта не даёт 409."""
    lock, attempts = world.repository.business_lock, []

    @contextmanager
    def released_on_second_try(business_id: int):
        attempts.append(business_id)
        if len(attempts) == 1:
            raise ChangeConflict("Этот бизнес уже обрабатывается.")
        with lock(business_id):
            yield

    monkeypatch.setattr(world.repository, "business_lock", released_on_second_try)
    world.onboarding.create_business(1, SITE, "Самара", 2 * BUDGET_KOP, None)
    assert len(attempts) == 2 and world.business.month_budget_kop == 2 * BUDGET_KOP


def test_metrica_goals_are_stored_once_and_in_order(world: World) -> None:
    world.onboarding.write_metrica(world.business.id, COUNTER, [3, 1, 3])
    assert world.business.lead_goal_ids == [1, 3]


async def test_metrica_waits_for_lock_like_description(world: World, monkeypatch) -> None:
    """Описание, маркировка и Метрика ждут блокировку одинаково: короткий шаг автопилота не даёт 409."""
    await world.onboarding.site_check(world.business.id, NOW, TODAY)
    lock, attempts = world.repository.business_lock, []

    @contextmanager
    def released_on_second_try(business_id: int):
        attempts.append(business_id)
        if len(attempts) == 1:
            raise ChangeConflict("Этот бизнес уже обрабатывается.")
        with lock(business_id):
            yield

    monkeypatch.setattr(world.repository, "business_lock", released_on_second_try)
    await world.onboarding.save_metrica(world.business.id, COUNTER, [1])
    assert len(attempts) == 2 and world.business.metrica_counter_id == COUNTER


async def test_lock_is_awaited_off_the_event_loop(world: World, monkeypatch) -> None:
    """Ожидание блокировки спит, а запросы к базе синхронные: запись Метрики идёт в пуле потоков, а не в цикле
    событий, который обслуживает всех пользователей."""
    import threading

    await world.onboarding.site_check(world.business.id, NOW, TODAY)
    lock, threads = world.repository.business_lock, []

    @contextmanager
    def watched(business_id: int):
        threads.append(threading.get_ident())
        with lock(business_id):
            yield

    monkeypatch.setattr(world.repository, "business_lock", watched)
    await world.onboarding.save_metrica(world.business.id, COUNTER, [1])
    assert threads and threading.get_ident() not in threads


async def test_one_save_waits_for_lock_per_business_and_others_get_409(world: World, monkeypatch) -> None:
    """Ждёт поток из пула запросов: пока одно сохранение бизнеса ждёт блокировку, остальные сразу получают 409, а не
    занимают пул. Дождавшееся сохраняет, и следующее снова может ждать."""
    import threading

    business_id = world.business.id
    await world.onboarding.site_check(business_id, NOW, TODAY)
    lock, release, waiting = world.repository.business_lock, threading.Event(), threading.Event()

    @contextmanager
    def held_by_step(business_id: int):
        if not release.is_set():
            waiting.set()
            raise ChangeConflict("Этот бизнес уже обрабатывается.")
        with lock(business_id):
            yield

    monkeypatch.setattr(world.repository, "business_lock", held_by_step)
    first = asyncio.create_task(
        asyncio.to_thread(world.onboarding.save_description, business_id, {"services": {"value": "Педикюр"}}, True, NOW)
    )
    await asyncio.to_thread(waiting.wait, 5)
    for save in (
        lambda: world.onboarding.save_description(business_id, {"services": {"value": "Стрижка"}}, True, NOW),
        lambda: world.onboarding.save_marking(
            business_id, "ip", "526317984689", "ИП Иванов", "89001234567", {"direct": True}
        ),
    ):
        with pytest.raises(ChangeConflict, match=SAVING):
            save()
    with pytest.raises(ChangeConflict, match=SAVING):
        await world.onboarding.save_metrica(business_id, COUNTER, [1])
    release.set()
    await asyncio.wait_for(first, 5)
    assert world.business.description["fields"]["services"]["value"] == "Педикюр" and LOCK_WAITERS == set()
    assert world.repository.marking(business_id) is None and world.business.metrica_counter_id is None
    world.onboarding.save_description(business_id, {"services": {"value": "Стрижка"}}, True, NOW)
    assert world.business.description["fields"]["services"]["value"] == "Стрижка"


def test_save_that_did_not_get_lock_frees_its_turn(world: World, monkeypatch) -> None:
    monkeypatch.setattr("app.services.onboarding.LOCK_WAIT_SECONDS", 0)
    world.repository.locked = True
    with pytest.raises(ChangeConflict, match="уже выполняется"):
        world.onboarding.save_description(world.business.id, {"services": {"value": "Педикюр"}}, True, NOW)
    assert LOCK_WAITERS == set()
    world.repository.locked = False
    world.onboarding.save_description(world.business.id, {"services": {"value": "Педикюр"}}, True, NOW)
    assert world.business.description["fields"]["services"]["value"] == "Педикюр"


async def test_description_and_marking_are_changed_after_launch_as_in_settings(world: World) -> None:
    """Настройки сохраняют описание и маркировку теми же шагами: после запуска они не запрещены."""
    business_id = world.business.id
    await onboard(world)
    world.business.status = "running"
    world.onboarding.save_description(business_id, {"services": {"value": "Педикюр"}}, True, NOW)
    world.onboarding.save_marking(business_id, "ooo", "7707083893", "ООО Ромашка", "89001234567", {"direct": True})
    assert world.business.description["fields"]["services"]["value"] == "Педикюр"
    assert world.repository.marking(business_id).name == "ООО Ромашка"


async def test_wizard_steps_of_deleted_business_are_not_found(world: World) -> None:
    business_id = world.business.id
    await world.onboarding.site_check(business_id, NOW, TODAY)
    world.repository.tables[Business].remove(world.business)
    with pytest.raises(NotFound):
        world.onboarding.save_description(business_id, {"services": {"value": "Педикюр"}}, True, NOW)
    with pytest.raises(NotFound):
        await world.onboarding.save_metrica(business_id, COUNTER, [1])
    with pytest.raises(NotFound):
        world.onboarding.save_marking(business_id, "ip", "526317984689", "ИП Иванов", "89001234567", {"direct": True})
    assert world.repository.marking(business_id) is None


async def test_no_counter_means_stop_with_reason(tmp_path: Path) -> None:
    pages = [Page(SITE, "Салон красоты. Маникюр классический — 1 500 ₽. Работаем ежедневно с 09:00 до 21:00. " * 2)]
    world = make_world(tmp_path, pages=pages)
    result = await world.onboarding.site_check(world.business.id, NOW, TODAY)
    assert result["status"] == "blocked" and result["counter_id"] is None
    assert "счётчик" in result["block_reason"]


async def test_unfound_facts_become_questions(tmp_path: Path) -> None:
    empty = json.dumps({"services": [], "address": {"value": "", "quote": ""}, "advantages": []})
    world = make_world(tmp_path, llm=ScriptedLLM(description=empty))
    fields = (await world.onboarding.site_check(world.business.id, NOW, TODAY))["description"]
    assert not fields["services"]["verified"] and not fields["address"]["verified"]
    assert fields["address"]["question"] == "По какому адресу вы принимаете клиентов?"


def test_found_services_stay_verified_when_another_item_was_dropped() -> None:
    draft = DescriptionDraft(
        services=[
            Service("Маникюр с покрытием гель-лаком", 220_000, "Маникюр с покрытием гель-лаком 2 200 ₽", SITE),
            Service("Педикюр", 250_000, "Педикюр 2 500 ₽", SITE),
        ],
        questions=[OwnerQuestion("services.2", "Как называется услуга и сколько она стоит?")],
    )
    fields = description_fields(draft)
    assert fields["services"]["value"] == "Маникюр с покрытием гель-лаком\nПедикюр" and fields["services"]["verified"]
    assert fields["prices"]["value"] == "Маникюр с покрытием гель-лаком — 2 200 ₽\nПедикюр — 2 500 ₽"


def test_owner_services_are_split_by_lines_commas_and_semicolons() -> None:
    value = "Маникюр, педикюр, наращивание ногтей; снятие покрытия\nМаникюр\n\nУход (руки, ноги)"
    assert service_names(value) == ["Маникюр", "педикюр", "наращивание ногтей", "снятие покрытия", "Уход (руки, ноги)"]


def test_owner_services_take_price_from_prices_field() -> None:
    fields = {
        "services": {"value": "Маникюр, педикюр, Маникюр с покрытием, снятие покрытия"},
        "prices": {
            "value": "Маникюр с покрытием — 2 200 ₽\nМаникюр — 1 500 ₽\nПедикюр: 2000 руб.\nСнятие покрытия от 300 ₽"
        },
    }
    business = Business(description={"fields": fields}, medical_level="none")
    draft = description_draft(business)
    assert [(item.name, item.price) for item in draft.services] == [
        ("Маникюр", 150_000),
        ("педикюр", 200_000),
        ("Маникюр с покрытием", 220_000),
        ("снятие покрытия", None),
    ]


async def test_medical_site_gets_warning_level(tmp_path: Path) -> None:
    world = make_world(tmp_path, pages=page_text("Услуги косметолога, ботокс и мезотерапия."))
    result = await world.onboarding.site_check(world.business.id, NOW, TODAY)
    assert result["medical_level"] == "required" and world.business.medical_level == "required"


async def test_llm_failure_degrades_to_owner_questions(tmp_path: Path) -> None:
    class Broken:
        async def complete_json(self, system: str, user: str, temperature: float = 0.2) -> str:
            raise YandexGPTError("down")

    world = make_world(tmp_path)
    world.onboarding.llm = Broken()
    result = await world.onboarding.site_check(world.business.id, NOW, TODAY)
    assert result["status"] == "ready" and not result["description"]["services"]["verified"]


async def test_description_requires_services_and_resets_approval_on_edit(world: World) -> None:
    await world.onboarding.site_check(world.business.id, NOW, TODAY)
    with pytest.raises(OnboardingError):
        world.onboarding.save_description(world.business.id, {"services": {"value": " "}}, True, NOW)
    assert world.business.description_approved_at is None
    kept = {"services": {"value": "Маникюр классический"}, "address": {"value": "Самара, ул. Полевая, д. 7"}}
    world.onboarding.save_description(world.business.id, kept, True, NOW)
    fields = world.business.description["fields"]
    assert world.business.description_approved_at == NOW
    assert fields["services"]["verified"] and fields["services"]["quote"]
    edited = {"services": {"value": "Маникюр и педикюр", "verified": True, "quote": "подделка"}}
    world.onboarding.save_description(world.business.id, edited, False, NOW)
    fields = world.business.description["fields"]
    assert not fields["services"]["verified"] and fields["services"]["quote"] is None
    assert world.business.description_approved_at is None


async def test_owner_text_is_sent_to_ai_even_with_fresh_site_check(world: World) -> None:
    reads = []
    original = world.onboarding.reader

    async def reader(url: str) -> SiteReadResult:
        reads.append(url)
        return await original(url)

    world.onboarding.reader = reader
    await world.onboarding.site_check(world.business.id, NOW, TODAY)
    await world.onboarding.site_check(world.business.id, NOW, TODAY)
    assert world.llm.calls == ["description"] and reads == [SITE]
    await world.onboarding.site_check(world.business.id, NOW, TODAY, owner_text="Маникюр и педикюр")
    assert world.llm.calls == ["description", "description"] and reads == [SITE, SITE]


async def test_owner_text_raises_medical_level(world: World) -> None:
    await world.onboarding.site_check(world.business.id, NOW, TODAY)
    world.onboarding.save_description(world.business.id, {"services": {"value": "Лазерная эпиляция"}}, True, NOW)
    assert world.business.medical_level == "maybe"
    world.onboarding.save_description(world.business.id, {"services": {"value": "Ботокс"}}, True, NOW)
    assert world.business.medical_level == "required"


async def test_metrica_choice_is_checked_against_account_and_site(world: World) -> None:
    await world.onboarding.site_check(world.business.id, NOW, TODAY)
    assert (await world.onboarding.counters(world.business.id))["items"][0]["id"] == COUNTER
    assert (await world.onboarding.goals(world.business.id, COUNTER))["items"] == [{"id": 1, "name": "Заявка"}]
    for counter, goals in ((COUNTER, []), (COUNTER, [99]), (777, [1])):
        with pytest.raises(OnboardingError):
            await world.onboarding.save_metrica(world.business.id, counter, goals)
    assert world.business.metrica_counter_id is None
    await world.onboarding.save_metrica(world.business.id, COUNTER, [1])
    assert world.business.metrica_counter_id == COUNTER and world.business.lead_goal_ids == [1]


async def test_site_counter_must_match_selected_one(world: World) -> None:
    world.business.description = {"check": {"counter_ids": [999]}}
    with pytest.raises(OnboardingError, match="не совпадает"):
        await world.onboarding.save_metrica(world.business.id, COUNTER, [1])


@pytest.mark.parametrize(
    ("subject", "inn", "name", "phone"),
    [
        ("ip", "526317984688", "ИП Иванов", "89001234567"),
        ("ooo", "526317984689", "ООО Ромашка", "89001234567"),
        ("ip", "7707083893", "ИП Иванов", "89001234567"),
        ("ip", "526317984689", "", "89001234567"),
        ("ip", "526317984689", "ИП Иванов", "123"),
        ("company", "526317984689", "ИП Иванов", "89001234567"),
    ],
)
async def test_invalid_marking_is_rejected(world: World, subject: str, inn: str, name: str, phone: str) -> None:
    with pytest.raises(OnboardingError):
        world.onboarding.save_marking(world.business.id, subject, inn, name, phone, {})
    assert world.repository.marking(world.business.id) is None


async def test_marking_is_saved_with_strict_confirmations(world: World) -> None:
    world.onboarding.save_marking(
        world.business.id, "ooo", "7707083893", "ООО Ромашка", "+7 900 123-45-67", {"direct": True, "vk": "yes"}
    )
    profile = world.repository.marking(world.business.id)
    assert profile.inn_enc == "7707083893" and profile.phone_enc == "+79001234567"
    assert profile.confirmed_in_platform == {"direct": True, "vk": False}


async def test_ai_spend_limit_stops_paid_calls(tmp_path: Path) -> None:
    world = make_world(tmp_path, llm=ScriptedLLM(cost_kop=4000), limits=AiLimits(6000, 2500))
    await world.onboarding.site_check(world.business.id, NOW, TODAY)
    assert world.repository.ai_spent(world.business.id) == 4000
    budget = ai_budget(world.repository, world.business, LIMITS, TODAY)
    guard = SpendLLM(world.llm, world.repository, world.business.id, budget, "test")
    await guard.complete_json("Напиши объявление", "{}")
    assert world.repository.ai_spent(world.business.id) == 8000
    with pytest.raises(YandexGPTError):
        await guard.complete_json("Напиши объявление", "{}")
    assert world.llm.calls.count("ad") == 1
    assert [call.cost_kop for call in world.repository.rows(LLMCall)] == [4000, 4000]


def spend(world: World, cost_kop: int, created_at: datetime) -> None:
    world.repository.add(
        LLMCall(
            business_id=world.business.id,
            purpose="x",
            model="m",
            tokens_in=1,
            tokens_out=1,
            cost_kop=cost_kop,
            status="DONE",
            created_at=created_at,
        )
    )


@pytest.mark.parametrize(
    "launched,expected",
    [
        (None, 300 + 400),
        (datetime(2026, 1, 6, 9).isoformat(), 400),
        (datetime(2026, 1, 6, 9, tzinfo=UTC).isoformat(), 400),
        (datetime(2025, 12, 30, 9, tzinfo=UTC).isoformat(), 300 + 400),
    ],
    ids=["running-without-launched-plan", "naive-this-week", "this-week", "previous-week"],
)
def test_weekly_ai_window_starts_at_monday_or_later_launch(world: World, launched: str | None, expected: int) -> None:
    if launched:
        world.repository.add(
            Plan(
                business_id=world.business.id,
                version=1,
                status="launched",
                payload={"launch": {"launched_at": launched}},
            )
        )
    world.business.status = "running"
    spend(world, 5000, datetime(2026, 1, 4, 20, 59, tzinfo=UTC))
    spend(world, 300, datetime(2026, 1, 4, 21, 0, tzinfo=UTC))
    spend(world, 400, datetime(2026, 1, 6, 9, tzinfo=UTC))
    assert ai_budget(world.repository, world.business, LIMITS, date(2026, 1, 11)).spent_kop == expected


def test_weekly_ai_limit_counts_calls_after_launch_in_current_moscow_week(world: World) -> None:
    launched = NOW + timedelta(hours=1)
    world.repository.add(
        Plan(
            business_id=world.business.id,
            version=1,
            status="launched",
            payload={"launch": {"launched_at": launched.isoformat()}},
        )
    )
    spend(world, 5000, NOW)
    world.business.status = "running"
    assert ai_budget(world.repository, world.business, LIMITS, TODAY).spent_kop == 0
    spend(world, 700, launched + timedelta(minutes=5))
    budget = ai_budget(world.repository, world.business, LIMITS, TODAY)
    assert (budget.limit_kop, budget.spent_kop) == (2500, 700)
    assert "_ai" not in world.business.settings
    # Неделя начинается в понедельник 00:00 по Москве, то есть в воскресенье 21:00 UTC.
    monday = date(2026, 1, 5)
    spend(world, 300, datetime(2026, 1, 4, 20, 59, tzinfo=UTC))
    spend(world, 400, datetime(2026, 1, 4, 21, 0, tzinfo=UTC))
    assert ai_budget(world.repository, world.business, LIMITS, monday).spent_kop == 400
    assert ai_budget(world.repository, world.business, LIMITS, monday + timedelta(days=7)).spent_kop == 0


async def test_demo_ai_is_free_and_never_counts_toward_weekly_limit(world: World) -> None:
    from app.services.registry import DemoArt, DemoLLM

    world.business.status = "running"
    budget = ai_budget(world.repository, world.business, LIMITS, TODAY)
    llm = SpendLLM(DemoLLM(), world.repository, world.business.id, budget, "ad_text")
    await llm.complete_json("Напиши объявление", json.dumps({"facts": ["Маникюр"]}))
    await DemoArt(budget).generate("Уютный салон", seed=1, business_id=world.business.id)
    assert not world.repository.rows(LLMCall) and not world.repository.rows(ArtCall)
    assert ai_budget(world.repository, world.business, LIMITS, TODAY).spent_kop == 0


def test_ai_mode_names_the_model_only_with_keys_in_live_mode() -> None:
    from types import SimpleNamespace

    from pydantic import SecretStr

    from app.services.registry import llm_mode

    def config(mode: str, key: str) -> SimpleNamespace:
        return SimpleNamespace(
            platform_mode=mode, yc_api_key=SecretStr(key), yc_folder_id="folder", yandexgpt_model="yandexgpt/latest"
        )

    assert llm_mode(config("live", "")) == "demo"
    assert llm_mode(config("fake", "key")) == "demo"
    assert llm_mode(config("live", "key")) == "yandexgpt/latest"
