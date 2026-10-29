"""Прогноз обращений до запуска: клики и цена клика — из прогноза бюджета Директа, конверсия — из Метрики бизнеса
или общий ориентир с подписью. Нейросеть только пересказывает готовые числа; новых чисел в её тексте быть не должно.

Отчёт Директа готовится до минуты: первый запрос заказывает его и ждёт несколько секунд, следующие спрашивают
готовность. Результат хранится в памяти процесса API (он один) по входу: фразы, регион, бюджет, цели Метрики."""

import asyncio
import json
import math
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal
from hashlib import sha256
from typing import Any

from app.content.llm import LLMClient
from app.content.yandexgpt import YandexGPTError
from app.db.models import Business
from app.domain.budget import allocate, platform_shares
from app.domain.settings import DomainSettings
from app.platforms.base import AuthExpired, PlatformError
from app.platforms.direct_forecast import DirectForecastClient, ForecastAccessDenied, PhraseForecast
from app.platforms.metrica import MAX_GOALS, MetricaError
from app.services.onboarding import AiLimits, SpendLLM, ai_budget
from app.services.plan import PlanRepository

FORECAST_DOC = "https://yandex.ru/dev/direct/doc/dg-v4/ru/reference/CreateNewForecast.md"
BENCHMARK_URL = "https://etodigital.ru/zametki/konversiya-sajta-uslug-norma/"
# Меньше сотни визитов с рекламы — доля обращений слишком шаткая даже как интервал.
MIN_VISITS = 100
TTL = {"ready": timedelta(hours=24), "pending": timedelta(minutes=10), "unavailable": timedelta(minutes=10)}
ACCESS_TTL = timedelta(hours=1)
EXPLANATION_LIMIT = 800
AI_NOTE = "Пояснение написал ИИ по готовым числам — это прогноз, он может ошибаться."
VK_NOTE = "Для ВКонтакте прогноз появится после первых дней работы: до запуска площадка его не даёт."
NUMBER = re.compile(r"\d+(?:[   ]\d{3})*(?:[.,]\d+)?")
SYSTEM = (
    "Перескажи прогноз рекламы для владельца небольшого бизнеса простыми словами, 3–5 предложений. "
    "Используй только числа из входа, не добавляй новых, не округляй и не пересчитывай. "
    'Скажи, что это прогноз и он может ошибаться. Ответь JSON: {"text": "..."}.'
)


@dataclass(frozen=True)
class Conversion:
    min: Decimal
    max: Decimal
    source: str
    label: str
    url: str | None = None


BENCHMARK = Conversion(
    Decimal("0.01"), Decimal("0.03"), "benchmark", "ориентир по рынку услуг, не измерение", BENCHMARK_URL
)


@dataclass(frozen=True)
class Clicks:
    min: int
    max: int
    potential: int
    cpc_min: Decimal | None
    cpc_max: Decimal | None


@dataclass
class Entry:
    key: str
    status: str
    expires: datetime
    forecast_id: int | None = None
    response: dict[str, Any] = field(default_factory=dict)


FORECASTS: dict[int, Entry] = {}
LOCKS: dict[int, asyncio.Lock] = {}


def clear() -> None:
    FORECASTS.clear()
    LOCKS.clear()


def weighted(pairs: list[tuple[int, Decimal]]) -> Decimal | None:
    total = sum(clicks for clicks, _ in pairs)
    return sum((clicks * price for clicks, price in pairs), Decimal(0)) / total if total else None


def direct_clicks(rows: list[PhraseForecast], budget_rub: Decimal) -> Clicks:
    """Клики за месяц при бюджете — вилка из двух сценариев нижнего блока, куда попадает автостратегия на малом
    бюджете: «вход» (Clicks по цене Min) и «первое место» (FirstPlaceClicks по цене Max). В каждом сценарии
    кликов не больше потенциала фраз и не больше, чем бюджет оплатит по средневзвешенной цене этого сценария.
    Спецразмещение (Premium*) не берём: оно дороже, и на малом бюджете на него не рассчитываем."""
    entry = (sum(row.clicks for row in rows), weighted([(row.clicks, row.min_cpc) for row in rows]))
    first = (
        sum(row.first_place_clicks for row in rows),
        weighted([(row.first_place_clicks, row.max_cpc) for row in rows]),
    )
    scenarios = [
        min(potential, int(budget_rub / price) if price else potential)
        for potential, price in (entry, first)
        if potential
    ] or [0]
    return Clicks(min(scenarios), max(scenarios), max(entry[0], first[0]), entry[1], first[1])


