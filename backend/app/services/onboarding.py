import asyncio
import re
from collections.abc import Awaitable, Callable, Iterator
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from threading import Lock
from typing import Any

from sqlalchemy import func, select
from starlette.concurrency import run_in_threadpool

from app.content.art import ArtBudget
from app.content.art import ArtCall as ArtRecord
from app.content.llm import LLMClient
from app.content.medical import MedicalLevel, classify
from app.content.site_reader import SiteReadResult, extract_description, read_site, safe_url
from app.content.verify import DescriptionDraft, Fact, OwnerQuestion, Service
from app.content.yandexgpt import CompletionResult, YandexGPTError
from app.db.models import ArtCall, Business, LLMCall, MarkingProfile, Plan, PlatformAccount
from app.domain.budget import PLATFORM_MINIMUMS, budget_minimum
from app.domain.explain import rubles
from app.platforms.metrica import Metrica
from app.services.changes import LOCK_WAIT_SECONDS, ChangeConflict, ChangesRepository, wait_for_lock, waiting
from app.worker.clock import MOSCOW

DESCRIPTION_KEYS = ("services", "prices", "address", "difference")
SUBJECT_TYPES = ("ip", "ooo", "selfemployed", "person")
INN_ERRORS = {
    "ip": "Проверьте ИНН: у ИП в нём 12 цифр, без ошибок.",
    "ooo": "Проверьте ИНН: у ООО в нём 10 цифр, без ошибок.",
    "selfemployed": "Проверьте ИНН: у самозанятого в нём 12 цифр, без ошибок.",
    "person": "Проверьте ИНН: у физлица в нём 12 цифр, без ошибок.",
}
MEDICAL_ORDER = ("none", "maybe", "required")
MAX_BUDGET_KOP = 1_000_000_000
DRAFT_STATUSES = ("draft", "plan_ready")
LAUNCHED = "Реклама уже запущена. Сведения о бизнесе меняются в настройках."
METRICA_LAUNCHED = "Реклама уже запущена. Счётчик и цели Метрики после запуска не меняются."
CHECK_TTL = timedelta(minutes=10)
MAX_FIELD = 2000
QUESTIONS = {
    "services": "Какие услуги вы оказываете?",
    "prices": "Сколько стоят ваши услуги?",
    "address": "По какому адресу вы принимаете клиентов?",
    "difference": "Чем ваши услуги отличаются?",
}
NICHES = (
    ("nails", ("маникюр", "педикюр", "ногт")),
    ("yoga", ("йог",)),
    ("barbershop", ("барбер", "стрижк", "бород")),
    ("fitness", ("фитнес", "тренаж", "спортзал", "тренер")),
    ("beauty", ("салон красоты", "парикмахер", "косметолог", "эпиляц", "визаж", "бров", "ресниц")),
)
# Проверки сайта, которые сейчас идут в процессе API (он один). Проверка того же бизнеса, пришедшая параллельно
# (повторный вход на шаг мастера), дожидается идущей и берёт её результат из кэша: сайт второй раз не читается и лимит
# частоты не тратится.
SITE_CHECKS: dict[int, asyncio.Event] = {}
# Сохранения описания, маркировки и Метрики, которые сейчас ждут блокировку бизнеса: не больше одного на бизнес. Ждёт
# поток из пула запросов, и сотни одновременных сохранений одного бизнеса заняли бы весь пул; пока одно ждёт,
# остальным сразу 409.
LOCK_WAITERS: set[int] = set()
WAITERS_LOCK = Lock()
SAVING = "Предыдущее сохранение ещё ждёт своей очереди. Повторите через несколько секунд."


class OnboardingError(ValueError):
    pass


@contextmanager
def waited_lock(repository: "OnboardingRepository", business_id: int, seconds: float) -> Iterator[None]:
    """Блокировка бизнеса, которую ждут до seconds секунд: её держат на секунды шаг автопилота, проверка сайта и
    перемотка демо. Ждёт поток (sleep), поэтому вызывается только вне цикла событий. Не дождались — ChangeConflict.

    Соединение сессии на время ожидания возвращается в пул: ждущие запросы не занимают его у остальных."""
    with WAITERS_LOCK:
        if business_id in LOCK_WAITERS:
            raise ChangeConflict(SAVING)
        LOCK_WAITERS.add(business_id)
    with ExitStack() as stack:
        try:
            repository.commit()
            stack.enter_context(waiting(lambda: repository.business_lock(business_id), seconds))
        finally:
            with WAITERS_LOCK:
                LOCK_WAITERS.discard(business_id)
        yield


