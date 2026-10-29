"""Прогноз обращений до запуска: клики и цена клика — из прогноза бюджета Директа, конверсия — из Метрики бизнеса
или общий ориентир с подписью. Нейросеть только пересказывает готовые числа; новых чисел в её тексте быть не должно.

Отчёт Директа готовится до минуты: первый запрос заказывает его и сразу отвечает «готовится», следующие спрашивают
готовность. Результат хранится в памяти процесса API (он один) по входу: фразы, регион, бюджет, цели Метрики и
подключение Директа — после переподключения кабинета прогноз спрашивается заново."""

import asyncio
import json
import logging
import math
import re
from dataclasses import dataclass, field, replace
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

logger = logging.getLogger(__name__)

FORECAST_DOC = "https://yandex.ru/dev/direct/doc/dg-v4/ru/reference/CreateNewForecast.md"
BENCHMARK_URL = "https://etodigital.ru/zametki/konversiya-sajta-uslug-norma/"
# Меньше сотни визитов с рекламы — доля обращений слишком шаткая даже как интервал.
MIN_VISITS = 100
# Каждый вызов Директа внутри запроса прогноза — не дольше этого: замок бизнеса не держится минутами.
DIRECT_TIMEOUT = 10.0
TTL = {"ready": timedelta(hours=24), "pending": timedelta(minutes=10), "unavailable": timedelta(minutes=10)}
ACCESS_TTL = timedelta(hours=1)
EXPLANATION_LIMIT = 800
AI_NOTE = "Пояснение написал ИИ по готовым числам — это прогноз, он может ошибаться."
VK_NOTE = "Для ВКонтакте прогноз появится после первых дней работы: до запуска площадка его не даёт."
NUMBER = re.compile(r"\d+(?:[   ]\d{3})*(?:[.,]\d+)?")
LEADS_WORD = re.compile(r"обращени", re.IGNORECASE)
# Число в стольких знаках от слова «обращения» нейросеть выдаёт за число обращений.
LEADS_NEAR = 12
PENDING = "Яндекс Директ готовит прогноз, обычно это занимает до минуты."
VISITS_NOTE = (
    "Не каждый, кто нажал на объявление, доходит до сайта: часть людей уходит, пока страница загружается. "
    "Поэтому обращений может выйти меньше."
)
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
    # Почему вместо измерения ориентир: Метрика без целей, мало визитов или ошибка ответа.
    note: str | None = None


BENCHMARK = Conversion(Decimal("0.01"), Decimal("0.03"), "benchmark", "средняя цифра для сайтов услуг", BENCHMARK_URL)


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
    # Подключение, которым заказан отчёт: им же он и удаляется, даже если кабинет уже подключён заново.
    info: Any = None


FORECASTS: dict[int, Entry] = {}
LOCKS: dict[int, asyncio.Lock] = {}
# Отчёты Директа, заказанные для бизнеса: при занятых местах удаляются только они, а не отчёты соседа по кабинету.
OWNED: dict[int, set[int]] = {}


def clear() -> None:
    FORECASTS.clear()
    LOCKS.clear()
    OWNED.clear()


