"""M6-14（8-4）：组合页聚合、快照生产者 CLI、worker 终态回写。"""
from __future__ import annotations

from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest

from quant.backtest.signal import Signal
from quant.cli import portfolio_tick
from quant.dashboard.portfolio_view import PortfolioView
from quant.foundation.config import Settings
from quant.state.sqlite_store import SQLiteStore

_UNITS = """
portfolio_id = "core"
allocator = "equal"
[[units]]
strategy_id = "sma_cross"
inst_id = "BTC-USDT"
enabled = true
[[units]]
strategy_id = "rsi"
inst_id = "ETH-USDT"
enabled = false
"""


def _settings(tmp_path: Path) -> Settings:
    cfg = tmp_path / "config.toml"
    cfg.write_text(
        '[okx]\napi_key = "k"\nsecret = "s"\npassphrase = "p"\n'
        f'[portfolio]\nenabled = false\nconfig_dir = "{tmp_path / "portfolios"}"\n'
        f'[state]\ndb_path = "{tmp_path / "quant.db"}"\n',
        encoding="utf-8",
    )
    return Settings.load(cfg)


def _with_config(tmp_path: Path) -> None:
    _settings(tmp_path)  # CLI 测试也需要 config.toml
    folder = tmp_path / "portfolios"
    folder.mkdir(exist_ok=True)
    (folder / "core.toml").write_text(_UNITS, encoding="utf-8")


def _view(tmp_path: Path) -> tuple[Settings, SQLiteStore, PortfolioView]:
    settings = _settings(tmp_path)
    store = SQLiteStore(settings.state.db_path)
    return settings, store, PortfolioView(store, settings)


class TestPortfolioView:
    def test_no_config_reports_reason_without_zero_data(self, tmp_path: Path) -> None:
        _settings, store, view = _view(tmp_path)
        try:
            result = view.snapshot()
            assert result["portfolio_id"] is None
            assert result["units"] == []
            assert result["snapshots"] == []
            assert result["attribution"] == []
            assert result["degraded"] == []
            assert result["reason"] and "未配置组合" in result["reason"]
        finally:
            store.close()

    def test_config_exposes_units_and_weights(self, tmp_path: Path) -> None:
        _with_config(tmp_path)
        settings, store, view = _view(tmp_path)
        try:
            result = view.snapshot()
            assert result["portfolio_id"] == "core"
            assert result["allocator"] == "equal"
            assert result["enabled"] is False
            assert len(result["units"]) == 2
            # 一个停用单元 -> 启用的那个拿满权重
            enabled = [u for u in result["units"] if u["enabled"]]
            assert enabled[0]["weight"] == "1"
            assert result["reason"] and "未启用" in result["reason"]
            assert result["rebalance"]["mode"] == "calendar"
        finally:
            store.close()

    def test_bad_config_marks_degraded(self, tmp_path: Path) -> None:
        folder = tmp_path / "portfolios"
        folder.mkdir()
        (folder / "bad.toml").write_text('portfolio_id = "bad"\n', encoding="utf-8")
        _settings, store, view = _view(tmp_path)
        try:
            result = view.snapshot()
            assert "portfolio_config" in result["degraded"]
            assert result["units"] == []
        finally:
            store.close()

    def test_tick_writes_snapshots_and_attribution(self, tmp_path: Path) -> None:
        _with_config(tmp_path)
        _settings, store, view = _view(tmp_path)
        try:
            first, _plan, reason = view.tick(Decimal("10000"), 1_000)
            assert reason is None and first is not None
            second, _plan2, _ = view.tick(Decimal("11000"), 2_000)
            assert second is not None and second.drawdown == Decimal("0")
            result = view.snapshot()
            assert len(result["snapshots"]) == 2
            assert result["snapshots"][0]["equity"] == "10000"
            assert result["state"]["updated_ts"] == 2_000
            assert len(result["attribution"]) == 1  # 只有一个启用单元
        finally:
            store.close()

    def test_tick_rejects_nonpositive_equity(self, tmp_path: Path) -> None:
        _with_config(tmp_path)
        _settings, store, view = _view(tmp_path)
        try:
            snap, plan, reason = view.tick(Decimal("0"), 1_000)
            assert snap is None and plan is None
            assert reason and "为正" in reason
        finally:
            store.close()