class Blocked(Exception):
    """Шаг нельзя выполнить из-за состояния бизнеса; текст показывается владельцу."""


class NotFound(Exception):
    """Объекта нет у этого бизнеса; текст показывается владельцу (404)."""


@dataclass(frozen=True)
class AiLimits:
    onboarding_kop: int
    weekly_kop: int

    @classmethod
    def from_settings(cls, settings: Any) -> "AiLimits":
        return cls(int(settings.ai_spend_onboarding_limit_rub * 100), int(settings.ai_spend_weekly_limit_rub * 100))


def valid_inn(inn: str) -> bool:
    if not re.fullmatch(r"[0-9]{10}|[0-9]{12}", inn) or not inn.strip("0"):
        return False
    digits = [int(char) for char in inn]

    def check(weights: tuple[int, ...]) -> int:
        return sum(weight * digit for weight, digit in zip(weights, digits, strict=False)) % 11 % 10

    if len(digits) == 10:
        return check((2, 4, 10, 3, 5, 9, 4, 6, 8)) == digits[9]
    return (
        check((7, 2, 4, 10, 3, 5, 9, 4, 6, 8)) == digits[10] and check((3, 7, 2, 4, 10, 3, 5, 9, 4, 6, 8)) == digits[11]
    )


def normalize_phone(phone: str) -> str:
    digits = re.sub(r"[^0-9]", "", phone)
    if len(digits) == 11 and digits[0] in "78":
        return "+7" + digits[1:]
    if len(digits) == 10:
        return "+7" + digits
    raise OnboardingError("Укажите телефон в российском формате, например +7 900 000-00-00.")


def detect_niche(text: str, medical_level: MedicalLevel) -> str:
    lowered = text.lower()
    for niche, words in NICHES:
        if any(word in lowered for word in words):
            return niche
    return "medical" if medical_level == "required" else "other"


def worst_medical(*levels: str) -> MedicalLevel:
    return max(levels, key=MEDICAL_ORDER.index)  # type: ignore[return-value]


def launch_time(value: str) -> datetime:
    """Время запуска из плана; запись без пояса считается UTC, чтобы сравнение с началом недели не падало."""
    launched = datetime.fromisoformat(value)
    return launched if launched.tzinfo else launched.replace(tzinfo=UTC)


def ai_budget(repository: "OnboardingRepository", business: Business, limits: AiLimits, today: date) -> ArtBudget:
    """Общий потолок трат на ИИ: до запуска — на онбординг, после — на московскую неделю с момента запуска.

    В демо (DemoLLM, DemoArt) ИИ бесплатный: вызовы не пишутся в llm_calls и art_calls, траты всегда 0, и сдвиг
    виртуальной даты демо относительно created_at (время базы) на лимит не влияет. Деньги в демо не тратятся.
    """
    if business.status in ("draft", "plan_ready"):
        limit, spent = limits.onboarding_kop, repository.ai_spent(business.id)
    else:
        since = datetime.combine(today - timedelta(days=today.weekday()), time(), MOSCOW)
        launched = repository.launched_at(business.id)
        limit, spent = limits.weekly_kop, repository.ai_spent(business.id, max(since, launched) if launched else since)
    return ArtBudget(limit, min(max(spent, 0), limit))


class SpendLLM:
    """Учитывает каждый вызов в llm_calls и не пускает дальше потолка трат."""

    def __init__(
        self, inner: LLMClient, repository: "OnboardingRepository", business_id: int, budget: ArtBudget, purpose: str
    ) -> None:
        self.inner = inner
        self.repository = repository
        self.business_id = business_id
        self.budget = budget
        self.purpose = purpose

    async def complete_json(self, system: str, user: str, temperature: float = 0.2) -> str:
        if self.budget.spent_kop >= self.budget.limit_kop:
            raise YandexGPTError("Лимит трат на ИИ исчерпан.")
        try:
            result = await self.inner.complete_json(system, user, temperature)
        except YandexGPTError as error:
            self.account(error.result)
            raise
        self.account(result)
        return result

    def account(self, result: object) -> None:
        if isinstance(result, CompletionResult):
            self.budget.spent_kop += result.cost_kop
            self.repository.record_llm(self.business_id, self.purpose, result)


