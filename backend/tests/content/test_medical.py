import pytest

from app.content.medical import classify


@pytest.mark.parametrize("text", ["Маникюр и укладки", "Йога и расслабляющий массаж", "", "Нелазерный принтер"])
def test_non_medical(text: str) -> None:
    result = classify(text)
    assert result.level == "none"
    assert result.words == []


@pytest.mark.parametrize(
    "text",
    [
        "Косметология",
        "БОТОКС",
        "Филлеры",
        "Мезотерапия",
        "Пилинг",
        "Инъекции",
        "Медицинский массаж",
        "Массаж лечебный",
        "Лечебный массаж",
        "Стоматология",
    ],
)
def test_required(text: str) -> None:
    result = classify(text)
    assert result.level == "required"
    assert result.words == [text.lower()]


@pytest.mark.parametrize("text", ["Лазерная эпиляция", "Татуаж", "Перманентный макияж", "Эпиляция"])
def test_maybe(text: str) -> None:
    result = classify(text)
    assert result.level == "maybe"
    assert result.words
    assert all(word in text.lower() for word in result.words)


def test_required_takes_priority_and_keeps_all_words() -> None:
    result = classify("Татуаж, ботокс, БОТОКС и лазерная эпиляция")
    assert result.level == "required"
    assert result.words == ["татуаж", "ботокс", "лазерная", "эпиляция"]
