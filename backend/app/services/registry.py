import json
from collections.abc import Callable
from dataclasses import dataclass, field
from io import BytesIO
from random import Random
from typing import Any

import httpx
from PIL import Image, ImageDraw
from sqlalchemy import select
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool

from app.content.art import ArtBudget, ArtResult, YandexArt, validate_request
from app.content.llm import LLMClient
from app.content.yandexgpt import TokenTariff, YandexGPTClient
from app.db.models import Business, MarkingProfile, PlatformAccount
from app.platforms.base import AdPlatform, PlatformError
from app.platforms.direct import MAX_GOALS as DIRECT_MAX_GOALS
from app.platforms.direct import DirectAdapter
from app.platforms.fake import FakeAdapter
from app.platforms.metrica import FakeMetrica, Metrica, MetricaClient, MetricaCounter, MetricaGoal
from app.platforms.vk import VkAdapter
from app.services.plan import ArtFactory

# YandexGPT Pro 5.1: 80 копеек за 1000 токенов на вход и на выход (docs/internal/research-platforms.md).
TOKEN_TARIFF = TokenTariff(80, 80)


@dataclass(frozen=True)
class AccountInfo:
    status: str
    href: str
    counter_id: int | None
    marking_confirmed: bool
    token: str = field(repr=False)
    goal_ids: tuple[int, ...] = ()
    goal_value_kop: int | None = None


type Loader = Callable[[int, str], AccountInfo | None]


def db_loader(session_factory: Callable[[], Session]) -> Loader:
    def load(business_id: int, platform: str) -> AccountInfo | None:
        with session_factory() as db:
            account = db.scalar(
                select(PlatformAccount).where(
                    PlatformAccount.business_id == business_id, PlatformAccount.platform == platform
                )
            )
            business = db.get(Business, business_id)
            if account is None or business is None:
                return None
            marking = db.get(MarkingProfile, business_id)
            return AccountInfo(
                account.status,
                business.site_url,
                business.metrica_counter_id,
                bool(marking and marking.confirmed_in_platform.get(platform) is True),
                account.access_token_enc,
                tuple(business.lead_goal_ids[:DIRECT_MAX_GOALS]),
                business.avg_check_kop,
            )

    return load


class AdapterRegistry:
    """Адаптеры по (business_id, platform): собираются по platform_accounts и пересобираются при смене данных."""

    def __init__(self, load: Loader, *, mode: str, http: httpx.AsyncClient, config: Any = None) -> None:
        self.load = load
        self.mode = mode
        self.http = http
        if config is None:
            from app.config import settings

            config = settings
        self.config = config
        self.cache: dict[tuple[int, str], tuple[AccountInfo | None, AdPlatform]] = {}
        self.fakes: dict[tuple[int, str], FakeAdapter] = {}

    def resolve(self, business_id: int, platform: str) -> AdPlatform:
        key = (business_id, platform)
        if self.mode == "fake":
            return self.fakes.setdefault(key, FakeAdapter())
        info = self.load(business_id, platform)
        if info is None or info.status != "ok":
            raise PlatformError("Кабинет площадки не подключён или требует входа заново.")
        cached = self.cache.get(key)
        if cached is None or cached[0] != info:
            cached = (info, self.build(platform, info))
            self.cache[key] = cached
        return cached[1]

    def get(self, key: tuple[int, str], default: AdPlatform | None = None) -> AdPlatform | None:
        try:
            return self.resolve(*key)
        except PlatformError:
            return default

    def build(self, platform: str, info: AccountInfo) -> AdPlatform:
        if platform == "vk":
            return VkAdapter(
                info.token,
                href=info.href,
                package_id=getattr(self.config, "vk_package_id", None),
                marking_confirmed=info.marking_confirmed,
                config=self.config,
                client=self.http,
            )
        return DirectAdapter(
            info.token,
            config=self.config,
            client=self.http,
            counter_ids=(info.counter_id,) if info.counter_id else (),
            goal_ids=info.goal_ids,
            # Ценность ключевых целей — средний чек, если владелец его указал. В кампанию они уходят только при
            # стратегии по конверсиям (DirectAdapter._campaign_settings).
            priority_goals=dict.fromkeys(info.goal_ids, info.goal_value_kop) if info.goal_value_kop else None,
            href=info.href,
            marking_confirmed=info.marking_confirmed,
        )

    def metrica(self, business_id: int) -> Metrica:
        if self.mode == "fake":
            return FakeMetrica(
                [MetricaCounter(12345, "Демо-счётчик", "example.ru")], {12345: [MetricaGoal(1, "Заявка", "action")]}
            )
        info = self.load(business_id, "direct")
        if info is None or info.status != "ok":
            raise PlatformError("Сначала подключите Яндекс Директ: счётчики берутся из вашей Метрики.")
        return MetricaClient(info.token, client=self.http)


