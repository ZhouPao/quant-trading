"""回测 / 寻优 CLI 测试。

覆盖：
- runner：日期区间、参数/网格解析、指标格式化、warmup 切分与报错
- Backtest warmup：不成交、不计入权益、驱动策略状态、边界校验
- GridOptimizer：base_params 合并、非法组合跳过、全部失败抛错
- CLI 端到端：backtest 出指标与 CSV/HTML，optimize 输出排名
"""
from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest

from quant.backtest.engine import Backtest
from quant.backtest.matcher import Matcher
from quant.backtest.optimizer import GridOptimizer
from quant.backtest.runner import (
    BacktestSetupError,
    build_matcher,
    format_metric,
    format_metrics,
    load_bars,
    parse_grid,
    parse_params,
    parse_range,
    report_paths,
)
from quant.backtest.strategy import Strategy
from quant.cli.backtest import main as backtest_main
from quant.cli.optimize import main as optimize_main
from quant.foundation.config import Settings
from quant.models import Bar
from quant.storage.bar_storage import BarStorage
from quant.strategies.python.sma_cross import SmaCrossStrategy

HOUR = 3_600_000
DAY = 86_400_000
# 2026-09-01 00:00 UTC，与 CLI 里写的日期字符串严格对齐
START = int(datetime(2026, 9, 1, tzinfo=UTC).timestamp() * 1000)
INST = "BTC-USDT"


def _bar(ts: int, price: str) -> Bar:
    value = Decimal(price)
    return Bar(
        ts=ts,
        open=value,
        high=value,
        low=value,
        close=value,
        vol=Decimal("1"),
        vol_quote=value,
        confirm=1,
        inst_id=INST,
        bar_size="1H",
    )


def _wave_bars(count: int, start_ts: int = START) -> list[Bar]:
    """缓慢正弦式波动，能触发 sma_cross 交易。"""
    import math

    out = []
    for i in range(count):
        price = 100 + 12 * math.sin(i / 9)
        out.append(_bar(start_ts + i * HOUR, f"{price:.2f}"))
    return out


class RecordingStrategy(Strategy):
    """记录 on_bar 调用与窗口长度，便于验证预热行为。"""

    def __init__(self) -> None:
        self.seen: list[int] = []

    @property
    def strategy_id(self) -> str:
        return "recording"

    def on_init(self, ctx) -> None:
        pass

    def on_bar(self, bar, ctx):
        self.seen.append(bar.ts)
        return None

    def on_finish(self, ctx) -> None:
        pass


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        okx={"api_key": "k", "secret": "s", "passphrase": "p"},
        storage={"data_dir": tmp_path / "data"},
        state={"db_path": tmp_path / "data" / "quant.db"},
        backtest={
            "initial_cash": "10000",
            "commission_rate": "0.001",
            "slippage_bps": "5",
            "window_size": 200,
        },
    )


def _write_config(tmp_path: Path) -> Path:
    cfg = tmp_path / "config.toml"
    cfg.write_text(
        "[okx]\n"
        'api_key = "k"\n'
        'secret = "s"\n'
        'passphrase = "p"\n\n'
        "[storage]\n"
        f'data_dir = "{(tmp_path / "data").as_posix()}"\n\n'
        "[backtest]\n"
        'initial_cash = "10000"\n'
        'commission_rate = "0.001"\n'
        "slippage_bps = 5\n"
        "window_size = 200\n"
    )
    return cfg


def _write_strategy_config(tmp_path: Path) -> Path:
    cfg_dir = tmp_path / "strategies"
    cfg_dir.mkdir(parents=True, exist_ok=True)
    (cfg_dir / "sma_cross.toml").write_text(
        "[strategy]\n"
        'strategy_id = "sma_cross"\n'
        'class = "quant.strategies.python.sma_cross.SmaCrossStrategy"\n\n'
        "[params]\nfast_len = 5\nslow_len = 20\n"
    )
    return cfg_dir


