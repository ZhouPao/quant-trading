"""M6-10/11/12（8-2）：快照与存储、绩效归因、组合回测。"""
from __future__ import annotations

import sqlite3
from decimal import Decimal
from pathlib import Path

import pytest

from quant.foundation.config import Settings
from quant.models.bar import Bar
from quant.portfolio import (
    Attribution,
    AttributionError,
    PortfolioBacktest,
    PortfolioBacktestError,
    PortfolioConfig,
    PortfolioSnapshot,
    PortfolioStore,
    PortfolioUnit,
)
from quant.state.sqlite_store import SCHEMA_VERSION, SQLiteStore

_A = "a:BTC-USDT"
_B = "b:ETH-USDT"
_TS = [1_700_000_000_000 + i * 86_400_000 for i in range(4)]


def _config(**kwargs: object) -> PortfolioConfig:
    base: dict[str, object] = {
        "portfolio_id": "core",
        "units": (
            PortfolioUnit("a", "BTC-USDT"),
            PortfolioUnit("b", "ETH-USDT"),
        ),
    }
    base.update(kwargs)
    return PortfolioConfig(**base)  # type: ignore[arg-type]


def _snapshot(ts: int, equity_a: Decimal, equity_b: Decimal) -> PortfolioSnapshot:
    equity = equity_a + equity_b
    return PortfolioSnapshot(
        portfolio_id="core",
        ts=ts,
        equity=equity,
        unit_equity={_A: equity_a, _B: equity_b},
        weights={_A: equity_a / equity, _B: equity_b / equity},
    )


def _bar(ts: int) -> Bar:
    return Bar(
        ts=ts,
        open=Decimal("100"),
        high=Decimal("101"),
        low=Decimal("99"),
        close=Decimal("100"),
        vol=Decimal("1"),
        vol_quote=Decimal("100"),
        confirm=1,
        inst_id="BTC-USDT",
        bar_size="1H",
    )


def _curves() -> dict[str, list[tuple[int, Decimal]]]:
    return {
        _A: list(
            zip(
                _TS,
                [Decimal(v) for v in ("100", "110", "121", "133.1")],
                strict=False,
            )
        ),
        _B: list(zip(_TS, [Decimal("100")] * 4, strict=False)),
    }


def _close(x: Decimal, expected: str) -> bool:
    return abs(x - Decimal(expected)) < Decimal("1e-18")


class TestSnapshotModel:
    def test_rejects_invalid_snapshot(self) -> None:
        with pytest.raises(ValueError, match="equity"):
            _snapshot(1, Decimal("-3"), Decimal("2"))
        with pytest.raises(ValueError, match="identical keys"):
            PortfolioSnapshot(
                portfolio_id="core",
                ts=1,
                equity=Decimal("1"),
                unit_equity={_A: Decimal("1")},
                weights={_B: Decimal("1")},
            )
        with pytest.raises(ValueError, match="drawdown"):
            PortfolioSnapshot(
                portfolio_id="core",
                ts=1,
                equity=Decimal("2"),
                unit_equity={_A: Decimal("1"), _B: Decimal("1")},
                weights={_A: Decimal("0.5"), _B: Decimal("0.5")},
                drawdown=Decimal("1.5"),
            )


