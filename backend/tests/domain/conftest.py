from datetime import UTC, datetime, timedelta

import pytest

from app.domain.types import AdSnapshot, DaySpend


@pytest.fixture
def now():
    return datetime(2026, 10, 26, 12, tzinfo=UTC)


@pytest.fixture
def make_ad(now):
    def factory(
        id="ad", group_id="group", platform="direct", spend=15_000, impressions=4_000, clicks=40, leads=1, **kwargs
    ):
        defaults = {
            "approved_at": now - timedelta(days=10),
            "days": (DaySpend(now.date() - timedelta(days=2), spend, impressions, clicks, leads),),
        }
        return AdSnapshot(id, group_id, platform, **(defaults | kwargs))

    return factory
