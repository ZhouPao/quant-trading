"""paper-report CLI 测试（M3）。

构造真实 SQLite + parquet 数据，验证：
- 无成交时返回失败
- 成交与回测一致时通过
- 偏离超阈值时返回失败
- CSV 输出
- 本地无 K线时返回失败
"""
from __future__ import annotations

from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest

from quant.cli import paper_report
from quant.foundation.config import OKXSettings, Settings
from quant.models import Bar
from quant.state.sqlite_store import SQLiteStore
from quant.storage.bar_storage import BarStorage

HOUR = 3_600_000
BASE_TS = 1_700_000_000_000


def _bars(count: int = 400) -> list[Bar]:
    """构造缓慢单调上涨的 K线，触发策略交易。"""
    bars = []
    for i in range(count):
        price = Decimal("100") + Decimal(i % 20)
        bars.append(
            Bar(
                ts=BASE_TS + i * HOUR,
                open=price,
                high=price + 1,
                low=price - 1,
                close=price,
                vol=Decimal("10"),
                vol_quote=Decimal("1000"),
                confirm=1,
                inst_id="BTC-USDT",
                bar_size="1H",
            )
        )
    return bars


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        okx=OKXSettings(api_key="k", secret="s", passphrase="p"),
        storage={"data_dir": tmp_path / "data"},
        state={"db_path": tmp_path / "data" / "state" / "quant.db"},
    )


def _args(**overrides):
    args = {
        "strategy": "sma_cross",
        "inst": "BTC-USDT",
        "bar": "1H",
        "limit": 500,
        "threshold": 0.05,
        "window_hours": 24,
        "out": None,
        "config_dir": "config/strategies",
        "config": "config/config.toml",
    }
    args.update(overrides)
    return SimpleNamespace(**args)


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    settings = _settings(tmp_path)
    monkeypatch.setattr(
        paper_report.Settings, "load", classmethod(lambda cls, p: settings)
    )
    BarStorage(settings.storage.data_dir).save_bars(_bars())
    return settings


def _seed_fills(db_path: Path, fills) -> None:
    store = SQLiteStore(db_path)
    try:
        for fill in fills:
            store.save_fill(fill)
    finally:
        store.close()


def test_no_fills_returns_failure(workspace, capsys):
    assert paper_report.main(_args()) == 1


def test_missing_bars_returns_failure(workspace, tmp_path, monkeypatch):
    from quant.backtest.order import Fill

    _seed_fills(
        workspace.state.db_path,
        [
            Fill(
                order_id="o1",
                inst_id="BTC-USDT",
                side="buy",
                qty=Decimal("1"),
                price=Decimal("100"),
                fee=Decimal("0.1"),
                ts=BASE_TS,
            )
        ],
    )
    # 指向空数据目录
    empty = _settings(tmp_path / "empty")
    monkeypatch.setattr(
        paper_report.Settings, "load", classmethod(lambda cls, p: empty)
    )
    import shutil

    shutil.copytree(
        workspace.state.db_path.parent,
        empty.state.db_path.parent,
        dirs_exist_ok=True,
    )
    assert paper_report.main(_args()) == 1


def test_invalid_bar_returns_failure(workspace):
    assert paper_report.main(_args(bar="2H")) == 1


def test_report_runs_and_writes_csv(workspace, tmp_path):
    """用真实回测产生的成交回填 SQLite，再生成报告。"""
    from quant.backtest.engine import Backtest
    from quant.backtest.matcher import Matcher
    from quant.strategies.python.sma_cross import SmaCrossStrategy

    # 与 paper_report 默认 settings/策略配置保持一致，才能复现同一成交序列
    result = Backtest(
        strategy=SmaCrossStrategy(),
        matcher=Matcher(
            slippage_rate=Decimal("0.0005"),
            commission_rate=Decimal("0.001"),
        ),
        initial_cash=Decimal("10000"),
        params={"fast_len": 10, "slow_len": 50, "commission_rate": "0.001"},
    ).run(_bars())

    assert result.trades, "fixture must produce trades"
    _seed_fills(workspace.state.db_path, result.trades)

    out = tmp_path / "paper.csv"
    code = paper_report.main(_args(strategy="sma_cross", out=str(out)))

    assert code == 0
    assert out.exists()
    content = out.read_text()
    assert "trade_count" in content


def test_deviation_exceeds_threshold_returns_failure(workspace):
    from quant.backtest.order import Fill

    fills = [
        Fill(
            order_id=f"o{i}",
            inst_id="BTC-USDT",
            side="buy" if i % 2 == 0 else "sell",
            qty=Decimal("1"),
            price=Decimal("10000"),
            fee=Decimal("1"),
            ts=BASE_TS + i * HOUR * 10,
        )
        for i in range(6)
    ]
    _seed_fills(workspace.state.db_path, fills)

    assert paper_report.main(_args(threshold=0.01)) == 1
