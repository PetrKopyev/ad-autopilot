import json
from dataclasses import dataclass, field, replace

import httpx
import pytest
import respx

from app.content.ad_text import (
    ANGLES,
    DIRECT_TEXT_LIMIT,
    DIRECT_TITLE2_LIMIT,
    DIRECT_TITLE_LIMIT,
    MEDICAL_DISCLAIMER,
    SEGMENTS,
    TITLE_FORMS,
    VK_TEXT_LIMIT,
    VK_TITLE_LIMIT,
    AdText,
    _glued_segment,
    _names,
    _titles,
    generate_ad_text,
    generate_matrix,
    offer_matrix,
    validate_ad_text,
)
from app.content.llm import FakeLLM
from app.content.medical import MedicalResult
from app.content.verify import DescriptionDraft, Fact, Service
from app.content.yandexgpt import COMPLETION_URL, TokenTariff, YandexGPTClient


@pytest.fixture
def description() -> DescriptionDraft:
    return DescriptionDraft(
        services=[Service("Маникюр", 150050, "На сайте также была скидка 25%", "https://source.example/42")],
        address=Fact("Самара, ул. Полевая, д. 7", "Ленина, д. 9", "https://source.example"),
        advantages=[
            Fact("Работаем ежедневно", "Было 24 часа", "https://source.example"),
            Fact("Телефон +7 (999) 123-45-67", "", ""),
            Fact("Почта hello@salon.example", "", ""),
            Fact("Сайт https://salon.example", "", ""),
        ],
    )


def errors(ad: AdText, description: DescriptionDraft, platform: str = "direct", medical: bool = False) -> list[str]:
    return validate_ad_text(ad, description, platform=platform, medical=medical)


def response(title: str = "Маникюр", text: str = "Работаем ежедневно", title2: str = "") -> str:
    return json.dumps({"title": title, "text": text, "title2": title2}, ensure_ascii=False)


@dataclass
class SequenceLLM:
    responses: list[str]
    calls: list[tuple[str, str]] = field(default_factory=list)

    async def complete_json(self, system: str, user: str, temperature: float = 0.2) -> str:
        self.calls.append((system, user))
        return self.responses[len(self.calls) - 1]


@pytest.mark.parametrize(
    "platform,title_limit,title2_limit,text_limit",
    [
        ("direct", DIRECT_TITLE_LIMIT, DIRECT_TITLE2_LIMIT, DIRECT_TEXT_LIMIT),
        ("vk", VK_TITLE_LIMIT, 0, VK_TEXT_LIMIT),
    ],
)
def test_platform_length_boundaries(
    description: DescriptionDraft,
    platform: str,
    title_limit: int,
    title2_limit: int,
    text_limit: int,
) -> None:
    ad = AdText("я" * title_limit, "я" * text_limit, "я" * title2_limit)
    assert not errors(ad, description, platform)
    assert "title_length" in errors(replace(ad, title=ad.title + "я"), description, platform)
    assert "title2_length" in errors(replace(ad, title2=ad.title2 + "я"), description, platform)
    assert "text_length" in errors(replace(ad, text=ad.text + "я"), description, platform)


@pytest.mark.parametrize("text", ["Всего 999 ₽", "Скидка 25%", "Работаем 24 часа", "Уже 42 клиента", "Цена1500₽"])
def test_invented_numbers_and_source_metadata_are_not_allowed(description: DescriptionDraft, text: str) -> None:
    assert "unapproved_number" in errors(AdText("Маникюр", text), description)


@pytest.mark.parametrize("text", ["Цена 1 500,50 ₽", "Стоимость 1500.50 ₽", "Самара, ул. Полевая, д. 7"])
def test_approved_price_uses_rubles_and_address_is_allowed(description: DescriptionDraft, text: str) -> None:
    assert not errors(AdText("Маникюр", text), description)


def test_approved_address_without_space_after_street_prefix(description: DescriptionDraft) -> None:
    description.address.value = "Самара, ул.Полевая, д. 7"
    assert not errors(AdText("Маникюр", description.address.value), description)


@pytest.mark.parametrize(
    "text",
    [
        "Телефон +7 (999) 123-45-76",
        "Телефон +7 123 999 45 67",
        "hello@invented.example",
        "https://invented.example",
        "https://salon.example/secret",
        "@invented_contact",
    ],
)
def test_fabricated_contacts_including_rearranged_phone_digits(description: DescriptionDraft, text: str) -> None:
    assert {"unapproved_phone", "unapproved_contact"} & set(errors(AdText("Маникюр", text), description))


@pytest.mark.parametrize(
    "text", ["Телефон +7 999 123 45 67", "Телефон 8 (999) 123-45-67", "hello@salon.example", "https://salon.example"]
)
def test_approved_contacts(description: DescriptionDraft, text: str) -> None:
    assert not errors(AdText("Маникюр", text), description)


def test_local_phone_is_checked_as_a_whole(description: DescriptionDraft) -> None:
    description.advantages.append(Fact("Телефон 123-45-67", "", ""))
    assert not errors(AdText("Маникюр", "Телефон 123-45-67"), description)
    assert "unapproved_phone" in errors(AdText("Маникюр", "Телефон 123-67-45"), description)
    assert "unapproved_number" in errors(AdText("Маникюр", "Цена 123 ₽"), description)