def wilson(successes: int, trials: int, z: float = 1.96) -> tuple[Decimal, Decimal]:
    """95% интервал Вильсона для доли: на малом числе обращений честнее точки."""
    rate = successes / trials
    center = rate + z * z / (2 * trials)
    spread = z * math.sqrt(rate * (1 - rate) / trials + z * z / (4 * trials * trials))
    scale = 1 + z * z / trials
    return (
        Decimal(max(0.0, (center - spread) / scale)).quantize(Decimal("0.0001")),
        Decimal(min(1.0, (center + spread) / scale)).quantize(Decimal("0.0001")),
    )


def measured(visits: int, reached: int) -> Conversion | None:
    if visits < MIN_VISITS:
        return None
    low, high = wilson(reached, visits)
    label = f"измерено на вашем сайте: {reached} обращений на {visits} визитов с рекламы за 90 дней (Метрика)"
    return Conversion(low, high, "metrica", label)


def leads(clicks: Clicks, conversion: Conversion) -> tuple[int, int]:
    return math.floor(clicks.min * conversion.min), math.ceil(clicks.max * conversion.max)


def rub(value: Decimal | int) -> str:
    text = f"{Decimal(value).quantize(Decimal(1), ROUND_HALF_UP):,}"
    return text.replace(",", " ")


def percent(value: Decimal) -> str:
    return f"{(value * 100).quantize(Decimal('0.1'), ROUND_HALF_UP).normalize():f}".replace(".", ",")


def numbers(text: str) -> set[Decimal]:
    return {Decimal(re.sub(r"\s", "", item).replace(",", ".")) for item in NUMBER.findall(text)}


def template(clicks: Clicks, budget_rub: Decimal, conversion: Conversion, leads_range: tuple[int, int]) -> str:
    if not clicks.potential:
        return (
            "Яндекс Директ не видит спроса по фразам плана в вашем регионе за прошлый месяц: прогноз обращений "
            "посчитать не из чего. Это прогноз, он может ошибаться."
        )
    price = (
        f"от {rub(clicks.cpc_min)} до {rub(clicks.cpc_max)} ₽"
        if clicks.cpc_min is not None and clicks.cpc_max is not None
        else f"около {rub(clicks.cpc_min or clicks.cpc_max or 0)} ₽"
    )
    if conversion.source == "metrica":
        rate = (
            f"На вашем сайте обращаются от {percent(conversion.min)} до {percent(conversion.max)}% посетителей "
            f"с рекламы — {conversion.label}."
        )
    else:
        rate = (
            f"Обычно заявку оставляют от {percent(conversion.min)} до {percent(conversion.max)} из 100 посетителей "
            f"сайта — это {conversion.label}, не по вашему сайту."
        )
    return (
        f"Яндекс Директ оценивает спрос по фразам плана в вашем регионе: за прошлый месяц было бы до "
        f"{rub(clicks.potential)} кликов. При бюджете {rub(budget_rub)} ₽ в месяц и цене клика {price} выйдет "
        f"примерно от {rub(clicks.min)} до {rub(clicks.max)} кликов. {rate} Итого примерно от {leads_range[0]} до "
        f"{leads_range[1]} обращений в месяц. Это прогноз, он может ошибаться: вся сумма не обязательно потратится, "
        "а после первой недели покажем цифры по вашему сайту."
    )


def ai_text(response: str, allowed: set[Decimal]) -> str | None:
    """Текст нейросети годится, только если в нём нет чисел, которых не было во входе."""
    try:
        text = json.loads(response)["text"]
    except (ValueError, KeyError, TypeError):
        return None
    if not isinstance(text, str) or not text.strip() or len(text) > EXPLANATION_LIMIT:
        return None
    if not numbers(text) <= allowed:
        return None
    return text.strip()


