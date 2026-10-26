import json

import pytest

from app.content.ideas import build_prompt, generate_ideas, segments_for
from app.content.llm import FakeLLM


@pytest.mark.parametrize("niche", ["nails", "yoga", "beauty", "barbershop", "fitness", "other"])
def test_niche_segments(niche: str) -> None:
    segments = segments_for(niche)
    assert "рядом с домом" in segments
    assert len(segments) >= 5
    assert "до/после" not in segments


def test_aliases_and_fallback() -> None:
    assert segments_for(" Маникюр ") == segments_for("nails")
    assert segments_for("йога") == segments_for("yoga")
    assert segments_for("салон") == segments_for("beauty")
    assert segments_for("барбершоп") == segments_for("barbershop")
    assert segments_for("фитнес") == segments_for("fitness")
    assert segments_for("unknown") == segments_for("other")
    assert "подарок подруге" in segments_for("nails")
    assert "утренняя практика" in segments_for("yoga")


def test_prompt_constraints_are_appended_by_code() -> None:
    prompt = build_prompt("Набор инструментов на светлом фоне")
    assert "Набор инструментов" in prompt
    assert "Современная рекламная иллюстрация" in prompt
    assert "без текста на изображении" in prompt
    assert "без лиц крупным планом" in prompt
    assert "без обещаний результата" in prompt
    assert "не фотография реальной работы" in prompt


async def test_eight_candidates_with_fixed_segments() -> None:
    llm = FakeLLM(json.dumps({"ideas": [f"Визуальная идея {index}" for index in range(8)]}))
    ideas = await generate_ideas(llm, "nails")
    assert len(ideas) == 8
    assert ideas[0].segment == "к празднику"
    assert ideas[5].segment == ideas[0].segment
    assert all(idea.label == "иллюстрация" for idea in ideas)
    assert all("без текста на изображении" in idea.prompt for idea in ideas)
    assert len(json.loads(llm.calls[0][1])["segments"]) == 8


@pytest.mark.parametrize("response", ["not json", "[]", "{}", '{"ideas": [null]}', '{"ideas": [""]}'])
async def test_invalid_llm_response(response: str) -> None:
    with pytest.raises(ValueError):
        await generate_ideas(FakeLLM(response), "yoga", count=1)


@pytest.mark.parametrize("count", [0, 9, True])
async def test_candidate_limits(count: int) -> None:
    llm = FakeLLM()
    with pytest.raises(ValueError):
        await generate_ideas(llm, "yoga", count=count)
    assert not llm.calls