def test_numbers_inside_approved_url_are_not_price_facts(description: DescriptionDraft) -> None:
    description.advantages.append(Fact("Сайт https://salon.example/2026", "", ""))
    assert not errors(AdText("Маникюр", "https://salon.example/2026"), description)
    assert "unapproved_number" in errors(AdText("Маникюр", "Всего 2026 ₽"), description)


@pytest.mark.parametrize(
    "text",
    [
        "ул. Ленина, д. 7",
        "Ленина 7",
        "Мира, 7",
        "Адрес: улица Новая",
        "проспект Мира, 7",
        "ул. Полевая, д. 7а",
        "ул.Новая",
        "ленина 7",
        "Самара, ул. Полевая, д. 7а",
    ],
)
def test_fabricated_addresses_with_approved_house_number(description: DescriptionDraft, text: str) -> None:
    assert "unapproved_address" in errors(AdText("Маникюр", text), description)


@pytest.mark.parametrize(
    "word",
    [
        "Лучший",
        "лучшая",
        "Гарантия",
        "гарантируем",
        "лечим",
        "излечим",
        "№1",
        "№ 1",
        "100%",
        "100 %",
        "без противопоказаний",
    ],
)
def test_stop_words_even_if_present_in_description(description: DescriptionDraft, word: str) -> None:
    description.advantages.append(Fact(word, "", ""))
    assert "stop_words" in errors(AdText("Маникюр", word), description)


def test_exclamations_across_fields_and_all_caps(description: DescriptionDraft) -> None:
    assert "exclamation_limit" in errors(AdText("Маникюр!", "Запишитесь!"), description)
    assert not errors(AdText("Маникюр", "Запишитесь!"), description)
    assert "all_caps" in errors(AdText("МАНИКЮР", "Запишитесь"), description)
    assert "all_caps" in errors(AdText("Маникюр", "Запись ОНЛАЙН"), description)


def test_medical_disclaimer_validation(description: DescriptionDraft) -> None:
    assert "medical_disclaimer" in errors(AdText("Маникюр", "Запись"), description, medical=True)
    assert not errors(AdText("Маникюр", MEDICAL_DISCLAIMER), description, medical=True)


async def test_two_retries_with_validation_feedback(description: DescriptionDraft) -> None:
    llm = SequenceLLM([response(text="Цена 999 ₽"), response(text="Лучший маникюр"), response()])
    variant = await generate_ad_text(llm, description, platform="direct", angle="цена", segment="к празднику")
    assert variant.ad == AdText("Маникюр", "Работаем ежедневно")
    assert variant.attempts == len(llm.calls) == 3
    assert not variant.used_fallback
    assert json.loads(llm.calls[1][1])["previous_errors"] == ["число 999 не из фактов — бери числа только из фактов"]
    assert json.loads(llm.calls[2][1])["previous_errors"] == ["слово «лучший» запрещено — убери его"]
    facts = json.loads(llm.calls[0][1])["facts"]
    assert "Маникюр — 1 500,50 ₽" in facts
    assert not any("source.example" in fact or "25%" in fact for fact in facts)


@pytest.mark.parametrize(
    "bad_response",
    ["not json", "[]", "{}", '{"title": 1, "text": "Запись"}', response(text="я" * 91), response(text="Скидка 90%")],
)
async def test_invalid_responses_fall_back_after_three_calls(description: DescriptionDraft, bad_response: str) -> None:
    llm = FakeLLM(bad_response)
    variant = await generate_ad_text(llm, description, platform="vk", angle="акция", segment="к празднику")
    assert variant.used_fallback
    assert variant.attempts == len(llm.calls) == 3
    assert variant.ad == AdText("Маникюр к празднику", "Маникюр — 1 500,50 ₽. Работаем ежедневно.")
    assert not errors(variant.ad, description, "vk")


@pytest.mark.parametrize("level", ["none", "maybe", "required"])
async def test_medical_flag_and_classification(description: DescriptionDraft, level: str) -> None:
    description.medical = MedicalResult(level, [])
    variant = await generate_ad_text(
        FakeLLM(response(text="Запись.")),
        description,
        platform="direct",
        angle="удобство",
        segment="консультация",
        medical=level == "none",
    )
    assert not variant.used_fallback
    assert variant.ad.text.count(MEDICAL_DISCLAIMER) == 1
    assert not errors(variant.ad, description, medical=True)


async def test_medical_cannot_be_disabled_and_fallback_fits(description: DescriptionDraft) -> None:
    description.medical = MedicalResult("required", [])
    llm = FakeLLM(response(text="я" * DIRECT_TEXT_LIMIT))
    variant = await generate_ad_text(
        llm,
        description,
        platform="direct",
        angle="результат",
        segment="консультация",
        medical=False,
    )
    assert variant.used_fallback
    assert MEDICAL_DISCLAIMER in variant.ad.text
    assert not errors(variant.ad, description, medical=True)


async def test_existing_disclaimer_is_not_duplicated(description: DescriptionDraft) -> None:
    variant = await generate_ad_text(
        FakeLLM(response(text=MEDICAL_DISCLAIMER)),
        description,
        platform="vk",
        angle="удобство",
        segment="консультация",
        medical=True,
    )
    assert variant.ad.text == MEDICAL_DISCLAIMER
    assert not variant.used_fallback