class TestParsing:
    def test_parse_range_includes_end_day(self) -> None:
        start, end = parse_range("2026-09-15", "2026-09-16")
        assert end - start == 2 * DAY

    def test_parse_range_rejects_reversed(self) -> None:
        with pytest.raises(BacktestSetupError, match="must not be after"):
            parse_range("2026-09-16", "2026-09-15")

    def test_parse_range_rejects_bad_date(self) -> None:
        with pytest.raises(BacktestSetupError, match="invalid date"):
            parse_range("2026/09/15", "2026-09-16")

    def test_parse_params(self) -> None:
        assert parse_params("fast_len=10;slow_len=50") == {
            "fast_len": 10,
            "slow_len": 50,
        }

    def test_parse_params_empty(self) -> None:
        assert parse_params(None) == {}
        assert parse_params("") == {}

    def test_parse_params_types(self) -> None:
        assert parse_params("a=1.5;b=true;c=hello") == {
            "a": 1.5,
            "b": True,
            "c": "hello",
        }

    def test_parse_params_invalid(self) -> None:
        with pytest.raises(BacktestSetupError, match="expect key=value"):
            parse_params("fast_len")

    def test_parse_grid(self) -> None:
        assert parse_grid("fast_len=5,10;slow_len=20,50") == {
            "fast_len": [5, 10],
            "slow_len": [20, 50],
        }

    def test_parse_grid_empty(self) -> None:
        with pytest.raises(BacktestSetupError, match="empty param grid"):
            parse_grid("")

    def test_parse_grid_missing_values(self) -> None:
        with pytest.raises(BacktestSetupError, match="empty value list"):
            parse_grid("fast_len=")

    def test_format_metric_percent(self) -> None:
        assert format_metric("total_return", 0.1234) == "12.34%"
        assert format_metric("max_drawdown", 0.5) == "50.00%"

    def test_format_metric_count(self) -> None:
        assert format_metric("trade_count", 7.0) == "7"

    def test_format_metrics_skips_unknown(self) -> None:
        text = format_metrics({"sharpe": 1.5, "unknown": 1.0})
        assert "夏普比率" in text
        assert "unknown" not in text

    def test_report_paths(self, tmp_path) -> None:
        csv_path, html_path = report_paths(
            tmp_path, "sma_cross", INST, "1H", "2026-09-15", "2026-10-24"
        )
        assert csv_path.name == "sma_cross-BTC-USDT-1H-2026-09-15_2026-10-24.csv"
        assert html_path.suffix == ".html"


class TestLoadBars:
    def test_splits_warmup_and_range(self, tmp_path) -> None:
        settings = _settings(tmp_path)
        BarStorage(settings.storage.data_dir).save_bars(_wave_bars(300))
        start = START + 100 * HOUR

        warmup, bars = load_bars(settings, INST, "1H", start, start + 50 * HOUR, 20)

        assert len(warmup) == 20
        assert all(b.ts < start for b in warmup)
        assert bars[0].ts == start
        assert len(bars) == 50

    def test_rejects_insufficient_warmup(self, tmp_path) -> None:
        settings = _settings(tmp_path)
        BarStorage(settings.storage.data_dir).save_bars(_wave_bars(30))

        with pytest.raises(BacktestSetupError, match="not enough warmup"):
            load_bars(settings, INST, "1H", START + 5 * HOUR, START + 10 * HOUR, 200)

    def test_rejects_empty_range(self, tmp_path) -> None:
        settings = _settings(tmp_path)
        BarStorage(settings.storage.data_dir).save_bars(_wave_bars(30))

        # 区间窄于一根 bar，落在两根之间；预热段仍能取到历史
        with pytest.raises(BacktestSetupError, match="no bars in range"):
            load_bars(
                settings,
                INST,
                "1H",
                START + 60_000,
                START + 120_000,
                20,
            )

    def test_rejects_missing_data(self, tmp_path) -> None:
        settings = _settings(tmp_path)
        with pytest.raises(BacktestSetupError, match="no local bars"):
            load_bars(settings, INST, "1H", START, START + HOUR, 0)

    def test_rejects_unknown_bar(self, tmp_path) -> None:
        settings = _settings(tmp_path)
        with pytest.raises(BacktestSetupError, match="unsupported bar"):
            load_bars(settings, INST, "2H", START, START + HOUR, 0)

    def test_supports_six_and_twelve_hour_bars(self) -> None:
        from quant.backtest.runner import BAR_MS

        assert BAR_MS["6H"] == 6 * HOUR
        assert BAR_MS["12H"] == 12 * HOUR

    def test_build_matcher(self, tmp_path) -> None:
        matcher = build_matcher(_settings(tmp_path))
        assert matcher.commission_rate == Decimal("0.001")
        assert matcher.slippage_rate == Decimal("0.0005")


