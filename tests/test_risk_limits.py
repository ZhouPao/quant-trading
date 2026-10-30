"""限制合并单元测试（MS4-B §3）。"""
from __future__ import annotations

from decimal import Decimal

from quant.foundation.config import RiskDefaults
from quant.risk.limits import (
    CODE_DEFAULTS,
    LIMIT_FIELDS,
    parse_override_hash,
    resolve_limits,
)


class TestDefaults:
    def test_defaults_do_not_participate(self) -> None:
        """全局层全 None 时全部回落代码默认。"""
        limits = resolve_limits("s1", defaults=RiskDefaults())

        for name in LIMIT_FIELDS:
            assert limits.value(name) == CODE_DEFAULTS[name]
            assert limits.source(name) == "default"

    def test_risk_defaults_flags(self) -> None:
        """两个行为开关的默认值必须与 spec §13 一致。"""
        defaults = RiskDefaults()
        assert defaults.allow_initial_equity_fallback is False
        assert defaults.snapshot_max_age_ms == 180_000
        assert defaults.event_log is True


class TestTwoLayerMin:
    def test_global_stricter_wins(self) -> None:
        limits = resolve_limits(
            "s1",
            defaults=RiskDefaults(max_position_pct=Decimal("0.2")),
            strategy_risk={"max_position_pct": "0.5"},
        )
        assert limits.max_position_pct == Decimal("0.2")
        assert limits.source("max_position_pct") == "global"

    def test_strategy_stricter_wins(self) -> None:
        limits = resolve_limits(
            "s1",
            defaults=RiskDefaults(max_position_pct=Decimal("0.5")),
            strategy_risk={"max_position_pct": "0.2"},
        )
        assert limits.max_position_pct == Decimal("0.2")
        assert limits.source("max_position_pct") == "strategy"

    def test_equal_values_report_strategy(self) -> None:
        limits = resolve_limits(
            "s1",
            defaults=RiskDefaults(max_position_pct=Decimal("0.3")),
            strategy_risk={"max_position_pct": "0.3"},
        )
        assert limits.source("max_position_pct") == "strategy"

    def test_only_strategy_configured(self) -> None:
        limits = resolve_limits(
            "s1", defaults=RiskDefaults(), strategy_risk={"max_order_qty": 500}
        )
        assert limits.max_order_qty == Decimal("500")
        assert limits.source("max_order_qty") == "strategy"

    def test_only_global_configured(self) -> None:
        limits = resolve_limits(
            "s1",
            defaults=RiskDefaults(consecutive_loss_limit=5),
            strategy_risk=None,
        )
        assert limits.consecutive_loss_limit == 5
        assert limits.source("consecutive_loss_limit") == "global"


class TestOverride:
    def test_override_wins_over_min(self) -> None:
        """④ 是覆盖而非取严：放宽也必须生效。"""
        limits = resolve_limits(
            "s1",
            defaults=RiskDefaults(max_position_pct=Decimal("0.2")),
            strategy_risk={"max_position_pct": "0.2"},
            overrides={"max_position_pct": "0.9"},
        )
        assert limits.max_position_pct == Decimal("0.9")
        assert limits.source("max_position_pct") == "override"

    def test_override_does_not_touch_other_fields(self) -> None:
        limits = resolve_limits(
            "s1",
            defaults=RiskDefaults(max_position_pct=Decimal("0.2")),
            overrides={"max_position_pct": "0.4"},
        )
        assert limits.source("max_order_qty") == "default"

    def test_parse_override_hash_filters_and_strips_prefix(self) -> None:
        parsed = parse_override_hash(
            {
                "sma_cross:max_position_pct": "0.2",
                "btc_trend:max_position_pct": "0.5",
                "sma_cross:unknown_param": "1",
                "max_total_pct": "0.7",
            },
            "sma_cross",
        )
        assert parsed == {"max_position_pct": "0.2"}


class TestBadValues:
    def test_invalid_config_value_falls_back(self) -> None:
        limits = resolve_limits(
            "s1",
            defaults=None,
            strategy_risk={"max_position_pct": "abc"},
        )
        assert limits.max_position_pct == Decimal("0.3")
        assert limits.source("max_position_pct") == "default"

    def test_as_dict_shape(self) -> None:
        limits = resolve_limits("s1", defaults=RiskDefaults())
        payload = limits.as_dict()
        assert set(payload) == set(LIMIT_FIELDS)
        assert payload["max_position_pct"] == {
            "value": Decimal("0.3"),
            "source": "default",
        }