async def test_unsafe_description_never_bypasses_fallback_checks(description: DescriptionDraft) -> None:
    description.services[0].name = "ЛУЧШИЙ МАНИКЮР!" * 20
    description.advantages = [Fact("Гарантия результата!", "", "")]
    variant = await generate_ad_text(FakeLLM("{}"), description, platform="vk", angle="цена", segment="запись")
    assert variant.used_fallback
    assert not errors(variant.ad, description, "vk")


@pytest.mark.parametrize("niche", list(SEGMENTS))
def test_matrix_assigns_each_angle_and_segment(niche: str) -> None:
    matrix = offer_matrix(niche)
    assert len(matrix) == len(ANGLES) * len(SEGMENTS[niche])
    assert set(matrix) == {(angle, segment) for angle in ANGLES for segment in SEGMENTS[niche]}
    assert offer_matrix("unknown") == offer_matrix("other")


async def test_generate_matrix_preserves_ab_angle_and_segment(description: DescriptionDraft) -> None:
    llm = FakeLLM(response(text="Самара, ул. Полевая, д. 7"))
    variants = await generate_matrix(llm, description, platform="direct", niche_class="nails")
    assert [(variant.angle, variant.segment) for variant in variants] == offer_matrix("nails")
    assert len(llm.calls) == len(variants)
    for variant, (_, user) in zip(variants, llm.calls, strict=True):
        data = json.loads(user)
        assert (data["angle"], data["segment"]) == (variant.angle, variant.segment)


@respx.mock
async def test_every_paid_generation_is_returned_for_caller_accounting(description: DescriptionDraft) -> None:
    def api_response(text: str) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "result": {
                    "alternatives": [{"message": {"text": text}, "status": "ALTERNATIVE_STATUS_FINAL"}],
                    "usage": {"inputTextTokens": "10", "completionTokens": "5", "totalTokens": "15"},
                }
            },
        )

    respx.post(COMPLETION_URL).mock(
        side_effect=[
            api_response(response(text="Телефон +7 123 999 45 67")),
            api_response(response(text="Скидка 99%")),
            api_response(response()),
        ]
    )
    async with httpx.AsyncClient() as http:
        llm = YandexGPTClient(
            http, api_key="test", folder_id="test", model="yandexgpt/latest", tariff=TokenTariff(20, 40)
        )
        variant = await generate_ad_text(llm, description, platform="vk", angle="цена", segment="к празднику")
    assert len(variant.llm_calls) == 3
    assert sum(call.cost_kop for call in variant.llm_calls) == 3
    assert sum(call.tokens_in for call in variant.llm_calls) == 30
    assert not variant.used_fallback


@respx.mock
async def test_content_filter_fallback_keeps_usage(description: DescriptionDraft) -> None:
    post = respx.post(COMPLETION_URL).respond(
        200,
        json={
            "result": {
                "alternatives": [{"message": {}, "status": "ALTERNATIVE_STATUS_CONTENT_FILTER"}],
                "usage": {"inputTextTokens": "10", "completionTokens": "0", "totalTokens": "10"},
            }
        },
    )
    async with httpx.AsyncClient() as http:
        llm = YandexGPTClient(
            http, api_key="test", folder_id="test", model="yandexgpt/latest", tariff=TokenTariff(20, 40)
        )
        variant = await generate_ad_text(llm, description, platform="vk", angle="цена", segment="запись")
    assert variant.used_fallback
    assert post.call_count == 1
    assert len(variant.llm_calls) == 1
    assert variant.llm_calls[0].cost_kop == 1
    assert variant.llm_calls[0].status == "ALTERNATIVE_STATUS_CONTENT_FILTER"


@pytest.fixture
def salon() -> DescriptionDraft:
    """Как в живом прогоне: пять услуг с ценами, адрес и отличия."""
    names = ["Маникюр с покрытием гель-лаком", "Педикюр", "Наращивание ресниц", "Окрашивание бровей", "Маникюр"]
    return DescriptionDraft(
        services=[Service(name, None, name, "owner") for name in names],
        address=Fact("Самара, ул. Полевая, д. 7", "", "owner"),
        advantages=[
            Fact("Маникюр с покрытием гель-лаком — 2 000 ₽", "", "owner"),
            Fact("Педикюр — 2 500 ₽", "", "owner"),
            Fact("Наращивание ресниц — 3 000 ₽", "", "owner"),
            Fact("Окрашивание бровей — 800 ₽", "", "owner"),
            Fact("Маникюр — 1 200 ₽", "", "owner"),
            Fact("Стерильные инструменты", "", "owner"),
            Fact("Гарантия на покрытие 7 дней", "", "owner"),
        ],
    )


async def test_long_text_gets_explained_in_words_and_next_attempt_passes(salon: DescriptionDraft) -> None:
    listing = ", ".join(fact.value for fact in salon.advantages[:5])
    llm = SequenceLLM([response("Маникюр", listing), response("Педикюр", "Педикюр — 2 500 ₽. Стерильные инструменты")])
    variant = await generate_ad_text(
        llm, salon, platform="vk", angle="цена", segment="к празднику", service=salon.services[1]
    )
    assert not variant.used_fallback and variant.attempts == 2
    assert json.loads(llm.calls[1][1])["previous_errors"] == [
        f"текст {len(listing)} символов, нужно не больше {VK_TEXT_LIMIT} — оставь одну услугу и одно отличие"
    ]
    system = llm.calls[0][0]
    assert f"не длиннее {VK_TITLE_LIMIT} символов" in system and f"не длиннее {VK_TEXT_LIMIT} символов" in system
    assert "title2 оставь пустым" in system