# Сцены демо-картинок: у каждой картинки своя подпись, а не «Маникюр: к празднику» под всеми.
DEMO_SCENES = (
    "Нежный нюд на светлом мраморе",
    "Яркий коралловый акцент",
    "Мягкий утренний свет у окна",
    "Глянец с золотыми искрами",
    "Пастельные оттенки на льне",
    "Глубокий винный оттенок",
    "Минимализм на белом фоне",
    "Сочный летний цвет",
    "Матовое покрытие крупным планом",
    "Тёплые осенние тона",
    "Холодный серебристый блеск",
    "Графичный узор на сером фоне",
)


class DemoLLM:
    """Ответы для демо без ключей YandexGPT: хватает на описание, идеи картинок и тексты по шаблону."""

    def __init__(self) -> None:
        self.scenes = 0

    async def complete_json(self, system: str, user: str, temperature: float = 0.2) -> str:
        if "визуальные идеи" in system:
            ideas = []
            for item in json.loads(user)["items"]:
                scene = DEMO_SCENES[self.scenes % len(DEMO_SCENES)]
                self.scenes += 1
                ideas.append({"title": scene, "visual": f"{scene.lower()}, {item['segment']}"})
            return json.dumps({"ideas": ideas}, ensure_ascii=False)
        if "Напиши объявление" in system:
            data = json.loads(user)
            facts = data.get("facts", [])
            # Два объявления группы — разные заходы: через цену с названием услуги и через услугу с поводом.
            if data.get("angle", "цена") == "цена":
                title = next(iter(facts), "Услуги и запись")
                text = next((fact for fact in facts if "₽" in fact), "Подробности и запись.")
            else:
                title = data.get("title_example") or "Услуги и запись"
                text = "Выберите время для записи."
            location = data.get("location")
            text = f"{location[0]}. {text}" if location else text
            return json.dumps({"title": title, "title2": "", "text": text}, ensure_ascii=False)
        from app.services.demo import DEMO_DESCRIPTION

        return json.dumps(DEMO_DESCRIPTION, ensure_ascii=False)


class DemoArt:
    """Картинки для демо: различимые между собой, тот же бюджет трат, что и у YandexART."""

    def __init__(self, budget: ArtBudget) -> None:
        self.budget = budget

    async def generate(self, prompt: str, *, seed: int, business_id: int) -> ArtResult:
        validate_request(prompt, seed)
        await self.budget.reserve()
        return ArtResult(await run_in_threadpool(self.draw, seed), seed, f"demo-{seed}")

    @staticmethod
    def draw(seed: int) -> bytes:
        rng = Random(seed)
        image = Image.new("RGB", (768, 768), tuple(rng.randrange(90, 230) for _ in range(3)))
        draw = ImageDraw.Draw(image)
        for _ in range(8):
            x, y = rng.randrange(0, 600), rng.randrange(0, 600)
            draw.ellipse(
                (x, y, x + rng.randrange(60, 300), y + rng.randrange(60, 300)),
                fill=tuple(rng.randrange(256) for _ in range(3)),
            )
        output = BytesIO()
        image.save(output, format="JPEG")
        return output.getvalue()


def llm_mode(config: Any) -> str:
    """Чем отвечает ИИ: заглушкой или моделью YandexGPT. При смене режима проверка сайта перечитывает его заново."""
    if config.platform_mode == "fake" or not config.yc_api_key.get_secret_value() or not config.yc_folder_id:
        return "demo"
    return config.yandexgpt_model


def build_llm(config: Any, http: httpx.AsyncClient) -> LLMClient:
    if llm_mode(config) == "demo":
        return DemoLLM()
    return YandexGPTClient(
        http,
        api_key=config.yc_api_key.get_secret_value(),
        folder_id=config.yc_folder_id,
        model=config.yandexgpt_model,
        tariff=TOKEN_TARIFF,
    )


def build_art_factory(config: Any, http: httpx.AsyncClient) -> ArtFactory:
    def factory(budget: ArtBudget, record_call: Any) -> Any:
        if config.platform_mode == "fake" or not config.yc_api_key.get_secret_value() or not config.yc_folder_id:
            return DemoArt(budget)
        return YandexArt(
            http,
            api_key=config.yc_api_key.get_secret_value(),
            folder_id=config.yc_folder_id,
            budget=budget,
            record_call=record_call,
            model=config.yandexart_model,
        )

    return factory
