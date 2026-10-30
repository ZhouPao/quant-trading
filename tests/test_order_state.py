"""订单状态机测试（MS4-B §7.2）。"""
from __future__ import annotations

import pytest

from quant.state.order_state import TERMINAL_STATES, can_transition


class TestAllowedTransitions:
    def test_pending_to_live(self) -> None:
        assert can_transition("pending", "live")

    @pytest.mark.parametrize("target", ["partially_filled", "filled", "canceled"])
    def test_live_to_settled(self, target: str) -> None:
        assert can_transition("live", target)

    def test_pending_to_filled_directly(self) -> None:
        """市价单一次回包即成交，没有 live 阶段。"""
        assert can_transition("pending", "filled")

    def test_partial_to_filled(self) -> None:
        assert can_transition("partially_filled", "filled")

    def test_partial_to_canceled(self) -> None:
        assert can_transition("partially_filled", "canceled")


class TestRejectedTransitions:
    @pytest.mark.parametrize("terminal", sorted(TERMINAL_STATES))
    def test_terminal_cannot_move(self, terminal: str) -> None:
        assert not can_transition(terminal, "pending")
        assert not can_transition(terminal, "live")

    def test_live_cannot_go_back_to_pending(self) -> None:
        assert not can_transition("live", "pending")

    def test_partial_cannot_go_back_to_live(self) -> None:
        assert not can_transition("partially_filled", "live")

    def test_unknown_status_cannot_move(self) -> None:
        assert not can_transition("unknown", "filled")


class TestIdempotency:
    @pytest.mark.parametrize("status", ["pending", "live", "filled", "canceled"])
    def test_same_status_is_allowed(self, status: str) -> None:
        assert can_transition(status, status)