def weighted(pairs: list[tuple[int, Decimal]]) -> Decimal | None:
    """Средняя цена по кликам. Нулевая цена — Директ цены не назвал: бесплатных кликов нет, в среднее она не идёт."""
    priced = [(clicks, price) for clicks, price in pairs if price]
    total = sum(clicks for clicks, _ in priced)
    return sum((clicks * price for clicks, price in priced), Decimal(0)) / total if total else None


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
    # Сценарий без цены не считается: сколько кликов оплатит бюджет, без цены не сказать.
    scenarios = [
        min(potential, int(budget_rub / price)) for potential, price in (entry, first) if potential and price
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
    label = (
        f"так было на вашем сайте: за последние 3 месяца из {visits} посетителей с рекламы в Яндексе "
        f"заявку оставили {reached}"
    )
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


def template(
    clicks: Clicks, budget_rub: Decimal, conversion: Conversion, leads_range: tuple[int, int], budget_note: str = ""
) -> str:
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
    if not clicks.max:
        return (
            f"Яндекс Директ оценивает спрос по фразам плана в вашем регионе: за прошлый месяц было бы до "
            f"{rub(clicks.potential)} кликов. Но при бюджете {rub(budget_rub)} ₽ в месяц и цене клика {price} не "
            f"выйдет ни одного клика, а значит, и обращений из Директа.{budget_note} Это прогноз, он может ошибаться."
        )
    rate = (
        f"Заявку оставляют от {percent(conversion.min)} до {percent(conversion.max)} из 100 посетителей сайта — "
        f"{conversion.label}."
    )
    rate += f" {conversion.note}" if conversion.note else ""
    return (
        f"Яндекс Директ оценивает спрос по фразам плана в вашем регионе: за прошлый месяц было бы до "
        f"{rub(clicks.potential)} кликов. При бюджете {rub(budget_rub)} ₽ в месяц и цене клика {price} выйдет "
        f"примерно от {rub(clicks.min)} до {rub(clicks.max)} кликов.{budget_note} {rate} "
        f"Итого примерно от {leads_range[0]} до {leads_range[1]} обращений в месяц. {VISITS_NOTE} "
        "Это прогноз, он может ошибаться: вся сумма не обязательно потратится, а после первой недели покажем цифры "
        "по вашему сайту."
    )


def near_leads(text: str) -> set[Decimal]:
    """Числа рядом со словом «обращения» в том же предложении."""
    found = set()
    for sentence in re.split(r"(?<=[.!?])\s+", text):
        words = [(word.start() - LEADS_NEAR, word.end() + LEADS_NEAR) for word in LEADS_WORD.finditer(sentence)]
        found |= {
            Decimal(re.sub(r"\s", "", number[0]).replace(",", "."))
            for number in NUMBER.finditer(sentence)
            if any(number.end() > start and number.start() < end for start, end in words)
        }
    return found


def ai_text(response: str, allowed: set[Decimal], leads_range: tuple[int, int]) -> str | None:
    """Текст нейросети годится, только если в нём нет чисел, которых не было во входе, есть обе границы обращений
    и рядом со словом «обращения» нет чисел вне их вилки."""
    try:
        text = json.loads(response)["text"]
    except (ValueError, KeyError, TypeError):
        return None
    if not isinstance(text, str) or not text.strip() or len(text) > EXPLANATION_LIMIT:
        return None
    low, high = leads_range
    if not numbers(text) <= allowed or not {Decimal(low), Decimal(high)} <= numbers(text):
        return None
    if any(not low <= number <= high for number in near_leads(text)):
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
        # Прогноз считается не на весь бюджет из плана: владельцу нужно сказать, на какую сумму и почему.
        reserve = f"{percent(self.domain.budget_reserve)}% оставляем запасом, чтобы расход не вышел за бюджет"
        if shares["direct"] < payload["month_budget_kop"]:
            budget_note = (
                f" Из бюджета {rub(Decimal(payload['month_budget_kop']) / 100)} ₽ на Яндекс Директ приходится "
                f"{rub(Decimal(shares['direct']) / 100)} ₽, остальное — на ВКонтакте; {reserve}."
            )
        else:
            budget_note = f" Это не весь бюджет {rub(Decimal(payload['month_budget_kop']) / 100)} ₽: {reserve}."
        city = payload.get("city", business.city)
        goals = list(business.lead_goal_ids or [])[:MAX_GOALS]
        info = None if self.demo else self.registry.load(business.id, "direct")
        # Подключение в ключе: после входа заново прежний отказ (58, истёкший вход) не держится до конца срока.
        account = [info.status, info.token] if info else None
        inputs = [phrases, city, str(budget_rub), business.metrica_counter_id, goals, self.demo, account]
        key = sha256(json.dumps(inputs, ensure_ascii=False).encode()).hexdigest()
        # Площадки и ИИ отвечают секундами: соединение с базой на это время возвращается в пул.
        self.repository.commit()
        lock = LOCKS.setdefault(business.id, asyncio.Lock())
        async with lock:
            entry = FORECASTS.get(business.id)
            if entry is not None and (entry.key != key or entry.expires <= now):
                # Заказанный и уже ненужный отчёт удаляется: у пользователя Директа всего пять мест под отчёты.
                await self.drop(business.id, entry.info, entry.forecast_id)
                entry = None
            if entry is not None and entry.status != "pending":
                return entry.response | {"vk": vk}
            entry = await self.refresh(business, info, entry, key, phrases, city, budget_rub, budget_note, now, today)
            FORECASTS[business.id] = entry
            return entry.response | {"vk": vk}

    async def refresh(
        self,
        business: Business,
        info: Any,
        entry: Entry | None,
        key: str,
        phrases: list[str],
        city: str,
        budget_rub: Decimal,
        budget_note: str,
        now: datetime,
        today: date,
    ) -> Entry:
        """Один шаг без ожидания: заказать отчёт или спросить его готовность; готовый прочитать и удалить."""
        if self.demo:
            return await self.ready(business, key, demo_rows(phrases), budget_rub, budget_note, now, today)
        forecast_id = entry.forecast_id if entry else None
        try:
            client = self.client(info, business.id)
            if forecast_id is None:
                region = await self.registry.resolve(business.id, "direct").geo_targeting(city)
                forecast_id = await client.create(phrases, region["region_ids"])
                state = "Pending"
            else:
                state = await client.status(forecast_id)
            if state == "Pending":
                expires = entry.expires if entry else now + TTL["pending"]
                return Entry(key, "pending", expires, forecast_id, status("pending", PENDING, None), info)
            rows = await client.get(forecast_id) if state == "Done" else None
        except PlatformError as error:
            await self.drop(business.id, info, forecast_id)
            return self.failure(key, error, now)
        await self.drop(business.id, info, forecast_id)
        if rows is None:
            return self.failed(key, "Яндекс Директ не смог посчитать прогноз. Попробуем ещё раз позже.", now)
        return await self.ready(business, key, rows, budget_rub, budget_note, now, today)

    def client(self, info: Any, business_id: int) -> DirectForecastClient:
        if info is None or info.status != "ok":
            raise PlatformError("подключите кабинет Яндекс Директа.")
        return DirectForecastClient(
            info.token,
            config=self.registry.config,
            client=self.registry.http,
            timeout=DIRECT_TIMEOUT,
            owned=OWNED.setdefault(business_id, set()),
        )

    async def drop(self, business_id: int, info: Any, forecast_id: int | None) -> None:
        """Не удалился — id остаётся в своих: при ошибке 31 его удалит следующий заказ или Директ через пять часов."""
        if forecast_id is None:
            return
        try:
            await self.client(info, business_id).delete(forecast_id)
        except PlatformError:
            pass

    @staticmethod
    def failed(key: str, message: str, now: datetime) -> Entry:
        return Entry(key, "unavailable", now + TTL["unavailable"], response=unavailable(message, None))

    @classmethod
    def failure(cls, key: str, error: PlatformError, now: datetime) -> Entry:
        if isinstance(error, ForecastAccessDenied):
            return Entry(key, "unavailable", now + ACCESS_TTL, response=unavailable(str(error), None))
        if isinstance(error, AuthExpired):
            return cls.failed(key, "Войдите в Яндекс Директ заново: без этого прогноз не получить.", now)
        return cls.failed(key, f"Прогноз Яндекс Директа сейчас недоступен: {error}", now)

    async def conversion(self, business: Business) -> Conversion:
        if not business.metrica_counter_id or not business.lead_goal_ids:
            return replace(BENCHMARK, note="Своих цифр по сайту нет: не выбрано, что на сайте считать обращением.")
        try:
            metrica = self.registry.metrica(business.id)
            visits, reached = await metrica.get_ad_conversions(
                business.metrica_counter_id, list(business.lead_goal_ids)[:MAX_GOALS]
            )
        except (MetricaError, PlatformError) as error:
            logger.warning("Доля обращений бизнеса %s по Метрике не посчитана: %s", business.id, error)
            return replace(BENCHMARK, note="Свои цифры по сайту получить не удалось: счётчик посещений не ответил.")
        return measured(visits, reached) or replace(
            BENCHMARK, note="Своих цифр по сайту пока мало: с рекламы в Яндексе на него заходили слишком редко."
        )

    async def ready(
        self,
        business: Business,
        key: str,
        rows: list[PhraseForecast],
        budget_rub: Decimal,
        budget_note: str,
        now: datetime,
        today: date,
    ) -> Entry:
        clicks = direct_clicks(rows, budget_rub)
        if clicks.potential and clicks.cpc_min is None and clicks.cpc_max is None:
            return self.failed(
                key, "Яндекс Директ не назвал цену клика по фразам плана: прогноз посчитать не из чего.", now
            )
        conversion = await self.conversion(business)
        leads_range = leads(clicks, conversion)
        text = template(clicks, budget_rub, conversion, leads_range, budget_note)
        explanation = await self.explain(business, text, leads_range, today) if clicks.max else None
        forecast = (
            "Демо: числа придуманы для показа, это не прогноз Яндекса"
            if self.demo
            else "Яндекс Директ, прогноз бюджета (API v4 Live): спрос по фразам плана в вашем регионе за прошлый месяц"
        )
        sources = [
            {"what": "Клики и цена клика", "source": forecast, "url": None if self.demo else FORECAST_DOC},
            {
                "what": "Бюджет",
                "source": f"Ваш план: {rub(budget_rub)} ₽ в месяц на Яндекс Директ.{budget_note}",
                "url": None,
            },
            {
                "what": "Сколько посетителей оставляют заявку",
                "source": " ".join(
                    filter(None, [f"{conversion.label[0].upper()}{conversion.label[1:]}.", conversion.note])
                ),
                "url": conversion.url,
            },
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

    async def explain(self, business: Business, text: str, leads_range: tuple[int, int], today: date) -> str | None:
        """Пересказ нейросетью по готовому тексту; любые сбои и новые числа — остаётся шаблон."""
        if self.llm is None:
            return None
        budget = ai_budget(self.repository, business, self.limits, today)
        llm = SpendLLM(self.llm, self.repository, business.id, budget, "forecast")
        try:
            response = await llm.complete_json(SYSTEM, json.dumps({"forecast": text}, ensure_ascii=False))
        except YandexGPTError:
            return None
        return ai_text(response, numbers(text), leads_range)
