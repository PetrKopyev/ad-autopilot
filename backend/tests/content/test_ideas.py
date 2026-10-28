import json

import pytest

from app.content.ideas import MAX_IDEAS, MOODS, SUBJECTS, TITLE_LIMIT, build_prompt, generate_ideas
from app.content.llm import FakeLLM

NAILS = [
    ("Маникюр с гель-лаком", "к празднику"),
    ("Педикюр с покрытием", "рядом с домом"),
    ("Наращивание ногтей", "новый дизайн"),
]


def answer(count: int, title: str = "Френч с золотой фольгой") -> str:
    return json.dumps({"ideas": [{"title": f"{title} {index}", "visual": f"Кадр {index}"} for index in range(count)]})


def scenes(*pairs: tuple[str, str]) -> str:
    return json.dumps({"ideas": [{"title": title, "visual": visual} for title, visual in pairs]}, ensure_ascii=False)


def test_pedicure_prompt_is_a_scene_starting_with_feet() -> None:
    prompt = build_prompt(
        "вид сверху, гель-лак цвета пыльной розы, без коробок и надписей, на белом полотенце", "Педикюр с покрытием",
        "nails",
    )
    assert prompt.startswith("Женские ступни с педикюром крупным планом ровно стоят рядом")
    assert "пальцы видны, вид сверху, гель-лак" in prompt
    assert "Реалистичная рекламная фотография" in prompt and prompt.endswith("без текста и надписей.")
    assert prompt.count("без") == 1 and "коробок" not in prompt and "Требования" not in prompt
    assert "услуги «Педикюр с покрытием»" not in prompt


@pytest.mark.parametrize(
    "service,start",
    [
        ("Маникюр без покрытия", "Женская кисть руки с маникюром"),
        ("Наращивание ногтей", "Женская кисть руки с длинными наращёнными ногтями"),
        ("Снятие покрытия", "Женская кисть руки с короткими натуральными ногтями"),
        ("Парафинотерапия", "Женская кисть руки с ухоженными ногтями"),
    ],
)
def test_subject_follows_service_of_the_niche(service: str, start: str) -> None:
    assert build_prompt("вид сбоку, нюдовый лак", service, "nails").startswith(start)


@pytest.mark.parametrize("service", ["Маникюр", "Снятие покрытия", "Наращивание ногтей", "Парафинотерапия"])
def test_hand_is_one_relaxed_hand_with_nothing_in_it(service: str) -> None:
    prompt = build_prompt("вид сбоку, нюдовый лак", service, "nails")
    assert "лежит ладонью вниз на ровной поверхности, пальцы прямые" in prompt
    assert "Женские руки" not in prompt
    assert "держ" not in prompt


def test_subjects_and_moods_have_no_negations_or_alcohol() -> None:
    texts = [scene for scenes in SUBJECTS.values() for _, scene in scenes] + list(MOODS.values())
    assert not any(word in text.lower() for text in texts for word in ("без ", "не ", "бокал", "игрист", "шампанск"))


def test_other_niches_get_default_subject() -> None:
    assert build_prompt("утренний свет", "Хатха-йога", "yoga").startswith("Человек в спокойной позе йоги")
    assert build_prompt("светлый фон", "Химчистка штор", "other").startswith(
        "Крупным планом результат услуги: Химчистка штор"
    )
    assert build_prompt("светлый фон", "", "other").startswith("Светлый уютный интерьер студии")


async def test_ideas_follow_service_and_segment_of_each_group() -> None:
    llm = FakeLLM(answer(len(NAILS)))
    ideas = await generate_ideas(llm, "nails", NAILS)
    system, user = llm.calls[0]
    items = json.loads(user)["items"]
    assert [(item["service"], item["segment"]) for item in items] == NAILS
    assert items[1]["subject"].startswith("Женские ступни") and "уют" in items[1]["mood"]
    assert "праздничная обстановка" in items[0]["mood"]
    for rule in (
        "Нельзя предлагать", "надписи", "карты, улицы, дома и транспорт", "товары в упаковке и коробки",
        "лица крупным планом", "до/после", "парикмахерские кресла", "косметику для макияжа", "«у дома»",
        "алкоголь и бокалы", "руки, держащие предметы", "переплетённые или сложенные пальцы",
        "Руки в кадре ничего не держат",
    ):
        assert rule in system
    assert "игрист" not in system and "игрист" not in user
    assert [(idea.service, idea.segment) for idea in ideas] == NAILS
    assert ideas[1].visual == "Кадр 1" and ideas[1].prompt.startswith("Женские ступни")
    assert all(idea.prompt.count("без") == 1 for idea in ideas)


async def test_two_ideas_of_a_pair_differ() -> None:
    pair = ("Педикюр с покрытием", "новый дизайн")
    llm = FakeLLM(
        scenes(
            ("Руки с аккуратными ногтями", "руки с аккуратными ногтями на нейтральном фоне"),
            ("Руки с ухоженными ногтями", "руки с ухоженными ногтями на нейтральном фоне"),
            ("Руки с красивыми ногтями", "руки с красивыми ногтями на нейтральном фоне"),
        )
    )
    ideas = await generate_ideas(llm, "nails", [pair] * 3)
    assert len({idea.label for idea in ideas}) == len({idea.prompt for idea in ideas}) == 3
    assert ideas[0].label == "Руки с аккуратными ногтями"
    assert all(idea.prompt.startswith("Женские ступни") for idea in ideas)


