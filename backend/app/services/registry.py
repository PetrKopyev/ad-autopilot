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

from app.content.art import ArtBudget, ArtResult, YandexArt, validate_request
from app.content.llm import LLMClient
from app.content.yandexgpt import TokenTariff, YandexGPTClient
from app.db.models import Business, MarkingProfile, PlatformAccount
from app.platforms.base import AdPlatform, PlatformError
from app.platforms.direct import DirectAdapter
from app.platforms.fake import FakeAdapter
from app.platforms.metrica import FakeMetrica, Metrica, MetricaClient, MetricaCounter, MetricaGoal
from app.platforms.vk import VkAdapter
from app.services.plan import ArtFactory

# YandexGPT Pro 5.1: 80 копеек за 1000 токенов на вход и на выход (docs/internal/research-platforms.md).
TOKEN_TARIFF = TokenTariff(80, 80)
# Идентификаторы регионов Яндекса (Директ). Для города вне списка запуск в Директе не выполняется.
CITY_REGIONS = {
    "москва": 213,
    "санкт-петербург": 2,
    "новосибирск": 65,
    "екатеринбург": 54,
    "нижний новгород": 47,
    "казань": 43,
    "челябинск": 56,
    "омск": 66,
    "самара": 51,
    "ростов-на-дону": 39,
    "уфа": 172,
    "красноярск": 62,
    "воронеж": 193,
    "пермь": 50,
    "волгоград": 38,
    "краснодар": 35,
}


@dataclass(frozen=True)
class AccountInfo:
    status: str
    city: str
    href: str
    counter_id: int | None
    marking_confirmed: bool
    token: str = field(repr=False)
    login: str | None = None


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
                business.city,
                business.site_url,
                business.metrica_counter_id,
                bool(marking and marking.confirmed_in_platform.get(platform) is True),
                account.access_token_enc,
                account.external_login,
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
        region = CITY_REGIONS.get(info.city.strip().lower())
        if region is None:
            raise PlatformError(f"Для города «{info.city}» пока не настроен регион Яндекс Директа.")
        return DirectAdapter(
            info.token,
            config=self.config,
            client=self.http,
            counter_ids=(info.counter_id,) if info.counter_id else (),
            href=info.href,
            region_ids=(region,),
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


class DemoLLM:
    """Ответы для демо без ключей YandexGPT: хватает на описание, идеи картинок и тексты по шаблону."""

    async def complete_json(self, system: str, user: str) -> str:
        if "визуальные идеи" in system:
            return json.dumps({"ideas": [f"Уютная сцена: {segment}" for segment in json.loads(user)["segments"]]})
        if "Напиши объявление" in system:
            data = json.loads(user)
            title = next(iter(data.get("facts", [])), "Услуги и запись")
            text = "Подробности и запись." if data.get("angle") == "цена" else "Выберите время для записи."
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
        return ArtResult(output.getvalue(), seed, f"demo-{seed}")


def build_llm(config: Any, http: httpx.AsyncClient) -> LLMClient:
    if config.platform_mode == "fake" or not config.yc_api_key.get_secret_value() or not config.yc_folder_id:
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
        )

    return factory