class TestWorkerTerminalWriteback:
    def test_terminal_saves_update_attempt(self, tmp_path: Path) -> None:
        _with_config(tmp_path)
        _settings, store, view = _view(tmp_path)
        try:
            view.tick(Decimal("10000"), 1_000)
            signal = Signal(
                strategy_id="sma_cross",
                side="buy",
                order_type="market",
                inst_id="BTC-USDT",
                qty=None,
                ts=1_000,
                signal_id="portfolio-core-1000-a:BTC-USDT",
                size_pct=Decimal("0.1"),
                source="portfolio",
            )
            store.save_rebalance_attempt(
                "core", 1_000, "a:BTC-USDT", signal.signal_id,
                "submitted", None, 1_000,
            )
            store.save_signal(signal, "rejected")
            rows = store.get_rebalance_attempts("core", 1_000)
            assert rows[0]["status"] == "rejected"
            assert "worker" in str(rows[0]["reason"])

            # 非组合信号不会误改留痕
            other = Signal(
                strategy_id="sma_cross", side="buy", order_type="market",
                inst_id="BTC-USDT", qty=Decimal("1"), ts=1_001,
                signal_id="webhook-1",
            )
            store.save_signal(other, "executed")
            assert store.get_rebalance_attempts("core", 1_000)[0]["status"] == "rejected"
        finally:
            store.close()


class TestPortfolioTickCLI:
    def test_dry_run_writes_snapshot_without_signals(
        self, tmp_path: Path, capsys
    ) -> None:
        _with_config(tmp_path)
        settings = _settings(tmp_path)
        code = portfolio_tick.main(
            SimpleNamespace(
                config=str(tmp_path / "config.toml"),
                equity="10000",
                execute=False,
            )
        )
        assert code == 0
        import json

        out = capsys.readouterr().out
        # 全量跑时日志可能也写 stdout，只取第一段 JSON
        payload, _ = json.JSONDecoder().raw_decode(out[out.index("{") :])
        assert payload["ok"] is True
        assert payload["execute"] is False
        assert payload["submitted"] == []
        assert "分摊" in payload["snapshot_note"]
        store = SQLiteStore(settings.state.db_path)
        try:
            assert len(PortfolioView(store, settings).snapshot()["snapshots"]) == 1
        finally:
            store.close()

    def test_execute_blocked_when_disabled(self, tmp_path: Path, capsys) -> None:
        _with_config(tmp_path)
        code = portfolio_tick.main(
            SimpleNamespace(
                config=str(tmp_path / "config.toml"),
                equity="10000",
                execute=True,
            )
        )
        assert code == 0
        import json

        out = capsys.readouterr().out
        payload, _ = json.JSONDecoder().raw_decode(out[out.index("{") :])
        assert payload["ok"] is False
        assert "enabled=false" in payload["reason"]
        assert payload["submitted"] == []

    def test_invalid_equity_returns_error(self, tmp_path: Path, capsys) -> None:
        _with_config(tmp_path)
        code = portfolio_tick.main(
            SimpleNamespace(
                config=str(tmp_path / "config.toml"),
                equity="not-a-number",
                execute=False,
            )
        )
        assert code == 1
        assert "读不到有效总权益" in capsys.readouterr().out

    @pytest.mark.parametrize("equity", ["0", "-1"])
    def test_nonpositive_override_rejected(
        self, tmp_path: Path, capsys, equity: str
    ) -> None:
        _with_config(tmp_path)
        code = portfolio_tick.main(
            SimpleNamespace(
                config=str(tmp_path / "config.toml"),
                equity=equity,
                execute=False,
            )
        )
        assert code == 1