class TestWarmup:
    def test_warmup_drives_strategy_without_trades(self) -> None:
        bars = _wave_bars(10)
        strategy = RecordingStrategy()

        result = Backtest(
            strategy,
            Matcher(),
            warmup_bars=_wave_bars(5, START - 5 * HOUR),
        ).run(bars)

        assert len(strategy.seen) == 15
        assert result.trades == []
        assert len(result.equity_curve) == len(bars)
        assert result.equity_curve[0][0] == bars[0].ts

    def test_warmup_makes_moving_average_ready(self) -> None:
        """预热后第一根即可交易，而冷启动需要 slow_len 根。"""
        bars = _wave_bars(80)
        warmup = _wave_bars(40, START - 40 * HOUR)
        params = {"fast_len": 5, "slow_len": 20}

        cold = Backtest(
            SmaCrossStrategy(), Matcher(), params=params
        ).run(bars)
        warm = Backtest(
            SmaCrossStrategy(), Matcher(), params=params, warmup_bars=warmup
        ).run(bars)

        assert len(warm.trades) > len(cold.trades)

    def test_rejects_warmup_after_range(self) -> None:
        bars = _wave_bars(10)
        with pytest.raises(ValueError, match="must precede backtest range"):
            Backtest(
                RecordingStrategy(), Matcher(), warmup_bars=bars
            ).run(bars)

    def test_rejects_warmup_other_inst(self) -> None:
        warmup = [
            Bar(
                ts=START - HOUR,
                open=Decimal("1"),
                high=Decimal("1"),
                low=Decimal("1"),
                close=Decimal("1"),
                vol=Decimal("1"),
                vol_quote=Decimal("1"),
                confirm=1,
                inst_id="ETH-USDT",
                bar_size="1H",
            )
        ]
        with pytest.raises(ValueError, match="warmup inst"):
            Backtest(
                RecordingStrategy(), Matcher(), warmup_bars=warmup
            ).run(_wave_bars(3))


class TestOptimizer:
    def _bars(self) -> list[Bar]:
        return _wave_bars(120)

    def test_base_params_reach_strategy(self) -> None:
        seen: list[dict] = []

        class Spy(RecordingStrategy):
            def on_init(self, ctx) -> None:
                seen.append(dict(ctx.params))

        GridOptimizer(
            lambda p: Spy(),
            {"fast_len": [3, 5]},
            base_params={"slow_len": 20, "commission_rate": "0.001"},
        ).search(self._bars())

        assert len(seen) == 2
        assert all(p["slow_len"] == 20 for p in seen)

    def test_skips_invalid_combinations(self) -> None:
        results = GridOptimizer(
            lambda p: SmaCrossStrategy(),
            {"fast_len": [5, 50], "slow_len": [20]},
        ).search(self._bars())

        # fast_len=50 >= slow_len=20 非法，只剩一组
        assert len(results) == 1
        assert results[0][0]["fast_len"] == 5

    def test_all_invalid_raises(self) -> None:
        with pytest.raises(ValueError, match="all 1 parameter combinations failed"):
            GridOptimizer(
                lambda p: SmaCrossStrategy(),
                {"fast_len": [100], "slow_len": [20]},
            ).search(self._bars())

    def test_unknown_metric_raises(self) -> None:
        with pytest.raises(ValueError, match="unknown metric"):
            GridOptimizer(
                lambda p: SmaCrossStrategy(), {"fast_len": [5]}
            ).search(self._bars(), metric="nope")


class TestCliBacktest:
    def _env(self, tmp_path):
        settings = _settings(tmp_path)
        # 数据覆盖 2026-08-23 .. 2026-09-13，保证区间前有预热
        BarStorage(settings.storage.data_dir).save_bars(
            _wave_bars(500, START - 200 * HOUR)
        )
        return _write_config(tmp_path), _write_strategy_config(tmp_path)

    def _args(self, tmp_path, config, cfg_dir, **overrides):
        base = dict(
            config=str(config),
            strategy="sma_cross",
            inst=INST,
            bar="1H",
            start="2026-09-05",
            end="2026-09-12",
            params=None,
            warmup_bars=100,
            config_dir=str(cfg_dir),
            out_dir=str(tmp_path / "reports"),
            no_report=False,
            funding=False,
        )
        base.update(overrides)
        return SimpleNamespace(**base)

    def test_writes_reports(self, tmp_path, capsys) -> None:
        config, cfg_dir = self._env(tmp_path)

        code = backtest_main(self._args(tmp_path, config, cfg_dir))

        assert code == 0
        out = capsys.readouterr().out
        assert "总收益率" in out
        files = list((tmp_path / "reports").iterdir())
        assert {p.suffix for p in files} == {".csv", ".html"}

    def test_no_report(self, tmp_path, capsys) -> None:
        config, cfg_dir = self._env(tmp_path)

        code = backtest_main(
            self._args(tmp_path, config, cfg_dir, no_report=True)
        )

        assert code == 0
        assert "CSV :" not in capsys.readouterr().out
        assert not (tmp_path / "reports").exists()

    def test_params_override(self, tmp_path) -> None:
        config, cfg_dir = self._env(tmp_path)

        code = backtest_main(
            self._args(
                tmp_path, config, cfg_dir, params="fast_len=10;slow_len=40"
            )
        )

        assert code == 0

    def test_missing_data_returns_1(self, tmp_path) -> None:
        config = _write_config(tmp_path)
        cfg_dir = _write_strategy_config(tmp_path)

        code = backtest_main(self._args(tmp_path, config, cfg_dir))

        assert code == 1

    def test_missing_strategy_returns_1(self, tmp_path) -> None:
        settings = _settings(tmp_path)
        BarStorage(settings.storage.data_dir).save_bars(
            _wave_bars(500, START - 200 * HOUR)
        )
        config = _write_config(tmp_path)

        code = backtest_main(
            self._args(tmp_path, config, tmp_path / "nope")
        )

        assert code == 1


