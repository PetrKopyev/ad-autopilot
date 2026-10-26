from datetime import date

from app.db.models import AdGroup
from integration.harness import BUDGET_KOP, World, make_world


def summary(world: World) -> dict:
    return {
        "january": world.spend_in_month(date(2026, 1, 1)),
        "february": world.spend_in_month(date(2026, 2, 1)),
        "paused": sorted(ad_id for ad_id, ad in world.ads.items() if ad.status == "paused_auto"),
        "changes": len(world.changes(status="applied")),
    }


async def test_35_days_salon_budget_is_not_exceeded() -> None:
    world = await make_world()
    await world.advance(world.worker(), days=35)
    result = summary(world)
    print("normal", result)
    assert BUDGET_KOP * 80 // 100 <= result["january"] <= BUDGET_KOP
    assert 0 < result["february"] <= BUDGET_KOP
    assert not world.repository.pending()
    assert {"weak-1", "weak-2"} <= set(result["paused"])
    assert not {"strong-1", "strong-2"} & set(result["paused"])
    limits = world.worker().metadata(world.business)["group_limits"]
    groups = {group.theme: group.external_id for group in world.repository.rows(AdGroup)}
    assert limits[groups["Маникюр"]] > limits[groups["Укладки"]]
    strong = world.adapter._states[world.refs.ads["strong-1"]]
    assert strong.status == "active"
    stats = {
        ad_id: sum(c.cost_kop for (a, _), c in world.adapter._stats.items() if a == world.refs.ads[ad_id])
        for ad_id in world.ads
    }
    assert stats["strong-1"] + stats["strong-2"] > stats["weak-1"] + stats["weak-2"]


async def test_35_days_evil_platform_budget_is_not_exceeded() -> None:
    world = await make_world(evil=True)
    await world.advance(world.worker(), days=35)
    result = summary(world)
    print("evil", result)
    assert 0 < result["january"] <= BUDGET_KOP
    assert 0 < result["february"] <= BUDGET_KOP
    assert not world.repository.pending()


async def test_ledger_follows_platform_spend() -> None:
    world = await make_world()
    await world.advance(world.worker(), days=10)
    assert world.ledger()
    assert sum(row.spent_kop for row in world.ledger()) <= world.spend_in_month(date(2026, 1, 1))