class TestPortfolioStorage:
    def test_fresh_db_has_both_tables_and_latest_version(self, tmp_path: Path) -> None:
        path = tmp_path / "quant.db"
        SQLiteStore(path).close()
        conn = sqlite3.connect(str(path))
        try:
            assert conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
            tables = {
                row[0]
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
            assert {"portfolio_state", "portfolio_snapshots"} <= tables
        finally:
            conn.close()

    def test_v12_db_gains_portfolio_tables_without_losing_data(
        self, tmp_path: Path
    ) -> None:
        """存量 v12 库升级：补两表且老数据不丢（AGENTS #2）。"""
        path = tmp_path / "quant.db"
        conn = sqlite3.connect(str(path))
        conn.executescript(
            """
            CREATE TABLE equity_snapshots (ts INTEGER PRIMARY KEY, total_equity TEXT);
            INSERT INTO equity_snapshots VALUES (1, '123.45');
            """
        )
        conn.execute("PRAGMA user_version = 12")
        conn.commit()
        conn.close()

        store = SQLiteStore(path)
        store.close()

        conn = sqlite3.connect(str(path))
        try:
            assert conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
            tables = {
                row[0]
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
            assert {"portfolio_state", "portfolio_snapshots"} <= tables
            assert (
                conn.execute("SELECT total_equity FROM equity_snapshots").fetchone()[0]
                == "123.45"
            )
        finally:
            conn.close()

    def test_snapshot_roundtrip_keeps_decimal_and_orders_by_ts(
        self, tmp_path: Path
    ) -> None:
        store = SQLiteStore(tmp_path / "quant.db")
        try:
            repo = PortfolioStore(store, "core")
            assert repo.state() is None
            early = _snapshot(1000, Decimal("6000.5"), Decimal("3999.623456789"))
            late = _snapshot(2000, Decimal("7000.5"), Decimal("3999.623456789"))
            repo.save(late)
            repo.save(early)
            rows = repo.snapshots()
            assert [row.ts for row in rows] == [1000, 2000]
            assert rows[0] == early
            assert rows[0].equity == Decimal("10000.123456789")

            # 同 (portfolio_id, ts) 覆盖而非追加
            repo.save(
                _snapshot(1000, Decimal("5000"), Decimal("5000"))
            )
            assert len(repo.snapshots()) == 2
            assert repo.snapshots()[0].equity == Decimal("10000")
        finally:
            store.close()

    def test_state_roundtrip_and_id_mismatch(self, tmp_path: Path) -> None:
        store = SQLiteStore(tmp_path / "quant.db")
        try:
            repo = PortfolioStore(store, "core")
            repo.save_state({_A: Decimal("0.6")}, [_A], 1234)
            state = repo.state()
            assert state is not None
            weights, enabled, ts = state
            assert weights == {_A: Decimal("0.6")}
            assert enabled == {_A}
            assert ts == 1234

            other = PortfolioSnapshot(
                portfolio_id="other",
                ts=1,
                equity=Decimal("2"),
                unit_equity={_A: Decimal("1"), _B: Decimal("1")},
                weights={_A: Decimal("0.5"), _B: Decimal("0.5")},
            )
            with pytest.raises(ValueError, match="belongs to"):
                repo.save(other)
        finally:
            store.close()


class TestAttribution:
    def test_equal_weights_equal_returns_has_no_allocation_or_selection(self) -> None:
        rows = Attribution(
            [
                _snapshot(0, Decimal("5000"), Decimal("5000")),
                _snapshot(1, Decimal("5500"), Decimal("5500")),
            ],
            _config(),
        ).rows()
        assert all(_close(row.allocation, "0") for row in rows)
        assert all(_close(row.selection, "0") for row in rows)
        assert all(_close(row.base, "0.05") for row in rows)
        # Σ total = 组合区间收益 10%
        assert _close(sum((r.total for r in rows), Decimal("0")), "0.1")

    def test_unequal_returns_separates_allocation_and_selection(self) -> None:
        rows = Attribution(
            [
                _snapshot(0, Decimal("8000"), Decimal("2000")),
                _snapshot(1, Decimal("9600"), Decimal("2000")),
            ],
            _config(),
        ).rows()
        by_key = {row.unit_key: row for row in rows}
        assert _close(by_key[_A].allocation, "0.03")
        assert _close(by_key[_B].allocation, "-0.03")
        assert _close(by_key[_A].selection, "0.08")
        assert _close(by_key[_B].selection, "-0.02")
        assert _close(by_key[_A].total, "0.16")
        assert _close(by_key[_B].total, "0")
        assert _close(sum((r.total for r in rows), Decimal("0")), "0.16")

    def test_report_returns_dataframe(self) -> None:
        report = Attribution(
            [
                _snapshot(0, Decimal("5000"), Decimal("5000")),
                _snapshot(1, Decimal("5500"), Decimal("5500")),
                _snapshot(2, Decimal("6050"), Decimal("6050")),
            ],
            _config(),
        ).report()
        assert list(report.columns) == [
            "unit_key",
            "base",
            "allocation",
            "selection",
            "total",
        ]
        assert len(report) == 2
        assert report["total"].sum() == pytest.approx(0.2, abs=1e-9)

    def test_insufficient_snapshots_or_units_fail(self) -> None:
        with pytest.raises(AttributionError, match="at least 2"):
            Attribution([_snapshot(0, Decimal("1"), Decimal("1"))], _config()).rows()
        cfg = PortfolioConfig(
            portfolio_id="core",
            units=(PortfolioUnit("a", "BTC-USDT", enabled=False),),
        )
        with pytest.raises(AttributionError, match="no enabled units"):
            Attribution(
                [_snapshot(0, Decimal("1"), Decimal("1"))] * 2, cfg
            ).rows()


class TestPortfolioBacktest:
    def test_buy_and_hold_matches_weighted_unit_curves(self) -> None:
        result = PortfolioBacktest(_config(), Decimal("10000")).run(
            _curves(), [_bar(ts) for ts in _TS]
        )
        assert len(result.equity_curve) == 4
        assert result.rebalance_count == 0
        assert _close(result.equity_curve[0][1], "10000")
        assert _close(result.equity_curve[1][1], "10500")
        assert _close(result.equity_curve[2][1], "11050")
        assert _close(result.equity_curve[3][1], "11655")
        # 与 M4 同口径的指标键
        assert result.metrics["total_return"] == pytest.approx(0.1655, abs=1e-9)
        for key in ("sharpe", "max_drawdown", "calmar", "turnover"):
            assert key in result.metrics

    def test_periodic_rebalance_resets_capital_to_target(self) -> None:
        result = PortfolioBacktest(
            _config(), Decimal("10000"), rebalance_every_bars=2
        ).run(_curves(), [_bar(ts) for ts in _TS])
        assert result.rebalance_count == 1
        # t2 再平衡后 a=b=5525；t3 a 再涨 10% -> 6077.5 + 5525
        assert _close(result.equity_curve[2][1], "11050")
        assert _close(result.unit_curves[_A][2][1], "5525")
        assert _close(result.equity_curve[3][1], "11602.5")

    @pytest.mark.parametrize("bad", ["missing", "short", "misaligned", "bars"])
    def test_incomplete_inputs_fail(self, bad: str) -> None:
        curves = _curves()
        bars = [_bar(ts) for ts in _TS]
        if bad == "missing":
            del curves[_B]
        elif bad == "short":
            curves[_A] = curves[_A][:1]
        elif bad == "misaligned":
            curves[_B] = [(ts + 1, value) for ts, value in curves[_B]]
        elif bad == "bars":
            bars = bars[:2]
        with pytest.raises(PortfolioBacktestError):
            PortfolioBacktest(_config()).run(curves, bars)

    def test_disabled_unit_is_excluded(self) -> None:
        cfg = PortfolioConfig(
            portfolio_id="core",
            units=(
                PortfolioUnit("a", "BTC-USDT"),
                PortfolioUnit("b", "ETH-USDT", enabled=False),
            ),
        )
        result = PortfolioBacktest(cfg, Decimal("10000")).run(
            {_A: _curves()[_A]}, [_bar(ts) for ts in _TS]
        )
        assert _close(result.equity_curve[3][1], "13310")

    def test_invalid_initial_cash_rejected(self) -> None:
        with pytest.raises(PortfolioBacktestError, match="initial_cash"):
            PortfolioBacktest(_config(), Decimal("0"))


class TestSnapshotFrequencyConfig:
    def test_default_is_every_bar(self, tmp_path: Path) -> None:
        path = tmp_path / "config.toml"
        path.write_text(
            '[okx]\napi_key = "k"\nsecret = "s"\npassphrase = "p"\n',
            encoding="utf-8",
        )
        settings = Settings.load(path)
        assert settings.portfolio.enabled is False
        assert settings.portfolio.snapshot_every_bars == 1

    def test_zero_frequency_rejected(self, tmp_path: Path) -> None:
        path = tmp_path / "config.toml"
        path.write_text(
            '[okx]\napi_key = "k"\nsecret = "s"\npassphrase = "p"\n'
            "[portfolio]\nsnapshot_every_bars = 0\n",
            encoding="utf-8",
        )
        with pytest.raises(ValueError, match="snapshot_every_bars"):
            Settings.load(path)