class TestCliOptimize:
    def _env(self, tmp_path):
        settings = _settings(tmp_path)
        BarStorage(settings.storage.data_dir).save_bars(
            _wave_bars(500, START - 200 * HOUR)
        )
        return _write_config(tmp_path), _write_strategy_config(tmp_path)

    def _args(self, tmp_path, config, cfg_dir, **overrides):
        base = dict(
            config=str(config),
            strategy="sma_cross",
            inst=INST,
            bar="1H",
            start="2026-09-05",
            end="2026-09-12",
            params=None,
            warmup_bars=100,
            config_dir=str(cfg_dir),
            grid="fast_len=3,5;slow_len=20",
            metric="sharpe",
            top=5,
            n_jobs=1,
            max_combos=None,
            funding=False,
        )
        base.update(overrides)
        return SimpleNamespace(**base)

    def test_none_metric_does_not_crash(self, tmp_path, monkeypatch) -> None:
        """calmar/sortino 可能为 None：CLI 不能按原始值再排一次序（P0）。"""
        config, cfg_dir = self._env(tmp_path)

        class _FakeOptimizer:
            def __init__(self, *_args, **_kwargs) -> None:
                pass

            def search(self, *_args, **_kwargs):
                # search 已按 _rank 排好：合法值在前、None 在末尾
                return [
                    ({"fast_len": 10, "slow_len": 20}, {"calmar": 0.5}),
                    ({"fast_len": 5, "slow_len": 20}, {"calmar": None}),
                ]

        monkeypatch.setattr(
            "quant.cli.optimize.GridOptimizer", _FakeOptimizer
        )

        code = optimize_main(
            self._args(tmp_path, config, cfg_dir, metric="calmar")
        )

        assert code == 0

    def test_prints_ranking(self, tmp_path, capsys) -> None:
        config, cfg_dir = self._env(tmp_path)

        code = optimize_main(self._args(tmp_path, config, cfg_dir))

        assert code == 0
        out = capsys.readouterr().out
        assert "fast_len" in out
        assert "sharpe" in out

    def test_reports_skipped_combos(self, tmp_path, capsys) -> None:
        config, cfg_dir = self._env(tmp_path)

        code = optimize_main(
            self._args(
                tmp_path,
                config,
                cfg_dir,
                grid="fast_len=5,50;slow_len=20",
            )
        )

        assert code == 0
        assert "跳过 1 组" in capsys.readouterr().out

    def test_bad_metric_returns_1(self, tmp_path) -> None:
        config, cfg_dir = self._env(tmp_path)

        code = optimize_main(
            self._args(tmp_path, config, cfg_dir, metric="nope")
        )

        assert code == 1

    def test_combo_limit_returns_1(self, tmp_path) -> None:
        config, cfg_dir = self._env(tmp_path)

        code = optimize_main(
            self._args(tmp_path, config, cfg_dir, max_combos=1)
        )

        assert code == 1

    def test_parallel_runs(self, tmp_path) -> None:
        """--n-jobs >1 走进程池，退出码与串行一致。"""
        config, cfg_dir = self._env(tmp_path)

        assert optimize_main(self._args(tmp_path, config, cfg_dir)) == 0
        assert (
            optimize_main(
                self._args(tmp_path, config, cfg_dir, n_jobs=2)
            )
            == 0
        )
