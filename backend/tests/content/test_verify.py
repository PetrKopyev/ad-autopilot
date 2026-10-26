import json

import pytest

from app.content.site_reader import Page
from app.content.verify import MAX_GAP, find_quote, supports_price, verify_description


def description(**changes: object) -> str:
    data = {
        "services": [{"name": "Маникюр", "price": 150050, "quote": "Маникюр — 1 500,50 ₽."}],
        "address": {"value": "Самара, ул. Полевая, д. 7", "quote": "Адрес: г. Самара, ул. Полевая, д. 7."},
        "advantages": [{"value": "Работаем ежедневно", "quote": "Работаем ежедневно с 09:00 до 21:00."}],
    }
    data.update(changes)
    return json.dumps(data, ensure_ascii=False)


@pytest.fixture
def pages() -> list[Page]:
    return [
        Page("https://salon.example/prices", "Маникюр — 1 500,50 ₽."),
        Page("https://salon.example/contacts", "Адрес: г. Самара, ул. Полевая, д. 7."),
        Page("https://salon.example/", "Работаем ежедневно с 09:00 до 21:00."),
    ]


def test_valid_description_and_integer_kopecks(pages: list[Page]) -> None:
    draft = verify_description(description(), pages)
    assert draft.questions == []
    assert draft.services[0].price == 150050
    assert type(draft.services[0].price) is int
    assert draft.services[0].source == pages[0].url
    assert draft.address.value == "Самара, ул. Полевая, д. 7"
    assert draft.address.source == pages[1].url
    assert draft.advantages[0].source == pages[2].url


def test_fabricated_quotes_become_questions(pages: list[Page]) -> None:
    draft = verify_description(
        description(
            services=[{"name": "Педикюр", "price": 100000, "quote": "Педикюр стоит 1000 рублей"}],
            address={"value": "Москва, Ленина 3", "quote": "Москва, Ленина 3"},
            advantages=[{"value": "Бесплатная парковка", "quote": "Бесплатная парковка"}],
        ),
        pages,
    )
    assert draft.services == []
    assert draft.address is None
    assert draft.advantages == []
    assert [question.field for question in draft.questions] == ["services.0", "address", "advantages.0"]


def test_existing_quote_cannot_support_fabricated_values(pages: list[Page]) -> None:
    draft = verify_description(
        description(
            services=[{"name": "Педикюр", "price": 150050, "quote": pages[0].text}],
            address={"value": "Москва, ул. Полевая, д. 99", "quote": pages[1].text},
            advantages=[{"value": "Бесплатная парковка", "quote": pages[2].text}],
        ),
        pages,
    )
    assert draft.services == []
    assert draft.address is None
    assert draft.advantages == []
    assert len(draft.questions) == 3


def test_unverified_price_does_not_discard_verified_service(pages: list[Page]) -> None:
    draft = verify_description(
        description(
            services=[{"name": "Маникюр", "price": 150051, "quote": pages[0].text}],
        ),
        pages,
    )
    assert draft.services[0].name == "Маникюр"
    assert draft.services[0].price is None
    assert [question.field for question in draft.questions] == ["services.0.price"]


@pytest.mark.parametrize("price", [True, -1, 150050.0, "150050"])
def test_strict_price_schema(price: object, pages: list[Page]) -> None:
    draft = verify_description(
        description(
            services=[{"name": "Маникюр", "price": price, "quote": pages[0].text}],
        ),
        pages,
    )
    assert draft.services == []
    assert [question.field for question in draft.questions] == ["services.0"]


def test_unknown_price_is_allowed(pages: list[Page]) -> None:
    draft = verify_description(
        description(
            services=[{"name": "Маникюр", "price": None, "quote": pages[0].text}],
        ),
        pages,
    )
    assert draft.services[0].price is None
    assert [question.field for question in draft.questions] == ["services.0.price"]


@pytest.mark.parametrize(
    "response",
    [
        "not JSON",
        "```json\n{}\n```",
        "[]",
        "null",
        "{}",
        '{"services":[],"address":null,"advantages":[],"extra":1}',
        '{"services":[],"services":[],"address":null,"advantages":[]}',
        '{"services":[],"address":null,"advantages":[NaN]}',
        '{"services":{},"address":null,"advantages":[]}',
    ],
)
def test_invalid_json_becomes_owner_question(response: str, pages: list[Page]) -> None:
    draft = verify_description(response, pages)
    assert draft.services == []
    assert draft.address is None
    assert draft.advantages == []
    assert [question.field for question in draft.questions] == ["description"]


def test_empty_fields_become_questions(pages: list[Page]) -> None:
    draft = verify_description(description(services=[], address={"value": "", "quote": ""}, advantages=[]), pages)
    assert [question.field for question in draft.questions] == ["services", "address", "advantages"]


def test_extra_field_in_fact_rejected(pages: list[Page]) -> None:
    draft = verify_description(description(address={"value": "Самара", "quote": pages[1].text, "extra": 1}), pages)
    assert draft.address is None
    assert draft.questions[0].field == "address"


def test_quote_normalizes_whitespace_quotes_dashes_and_yo() -> None:
    pages = [Page("source", "Студия «Притяжение» — всё\nпо записи.")]
    assert find_quote('студия "Притяжение" - все по записи.', pages) == "source"


def test_ellipsis_with_bounded_gap() -> None:
    first, last = "Работаем ежедневно", "Запись по телефону"
    assert find_quote(f"{first}…{last}", [Page("source", f"{first} в Самаре. {last}")]) == "source"
    assert find_quote(f"{first}...{last}", [Page("source", first + "x" * (MAX_GAP + 1) + last)]) is None


def test_quote_cannot_span_pages_or_use_tiny_fragments() -> None:
    assert (
        find_quote(
            "Работаем ежедневно ... Запись по телефону",
            [
                Page("a", "Работаем ежедневно"),
                Page("b", "Запись по телефону"),
            ],
        )
        is None
    )
    assert find_quote("да ... нет", [Page("a", "да и нет")]) is None
    assert find_quote("", [Page("a", "some text")]) is None


def test_quote_search_tries_later_occurrences() -> None:
    quote = "Работаем ежедневно ... Запись по телефону"
    text = "Работаем ежедневно" + "x" * (MAX_GAP + 1) + "Работаем ежедневно. Запись по телефону"
    assert find_quote(quote, [Page("a", text)]) == "a"


@pytest.mark.parametrize(
    "quote, price, expected",
    [
        ("Маникюр — 1 500,50 ₽.", 150050, True),
        ("Маникюр — 1500 рублей.", 150000, True),
        ("Маникюр — 1500руб.", 150000, True),
        ("Маникюр: цена от 1500", 150000, True),
        ("Маникюр: стоимость — 1500", 150000, True),
        ("Маникюр — 0 ₽.", 0, True),
        ("Маникюр в 2026 году.", 202600, False),
        ("Маникюр — 1500 ₽, скидка 20%.", 2000, False),
        ("Маникюр длится 60 минут.", 6000, False),
        ("Маникюр — 1 500,50 ₽.", 150051, False),
    ],
)
def test_price_requires_amount_and_price_context(quote: str, price: int, expected: bool) -> None:
    assert supports_price(price, quote) == expected