async def test_ideas_of_a_pair_alternate_close_up_and_scene() -> None:
    pair = ("Маникюр с гель-лаком", "к празднику")
    llm = FakeLLM(answer(4))
    await generate_ideas(llm, "nails", [pair, NAILS[1], pair, pair])
    system, user = llm.calls[0]
    roles = [item["role"] for item in json.loads(user)["items"]]
    assert roles[0].startswith("крупный план") and roles[1].startswith("крупный план")
    assert roles[2].startswith("сцена из жизни") and roles[3].startswith("крупный план")
    assert "role" in system and "во второй не повторяются" in system


@pytest.mark.parametrize(
    "niche,service",
    [
        ("barbershop", "Мужская стрижка"),
        ("fitness", "Персональная тренировка"),
        ("yoga", "Хатха-йога"),
        ("medical", "Приём терапевта"),
        ("other", "Химчистка штор"),
    ],
)
async def test_close_up_role_does_not_mention_nails_in_other_niches(niche: str, service: str) -> None:
    llm = FakeLLM(answer(2))
    ideas = await generate_ideas(llm, niche, [(service, "время для себя")] * 2)
    system, user = llm.calls[0]
    assert json.loads(user)["items"][0]["role"].startswith("крупный план результата услуги")
    assert not any("ногт" in text.lower() for text in (system, user, *(idea.prompt for idea in ideas)))


async def test_second_idea_with_props_of_the_first_is_replaced() -> None:
    pair = ("Наращивание ногтей", "к празднику")
    llm = FakeLLM(
        scenes(
            ("Миндальная форма в золоте", "золотые миндальные ногти, рядом бокал шампанского, тёмный бархат"),
            ("Золотые блики на миндале", "вечеринка, блики огней, рука держит бокалы игристого у окна"),
        )
    )
    first, second = await generate_ideas(llm, "nails", [pair] * 2)
    assert first.label == "Миндальная форма в золоте"
    assert "бокал" not in second.visual and second.label != "Золотые блики на миндале"


async def test_scene_without_props_of_the_first_is_kept() -> None:
    pair = ("Маникюр с гель-лаком", "рядом с домом")
    llm = FakeLLM(
        scenes(
            ("Нюд на светлом льне", "вид сверху, нюдовый гель-лак, светлый лён, мягкий свет"),
            ("Чай у окна", "у окна, рядом на подоконнике чашка чая и плед, тёплый вечерний свет"),
        )
    )
    _, second = await generate_ideas(llm, "nails", [pair] * 2)
    assert second.label == "Чай у окна"


@pytest.mark.parametrize(
    "visual",
    [
        "руки держат чашку чая у окна, тёплый свет",
        "бокал в руке на фоне гирлянды",
        "чашка в руках у окна",
        "пальцы сжимают кружку какао",
        "ладони обхватывают чашку, плед",
    ],
)
async def test_scene_with_something_in_hands_is_replaced(visual: str) -> None:
    (idea,) = await generate_ideas(FakeLLM(scenes(("Чай у окна", visual))), "nails", NAILS[:1])
    assert idea.label != "Чай у окна" and idea.visual != visual
    assert not any(word in idea.prompt for word in ("держ", "в руке", "в руках", "сжима", "обхват"))


def test_style_has_no_plain_band_at_the_bottom() -> None:
    prompt = build_prompt("вид сверху, нюдовый лак", "Маникюр", "nails")
    assert "фон в нижней части" not in prompt and "однотонный" not in prompt


async def test_same_scene_for_different_pairs_is_kept() -> None:
    llm = FakeLLM(scenes(("Бордовый лак", "вид сверху, бордовый лак"), ("Бордовый лак", "вид сверху, бордовый лак")))
    ideas = await generate_ideas(llm, "nails", NAILS[:2])
    assert [idea.label for idea in ideas] == ["Бордовый лак", "Бордовый лак"]


@pytest.mark.parametrize(
    "title", ["Педикюр с покрытием новый дизайн", "Педикюр у дома", "Маникюр на дому", "Выезд мастера"]
)
async def test_title_is_a_scene_not_service_and_segment(title: str) -> None:
    llm = FakeLLM(scenes((title, "вид сверху, лак цвета пыльной розы на белом полотенце")))
    (idea,) = await generate_ideas(llm, "nails", [("Педикюр с покрытием", "новый дизайн")])
    assert idea.label == "Вид сверху, лак цвета пыльной розы на…"


async def test_label_is_short_idea_title() -> None:
    ideas = await generate_ideas(FakeLLM(answer(1)), "nails", NAILS[:1])
    assert ideas[0].label == "Френч с золотой фольгой 0"
    long = "Нежный френч с золотой фольгой и блёстками на коротких ногтях"
    (idea,) = await generate_ideas(FakeLLM(scenes((long, "Кадр"))), "nails", NAILS[:1])
    assert len(idea.label) <= TITLE_LIMIT and idea.label.endswith("…")


@pytest.mark.parametrize(
    "response",
    [
        "not json", "[]", "{}", '{"ideas": [null]}', '{"ideas": [""]}', '{"ideas": [{"title": "Френч"}]}',
        '{"ideas": [{"title": " ", "visual": "Кадр"}]}',
    ],
)
async def test_invalid_llm_response(response: str) -> None:
    with pytest.raises(ValueError):
        await generate_ideas(FakeLLM(response), "yoga", [("Хатха-йога", "первое занятие")])


@pytest.mark.parametrize("count", [0, MAX_IDEAS + 1])
async def test_candidate_limits(count: int) -> None:
    llm = FakeLLM()
    with pytest.raises(ValueError):
        await generate_ideas(llm, "yoga", [("Хатха-йога", "первое занятие")] * count)
    assert not llm.calls
