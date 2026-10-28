import json
import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Literal, Protocol

from app.content.medical import MedicalResult, classify

QUOTES = str.maketrans({char: '"' for char in "«»“”„‟″'`’‘"} | {char: "-" for char in "‐‑‒–—―−"})
NUMBER = re.compile(r"(?<!\w)\d+(?:[ \xa0\u202f]\d{3})*(?:[.,]\d+)?(?!\w)")
MONEY_NUMBER = r"\d+(?:[ \xa0\u202f]\d{3})*(?:[.,]\d{1,2})?"
MONEY = re.compile(
    rf"(?<![\w.,])(?P<amount>{MONEY_NUMBER})\s*(?:₽|руб(?:л[а-я]*)?\.?|р\.)(?!\w)|"
    rf"(?:цен[аы]|стоимость)\s*[:—–-]?\s*(?:от\s+)?(?P<labelled>{MONEY_NUMBER})(?![\w.,])",
    re.IGNORECASE,
)
MIN_QUOTE = 8
MIN_PART = 12
MAX_GAP = 200


class SourcePage(Protocol):
    url: str
    text: str


@dataclass
class Fact:
    value: str
    quote: str
    source: str


@dataclass
class Service:
    name: str
    price: int | None
    quote: str
    source: str


@dataclass
class OwnerQuestion:
    field: str
    question: str


@dataclass
class DescriptionDraft:
    services: list[Service] = field(default_factory=list)
    address: Fact | None = None
    advantages: list[Fact] = field(default_factory=list)
    questions: list[OwnerQuestion] = field(default_factory=list)
    source: Literal["site", "owner_text"] = "site"
    medical: MedicalResult = field(default_factory=lambda: MedicalResult("none", []))


def normalize(text: str) -> str:
    text = text.lower().replace("ё", "е").replace("…", "...").translate(QUOTES)
    return " ".join(text.split())


def find_quote(quote: str, pages: Sequence[SourcePage]) -> str | None:
    parts = [part.strip(' .,;:!?"-') for part in normalize(quote).split("...")]
    parts = [part for part in parts if part]
    if not parts or sum(map(len, parts)) < MIN_QUOTE:
        return None
    # Короткий кусок допустим, только если это цена: «Маникюр … 2 200 ₽».
    if len(parts) > 1 and any(len(part) < MIN_PART and not MONEY.search(part) for part in parts):
        return None
    # Кусок с числа не начинается внутри другого числа: «2 200 ₽» не находится в «12 200 ₽».
    patterns = [re.compile((r"(?<!\d)(?<!\d )" if part[0].isdigit() else "") + re.escape(part)) for part in parts]
    for page in pages:
        text = normalize(page.text)
        first = patterns[0].search(text)
        while first:
            position = first.end()
            for part, pattern in zip(parts[1:], patterns[1:], strict=True):
                found = pattern.search(text, position, position + MAX_GAP + len(part))
                # Цена внутри пропуска — признак соседней карточки: её цена не подтверждает эту услугу.
                if not found or MONEY.search(text, position, found.start()):
                    break
                position = found.end()
            else:
                return page.url
            first = patterns[0].search(text, first.start() + 1)
    return None


def numbers(text: str) -> set[Decimal]:
    return {Decimal(re.sub(r"\s", "", number).replace(",", ".")) for number in NUMBER.findall(text)}


def supports(value: str, quote: str) -> bool:
    words = {word[:5] for word in re.findall(r"[а-яa-z]{3,}", normalize(value))}
    quoted_words = {word[:5] for word in re.findall(r"[а-яa-z]{3,}", normalize(quote))}
    return bool(value.strip()) and words <= quoted_words and numbers(value) <= numbers(quote)


def supports_price(price: int, quote: str) -> bool:
    for match in MONEY.finditer(quote):
        amount = re.sub(r"\s", "", match.group("amount") or match.group("labelled")).replace(",", ".")
        if Decimal(amount) * 100 == price:
            return True
    return False


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Повторяющееся поле JSON")
        result[key] = value
    return result


def _invalid_constant(value: str) -> None:
    raise ValueError("Недопустимое число JSON")


def verify_description(
    response: str, pages: Sequence[SourcePage], *, source: Literal["site", "owner_text"] = "site"
) -> DescriptionDraft:
    draft = DescriptionDraft(source=source, medical=classify("\n".join(page.text for page in pages)))
    try:
        data = json.loads(
            response, object_pairs_hook=_unique_object, parse_float=Decimal, parse_constant=_invalid_constant
        )
    except (ValueError, TypeError, RecursionError):
        data = None
    if (
        not isinstance(data, dict)
        or set(data) != {"services", "address", "advantages"}
        or not isinstance(data["services"], list)
        or not isinstance(data["advantages"], list)
    ):
        draft.questions.append(OwnerQuestion("description", "Расскажите об услугах, ценах, адресе и отличиях бизнеса."))
        return draft

    for index, item in enumerate(data["services"]):
        key = f"services.{index}"
        if (
            not isinstance(item, dict)
            or set(item) != {"name", "price", "quote"}
            or not isinstance(item["name"], str)
            or not isinstance(item["quote"], str)
            or (item["price"] is not None and (type(item["price"]) is not int or item["price"] < 0))
        ):
            draft.questions.append(OwnerQuestion(key, "Уточните название и цену услуги в копейках."))
            continue
        url = find_quote(item["quote"], pages)
        if not url or not supports(item["name"], item["quote"]):
            draft.questions.append(OwnerQuestion(key, "Как называется услуга и сколько она стоит?"))
            continue
        price = item["price"]
        if price is None or not supports_price(price, item["quote"]):
            draft.questions.append(OwnerQuestion(f"{key}.price", f"Сколько стоит услуга «{item['name']}»?"))
            price = None
        draft.services.append(Service(item["name"], price, item["quote"], url))
    if not data["services"]:
        draft.questions.append(OwnerQuestion("services", "Какие услуги вы предлагаете и сколько они стоят?"))

    def verify_value(item: object, key: str, question: str) -> Fact | None:
        if (
            isinstance(item, dict)
            and set(item) == {"value", "quote"}
            and isinstance(item["value"], str)
            and isinstance(item["quote"], str)
        ):
            url = find_quote(item["quote"], pages)
            if url and supports(item["value"], item["quote"]):
                return Fact(item["value"], item["quote"], url)
        draft.questions.append(OwnerQuestion(key, question))
        return None

    draft.address = verify_value(data["address"], "address", "По какому адресу вы принимаете клиентов?")
    for index, item in enumerate(data["advantages"]):
        fact = verify_value(item, f"advantages.{index}", "Какое отличие бизнеса можно подтвердить?")
        if fact:
            draft.advantages.append(fact)
    if not data["advantages"]:
        draft.questions.append(OwnerQuestion("advantages", "Чем ваш бизнес отличается от других?"))
    return draft