def demo_rows(phrases: list[str]) -> list[PhraseForecast]:
    """Правдоподобный отчёт для демо: стабильный для одной фразы, без запросов к Яндексу."""
    rows = []
    for phrase in phrases:
        seed = int(sha256(phrase.encode()).hexdigest()[:8], 16)
        clicks = 40 + seed % 60
        rows.append(
            PhraseForecast(
                phrase, clicks * 25, clicks, clicks * 3 // 2, Decimal(25 + seed % 15), Decimal(45 + seed % 25)
            )
        )
    return rows


def unavailable(message: str, vk: dict | None) -> dict:
    return status("unavailable", message, vk)


def status(name: str, message: str, vk: dict | None) -> dict:
    return {
        "status": name,
        "leads_min": None,
        "leads_max": None,
        "clicks_min": None,
        "clicks_max": None,
        "cpc_rub": None,
        "cpc_rub_max": None,
        "budget_rub": None,
        "conversion": None,
        "explanation": message,
        "explanation_by_ai": False,
        "sources": [],
        "computed_at": None,
        "demo": False,
        "vk": vk,
    }


class ForecastService:
    poll_delay = 2.0

    def __init__(
        self,
        repository: PlanRepository,
        registry: Any,
        llm: LLMClient | None,
        limits: AiLimits,
        domain: DomainSettings,
    ) -> None:
        self.repository = repository
        self.registry = registry
        self.llm = llm
        self.limits = limits
        self.domain = domain

    @property
    def demo(self) -> bool:
        return self.registry.mode == "fake"

    async def forecast(self, business: Business, now: datetime, today: date) -> dict:
        plan = self.repository.latest_plan(business.id)
        if plan is None:
            return unavailable("Прогноз появится, когда будет готов план.", None)
        payload = plan.payload
        platforms = payload["platforms"]
        vk = {"status": "unavailable", "label": VK_NOTE} if "vk" in platforms else None
        phrases = list(
            dict.fromkeys(
                phrase for group in payload["groups"] if group["platform"] == "direct" for phrase in group["phrases"]
            )
        )
        if "direct" not in platforms or not phrases:
            return unavailable("Для Яндекс Директа в плане нет ключевых фраз — прогнозировать нечего.", vk)
        shares = allocate(payload["month_budget_kop"], platform_shares(platforms, self.domain))
        budget_rub = Decimal(shares["direct"]) * (1 - self.domain.budget_reserve) / 100
        city = payload.get("city", business.city)
        goals = list(business.lead_goal_ids or [])[:MAX_GOALS]
        inputs = [phrases, city, str(budget_rub), business.metrica_counter_id, goals, self.demo]
        key = sha256(json.dumps(inputs, ensure_ascii=False).encode()).hexdigest()
        # Площадки и ИИ отвечают секундами: соединение с базой на это время возвращается в пул.
        self.repository.commit()
        lock = LOCKS.setdefault(business.id, asyncio.Lock())
        async with lock:
            entry = FORECASTS.get(business.id)
            if entry is None or entry.key != key or entry.expires <= now:
                entry = None
            elif entry.status != "pending":
                return entry.response | {"vk": vk}
            entry = await self.refresh(business, entry, key, phrases, city, budget_rub, now, today)
            FORECASTS[business.id] = entry
            return entry.response | {"vk": vk}

    async def refresh(
        self,
        business: Business,
        entry: Entry | None,
        key: str,
        phrases: list[str],
        city: str,
        budget_rub: Decimal,
        now: datetime,
        today: date,
    ) -> Entry:
        if self.demo:
            return await self.ready(business, key, demo_rows(phrases), budget_rub, now, today)
        try:
            client = self.client(business.id)
            if entry is None:
                region = await self.registry.resolve(business.id, "direct").geo_targeting(city)
                forecast_id = await client.create(phrases, region["region_ids"])
                state = await client.wait(forecast_id)
            else:
                forecast_id = entry.forecast_id
                state = await client.status(forecast_id)
            if state == "Pending":
                message = "Яндекс Директ готовит прогноз, обычно это занимает до минуты."
                return Entry(
                    key,
                    "pending",
                    entry.expires if entry else now + TTL["pending"],
                    forecast_id,
                    status("pending", message, None),
                )
            rows = await client.get(forecast_id) if state == "Done" else None
            # Отчёт больше не нужен: у пользователя Директа всего пять мест под отчёты.
            try:
                await client.delete(forecast_id)
            except PlatformError:
                pass
            if rows is None:
                return self.failed(key, "Яндекс Директ не смог посчитать прогноз. Попробуем ещё раз позже.", now)
        except ForecastAccessDenied as error:
            return Entry(key, "unavailable", now + ACCESS_TTL, response=unavailable(str(error), None))
        except AuthExpired:
            return self.failed(key, "Войдите в Яндекс Директ заново: без этого прогноз не получить.", now)
        except PlatformError as error:
            return self.failed(key, f"Прогноз Яндекс Директа сейчас недоступен: {error}", now)
        return await self.ready(business, key, rows, budget_rub, now, today)

    def client(self, business_id: int) -> DirectForecastClient:
        info = self.registry.load(business_id, "direct")
        if info is None or info.status != "ok":
            raise PlatformError("подключите кабинет Яндекс Директа.")
        return DirectForecastClient(
            info.token, config=self.registry.config, client=self.registry.http, delay=self.poll_delay
        )

    @staticmethod
    def failed(key: str, message: str, now: datetime) -> Entry:
        return Entry(key, "unavailable", now + TTL["unavailable"], response=unavailable(message, None))

    async def conversion(self, business: Business) -> Conversion:
        if not business.metrica_counter_id or not business.lead_goal_ids:
            return BENCHMARK
        try:
            metrica = self.registry.metrica(business.id)
            visits, reached = await metrica.get_ad_conversions(
                business.metrica_counter_id, list(business.lead_goal_ids)[:MAX_GOALS]
            )
        except (MetricaError, PlatformError):
            return BENCHMARK
        return measured(visits, reached) or BENCHMARK

    async def ready(
        self, business: Business, key: str, rows: list[PhraseForecast], budget_rub: Decimal, now: datetime, today: date
    ) -> Entry:
        clicks = direct_clicks(rows, budget_rub)
        conversion = await self.conversion(business)
        leads_range = leads(clicks, conversion)
        text = template(clicks, budget_rub, conversion, leads_range)
        explanation = await self.explain(business, text, today) if clicks.potential else None
        forecast = (
            "Демо: числа придуманы для показа, это не прогноз Яндекса"
            if self.demo
            else "Яндекс Директ, прогноз бюджета (API v4 Live): спрос по фразам плана в вашем регионе за прошлый месяц"
        )
        sources = [
            {"what": "Клики и цена клика", "source": forecast, "url": None if self.demo else FORECAST_DOC},
            {
                "what": "Бюджет",
                "source": f"Ваш план: {rub(budget_rub)} ₽ в месяц на Яндекс Директ, без запаса на перерасход",
                "url": None,
            },
            {"what": "Доля обращений", "source": conversion.label, "url": conversion.url},
        ]
        response = {
            "status": "ready",
            "leads_min": leads_range[0],
            "leads_max": leads_range[1],
            "clicks_min": clicks.min,
            "clicks_max": clicks.max,
            "cpc_rub": float(clicks.cpc_min.quantize(Decimal("0.01"))) if clicks.cpc_min is not None else None,
            "cpc_rub_max": float(clicks.cpc_max.quantize(Decimal("0.01"))) if clicks.cpc_max is not None else None,
            "budget_rub": float(budget_rub.quantize(Decimal("0.01"))),
            "conversion": {
                "min": float(conversion.min),
                "max": float(conversion.max),
                "source": conversion.source,
                "label": conversion.label,
            },
            "explanation": f"{explanation} {AI_NOTE}" if explanation else text,
            "explanation_by_ai": explanation is not None,
            "sources": sources,
            "computed_at": now.isoformat(),
            "demo": self.demo,
        }
        return Entry(key, "ready", now + TTL["ready"], response=response)

    async def explain(self, business: Business, text: str, today: date) -> str | None:
        """Пересказ нейросетью по готовому тексту; любые сбои и новые числа — остаётся шаблон."""
        if self.llm is None:
            return None
        budget = ai_budget(self.repository, business, self.limits, today)
        llm = SpendLLM(self.llm, self.repository, business.id, budget, "forecast")
        try:
            response = await llm.complete_json(SYSTEM, json.dumps({"forecast": text}, ensure_ascii=False))
        except YandexGPTError:
            return None
        return ai_text(response, numbers(text))
