from dataclasses import FrozenInstanceError, replace
from decimal import Decimal

import pytest

from app.domain.settings import DEFAULT_SETTINGS, DomainSettings


def test_settings_are_immutable_and_independent():
    custom = replace(DEFAULT_SETTINGS, no_leads_clicks=100, max_changes=2, confidence=0.9)
    assert custom.no_leads_clicks == 100 and custom.max_changes == 2 and custom.confidence == 0.9
    assert DEFAULT_SETTINGS.no_leads_clicks == 80
    with pytest.raises(FrozenInstanceError):
        custom.max_changes = 1


@pytest.mark.parametrize(
    "kwargs",
    [
        {"budget_reserve": 0.08},
        {"budget_reserve": Decimal("-0.01")},
        {"budget_reserve": Decimal("1.01")},
        {"direct_share": Decimal("NaN")},
        {"max_changes": -1},
        {"min_price_clicks": 40.0},
        {"decision_window_days": 0},
        {"prior_leads": 0},
        {"confidence": 1},
        {"confidence": 0},
        {"test_power": 0.5},
        {"test_power": 1},
        {"test_win_lift": -0.1},
        {"test_design_lift": 0},
        {"test_extension_factor": Decimal(1)},
        {"test_extension_alpha": 0.06},
    ],
)
def test_invalid_settings_fail_early(kwargs):
    with pytest.raises(ValueError):
        DomainSettings(**kwargs)