async def test_focus_facts_hold_one_service_and_its_price(salon: DescriptionDraft) -> None:
    llm = FakeLLM(response("Педикюр", "Стерильные инструменты"))
    await generate_ad_text(
        llm, salon, platform="direct", angle="цена", segment="к празднику", service=salon.services[1]
    )
    facts = json.loads(llm.calls[0][1])["facts"]
    assert facts == ["Педикюр", "Самара, ул. Полевая, д. 7", "Педикюр — 2 500 ₽", "Стерильные инструменты",
                     "Гарантия на покрытие 7 дней"]
    llm = FakeLLM(response("Маникюр", "Стерильные инструменты"))
    await generate_ad_text(
        llm, salon, platform="direct", angle="цена", segment="к празднику", service=salon.services[4]
    )
    assert "Маникюр — 1 200 ₽" in json.loads(llm.calls[0][1])["facts"]
    assert "Маникюр с покрытием гель-лаком — 2 000 ₽" not in json.loads(llm.calls[0][1])["facts"]
    assert f"не длиннее {DIRECT_TITLE2_LIMIT} символов" in llm.calls[0][0]


async def test_fact_check_still_rejects_invented_price_for_focused_service(salon: DescriptionDraft) -> None:
    llm = FakeLLM(response("Ухоженные ноги", "Педикюр — 1 990 ₽"))
    variant = await generate_ad_text(llm, salon, platform="vk", angle="цена", segment="к празднику",
                                     service=salon.services[1])
    assert variant.used_fallback and len(llm.calls) == 3
    assert json.loads(llm.calls[1][1])["previous_errors"] == ["число 1990 не из фактов — бери числа только из фактов"]


async def test_temperature_rises_on_retry(description: DescriptionDraft) -> None:
    llm = FakeLLM(response(text="я" * 200))
    await generate_ad_text(llm, description, platform="vk", angle="цена", segment="к празднику")
    assert llm.temperatures[0] == 0.2 and 0.5 <= llm.temperatures[1] <= llm.temperatures[2] <= 0.7


async def test_second_ad_of_group_differs_from_first(salon: DescriptionDraft) -> None:
    first = AdText("Педикюр", "Стерильные инструменты")
    llm = SequenceLLM(
        [
            response("Педикюр рядом", " стерильные  инструменты"),
            response("Педикюр", "Педикюр — 2 500 ₽"),
            response("Педикюр к празднику", "Ухоженные ноги на праздники — 2 500 ₽"),
        ]
    )
    variant = await generate_ad_text(
        llm, salon, platform="vk", angle="результат", segment="к празднику", service=salon.services[1], other=first
    )
    assert variant.ad.text == "Ухоженные ноги на праздники — 2 500 ₽" and variant.attempts == 3
    assert json.loads(llm.calls[0][1])["other_ad"] == {"title": "Педикюр", "text": "Стерильные инструменты"}
    assert "текст « стерильные  инструменты» совпал" in json.loads(llm.calls[1][1])["previous_errors"][0]
    assert "заголовок «Педикюр» совпал" in json.loads(llm.calls[2][1])["previous_errors"][0]


async def test_fallback_takes_group_service_and_differs_from_other(salon: DescriptionDraft) -> None:
    llm = FakeLLM("{}")
    first = await generate_ad_text(llm, salon, platform="vk", angle="цена", segment="к празднику",
                                   service=salon.services[2])
    second = await generate_ad_text(llm, salon, platform="vk", angle="результат", segment="к празднику",
                                    service=salon.services[2], other=first.ad)
    assert first.used_fallback and second.used_fallback
    assert first.ad == AdText("Наращивание ресниц", "Наращивание ресниц — 3 000 ₽. Стерильные инструменты.")
    # «Наращивание ресниц к празднику» не влезает в 25 знаков VK: тот же заголовок с другим текстом лучше голого повода.
    assert second.ad == AdText("Наращивание ресниц", "Наращивание ресниц — 3 000 ₽")
    assert all("Гарантия" not in variant.ad.text for variant in (first, second))
    assert not errors(first.ad, salon, "vk") and not errors(second.ad, salon, "vk")


async def test_near_home_ad_names_location_from_facts(salon: DescriptionDraft) -> None:
    salon.advantages.append(Fact("5 минут от метро Сокольники", "", "owner"))
    llm = SequenceLLM(
        [
            response("Педикюр рядом с домом", "Стерильные инструменты"),
            response("Педикюр", "Самара, ул. Сокольническая, д. 7"),
            response("Педикюр рядом", "5 минут от метро Сокольники"),
        ]
    )
    variant = await generate_ad_text(
        llm, salon, platform="vk", angle="удобство", segment="рядом с домом", service=salon.services[1]
    )
    assert variant.ad.text == "5 минут от метро Сокольники" and variant.attempts == 3
    data = json.loads(llm.calls[0][1])
    assert data["location"] == ["Самара, ул. Полевая, д. 7", "5 минут от метро Сокольники"]
    assert "адрес" in data["segment_hint"] and "удобство" in data["angle_hint"]
    assert "нет адреса" in json.loads(llm.calls[1][1])["previous_errors"][0]
    assert "адрес не из фактов" in " ".join(json.loads(llm.calls[2][1])["previous_errors"])


