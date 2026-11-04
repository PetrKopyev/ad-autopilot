"""Заметки 1–2 круга 5: missing_conversion_sources и опорная цена без расхода площадок без источника."""

from types import SimpleNamespace

import pytest

from app.db.models import Business
from app.domain.types import AdSnapshot, DaySpend
from app.services.optimize import missing_conversion_sources, reference


def business(metrica_counter_id=None, lead_goal_ids=(), business_settings=None):
    return Business(
        id=1,
        user_id=1,
        site_url="https://salon.example",
        city="Самара",
        month_budget_kop=100_000_000,
        status="running",
        metrica_counter_id=metrica_counter_id,
        lead_goal_ids=list(lead_goal_ids),
        settings=business_settings or {},
    )


@pytest.mark.parametrize(
    "metrica_counter_id,lead_goal_ids,business_settings,config_goal,expected",
    [
        # Директ без счётчика Метрики — источника обращений нет, VK тоже без пикселя.
        (None, [1], {}, None, {"direct", "vk"}),
        # Директ без целей: счётчик есть, но обращения по целям не видны.
        (12345, [], {}, None, {"direct", "vk"}),
        # VK с целью кабинета: Директ полностью настроен, VK закрыта своей целью.
        (12345, [1], {"vk_priced_goal_source_id": 7}, None, set()),
        # VK с общей целью из настроек сервера (VK_PRICED_GOAL_SOURCE_ID).
        (12345, [1], {}, 42, set()),
        # Директ настроен, у VK ничего нет — не хватает только VK.
        (12345, [1], {}, None, {"vk"}),
    ],
)
def test_missing_conversion_sources(
    monkeypatch, metrica_counter_id, lead_goal_ids, business_settings, config_goal, expected
):
    from app.config import settings as config  # внутри теста: нужен env из autouse-фикстуры

    monkeypatch.setattr(config, "vk_priced_goal_source_id", config_goal)
    assert missing_conversion_sources(business(metrica_counter_id, lead_goal_ids, business_settings)) == expected


def test_reference_ignores_spend_of_platforms_without_conversion_source(monkeypatch):
    """Заметка 1: расход VK без пикселя (обращений не видно) не входит в опорную цену обращения,
    иначе Директ реже останавливал бы дорогие объявления."""
    from datetime import UTC, datetime, timedelta

    monkeypatch.setattr("app.config.settings.vk_priced_goal_source_id", None)
    biz = business(metrica_counter_id=12345, lead_goal_ids=[1])
    now = datetime(2026, 11, 4, tzinfo=UTC)
    day = now.date() - timedelta(days=3)
    direct = AdSnapshot(
        "direct-1", "direct-g", "direct", now - timedelta(days=10), (DaySpend(day, 300_000, 1000, 100, 3),)
    )
    vk = AdSnapshot("vk-1", "vk-g", "vk", now - timedelta(days=10), (DaySpend(day, 1_000_000, 1000, 100, 0),))
    worker = SimpleNamespace(clock=SimpleNamespace(now=now), repository=SimpleNamespace(rows=lambda *a, **k: []))
    assert reference(worker, biz, [direct, vk]) == 100_000  # 300 000 ÷ 3, без миллиона VK
    with_pixel = business(
        metrica_counter_id=12345, lead_goal_ids=[1], business_settings={"vk_priced_goal_source_id": 7}
    )
    assert reference(worker, with_pixel, [direct, vk]) == 433_333
