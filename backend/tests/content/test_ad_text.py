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
    VK_TEXT_LIMIT,
    VK_TITLE_LIMIT,
    AdText,
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

    async def complete_json(self, system: str, user: str) -> str:
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
    variant = await generate_ad_text(llm, description, platform="direct", angle="цена", segment="рядом с домом")
    assert variant.ad == AdText("Маникюр", "Работаем ежедневно")
    assert variant.attempts == len(llm.calls) == 3
    assert not variant.used_fallback
    assert "unapproved_number" in json.loads(llm.calls[1][1])["previous_errors"]
    assert "stop_words" in json.loads(llm.calls[2][1])["previous_errors"]
    facts = json.loads(llm.calls[0][1])["facts"]
    assert "Маникюр 1500,50 ₽" in facts
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
    assert variant.ad.title == description.services[0].name
    assert variant.ad.text == description.advantages[0].value
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
    llm = FakeLLM(response())
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
        variant = await generate_ad_text(llm, description, platform="vk", angle="цена", segment="рядом с домом")
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