async def test_near_home_fallback_leads_with_address(salon: DescriptionDraft) -> None:
    llm = FakeLLM(response("Педикюр", "Стерильные инструменты"))
    first = await generate_ad_text(llm, salon, platform="vk", angle="цена", segment="рядом с домом",
                                   service=salon.services[1])
    assert first.used_fallback and first.ad.text == "Самара, ул. Полевая, д. 7. Педикюр — 2 500 ₽."
    assert not errors(first.ad, salon, "vk")


@pytest.fixture
def sokolniki() -> DescriptionDraft:
    """Как на стенде: адрес — город и район, у второго объявления группы был запасной шаблон с голым адресом."""
    return DescriptionDraft(
        services=[
            Service("Снятие покрытия", 50000, "", "owner"),
            Service("Маникюр без покрытия", 150000, "", "owner"),
            Service("Маникюр с покрытием гель-лаком", None, "", "owner"),
        ],
        address=Fact("Москва, Сокольники", "", "owner"),
        advantages=[
            Fact("Маникюр с покрытием гель-лаком — 2 200 ₽", "", "owner"),
            Fact("Мастера со стажем от 4 лет", "", "owner"),
        ],
    )


@pytest.mark.parametrize(
    "text",
    ["Бережно снимем гель-лак в Сокольниках.", "Снятие покрытия в Москве, Сокольники.", "Москва, Сокольники. Запись."],
)
async def test_location_counts_in_declined_form(sokolniki: DescriptionDraft, text: str) -> None:
    llm = FakeLLM(response("Ногти отдохнут", text))
    variant = await generate_ad_text(llm, sokolniki, platform="vk", angle="результат", segment="рядом с домом",
                                     service=sokolniki.services[0])
    assert not variant.used_fallback and variant.attempts == 1
    assert "«Москва, Сокольники. …»" in json.loads(llm.calls[0][1])["segment_hint"]


@pytest.mark.parametrize("text", ["Бережно снимем гель-лак в Москве.", "Мастера со стажем от 4 лет."])
async def test_city_alone_is_not_a_location(sokolniki: DescriptionDraft, text: str) -> None:
    llm = FakeLLM(response("Ногти отдохнут", text))
    variant = await generate_ad_text(llm, sokolniki, platform="vk", angle="результат", segment="рядом с домом",
                                     service=sokolniki.services[0])
    assert variant.used_fallback
    assert "нет адреса" in json.loads(llm.calls[1][1])["previous_errors"][0]


@pytest.mark.parametrize("service_index,title", [(0, "Снятие покрытия"), (1, "Маникюр рядом с домом")])
async def test_near_home_fallback_is_not_bare_address_and_titles_service(
    sokolniki: DescriptionDraft, service_index: int, title: str
) -> None:
    service = sokolniki.services[service_index]
    first = AdText(service.name, f"{service.name} в Москве, Сокольники")
    variant = await generate_ad_text(FakeLLM("{}"), sokolniki, platform="vk", angle="результат",
                                     segment="рядом с домом", service=service, other=first)
    assert variant.used_fallback
    assert variant.ad.title == title
    assert variant.ad.text.startswith("Москва, Сокольники. ") and variant.ad.text != "Москва, Сокольники"
    assert not errors(variant.ad, sokolniki, "vk")


async def test_fallback_with_long_service_name_adds_advantage_to_price(sokolniki: DescriptionDraft) -> None:
    service = sokolniki.services[2]
    first = AdText("Маникюр гель-лаком", "Маникюр с покрытием гель-лаком — 2 200 ₽")
    variant = await generate_ad_text(FakeLLM("{}"), sokolniki, platform="vk", angle="результат",
                                     segment="к празднику", service=service, other=first)
    assert variant.ad == AdText(
        "Маникюр к празднику", "Маникюр с покрытием гель-лаком — 2 200 ₽. Мастера со стажем от 4 лет."
    )
    assert len(variant.ad.text) <= VK_TEXT_LIMIT and not errors(variant.ad, sokolniki, "vk")


async def test_holiday_is_an_allowed_occasion_not_a_fact(salon: DescriptionDraft) -> None:
    ad = AdText("Педикюр к празднику", "Педикюр — 2 500 ₽. На праздники")
    assert not errors(ad, salon, "vk")
    llm = FakeLLM(response("Педикюр к празднику", "Педикюр к 8 Марта со скидкой"))
    variant = await generate_ad_text(llm, salon, platform="vk", angle="акция", segment="к празднику",
                                     service=salon.services[1])
    assert variant.used_fallback
    system, user = llm.calls[0]
    assert "повод сегмента — не факт" in system and "праздник" in json.loads(user)["segment_hint"]


async def test_new_design_does_not_invent_design_service(salon: DescriptionDraft) -> None:
    llm = FakeLLM(response("Маникюр: новый образ", "Стерильные инструменты"))
    variant = await generate_ad_text(llm, salon, platform="vk", angle="результат", segment="новый дизайн",
                                     service=salon.services[4])
    assert not variant.used_fallback
    hint = json.loads(llm.calls[0][1])["segment_hint"]
    assert "новый образ" in hint and "только если она есть в фактах" in hint


