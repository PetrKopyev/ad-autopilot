import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any

from sqlalchemy import func, select

from app.content.art import ArtBudget
from app.content.art import ArtCall as ArtRecord
from app.content.llm import LLMClient
from app.content.medical import MedicalLevel, classify
from app.content.site_reader import SiteReadResult, extract_description, read_site, safe_url
from app.content.verify import DescriptionDraft, Fact, OwnerQuestion, Service
from app.content.yandexgpt import CompletionResult, YandexGPTError
from app.db.models import ArtCall, Business, LLMCall, MarkingProfile, PlatformAccount
from app.domain.explain import rubles
from app.platforms.metrica import Metrica
from app.services.changes import ChangesRepository

DESCRIPTION_KEYS = ("services", "prices", "address", "difference")
SUBJECT_TYPES = ("ip", "ooo", "selfemployed", "person")
MEDICAL_ORDER = ("none", "maybe", "required")
MAX_BUDGET_KOP = 1_000_000_000
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


class OnboardingError(ValueError):
    pass


class Blocked(Exception):
    """Шаг нельзя выполнить из-за состояния бизнеса; текст показывается владельцу."""


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


def ai_budget(repository: "OnboardingRepository", business: Business, limits: AiLimits, today: date) -> ArtBudget:
    """Общий потолок трат на ИИ: до запуска — на онбординг, после — на неделю (по снимку счётчика)."""
    total = repository.ai_spent(business.id)
    if business.status in ("draft", "plan_ready"):
        limit, spent = limits.onboarding_kop, total
    else:
        week = (today - timedelta(days=today.weekday())).isoformat()
        if business.settings.get("_ai", {}).get("week") != week:
            business.settings = business.settings | {"_ai": {"week": week, "base": total}}
            repository.commit()
        limit, spent = limits.weekly_kop, total - business.settings["_ai"]["base"]
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

    async def complete_json(self, system: str, user: str) -> str:
        if self.budget.spent_kop >= self.budget.limit_kop:
            raise YandexGPTError("Лимит трат на ИИ исчерпан.")
        try:
            result = await self.inner.complete_json(system, user)
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

    def add(self, item: object) -> None:
        self.db.add(item)
        self.db.commit()

    def user_businesses(self, user_id: int) -> list[Business]:
        return list(self.db.scalars(select(Business).where(Business.user_id == user_id).order_by(Business.id)))

    def marking(self, business_id: int) -> MarkingProfile | None:
        return self.db.get(MarkingProfile, business_id)

    def accounts(self, business_id: int) -> list[PlatformAccount]:
        return list(self.db.scalars(select(PlatformAccount).where(PlatformAccount.business_id == business_id)))

    def ai_spent(self, business_id: int) -> int:
        llm = self.db.scalar(
            select(func.coalesce(func.sum(LLMCall.cost_kop), 0)).where(LLMCall.business_id == business_id)
        )
        art = self.db.scalar(
            select(func.coalesce(func.sum(ArtCall.cost_kop), 0)).where(ArtCall.business_id == business_id)
        )
        return int(llm) + int(art)

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
            "verified": bool(values[key]) and key not in asked,
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
    draft.services = [Service(name, None, name, "owner") for name in lines(value["services"])]
    if value["address"].strip():
        draft.address = Fact(value["address"].strip(), value["address"].strip(), "owner")
    # Цены входят в белый список чисел как факты; отдельного поля цен в проверке текстов нет.
    draft.advantages = [Fact(line, line, "owner") for line in lines(value["prices"]) + lines(value["difference"])]
    return draft


class OnboardingService:
    def __init__(
        self,
        repository: OnboardingRepository,
        llm: LLMClient,
        metrica: Callable[[int], Metrica],
        limits: AiLimits,
        *,
        reader: Callable[[str], Awaitable[SiteReadResult]] = read_site,
    ) -> None:
        self.repository = repository
        self.llm = llm
        self.metrica = metrica
        self.limits = limits
        self.reader = reader

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
        if avg_check_kop is not None and (type(avg_check_kop) is not int or not 0 < avg_check_kop <= MAX_BUDGET_KOP):
            raise OnboardingError("Средний чек должен быть положительной суммой.")
        business = next(
            (item for item in self.repository.user_businesses(user_id) if item.status in ("draft", "plan_ready")), None
        )
        if business is None:
            if self.repository.user_businesses(user_id):
                raise Blocked("Реклама уже запущена. Сведения о бизнесе меняются в настройках.")
            business = Business(user_id=user_id, site_url=checked, city=city, month_budget_kop=month_budget_kop)
        elif business.site_url != checked:
            business.description = {}
            business.description_approved_at = None
            business.metrica_counter_id = None
            business.lead_goal_ids = []
            business.medical_level = "none"
            business.niche_class = business.niche_text = None
        business.site_url = checked
        business.city = city
        business.month_budget_kop = month_budget_kop
        business.avg_check_kop = avg_check_kop
        self.repository.add(business)
        return business_view(business)

    async def site_check(self, business_id: int, now: datetime, today: date, *, owner_text: str | None = None) -> dict:
        business = self.repository.business(business_id)
        cached = business.description.get("check")
        if (
            cached
            and owner_text is None
            and cached["site_url"] == business.site_url
            and now - datetime.fromisoformat(cached["checked_at"]) < CHECK_TTL
        ):
            return self.check_view(business, cached)
        site = await self.reader(business.site_url)
        llm = SpendLLM(
            self.llm, self.repository, business.id, ai_budget(self.repository, business, self.limits, today), "site"
        )
        try:
            draft = await extract_description(site, llm, owner_text=owner_text)
        except YandexGPTError:
            draft = DescriptionDraft(questions=[OwnerQuestion("description", QUESTIONS["services"])])
        fields = description_fields(draft)
        check = {
            "site_url": business.site_url,
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
        description["site_medical"] = draft.medical.level
        business.description = description
        business.medical_level = worst_medical(draft.medical.level, business.medical_level)
        self.apply_niche(business)
        self.repository.commit()
        return self.check_view(business, check)

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
        business = self.repository.business(business_id)
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
        business = self.repository.business(business_id)
        goals = {item["id"] for item in (await self.goals(business_id, counter_id))["items"]}
        if not goal_ids or not set(goal_ids) <= goals:
            raise OnboardingError("Выберите хотя бы одну цель выбранного счётчика.")
        site_counters = business.description.get("check", {}).get("counter_ids", [])
        if site_counters and counter_id not in site_counters:
            raise OnboardingError("Выбранный счётчик не совпадает со счётчиком на сайте. Вернитесь к выбору счётчика.")
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
        if not valid_inn(inn):
            raise OnboardingError("Проверьте ИНН: нужны 10 или 12 цифр с верной контрольной суммой.")
        if len(inn) != (10 if subject_type == "ooo" else 12):
            raise OnboardingError("Для ООО укажите ИНН из 10 цифр, для ИП, самозанятого и физлица — из 12.")
        profile = self.repository.marking(business_id) or MarkingProfile(business_id=business_id)
        profile.subject_type = subject_type
        profile.inn_enc = inn
        profile.name = name
        profile.phone_enc = normalize_phone(phone)
        profile.confirmed_in_platform = {platform: confirmed.get(platform) is True for platform in ("direct", "vk")}
        self.repository.add(profile)
        return {"confirmed_in_platform": profile.confirmed_in_platform}
