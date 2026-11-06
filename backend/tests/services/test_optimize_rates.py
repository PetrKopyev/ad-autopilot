from decimal import Decimal

from app.db.models import AdGroup, Campaign
from app.domain.types import GroupSnapshot
from app.services.optimize import in_rubles, ruble_rates


def campaign(id: int, platform: str, daily_limit_kop: int) -> Campaign:
    return Campaign(
        id=id,
        business_id=1,
        platform_account_id=1,
        platform=platform,
        external_id=f"campaign-{id}",
        type="unified",
        name="Тест",
        status="active",
        daily_limit_kop=daily_limit_kop,
        period_cap_kop=0,
    )


def local_group(id: int, external_id: str, campaign_id: int) -> AdGroup:
    return AdGroup(id=id, campaign_id=campaign_id, external_id=external_id, theme="Тест", geo={}, status="active")


def test_ruble_rates_translate_weights_from_current_campaign_limit():
    groups = [GroupSnapshot(f"g-{i}", "direct", 79_100) for i in range(3)]
    rows = [local_group(i + 1, group.id, 1) for i, group in enumerate(groups)]
    rates = ruble_rates(groups, rows, [campaign(1, "direct", 786_300)])
    assert rates == {"direct": Decimal(786_300) / Decimal(79_100 * 3)}
    rubles = in_rubles(groups, rates)
    assert [group.daily_limit for group in rubles] == [262_100] * 3


def test_ruble_rates_skip_inactive_groups_but_translate_them_with_platform_rate():
    groups = [
        GroupSnapshot("active-1", "direct", 50_000),
        GroupSnapshot("active-2", "direct", 50_000),
        GroupSnapshot("stopped", "direct", 70_000, active=False),
    ]
    rows = [local_group(i + 1, group.id, 1) for i, group in enumerate(groups)]
    rates = ruble_rates(groups, rows, [campaign(1, "direct", 200_000)])
    # Неактивная группа не входит в сумму весов: курс = 200 000 / 100 000.
    assert rates == {"direct": Decimal(2)}
    rubles = in_rubles(groups, rates)
    assert {group.id: group.daily_limit for group in rubles} == {
        "active-1": 100_000,
        "active-2": 100_000,
        "stopped": 140_000,
    }


def test_ruble_rates_are_independent_between_platforms():
    groups = [
        GroupSnapshot("d-1", "direct", 10_000),
        GroupSnapshot("d-2", "direct", 30_000),
        GroupSnapshot("v-1", "vk", 20_000),
    ]
    rows = [local_group(i + 1, group.id, campaign_id) for i, (group, campaign_id) in enumerate(zip(groups, (1, 1, 2)))]
    rates = ruble_rates(groups, rows, [campaign(1, "direct", 400_000), campaign(2, "vk", 50_000)])
    assert rates == {"direct": Decimal(10), "vk": Decimal("2.5")}
    rubles = in_rubles(groups, rates)
    assert {group.id: group.daily_limit for group in rubles} == {"d-1": 100_000, "d-2": 300_000, "v-1": 50_000}


def test_ruble_rates_fall_back_to_one_without_weight_or_limit():
    stopped = [GroupSnapshot("stopped", "direct", 10_000, active=False)]
    rows = [local_group(1, "stopped", 1)]
    assert ruble_rates(stopped, rows, [campaign(1, "direct", 100_000)]) == {"direct": Decimal(1)}
    assert ruble_rates(stopped, rows, [campaign(1, "direct", 0)]) == {"direct": Decimal(1)}
    active = [GroupSnapshot("active", "direct", 10_000)]
    rows = [local_group(1, "active", 1)]
    assert ruble_rates(active, rows, [campaign(1, "direct", 0)]) == {"direct": Decimal(1)}