@pytest.fixture
def owner() -> DescriptionDraft:
    """Как описание владельца на стенде: цены услуг ещё и строками отличий, отличия — одной строкой."""
    prices = {"Маникюр с покрытием гель-лаком": 220000, "Снятие покрытия": 40000}
    return DescriptionDraft(
        services=[Service(name, price, name, "owner") for name, price in prices.items()],
        address=Fact("Москва, Сокольники", "", "owner"),
        advantages=[
            Fact("Маникюр с покрытием гель-лаком — 2 200 ₽", "", "owner"),
            Fact("Снятие покрытия — 400 ₽", "", "owner"),
            Fact(
                "Мастера со стажем от 4 лет, работаем по записи без очередей. Гарантия на покрытие 7 дней.", "", "owner"
            ),
        ],
    )


async def test_price_is_written_with_thousands_like_owner_facts(owner: DescriptionDraft) -> None:
    llm = FakeLLM("{}")
    variant = await generate_ad_text(llm, owner, platform="vk", angle="цена", segment="к празднику",
                                     service=owner.services[0])
    facts = json.loads(llm.calls[0][1])["facts"]
    assert facts.count("Маникюр с покрытием гель-лаком — 2 200 ₽") == 1 and not any("2200" in fact for fact in facts)
    assert "2 200 ₽" in variant.ad.text and "2200" not in variant.ad.text


async def test_price_with_kopecks_from_owner_facts_is_not_repeated(owner: DescriptionDraft) -> None:
    service = replace(owner.services[0], price=150050)
    owner = replace(
        owner, services=[service], advantages=[Fact("Маникюр с покрытием гель-лаком — 1 500,50 ₽", "", "owner")]
    )
    llm = FakeLLM("{}")
    await generate_ad_text(llm, owner, platform="vk", angle="цена", segment="к празднику", service=service)
    facts = json.loads(llm.calls[0][1])["facts"]
    assert sum("1 500,50 ₽" in fact for fact in facts) == 1


async def test_fallback_does_not_repeat_price_from_owner_facts(owner: DescriptionDraft) -> None:
    first = AdText("Маникюр с гель-лаком", "Маникюр с покрытием гель-лаком — 2 200 ₽")
    variant = await generate_ad_text(FakeLLM("{}"), owner, platform="vk", angle="результат", segment="новый дизайн",
                                     service=owner.services[0], other=first)
    assert variant.used_fallback
    assert variant.ad == AdText(
        "Маникюр: новый образ", "Маникюр с покрытием гель-лаком — 2 200 ₽. Мастера со стажем от 4 лет."
    )
    assert not errors(variant.ad, owner, "vk")


async def test_near_home_fallback_differs_from_first_beyond_punctuation(owner: DescriptionDraft) -> None:
    first = AdText("Снятие покрытия", "Москва, Сокольники. Снятие покрытия — 400 ₽")
    variant = await generate_ad_text(FakeLLM("{}"), owner, platform="vk", angle="результат", segment="рядом с домом",
                                     service=owner.services[1], other=first)
    assert variant.used_fallback and variant.ad.title == "Снятие покрытия"
    assert variant.ad.text == "Москва, Сокольники. Мастера со стажем от 4 лет, работаем по записи без очередей."


@pytest.mark.parametrize(
    "text", ["москва сокольники: снятие покрытия 400 ₽!", "Москва, Сокольники. Снятие покрытия — 400₽."]
)
async def test_duplicate_ignores_punctuation_and_case(owner: DescriptionDraft, text: str) -> None:
    first = AdText("Снятие покрытия", "Москва, Сокольники. Снятие покрытия — 400 ₽")
    llm = FakeLLM(response("Ногти отдохнут", text))
    variant = await generate_ad_text(llm, owner, platform="vk", angle="результат", segment="рядом с домом",
                                     service=owner.services[1], other=first)
    assert variant.used_fallback
    assert "совпал с первым объявлением" in json.loads(llm.calls[1][1])["previous_errors"][0]


async def test_second_ad_takes_title_and_text_from_different_attempts(owner: DescriptionDraft) -> None:
    """Как на стенде: модель чинит названную ошибку и переписывает остальное — каждая попытка проваливается
    по-своему, хотя годный заголовок и годный текст у неё уже есть."""
    first = AdText("Снятие покрытия", "Москва, Сокольники. Снятие покрытия — 400 ₽")
    llm = SequenceLLM(
        [
            response("Снятие покрытия", "Москва, Сокольники. Лёгкая смена образа — снятие покрытия за 400 ₽"),
            response("Быстрое снятие покрытия", "Москва, Сокольники. Снятие покрытия — 400 ₽"),
            response("Снятие покрытия в Сокольниках", "Москва, Сокольники. После процедуры покрытие будет удалено"),
        ]
    )
    variant = await generate_ad_text(llm, owner, platform="vk", angle="результат", segment="рядом с домом",
                                     service=owner.services[1], other=first)
    assert not variant.used_fallback and variant.attempts == 3
    assert variant.ad == AdText(
        "Быстрое снятие покрытия", "Москва, Сокольники. Лёгкая смена образа — снятие покрытия за 400 ₽"
    )
    second = json.loads(llm.calls[1][1])
    assert second["previous_ad"]["title"] == "Снятие покрытия"
    assert "заголовок «Снятие покрытия» совпал" in second["previous_errors"][0]


