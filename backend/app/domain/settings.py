from dataclasses import dataclass, fields
from decimal import Decimal


@dataclass(frozen=True)
class DomainSettings:
    warmup_days: int = 5
    decision_window_days: int = 14
    settlement_days: int = 2
    min_ctr_impressions: int = 2000
    min_price_clicks: int = 40
    no_leads_clicks: int = 80
    confidence: float = 0.8
    min_good_leads: int = 3
    bad_cpl_multiplier: Decimal = Decimal("1.5")
    bad_spend_multiplier: Decimal = Decimal("2")
    borderline_spend_multiplier: Decimal = Decimal("1.5")
    low_ctr_group_ratio: Decimal = Decimal("0.4")
    low_ctr_absolute: Decimal = Decimal("0.005")
    scale_step: Decimal = Decimal("0.2")
    min_active_groups: int = 2
    max_changes: int = 5
    max_budget_transfer: Decimal = Decimal("0.3")
    cooldown_hours: int = 72
    owner_cooldown_hours: int = 24
    anomaly_days: int = 3
    anomaly_previous_leads_per_day: Decimal = Decimal("1")
    not_showing_hours: int = 48
    reconsider_days: int = 21
    max_equal_share_multiplier: Decimal = Decimal("3")
    check_ad_share: Decimal = Decimal("0.3")
    initial_lead_to_client: Decimal = Decimal("0.3")
    prior_clients: int = 3
    prior_leads: int = 10
    min_reference_units: int = 3
    client_window_weeks: int = 4
    budget_reserve: Decimal = Decimal("0.08")
    daily_average_multiplier: Decimal = Decimal("1.3")
    direct_share: Decimal = Decimal("0.7")
    platform_rebalance_days: int = 14
    platform_share_step: Decimal = Decimal("0.1")
    guard_poll_minutes: int = 30
    guard_missed_polls: int = 2
    guard_spend_multiplier: Decimal = Decimal("1.5")
    max_tests_per_group: int = 1
    max_tests_per_business: int = 2
    audience_min_daily_budget: int = 70_000
    audience_min_leads: int = 10
    test_budget_share: Decimal = Decimal("0.25")
    test_alpha: float = 0.05
    test_power: float = 0.8
    test_design_lift: float = 0.3
    test_win_lift: float = 0.15
    test_max_days: int = 14
    test_extension_factor: Decimal = Decimal("1.5")
    test_extension_p: float = 0.2
    test_extension_alpha: float = 0.01
    guardrail_alpha: float = 0.1
    guardrail_min_leads: int = 10
    fisher_expected_min: int = 5

    def __post_init__(self) -> None:
        for item in fields(self):
            value = getattr(self, item.name)
            if isinstance(item.default, Decimal):
                if not isinstance(value, Decimal) or not value.is_finite() or value < 0:
                    raise ValueError(f"{item.name} must be a nonnegative finite Decimal")
            elif type(item.default) is int and (type(value) is not int or value < 0):
                raise ValueError(f"{item.name} must be a nonnegative int")
        for name in (
            "budget_reserve",
            "direct_share",
            "platform_share_step",
            "max_budget_transfer",
            "check_ad_share",
            "initial_lead_to_client",
            "test_budget_share",
            "low_ctr_absolute",
            "low_ctr_group_ratio",
        ):
            if getattr(self, name) > 1:
                raise ValueError(f"{name} must be between 0 and 1")
        for name in ("confidence", "test_alpha", "test_extension_alpha", "test_extension_p", "guardrail_alpha"):
            if not 0 < getattr(self, name) < 1:
                raise ValueError(f"{name} must be between 0 and 1")
        # Пауза по кликам или показам без единого клика или показа не опиралась бы ни на один устоявшийся день.
        for name in (
            "min_ctr_impressions",
            "min_price_clicks",
            "no_leads_clicks",
            "decision_window_days",
            "prior_leads",
            "guard_poll_minutes",
            "guard_missed_polls",
            "test_max_days",
        ):
            if not getattr(self, name):
                raise ValueError(f"{name} must be positive")
        if not 0.5 < self.test_power < 1 or self.test_design_lift <= 0 or self.test_win_lift < 0:
            raise ValueError("Invalid experiment power or lift")
        if self.test_extension_factor <= 1 or self.test_extension_alpha > self.test_alpha:
            raise ValueError("Extension must increase the sample and cannot relax significance")


DEFAULT_SETTINGS = DomainSettings()