class OnboardingRepository(ChangesRepository):
    def commit(self) -> None:
        self.db.commit()

    def refresh(self, item: object) -> None:
        self.db.refresh(item)

    def add(self, item: object) -> None:
        self.db.add(item)
        self.db.commit()

    def user_businesses(self, user_id: int) -> list[Business]:
        return list(self.db.scalars(select(Business).where(Business.user_id == user_id).order_by(Business.id)))

    def marking(self, business_id: int) -> MarkingProfile | None:
        return self.db.get(MarkingProfile, business_id)

    def fresh_business(self, business_id: int) -> Business | None:
        """Строка из базы, а не из сессии: под блокировкой видны удаление и правки, сделанные, пока её ждали."""
        return self.db.scalar(
            select(Business).where(Business.id == business_id).execution_options(populate_existing=True)
        )

    def accounts(self, business_id: int) -> list[PlatformAccount]:
        return list(self.db.scalars(select(PlatformAccount).where(PlatformAccount.business_id == business_id)))

    def ai_spent(self, business_id: int, since: datetime | None = None) -> int:
        total = 0
        for model in (LLMCall, ArtCall):
            statement = select(func.coalesce(func.sum(model.cost_kop), 0)).where(model.business_id == business_id)
            if since is not None:
                statement = statement.where(model.created_at >= since)
            total += int(self.db.scalar(statement))
        return total

    def launched_at(self, business_id: int) -> datetime | None:
        value = self.db.scalar(
            select(Plan.payload["launch"]["launched_at"].astext)
            .where(Plan.business_id == business_id, Plan.status == "launched")
            .order_by(Plan.version.desc())
            .limit(1)
        )
        return launch_time(value) if value else None

    def record_llm(self, business_id: int, purpose: str, result: CompletionResult) -> None:
        self.add(
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

    def save_art_call(self, record: ArtRecord) -> None:
        row = self.db.get(ArtCall, record.id) if record.id is not None else None
        if row is None:
            row = ArtCall(business_id=record.business_id)
            self.db.add(row)
        row.cost_kop = record.cost_kop
        row.status = record.status
        row.rejection_reason = record.rejection_reason
        row.operation_id = record.operation_id
        row.seed = record.seed
        self.db.commit()
        record.id = row.id


def business_view(business: Business) -> dict:
    return {
        "id": business.id,
        "site_url": business.site_url,
        "city": business.city,
        "month_budget_kop": business.month_budget_kop,
        "avg_check_kop": business.avg_check_kop,
    }


def lines(value: str) -> list[str]:
    return [line.strip(" -•\t") for line in re.split(r"[\n;]", value) if line.strip(" -•\t")]


def service_names(value: str) -> list[str]:
    """Услуги владелец пишет и по строке, и через запятую; запятая в скобках название не делит. Повторы убираются."""
    names: dict[str, str] = {}
    for item in re.split(r"[\n;]|,(?![^()]*\))", value):
        if name := item.strip(" -•\t"):
            names.setdefault(name.casefold(), name)
    return list(names.values())


OWNER_PRICE = re.compile(
    r"\s*[—–:-]?\s*(\d{1,3}(?:[ \u00a0]\d{3})+|\d+)(?:[,.](\d{2}))?\s*(?:₽|руб\.?|р\.?)", re.IGNORECASE
)


def owner_price(name: str, prices: list[str]) -> int | None:
    """Цена услуги из поля «Цены»: строка — название услуги и сумма, как её записывает проверка сайта."""
    for line in prices:
        if line.casefold().startswith(name.casefold()) and (match := OWNER_PRICE.fullmatch(line[len(name) :])):
            return int(re.sub(r"\D", "", match[1])) * 100 + int(match[2] or 0) or None
    return None


def description_fields(draft: DescriptionDraft) -> dict[str, dict]:
    asked: dict[str, str] = {}
    for item in draft.questions:
        head = item.field.split(".")
        if "price" in head:
            key = "prices"
        elif head[0] == "address":
            key = "address"
        else:
            key = "difference" if head[0] == "advantages" else "services"
        asked.setdefault(key, item.question)
    prices = [f"{service.name} — {rubles(service.price)} ₽" for service in draft.services if service.price is not None]
    values = {
        "services": "\n".join(service.name for service in draft.services),
        "prices": "\n".join(prices),
        "address": draft.address.value if draft.address else "",
        "difference": "\n".join(fact.value for fact in draft.advantages),
    }
    quotes = {
        "services": [service.quote for service in draft.services],
        "prices": [service.quote for service in draft.services if service.price is not None],
        "address": [draft.address.quote] if draft.address else [],
        "difference": [fact.quote for fact in draft.advantages],
    }
    return {
        key: {
            "value": values[key],
            "quote": " … ".join(dict.fromkeys(quotes[key])) or None,
            # В услугах только подтверждённые цитатой: вопрос об отброшенной услуге найденные не отменяет.
            "verified": bool(values[key]) and (key not in asked or key == "services"),
            "question": asked.get(key, QUESTIONS[key]),
        }
        for key in DESCRIPTION_KEYS
    }


def description_draft(business: Business) -> DescriptionDraft:
    """Утверждённое владельцем описание в виде, который понимает проверка текстов (белый список фактов)."""
    fields = business.description.get("fields", {})
    value = {key: fields.get(key, {}).get("value", "") for key in DESCRIPTION_KEYS}
    draft = DescriptionDraft(source="owner_text", medical=classify(" ".join(value.values())))
    draft.medical.level = business.medical_level
    prices = lines(value["prices"])
    draft.services = [
        Service(name, owner_price(name, prices), name, "owner") for name in service_names(value["services"])
    ]
    if value["address"].strip():
        draft.address = Fact(value["address"].strip(), value["address"].strip(), "owner")
    # Цены входят в белый список чисел как факты; отдельного поля цен в проверке текстов нет.
    draft.advantages = [Fact(line, line, "owner") for line in prices + lines(value["difference"])]
    return draft


def check_budget(amount: int) -> None:
    """Директ обязателен: бюджет, с которым его кампания не наберёт минимум площадки, не сохраняется."""
    from app.config import get_domain_settings

    minimum = budget_minimum(("direct",), get_domain_settings())
    if amount < minimum:
        raise OnboardingError(
            f"Минимальный бюджет — {rubles(minimum)} ₽ в месяц. Яндекс Директ требует не меньше "
            f"{rubles(PLATFORM_MINIMUMS['direct']['weekly_limit_kop'])} ₽ на кампанию в неделю, а мы считаем с "
            "запасом, чтобы реклама не вставала на паузу в конце недели и месяца."
        )


class OnboardingService:
    def __init__(
        self,
        repository: OnboardingRepository,
        llm: LLMClient,
        metrica: Callable[[int], Metrica],
        limits: AiLimits,
        *,
        reader: Callable[[str], Awaitable[SiteReadResult]] = read_site,
        ai_mode: str = "",
    ) -> None:
        self.repository = repository
        self.llm = llm
        self.metrica = metrica
        self.limits = limits
        self.reader = reader
        self.ai_mode = ai_mode

    def create_business(
        self, user_id: int, site_url: str, city: str, month_budget_kop: int, avg_check_kop: int | None
    ) -> dict:
        url = site_url.strip()
        checked = safe_url(url if "://" in url else f"https://{url}")
        city = city.strip()
        if not checked or not city or len(city) > 100:
            raise OnboardingError("Укажите ссылку на сайт и город.")
        if type(month_budget_kop) is not int or not 0 < month_budget_kop <= MAX_BUDGET_KOP:
            raise OnboardingError("Укажите положительную сумму в рублях, не более двух знаков после запятой.")
        check_budget(month_budget_kop)
        if avg_check_kop is not None and (type(avg_check_kop) is not int or not 0 < avg_check_kop <= MAX_BUDGET_KOP):
            raise OnboardingError("Средний чек должен быть положительной суммой.")
        business = next(
            (item for item in self.repository.user_businesses(user_id) if item.status in DRAFT_STATUSES), None
        )
        if business is None:
            if self.repository.user_businesses(user_id):
                raise Blocked(LAUNCHED)
            business = Business(user_id=user_id, site_url=checked, city=city, month_budget_kop=month_budget_kop)
            self.update(business, checked, city, month_budget_kop, avg_check_kop)
            return business_view(business)
        if (business.site_url, business.city, business.month_budget_kop, business.avg_check_kop) == (
            checked,
            city,
            month_budget_kop,
            avg_check_kop,
        ):
            # Повторное «Далее» без правок ничего не пишет: блокировка не нужна, и проверка сайта, которая её держит,
            # не отвечает на него 409.
            return business_view(business)
        # Под блокировкой: проверка сайта пишет ответ ИИ под ней же и не запишет описание старого сайта поверх
        # сброшенного, а запуск держит её от проверки плана до перевода бизнеса в running, и бюджет посреди запуска не
        # меняется. Как и описание, ждём её несколько секунд и только потом отвечаем 409. Если запуск завершился, пока
        # ждали, бизнес уже не черновик: сведения и бюджет не меняются.
        with waited_lock(self.repository, business.id, LOCK_WAIT_SECONDS):
            business = self.current(business.id)
            if business.status not in DRAFT_STATUSES:
                raise Blocked(LAUNCHED)
            if business.site_url != checked:
                self.reset_description(business)
            self.update(business, checked, city, month_budget_kop, avg_check_kop)
        return business_view(business)

    def reset_description(self, business: Business) -> None:
        business.description = {}
        business.description_approved_at = None
        business.metrica_counter_id = None
        business.lead_goal_ids = []
        business.medical_level = "none"
        business.niche_class = business.niche_text = None

    def update(
        self, business: Business, site_url: str, city: str, month_budget_kop: int, avg_check_kop: int | None
    ) -> None:
        business.site_url = site_url
        business.city = city
        business.month_budget_kop = month_budget_kop
        business.avg_check_kop = avg_check_kop
        self.repository.add(business)

    def current(self, business_id: int) -> Business:
        business = self.repository.fresh_business(business_id)
        if business is None:
            raise NotFound("Бизнес не найден.")
        return business

    def checked_business(self, business_id: int, url: str) -> Business:
        """Под блокировкой: пока читался сайт или отвечал ИИ, бизнес могли удалить или сменить ему сайт."""
        business = self.current(business_id)
        if business.site_url != url:
            raise ChangeConflict("Адрес сайта изменился во время проверки. Проверьте сайт ещё раз.")
        return business

    async def site_check(
        self,
        business_id: int,
        now: datetime,
        today: date,
        *,
        owner_text: str | None = None,
        on_miss: Callable[[], None] | None = None,
        refresh: bool = False,
    ) -> dict:
        """on_miss вызывается перед настоящим чтением сайта: лимит частоты не тратится на ответ из кэша, в том числе на
        ответ, который принесла шедшая параллельно проверка того же бизнеса. Если та проверка сорвалась, сайт читает
        первая из ждавших, остальные снова ждут её: чтение и лимит — по одному разу. refresh — явный повтор владельца:
        кэш пропускается, но ответ параллельной проверки, которую он дождался, свежий и берётся без второго чтения."""
        business = self.current(business_id)
        while (running := SITE_CHECKS.get(business_id)) is not None and owner_text is None:
            self.repository.commit()
            await running.wait()
            business = self.current(business_id)
            refresh = False
        if not refresh and (cached := self.cached_check(business, now, owner_text)):
            return self.check_view(business, cached)
        if on_miss is not None:
            on_miss()
        done = SITE_CHECKS[business_id] = asyncio.Event()
        try:
            return await self.read_site(business_id, business.site_url, now, today, owner_text, refresh)
        finally:
            done.set()
            if SITE_CHECKS.get(business_id) is done:
                del SITE_CHECKS[business_id]

    async def read_site(
        self, business_id: int, url: str, now: datetime, today: date, owner_text: str | None, refresh: bool
    ) -> dict:
        # Чтение сайта идёт до минуты: без блокировки бизнеса, соединение с базой на это время возвращается в пул.
        self.repository.commit()
        site = await self.reader(url)
        # Под блокировкой только трата на ИИ и запись. Её может держать шаг воркера или смена сведений о бизнесе:
        # ждём, не отвечая владельцу 409.
        with ExitStack() as stack:
            await wait_for_lock(stack, self.repository, business_id, LOCK_WAIT_SECONDS)
            business = self.checked_business(business_id, url)
            if not refresh and (cached := self.cached_check(business, now, owner_text)):
                return self.check_view(business, cached)
            llm = SpendLLM(
                self.llm, self.repository, business.id, ai_budget(self.repository, business, self.limits, today), "site"
            )
            self.repository.commit()
            try:
                draft = await extract_description(site, llm, owner_text=owner_text)
            except YandexGPTError:
                draft = DescriptionDraft(questions=[OwnerQuestion("description", QUESTIONS["services"])])
            # Ответ ИИ по старому сайту или удалённому бизнесу не записывается: потраченное на ИИ уже учтено.
            business = self.checked_business(business_id, url)
            fields = description_fields(draft)
            check = {
                "site_url": url,
                "ai_mode": self.ai_mode,
                "checked_at": now.isoformat(),
                "pages_read": len(site.pages),
                "services_found": len(draft.services),
                "counter_ids": site.counter_ids,
                "block_reason": site.launch_block_reason,
                "fresh": fields,
            }
            description = dict(business.description)
            description["check"] = check
            description.setdefault("fields", fields)
            # Новое чтение дополняет неподтверждённое описание: заполняются только пустые поля, введённое не трогаем.
            if business.description_approved_at is None:
                description["fields"] = {
                    key: item if item.get("value") else fields[key] for key, item in description["fields"].items()
                }
            description["site_medical"] = draft.medical.level
            business.description = description
            business.medical_level = worst_medical(draft.medical.level, business.medical_level)
            self.apply_niche(business)
            self.repository.commit()
        return self.check_view(business, check)

    def cached_check(self, business: Business, now: datetime, owner_text: str | None) -> dict | None:
        cached = business.description.get("check")
        if (
            cached
            and owner_text is None
            and cached["site_url"] == business.site_url
            and cached.get("ai_mode", self.ai_mode) == self.ai_mode
            and now - datetime.fromisoformat(cached["checked_at"]) < CHECK_TTL
        ):
            return cached
        return None

    def check_view(self, business: Business, check: dict) -> dict:
        ids = check["counter_ids"]
        return {
            "status": "ready" if not check["block_reason"] else "blocked",
            "block_reason": check["block_reason"],
            "pages_read": check["pages_read"],
            "services_found": check["services_found"],
            "counter_id": ids[0] if ids else None,
            "counter_ids": ids,
            "description": business.description.get("fields", check["fresh"]),
            "medical_level": business.medical_level,
        }

    def apply_niche(self, business: Business) -> None:
        fields = business.description.get("fields", {})
        text = " ".join(fields.get(key, {}).get("value", "") for key in DESCRIPTION_KEYS)
        business.niche_text = fields.get("services", {}).get("value") or None
        business.niche_class = detect_niche(text, business.medical_level)

    def save_description(self, business_id: int, fields: dict, approved: bool, now: datetime) -> dict:
        # Описание владелец меняет и после запуска — в настройках, поэтому статус не проверяется. Под блокировкой и по
        # строке из базы: проверка сайта записывает описание целиком и не затрёт сохранённое. Блокировку держат и шаг
        # автопилота, и перемотка демо: ждём несколько секунд, как проверка сайта, и только потом отвечаем 409.
        with waited_lock(self.repository, business_id, LOCK_WAIT_SECONDS):
            business = self.current(business_id)
            previous = business.description.get("fields", {})
            clean = {}
            for key in DESCRIPTION_KEYS:
                item = fields.get(key)
                value = item.get("value", "") if isinstance(item, dict) else ""
                if not isinstance(value, str) or len(value) > MAX_FIELD:
                    raise OnboardingError("Описание слишком длинное или заполнено неверно.")
                value = value.strip()
                old = previous.get(key, {})
                kept = bool(value) and value == old.get("value")
                clean[key] = {
                    "value": value,
                    "quote": old.get("quote") if kept else None,
                    "verified": bool(old.get("verified")) if kept else False,
                    "question": QUESTIONS[key],
                }
            if approved and not clean["services"]["value"]:
                raise OnboardingError("Укажите услуги и подтвердите описание.")
            description = dict(business.description)
            description["fields"] = clean
            business.description = description
            business.description_approved_at = now if approved else None
            owner_level = classify(" ".join(item["value"] for item in clean.values())).level
            business.medical_level = worst_medical(description.get("site_medical", "none"), owner_level)
            self.apply_niche(business)
            self.repository.commit()
        return {"approved": approved, "medical_level": business.medical_level}

    async def counters(self, business_id: int) -> dict:
        items = await self.metrica(business_id).list_counters()
        return {"items": [{"id": item.id, "name": item.name, "site": item.site} for item in items]}

    async def goals(self, business_id: int, counter_id: int) -> dict:
        client = self.metrica(business_id)
        if not any(item.id == counter_id for item in await client.list_counters()):
            raise OnboardingError("Счётчик не найден среди ваших счётчиков Метрики.")
        return {"items": [{"id": item.id, "name": item.name} for item in await client.list_goals(counter_id)]}

    async def save_metrica(self, business_id: int, counter_id: int, goal_ids: list[int]) -> dict:
        goals = {item["id"] for item in (await self.goals(business_id, counter_id))["items"]}
        if not goal_ids or not set(goal_ids) <= goals:
            raise OnboardingError("Выберите хотя бы одну цель выбранного счётчика.")
        # Метрика спрошена до блокировки: её ответа запись не ждёт. Блокировку запись ждёт, как описание и маркировка,
        # в пуле потоков: ожидание и запросы к базе не останавливают цикл событий.
        return await run_in_threadpool(self.write_metrica, business_id, counter_id, goal_ids)

    def write_metrica(self, business_id: int, counter_id: int, goal_ids: list[int]) -> dict:
        # Под блокировкой и по строке из базы: по целям считаются заявки в статистике и автопилоте, и запуск,
        # завершившийся до блокировки, их уже не меняет.
        with waited_lock(self.repository, business_id, LOCK_WAIT_SECONDS):
            business = self.current(business_id)
            if business.status not in DRAFT_STATUSES:
                raise Blocked(METRICA_LAUNCHED)
            site_counters = business.description.get("check", {}).get("counter_ids", [])
            if site_counters and counter_id not in site_counters:
                raise OnboardingError(
                    "Выбранный счётчик не совпадает со счётчиком на сайте. Вернитесь к выбору счётчика."
                )
            business.metrica_counter_id = counter_id
            business.lead_goal_ids = sorted(set(goal_ids))
            self.repository.commit()
        return {"counter_id": counter_id, "lead_goal_ids": business.lead_goal_ids}

    def save_marking(
        self, business_id: int, subject_type: str, inn: str, name: str, phone: str, confirmed: dict[str, bool]
    ) -> dict:
        inn = inn.strip()
        name = name.strip()
        if subject_type not in SUBJECT_TYPES or not name or len(name) > 300:
            raise OnboardingError("Заполните данные рекламодателя.")
        if not valid_inn(inn) or len(inn) != (10 if subject_type == "ooo" else 12):
            raise OnboardingError(INN_ERRORS[subject_type])
        phone = normalize_phone(phone)
        # Маркировку владелец меняет и после запуска — в настройках, поэтому статус не проверяется. Под блокировкой:
        # запись не идёт посреди запуска или шага автопилота, и удалённому бизнесу профиль не создаётся. Как и описание,
        # ждёт блокировку несколько секунд.
        with waited_lock(self.repository, business_id, LOCK_WAIT_SECONDS):
            self.current(business_id)
            profile = self.repository.marking(business_id) or MarkingProfile(business_id=business_id)
            profile.subject_type = subject_type
            profile.inn_enc = inn
            profile.name = name
            profile.phone_enc = phone
            profile.confirmed_in_platform = {platform: confirmed.get(platform) is True for platform in ("direct", "vk")}
            self.repository.add(profile)
        return {"confirmed_in_platform": profile.confirmed_in_platform}