async def test_duplicate_and_same_title_are_reported_together(owner: DescriptionDraft) -> None:
    first = AdText("Снятие покрытия", "Москва, Сокольники. Снятие покрытия — 400 ₽")
    llm = FakeLLM(response("Снятие покрытия.", "Москва, Сокольники. Снятие покрытия — 400 ₽"))
    await generate_ad_text(llm, owner, platform="vk", angle="результат", segment="рядом с домом",
                           service=owner.services[1], other=first)
    previous_errors = json.loads(llm.calls[1][1])["previous_errors"]
    assert len(previous_errors) == 2 and "текст «" in previous_errors[0] and "заголовок «" in previous_errors[1]


async def test_title_length_names_the_title_and_the_excess(owner: DescriptionDraft) -> None:
    llm = FakeLLM(response("Гель-лак для обновления образа", "Мастера со стажем от 4 лет"))
    await generate_ad_text(llm, owner, platform="vk", angle="результат", segment="новый дизайн",
                           service=owner.services[0])
    assert json.loads(llm.calls[1][1])["previous_errors"] == [
        "заголовок «Гель-лак для обновления образа» — 30 символов, нужно не больше 25: убери или замени слово, "
        "чтобы стало хотя бы на 5 символов короче"
    ]


async def test_near_copy_of_first_ad_gets_one_retry_then_least_similar_answer(sokolniki: DescriptionDraft) -> None:
    """Как на живом плане: второе объявление VK — первое с заменой одного слова. Повтор — один; оба ответа проходят
    правила площадки, поэтому идёт менее похожий, а не шаблон."""
    service = sokolniki.services[2]
    first = AdText("Гель-лак к празднику", "Маникюр с покрытием гель-лаком — 2 200 ₽. Обновите ваш образ!")
    llm = SequenceLLM(
        [
            response("Гель-лак на праздник", "Маникюр с покрытием гель-лаком — 2 200 ₽. Обновите ваш стиль!"),
            response("Маникюр: новый образ", "Маникюр с покрытием гель-лаком — 2 200 ₽. Обновите образ ногтей"),
        ]
    )
    variant = await generate_ad_text(llm, sokolniki, platform="vk", angle="результат", segment="новый дизайн",
                                     service=service, other=first)
    assert len(llm.calls) == 2 and not variant.used_fallback
    assert variant.ad == AdText(
        "Маникюр: новый образ", "Маникюр с покрытием гель-лаком — 2 200 ₽. Обновите образ ногтей"
    )
    previous_errors = json.loads(llm.calls[1][1])["previous_errors"]
    assert "почти повторяет текст other_ad" in previous_errors[0]
    assert "почти повторяет заголовок other_ad" in previous_errors[1]


async def test_similarity_ignores_medical_disclaimer(description: DescriptionDraft) -> None:
    description.medical = MedicalResult("required", [])
    first = AdText("Маникюр", f"Ежедневно. {MEDICAL_DISCLAIMER}")
    llm = FakeLLM(response("Запись открыта", "Без выходных."))
    variant = await generate_ad_text(llm, description, platform="direct", angle="результат", segment="к празднику",
                                     other=first)
    assert len(llm.calls) == 1 and variant.ad.text == f"Без выходных. {MEDICAL_DISCLAIMER}"


async def test_text_repeating_title_is_rewritten(sokolniki: DescriptionDraft) -> None:
    llm = SequenceLLM(
        [
            response("Маникюр с покрытием гель-лаком к празднику", "Маникюр с покрытием гель-лаком — 2 200 ₽"),
            response("Маникюр с покрытием гель-лаком к празднику", "Мастера со стажем от 4 лет. Запись онлайн"),
        ]
    )
    variant = await generate_ad_text(llm, sokolniki, platform="direct", angle="цена", segment="к празднику",
                                     service=sokolniki.services[2])
    assert not variant.used_fallback and variant.attempts == 2
    assert json.loads(llm.calls[1][1])["previous_errors"] == [
        "текст «Маникюр с покрытием гель-лаком — 2 200 ₽» повторяет заголовок «Маникюр с покрытием гель-лаком к "
        "празднику» — не повторяй в тексте слова заголовка, напиши то, чего в нём нет: отличие, цену или повод"
    ]


@pytest.mark.parametrize(
    "title", ["К празднику", "рядом с домом!", "Новый дизайн", "Новый образ", "Консультация", "Первое занятие"]
)
async def test_bare_occasion_title_is_rewritten_with_service(salon: DescriptionDraft, title: str) -> None:
    """Как на проде: заголовок VK — одно название повода, без группы непонятно, о чём объявление."""
    llm = SequenceLLM(
        [response(title, "Стерильные инструменты"), response("Педикюр к празднику", "Стерильные инструменты")]
    )
    variant = await generate_ad_text(llm, salon, platform="vk", angle="результат", segment="к празднику",
                                     service=salon.services[1])
    assert not variant.used_fallback and variant.ad.title == "Педикюр к празднику"
    second = json.loads(llm.calls[1][1])
    assert second["previous_errors"] == [
        f"заголовок «{title}» — одно название повода, без услуги он непонятен: назови услугу и повод одной фразой, "
        "как title_example"
    ]
    assert json.loads(llm.calls[0][1])["title_example"] == "Педикюр к празднику"


