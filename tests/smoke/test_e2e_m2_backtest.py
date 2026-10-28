"""M2 端到端烟测：真实 K线回测 + 报告生成（spec §14.3）。

流程：
1. CLI fetch-candles 拉取 BTC-USDT 1H 最近 30 天
2. BarStorage 读回
3. SMA Cross 回测 → CSV + HTML 报告
4. RSI 回测 → CSV + HTML 报告

运行：
    python3 tests/smoke/test_e2e_m2_backtest.py
"""
from __future__ import annotations

import sys
import tempfile
import time
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from quant.backtest.engine import Backtest  # noqa: E402
from quant.backtest.matcher import Matcher  # noqa: E402
from quant.backtest.report import ReportGenerator  # noqa: E402
from quant.cli import main as cli_main  # noqa: E402
from quant.foundation.logger import setup_logger  # noqa: E402
from quant.storage.bar_storage import BarStorage  # noqa: E402
from quant.strategies.python.rsi import RsiStrategy  # noqa: E402
from quant.strategies.python.sma_cross import SmaCrossStrategy  # noqa: E402

BAR_SIZE_MS_1H = 3_600_000
DAYS = 30

_passed = 0
_failed = 0


def _print(label: str, ok: bool, detail: str = "") -> None:
    global _passed, _failed
    mark = "PASS" if ok else "FAIL"
    if ok:
        _passed += 1
    else:
        _failed += 1
    print(f"[{mark}] {label}{f' — {detail}' if detail else ''}")


def _make_config(data_dir: Path) -> Path:
    load_dotenv(ROOT / ".env")
    config_text = f"""
[okx]
api_key = "${{OKX_API_KEY}}"
secret = "${{OKX_SECRET}}"
passphrase = "${{OKX_PASSPHRASE}}"
demo = false

[storage]
data_dir = "{data_dir}"
retention_days = 365

[rate_limit]
timeout_seconds = 10.0

[logging]
level = "WARNING"
file = "{data_dir / 'test.log'}"
rotation = "10 MB"
retention = "1 days"
"""
    cfg_path = data_dir / "config.toml"
    cfg_path.write_text(config_text, encoding="utf-8")
    return cfg_path


def _fetch_bars(data_dir: Path, cfg: Path):
    now_ms = int(time.time() * 1000)
    end_ts = now_ms - BAR_SIZE_MS_1H
    start_ts = end_ts - DAYS * 24 * BAR_SIZE_MS_1H

    start_date = datetime.fromtimestamp(start_ts / 1000, tz=UTC).strftime("%Y-%m-%d")
    end_date = datetime.fromtimestamp(end_ts / 1000, tz=UTC).strftime("%Y-%m-%d")

    ret = cli_main([
        "fetch-candles",
        "--inst", "BTC-USDT",
        "--bar", "1H",
        "--start", start_date,
        "--end", end_date,
        "--config", str(cfg),
    ])
    if ret != 0:
        return None, start_ts, end_ts

    storage = BarStorage(data_dir)
    return storage.load_bars("BTC-USDT", "1H", start_ts, end_ts), start_ts, end_ts


def _run_strategy(name: str, strategy, bars, params, data_dir: Path) -> bool:
    matcher = Matcher(
        slippage_rate=Decimal("0.0005"),  # 5bp
        commission_rate=Decimal("0.001"),  # 0.1%
    )
    result = Backtest(
        strategy,
        matcher,
        initial_cash=Decimal("10000"),
        params=params,
    ).run(bars)

    csv_path = data_dir / f"{name}_report.csv"
    html_path = data_dir / f"{name}_report.html"
    reporter = ReportGenerator()
    reporter.to_csv(result, csv_path)
    reporter.to_html(result, html_path)

    metrics = result.metrics
    ok = (
        len(result.equity_curve) == len(bars)
        and csv_path.exists()
        and html_path.exists()
        and set(metrics) == {
            "total_return",
            "annual_return",
            "sharpe",
            "max_drawdown",
            "win_rate",
            "profit_factor",
            "trade_count",
            "avg_holding_bars",
        }
    )
    _print(
        f"{name} 回测 + 报告",
        ok,
        f"bars={len(bars)} trades={len(result.trades)} "
        f"return={metrics['total_return']:.4f} "
        f"mdd={metrics['max_drawdown']:.4f}",
    )
    return ok


def main() -> int:
    setup_logger(level="WARNING")
    print("=" * 60)
    print(f"M2 端到端烟测：真实 K线回测（最近 {DAYS} 天 BTC-USDT 1H）")
    print("=" * 60)

    with tempfile.TemporaryDirectory() as tmp:
        data_dir = Path(tmp)
        cfg = _make_config(data_dir)

        print(f"\n-- 拉取 {DAYS} 天 BTC-USDT 1H --")
        bars, start_ts, end_ts = _fetch_bars(data_dir, cfg)
        if not bars or len(bars) < 100:
            _print("拉取 K线", False, f"bars={0 if bars is None else len(bars)}")
            print("\n总计: 1 个场景, 0 通过, 1 失败")
            return 1
        _print("拉取 K线", True, f"bars={len(bars)}")

        _run_strategy(
            "sma_cross",
            SmaCrossStrategy(),
            bars,
            {"fast_len": 10, "slow_len": 50, "commission_rate": "0.001"},
            data_dir,
        )
        _run_strategy(
            "rsi",
            RsiStrategy(),
            bars,
            {
                "rsi_period": 14,
                "oversold": 30,
                "overbought": 70,
                "commission_rate": "0.001",
            },
            data_dir,
        )

    print("\n" + "=" * 60)
    print(f"总计: {_passed + _failed} 个场景, {_passed} 通过, {_failed} 失败")
    print("=" * 60)
    return 0 if _failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