async def test_occasion_glued_to_service_is_rewritten(salon: DescriptionDraft) -> None:
    llm = SequenceLLM(
        [
            response("Педикюр новый дизайн", "Стерильные инструменты"),
            response("Педикюр: новый образ", "Стерильные инструменты"),
        ]
    )
    variant = await generate_ad_text(llm, salon, platform="direct", angle="результат", segment="новый дизайн",
                                     service=salon.services[1])
    assert variant.ad.title == "Педикюр: новый образ" and variant.attempts == 2
    assert "повод приклеен к услуге" in json.loads(llm.calls[1][1])["previous_errors"][0]
    assert json.loads(llm.calls[0][1])["title_example"] == "Педикюр: новый образ"


@pytest.mark.parametrize(
    "platform,name,segment,titles",
    [
        ("vk", "Маникюр с покрытием гель-лаком", "к празднику", ("Маникюр к празднику", "Маникюр к торжеству")),
        ("vk", "Маникюр без покрытия", "рядом с домом", ("Маникюр рядом с домом", "Маникюр недалеко от дома")),
        ("vk", "Педикюр с покрытием", "новый дизайн", ("Педикюр: новый образ", "Педикюр для нового образа")),
        (
            "direct",
            "Наращивание ногтей",
            "к празднику",
            ("Наращивание ногтей к празднику", "Наращивание ногтей: праздничный образ"),
        ),
        (
            "direct",
            "Педикюр с покрытием",
            "новый дизайн",
            ("Педикюр с покрытием: новый образ", "Педикюр: новый образ"),
        ),
    ],
)
async def test_fallback_titles_name_service_and_occasion(
    platform: str, name: str, segment: str, titles: tuple[str, str]
) -> None:
    """Группы плана с прода: шаблон давал «К празднику», «Рядом с домом», «Новый дизайн» и «Услуги и запись»."""
    draft = DescriptionDraft(
        services=[Service(name, 200000, name, "owner")],
        address=Fact("Москва, Сокольники", "", "owner"),
        advantages=[Fact("Мастера со стажем от 4 лет", "", "owner"), Fact("Стерильные инструменты", "", "owner")],
    )
    first = await generate_ad_text(FakeLLM("{}"), draft, platform=platform, angle="цена", segment=segment)
    second = await generate_ad_text(FakeLLM("{}"), draft, platform=platform, angle="результат", segment=segment,
                                    other=first.ad)
    assert (first.ad.title, second.ad.title) == titles
    assert not errors(first.ad, draft, platform) and not errors(second.ad, draft, platform)


@pytest.mark.parametrize(
    "title,segment,name",
    [
        ("Найдите время для себя", "время для себя", "Массаж"),
        ("Создайте новый образ", "новый образ", "Стрижка"),
        ("Запишитесь на первое занятие", "первое занятие", "Йога"),
        ("Профессиональный уход за бородой", "уход за бородой", "Стрижка"),
        ("Бесплатная консультация", "консультация", "Приём терапевта"),
        ("Закажите новый дизайн ногтей", "новый дизайн", "Маникюр"),
    ],
)
def test_occasion_in_own_phrase_is_not_glued(title: str, segment: str, name: str) -> None:
    """Нормальные заголовки модели, которые первая версия проверки считала склейкой."""
    assert not _glued_segment(title, segment, Service(name, None, name, "owner"))


@pytest.mark.parametrize(
    "title,segment,name",
    [
        ("Педикюр с покрытием новый дизайн", "новый дизайн", "Педикюр с покрытием"),
        ("Педикюр новый дизайн", "новый дизайн", "Педикюр с покрытием"),
        ("Стрижка уход за бородой", "уход за бородой", "Стрижка"),
    ],
)
def test_occasion_right_after_service_is_glued(title: str, segment: str, name: str) -> None:
    assert _glued_segment(title, segment, Service(name, None, name, "owner"))


@pytest.mark.parametrize(
    "name,names",
    [
        ("Окрашивание волос в один тон", ["Окрашивание волос в один тон", "Окрашивание волос"]),
        ("Массаж спины 60 минут", ["Массаж спины 60 минут"]),
        ("Абонемент на 8 занятий", ["Абонемент на 8 занятий", "Абонемент"]),
        ("Стрижка + борода", ["Стрижка + борода"]),
    ],
)
def test_service_name_is_cut_only_before_preposition(name: str, names: list[str]) -> None:
    assert _names(Service(name, None, name, "owner")) == names


@pytest.mark.parametrize("segment", sorted({segment for segments in SEGMENTS.values() for segment in segments}))
def test_every_segment_has_checked_title_forms(segment: str) -> None:
    """У каждого повода всех ниш — проверенные формы или явно пустые: тогда заголовок — услуга без повода."""
    service = Service("Стрижка", None, "Стрижка", "owner")
    assert segment in TITLE_FORMS
    titles = _titles(service, segment)
    if TITLE_FORMS[segment]:
        assert titles[0] == TITLE_FORMS[segment][0].format("Стрижка")
    else:
        assert titles == ["Стрижка", "Услуги и запись"]
