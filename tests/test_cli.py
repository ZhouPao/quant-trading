"""CLI 入口单元测试（spec §11）。

覆盖：
- parse_date_to_ms 日期解析
- build_parser argparse 子命令解析
- fetch_candles.main 拉取命令（mock 下游）
- validate_data.main 校验命令（mock 下游）
- align_data.main 对齐命令（mock 下游）
- cli.main 分发逻辑

测试策略：用 unittest.mock.patch 替代 Settings.load、OKXClient、CandleFetcher 等，
不联网、不依赖实际配置文件。
"""
from __future__ import annotations

from decimal import Decimal
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from quant.cli import BAR_CHOICES, build_parser
from quant.cli import main as cli_main
from quant.cli.align_data import main as align_main
from quant.cli.fetch_candles import main as fetch_main
from quant.cli.fetch_candles import parse_date_to_ms
from quant.cli.fetch_funding_rates import main as fetch_funding_rates_main
from quant.cli.fetch_instruments import main as fetch_instruments_main
from quant.cli.fetch_open_interest import main as fetch_open_interest_main
from quant.cli.run_worker import _apply_paper_mode, _parse_strategies
from quant.cli.serve_webhook import needs_auth
from quant.cli.sync_candles import _sync_once
from quant.cli.validate_data import main as validate_main
from quant.foundation.config import OKXSettings, Settings, StorageSettings
from quant.risk.config import RiskConfig

# =====================================================================
# 工具函数
# =====================================================================


def _make_settings(data_dir: Path) -> Settings:
    """构造测试用 Settings（不读文件）。"""
    return Settings(
        okx=OKXSettings(
            api_key="test-key",
            secret="test-secret",
            passphrase="test-pass",
            demo=True,
        ),
        storage=StorageSettings(data_dir=data_dir),
    )


def _ns(**kwargs) -> MagicMock:
    """构造 argparse Namespace。"""
    ns = MagicMock()
    for k, v in kwargs.items():
        setattr(ns, k, v)
    return ns


# =====================================================================
# parse_date_to_ms 测试
# =====================================================================


class TestParseDateToMs:
    def test_normal_date(self) -> None:
        # 2024-01-01 00:00 UTC = 1704067200000
        assert parse_date_to_ms("2024-01-01") == 1704067200000

    def test_leap_year(self) -> None:
        # 2024-02-29 00:00 UTC
        assert parse_date_to_ms("2024-02-29") == 1709164800000

    def test_end_of_year(self) -> None:
        # 2024-12-31 00:00 UTC
        assert parse_date_to_ms("2024-12-31") == 1735603200000

    def test_invalid_format_raises(self) -> None:
        with pytest.raises(ValueError):
            parse_date_to_ms("2024/01/01")

    def test_invalid_date_raises(self) -> None:
        with pytest.raises(ValueError):
            parse_date_to_ms("2024-02-30")


# =====================================================================
# build_parser 测试
# =====================================================================


class TestBuildParser:
    def test_fetch_candles_parsed(self) -> None:
        parser = build_parser()
        args = parser.parse_args(
            [
                "fetch-candles",
                "--inst", "BTC-USDT",
                "--bar", "1H",
                "--start", "2024-01-01",
                "--end", "2024-12-31",
            ]
        )
        assert args.command == "fetch-candles"
        assert args.inst == "BTC-USDT"
        assert args.bar == "1H"
        assert args.start == "2024-01-01"
        assert args.end == "2024-12-31"
        assert args.config == "config/config.toml"

    def test_fetch_candles_bar_default(self) -> None:
        parser = build_parser()
        args = parser.parse_args(
            ["fetch-candles", "--inst", "BTC-USDT",
             "--start", "2024-01-01", "--end", "2024-12-31"]
        )
        assert args.bar == "1H"

    def test_validate_data_parsed(self) -> None:
        parser = build_parser()
        args = parser.parse_args(
            ["validate-data", "--inst", "BTC-USDT", "--bar", "1H"]
        )
        assert args.command == "validate-data"
        assert args.inst == "BTC-USDT"
        assert args.bar == "1H"
        assert args.start is None
        assert args.end is None

    def test_validate_data_with_range(self) -> None:
        parser = build_parser()
        args = parser.parse_args(
            [
                "validate-data",
                "--inst", "BTC-USDT",
                "--bar", "1H",
                "--start", "2024-01-01",
                "--end", "2024-12-31",
            ]
        )
        assert args.start == "2024-01-01"
        assert args.end == "2024-12-31"

    def test_align_data_parsed(self) -> None:
        parser = build_parser()
        args = parser.parse_args(
            [
                "align-data",
                "--insts", "BTC-USDT,ETH-USDT",
                "--bar", "1H",
                "--fill", "ffill",
            ]
        )
        assert args.command == "align-data"
        assert args.insts == "BTC-USDT,ETH-USDT"
        assert args.bar == "1H"
        assert args.fill == "ffill"
        assert args.output is None

    def test_align_data_fill_invalid_choice(self) -> None:
        parser = build_parser()
        with pytest.raises(SystemExit):
            parser.parse_args(
                [
                    "align-data",
                    "--insts", "BTC-USDT,ETH-USDT",
                    "--bar", "1H",
                    "--fill", "invalid",
                ]
            )

    def test_no_subcommand_error(self) -> None:
        parser = build_parser()
        with pytest.raises(SystemExit):
            parser.parse_args([])

    def test_missing_required_arg(self) -> None:
        parser = build_parser()
        with pytest.raises(SystemExit):
            parser.parse_args(["fetch-candles", "--inst", "BTC-USDT"])


# =====================================================================
# fetch_candles.main 测试
# =====================================================================


class TestFetchCandlesMain:
    @patch("quant.cli.fetch_candles.CandleFetcher")
    @patch("quant.cli.fetch_candles.OKXClient")
    @patch("quant.cli.fetch_candles.Settings.load")
    def test_normal_fetch(
        self, mock_load, mock_client_cls, mock_fetcher_cls, tmp_path
    ):
        mock_load.return_value = _make_settings(tmp_path)
        mock_client = MagicMock()
        mock_client_cls.return_value = mock_client
        mock_fetcher = MagicMock()
        mock_fetcher_cls.return_value = mock_fetcher
        mock_fetcher.fetch.return_value = 720

        args = _ns(
            inst="BTC-USDT",
            bar="1H",
            start="2024-01-01",
            end="2024-12-31",
            config="config/config.toml",
        )

        ret = fetch_main(args)

        assert ret == 0
        mock_load.assert_called_once_with("config/config.toml")
        mock_client_cls.assert_called_once()
        mock_fetcher_cls.assert_called_once_with(
            mock_client, tmp_path
        )
        mock_fetcher.fetch.assert_called_once()
        call_kwargs = mock_fetcher.fetch.call_args
        assert call_kwargs.kwargs["inst_id"] == "BTC-USDT"
        assert call_kwargs.kwargs["bar_size"] == "1H"
        assert call_kwargs.kwargs["start_ts"] == 1704067200000
        assert call_kwargs.kwargs["end_ts"] == 1735603200000
        mock_client.close.assert_called_once()

    @patch("quant.cli.fetch_candles.Settings.load")
    def test_start_after_end(self, mock_load, tmp_path):
        mock_load.return_value = _make_settings(tmp_path)

        args = _ns(
            inst="BTC-USDT",
            bar="1H",
            start="2024-12-31",
            end="2024-01-01",
            config="config/config.toml",
        )

        ret = fetch_main(args)
        assert ret == 1

    @patch("quant.cli.fetch_candles.CandleFetcher")
    @patch("quant.cli.fetch_candles.OKXClient")
    @patch("quant.cli.fetch_candles.Settings.load")
    def test_fetch_exception_returns_1(
        self, mock_load, mock_client_cls, mock_fetcher_cls, tmp_path
    ):
        mock_load.return_value = _make_settings(tmp_path)
        mock_client = MagicMock()
        mock_client_cls.return_value = mock_client
        mock_fetcher = MagicMock()
        mock_fetcher_cls.return_value = mock_fetcher
        mock_fetcher.fetch.side_effect = RuntimeError("network error")

        args = _ns(
            inst="BTC-USDT",
            bar="1H",
            start="2024-01-01",
            end="2024-12-31",
            config="config/config.toml",
        )

        ret = fetch_main(args)
        assert ret == 1
        mock_client.close.assert_called_once()

    @patch("quant.cli.fetch_candles.CandleFetcher")
    @patch("quant.cli.fetch_candles.OKXClient")
    @patch("quant.cli.fetch_candles.Settings.load")
    def test_config_path_passed(
        self, mock_load, mock_client_cls, mock_fetcher_cls, tmp_path
    ):
        mock_load.return_value = _make_settings(tmp_path)
        mock_client = MagicMock()
        mock_client_cls.return_value = mock_client
        mock_fetcher = MagicMock()
        mock_fetcher_cls.return_value = mock_fetcher
        mock_fetcher.fetch.return_value = 10

        args = _ns(
            inst="BTC-USDT",
            bar="1D",
            start="2024-01-01",
            end="2024-01-31",
            config="custom.toml",
        )

        ret = fetch_main(args)
        assert ret == 0
        mock_load.assert_called_once_with("custom.toml")


# =====================================================================
# validate_data.main 测试
# =====================================================================


class TestValidateDataMain:
    @patch("quant.cli.validate_data.DataValidator")
    @patch("quant.cli.validate_data.BarStorage")
    @patch("quant.cli.validate_data.Settings.load")
    def test_data_ok_returns_0(
        self, mock_load, mock_storage_cls, mock_validator_cls, tmp_path
    ):
        mock_load.return_value = _make_settings(tmp_path)
        mock_storage = MagicMock()
        mock_storage_cls.return_value = mock_storage
        mock_storage.load_bars.return_value = [MagicMock(), MagicMock()]
        mock_validator = MagicMock()
        mock_validator_cls.return_value = mock_validator
        report = MagicMock()
        report.is_ok = True
        report.summary.return_value = "OK"
        report.duplicates = []
        report.missing = []
        report.invalid_ohlc = []
        report.errors = []
        mock_validator.validate.return_value = report

        args = _ns(
            inst="BTC-USDT",
            bar="1H",
            start="2024-01-01",
            end="2024-12-31",
            config="config/config.toml",
        )

        ret = validate_main(args)
        assert ret == 0
        mock_storage.load_bars.assert_called_once()

    @patch("quant.cli.validate_data.DataValidator")
    @patch("quant.cli.validate_data.BarStorage")
    @patch("quant.cli.validate_data.Settings.load")
    def test_data_has_issues_returns_1(
        self, mock_load, mock_storage_cls, mock_validator_cls, tmp_path
    ):
        mock_load.return_value = _make_settings(tmp_path)
        mock_storage = MagicMock()
        mock_storage_cls.return_value = mock_storage
        mock_storage.load_bars.return_value = [MagicMock()]
        mock_validator = MagicMock()
        mock_validator_cls.return_value = mock_validator
        report = MagicMock()
        report.is_ok = False
        report.summary.return_value = "has errors"
        report.duplicates = [MagicMock(ts=123, count=2)]
        report.missing = []
        report.invalid_ohlc = []
        report.errors = ["some error"]
        mock_validator.validate.return_value = report

        args = _ns(
            inst="BTC-USDT",
            bar="1H",
            start="2024-01-01",
            end="2024-12-31",
            config="config/config.toml",
        )

        ret = validate_main(args)
        assert ret == 1

    @patch("quant.cli.validate_data.BarStorage")
    @patch("quant.cli.validate_data.Settings.load")
    def test_no_data_with_range_returns_1(
        self, mock_load, mock_storage_cls, tmp_path
    ):
        mock_load.return_value = _make_settings(tmp_path)
        mock_storage = MagicMock()
        mock_storage_cls.return_value = mock_storage
        mock_storage.load_bars.return_value = []

        args = _ns(
            inst="BTC-USDT",
            bar="1H",
            start="2024-01-01",
            end="2024-12-31",
            config="config/config.toml",
        )

        ret = validate_main(args)
        assert ret == 1

    @patch("quant.cli.validate_data.BarStorage")
    @patch("quant.cli.validate_data.Settings.load")
    def test_no_data_without_range_returns_1(
        self, mock_load, mock_storage_cls, tmp_path
    ):
        mock_load.return_value = _make_settings(tmp_path)
        mock_storage = MagicMock()
        mock_storage_cls.return_value = mock_storage
        mock_storage.list_years.return_value = []

        args = _ns(
            inst="BTC-USDT",
            bar="1H",
            start=None,
            end=None,
            config="config/config.toml",
        )

        ret = validate_main(args)
        assert ret == 1
        mock_storage.list_years.assert_called_once_with("BTC-USDT", "1H")

    @patch("quant.cli.validate_data.DataValidator")
    @patch("quant.cli.validate_data.BarStorage")
    @patch("quant.cli.validate_data.Settings.load")
    def test_full_range_uses_list_years(
        self, mock_load, mock_storage_cls, mock_validator_cls, tmp_path
    ):
        mock_load.return_value = _make_settings(tmp_path)
        mock_storage = MagicMock()
        mock_storage_cls.return_value = mock_storage
        mock_storage.list_years.return_value = [2024]
        mock_storage.load_bars.return_value = [MagicMock()]
        mock_validator = MagicMock()
        mock_validator_cls.return_value = mock_validator
        report = MagicMock()
        report.is_ok = True
        report.summary.return_value = "OK"
        report.duplicates = []
        report.missing = []
        report.invalid_ohlc = []
        report.errors = []
        mock_validator.validate.return_value = report

        args = _ns(
            inst="BTC-USDT",
            bar="1H",
            start=None,
            end=None,
            config="config/config.toml",
        )

        ret = validate_main(args)
        assert ret == 0
        mock_storage.list_years.assert_called_once()


# =====================================================================
# align_data.main 测试
# =====================================================================


class TestAlignDataMain:
    @patch("quant.cli.align_data.DataAligner")
    @patch("quant.cli.align_data.BarStorage")
    @patch("quant.cli.align_data.Settings.load")
    def test_normal_align(
        self, mock_load, mock_storage_cls, mock_aligner_cls, tmp_path
    ):
        mock_load.return_value = _make_settings(tmp_path)
        mock_storage = MagicMock()
        mock_storage_cls.return_value = mock_storage
        mock_storage.load_bars.return_value = [MagicMock()]
        mock_aligner = MagicMock()
        mock_aligner_cls.return_value = mock_aligner
        mock_df = MagicMock()
        mock_df.__len__ = lambda self: 720
        mock_df.columns = ["BTC-USDT_close", "ETH-USDT_close"]
        mock_df.head.return_value.to_string.return_value = "preview"
        mock_aligner.align.return_value = mock_df

        args = _ns(
            insts="BTC-USDT,ETH-USDT",
            bar="1H",
            fill="ffill",
            start="2024-01-01",
            end="2024-12-31",
            output=None,
            config="config/config.toml",
        )

        ret = align_main(args)
        assert ret == 0
        assert mock_storage.load_bars.call_count == 2
        mock_aligner.align.assert_called_once()

    @patch("quant.cli.align_data.BarStorage")
    @patch("quant.cli.align_data.Settings.load")
    def test_single_inst_returns_1(
        self, mock_load, mock_storage_cls, tmp_path
    ):
        mock_load.return_value = _make_settings(tmp_path)

        args = _ns(
            insts="BTC-USDT",
            bar="1H",
            fill="ffill",
            start=None,
            end=None,
            output=None,
            config="config/config.toml",
        )

        ret = align_main(args)
        assert ret == 1

    @patch("quant.cli.align_data.BarStorage")
    @patch("quant.cli.align_data.Settings.load")
    def test_no_data_returns_1(
        self, mock_load, mock_storage_cls, tmp_path
    ):
        mock_load.return_value = _make_settings(tmp_path)
        mock_storage = MagicMock()
        mock_storage_cls.return_value = mock_storage
        mock_storage.list_years.return_value = []

        args = _ns(
            insts="BTC-USDT,ETH-USDT",
            bar="1H",
            fill="ffill",
            start=None,
            end=None,
            output=None,
            config="config/config.toml",
        )

        ret = align_main(args)
        assert ret == 1

    @patch("quant.cli.align_data.DataAligner")
    @patch("quant.cli.align_data.BarStorage")
    @patch("quant.cli.align_data.Settings.load")
    def test_one_inst_empty_returns_1(
        self, mock_load, mock_storage_cls, mock_aligner_cls, tmp_path
    ):
        mock_load.return_value = _make_settings(tmp_path)
        mock_storage = MagicMock()
        mock_storage_cls.return_value = mock_storage

        def load_bars(inst_id, bar, start, end):
            return [MagicMock()] if inst_id == "BTC-USDT" else []

        mock_storage.load_bars.side_effect = load_bars
        mock_storage.list_years.return_value = [2024]

        args = _ns(
            insts="BTC-USDT,ETH-USDT",
            bar="1H",
            fill="ffill",
            start=None,
            end=None,
            output=None,
            config="config/config.toml",
        )

        ret = align_main(args)
        assert ret == 1

    @patch("quant.cli.align_data.DataAligner")
    @patch("quant.cli.align_data.BarStorage")
    @patch("quant.cli.align_data.Settings.load")
    def test_output_parquet(
        self, mock_load, mock_storage_cls, mock_aligner_cls, tmp_path
    ):
        mock_load.return_value = _make_settings(tmp_path)
        mock_storage = MagicMock()
        mock_storage_cls.return_value = mock_storage
        mock_storage.load_bars.return_value = [MagicMock()]
        mock_aligner = MagicMock()
        mock_aligner_cls.return_value = mock_aligner

        mock_df = MagicMock()
        mock_df.__len__ = lambda self: 720
        mock_df.columns = ["BTC-USDT_close", "ETH-USDT_close"]
        mock_aligner.align.return_value = mock_df

        out_path = tmp_path / "aligned.parquet"
        args = _ns(
            insts="BTC-USDT,ETH-USDT",
            bar="1H",
            fill="ffill",
            start="2024-01-01",
            end="2024-12-31",
            output=str(out_path),
            config="config/config.toml",
        )

        ret = align_main(args)
        assert ret == 0
        mock_df.to_parquet.assert_called_once_with(Path(str(out_path)))


# =====================================================================
# cli.main 分发测试
# =====================================================================


class TestCliMainDispatch:
    @patch("quant.cli.fetch_candles_main")
    def test_dispatch_fetch_candles(self, mock_fetch):
        mock_fetch.return_value = 0
        ret = cli_main(
            [
                "fetch-candles",
                "--inst", "BTC-USDT",
                "--bar", "1H",
                "--start", "2024-01-01",
                "--end", "2024-12-31",
            ]
        )
        assert ret == 0
        mock_fetch.assert_called_once()

    @patch("quant.cli.validate_data_main")
    def test_dispatch_validate_data(self, mock_validate):
        mock_validate.return_value = 0
        ret = cli_main(
            ["validate-data", "--inst", "BTC-USDT", "--bar", "1H"]
        )
        assert ret == 0
        mock_validate.assert_called_once()

    @patch("quant.cli.align_data_main")
    def test_dispatch_align_data(self, mock_align):
        mock_align.return_value = 0
        ret = cli_main(
            [
                "align-data",
                "--insts", "BTC-USDT,ETH-USDT",
                "--bar", "1H",
            ]
        )
        assert ret == 0
        mock_align.assert_called_once()

    def test_no_command_exits(self):
        with pytest.raises(SystemExit):
            cli_main([])


# =====================================================================
# build_parser: fetch-instruments / fetch-funding-rates 解析
# =====================================================================


class TestBuildParserNewCommands:
    def test_fetch_instruments_parsed(self) -> None:
        parser = build_parser()
        args = parser.parse_args(
            ["fetch-instruments", "--inst-type", "SPOT"]
        )
        assert args.command == "fetch-instruments"
        assert args.inst_type == "SPOT"
        assert args.config == "config/config.toml"

    def test_fetch_instruments_missing_inst_type(self) -> None:
        parser = build_parser()
        with pytest.raises(SystemExit):
            parser.parse_args(["fetch-instruments"])

    def test_fetch_funding_rates_parsed(self) -> None:
        parser = build_parser()
        args = parser.parse_args(
            ["fetch-funding-rates", "--inst", "BTC-USDT-SWAP"]
        )
        assert args.command == "fetch-funding-rates"
        assert args.inst == "BTC-USDT-SWAP"
        assert args.config == "config/config.toml"

    def test_fetch_funding_rates_missing_inst(self) -> None:
        parser = build_parser()
        with pytest.raises(SystemExit):
            parser.parse_args(["fetch-funding-rates"])

    def test_run_worker_parsed_with_broker_flags(self) -> None:
        parser = build_parser()
        args = parser.parse_args(
            [
                "run-worker",
                "--strategy", "sma_cross",
                "--broker", "okx",
                "--td-mode", "cash",
                "--ws",
            ]
        )
        assert args.command == "run-worker"
        assert args.strategy == "sma_cross"
        assert args.broker == "okx"
        assert args.td_mode == "cash"
        assert args.ws is True
        assert args.heartbeat_interval == 60

    def test_run_worker_broker_defaults_to_mock(self) -> None:
        parser = build_parser()
        args = parser.parse_args(["run-worker", "--strategy", "rsi"])
        assert args.broker == "mock"
        assert args.ws is False

    def test_check_live_parsed(self) -> None:
        parser = build_parser()
        args = parser.parse_args(["check-live"])
        assert args.command == "check-live"
        assert args.config == "config/config.toml"

    def test_run_worker_paper_mode_flag(self) -> None:
        parser = build_parser()
        args = parser.parse_args(
            ["run-worker", "--strategy", "sma_cross", "--paper-mode"]
        )
        assert args.paper_mode is True

    def test_run_worker_paper_mode_defaults_off(self) -> None:
        parser = build_parser()
        args = parser.parse_args(["run-worker", "--strategy", "sma_cross"])
        assert args.paper_mode is False

    def test_run_worker_order_lifecycle_defaults_from_config(self) -> None:
        parser = build_parser()
        args = parser.parse_args(["run-worker", "--strategy", "sma_cross"])
        assert args.reconcile_interval is None
        assert args.order_timeout is None

    def test_run_worker_order_lifecycle_flags(self) -> None:
        parser = build_parser()
        args = parser.parse_args(
            [
                "run-worker",
                "--strategy", "sma_cross",
                "--reconcile-interval", "30",
                "--order-timeout", "0",
            ]
        )
        assert args.reconcile_interval == 30
        assert args.order_timeout == 0

    def test_simulate_alert_parsed(self) -> None:
        parser = build_parser()
        args = parser.parse_args(
            [
                "simulate-alert",
                "--side", "buy",
                "--qty", "0.001",
                "--inst", "BTC-USDT",
                "--order-type", "limit",
                "--price", "64000",
            ]
        )
        assert args.command == "simulate-alert"
        assert args.side == "buy"
        assert args.qty == "0.001"
        assert args.order_type == "limit"
        assert args.price == "64000"
        assert args.dry_run is False

    def test_simulate_alert_requires_side_and_qty(self) -> None:
        parser = build_parser()
        with pytest.raises(SystemExit):
            parser.parse_args(["simulate-alert", "--qty", "0.001"])
        with pytest.raises(SystemExit):
            parser.parse_args(["simulate-alert", "--side", "buy"])

    def test_paper_report_parsed(self) -> None:
        parser = build_parser()
        args = parser.parse_args(
            ["paper-report", "--strategy", "sma_cross", "--inst", "BTC-USDT"]
        )
        assert args.command == "paper-report"
        assert args.strategy == "sma_cross"
        assert args.inst == "BTC-USDT"
        assert args.window_hours == 24

    def test_paper_report_window_hours_flag(self) -> None:
        parser = build_parser()
        args = parser.parse_args(
            ["paper-report", "--inst", "BTC-USDT", "--window-hours", "60"]
        )
        assert args.window_hours == 60

    def test_monitor_parsed(self) -> None:
        parser = build_parser()
        args = parser.parse_args(["monitor"])
        assert args.command == "monitor"
        assert args.config == "config/config.toml"

    def test_kill_switch_parsed_on(self) -> None:
        parser = build_parser()
        args = parser.parse_args(
            ["kill-switch", "--on", "--reason", "panic", "--broker", "okx"]
        )
        assert args.command == "kill-switch"
        assert args.on is True
        assert args.reason == "panic"
        assert args.broker == "okx"

    def test_kill_switch_parsed_status_default(self) -> None:
        parser = build_parser()
        args = parser.parse_args(["kill-switch"])
        assert args.command == "kill-switch"
        assert args.on is False
        assert args.off is False
        assert args.status is False

    def test_dashboard_parsed_defaults(self) -> None:
        parser = build_parser()
        args = parser.parse_args(["dashboard"])
        assert args.command == "dashboard"
        assert args.host is None
        assert args.port is None
        assert args.web_dir is None
        assert args.config == "config/config.toml"

    def test_dashboard_parsed_overrides(self) -> None:
        parser = build_parser()
        args = parser.parse_args(
            ["dashboard", "--host", "0.0.0.0", "--port", "9000",
             "--web-dir", "/tmp/dist"]
        )
        assert args.host == "0.0.0.0"
        assert args.port == 9000
        assert args.web_dir == "/tmp/dist"


# =====================================================================
# _apply_paper_mode 测试
# =====================================================================


class TestApplyPaperMode:
    def _config(self) -> RiskConfig:
        return RiskConfig(
            strategy_id="sma_cross",
            max_position_pct=Decimal("0.3"),
            max_total_pct=Decimal("0.8"),
            max_order_qty=Decimal("10000"),
            daily_loss_limit=Decimal("0.05"),
            consecutive_loss_limit=3,
        )

    def test_relaxes_position_limits(self) -> None:
        result = _apply_paper_mode(self._config())
        assert result.max_position_pct == Decimal("1.0")
        assert result.max_total_pct == Decimal("1.0")

    def test_keeps_other_limits(self) -> None:
        result = _apply_paper_mode(self._config())
        assert result.max_order_qty == Decimal("10000")
        assert result.daily_loss_limit == Decimal("0.05")
        assert result.consecutive_loss_limit == 3
        assert result.strategy_id == "sma_cross"

    def test_does_not_mutate_original(self) -> None:
        config = self._config()
        _apply_paper_mode(config)
        assert config.max_position_pct == Decimal("0.3")
        assert config.max_total_pct == Decimal("0.8")


class TestMultiStrategyParsing:
    """多策略（M8-8）：--strategy 用逗号声明，信号按 strategy_id 路由。"""

    def test_single_strategy(self) -> None:
        assert _parse_strategies("sma_cross") == ["sma_cross"]

    def test_comma_separated_keeps_order(self) -> None:
        assert _parse_strategies("b, a ,c") == ["b", "a", "c"]

    def test_duplicates_removed(self) -> None:
        assert _parse_strategies("a,a,b") == ["a", "b"]

    def test_empty_rejected(self) -> None:
        with pytest.raises(ValueError):
            _parse_strategies(" , ")


class TestWebhookPublicBind:
    """公网监听必须配鉴权，否则任何人可推送下单信号（M8-8）。"""

    def test_loopback_without_auth_is_allowed(self) -> None:
        assert needs_auth("127.0.0.1", "", "") is False
        assert needs_auth("localhost", "", "") is False
        assert needs_auth("::1", "", "") is False

    def test_public_without_auth_is_refused(self) -> None:
        assert needs_auth("0.0.0.0", "", "") is True
        assert needs_auth("203.0.113.7", "", "") is True

    def test_public_with_token_or_hmac_is_allowed(self) -> None:
        assert needs_auth("0.0.0.0", "tk", "") is False
        assert needs_auth("0.0.0.0", "", "secret") is False


# =====================================================================
# kill-switch 撤单测试
# =====================================================================


class TestKillSwitchCancelOpenOrders:
    @pytest.mark.asyncio
    async def test_marks_local_open_orders_canceled(self, tmp_path) -> None:
        from quant.backtest.order import Order
        from quant.backtest.signal import Signal
        from quant.cli.kill_switch import _cancel_open_orders
        from quant.foundation.config import LoggingSettings, StateSettings
        from quant.state.sqlite_store import SQLiteStore

        settings = Settings(
            okx=OKXSettings(
                api_key="k", secret="s", passphrase="p", demo=True
            ),
            storage=StorageSettings(data_dir=tmp_path),
            state=StateSettings(db_path=tmp_path / "quant.db"),
            logging=LoggingSettings(),
        )
        store = SQLiteStore(tmp_path / "quant.db")
        signal = Signal(
            strategy_id="sma_cross",
            side="buy",
            order_type="market",
            inst_id="BTC-USDT",
            qty=Decimal("1"),
            ts=1_700_000_000_000,
            signal_id="sig-1",
        )
        store.save_signal(signal, "executed")
        store.save_order(
            Order(
                order_id="o-1",
                signal=signal,
                status="live",
                created_ts=1_700_000_000_000,
            )
        )
        store.close()

        with patch("quant.cli.kill_switch.OKXClient"), patch(
            "quant.cli.kill_switch.OkxBroker"
        ) as broker_cls:
            broker_cls.return_value.cancel_all_open_orders = _async_return(2)

            canceled = await _cancel_open_orders(settings, MagicMock())

        assert canceled == 2
        check = SQLiteStore(tmp_path / "quant.db")
        try:
            assert check.get_open_orders() == []
        finally:
            check.close()

    @pytest.mark.asyncio
    async def test_exchange_failure_leaves_local_intact(self, tmp_path) -> None:
        from quant.backtest.order import Order
        from quant.backtest.signal import Signal
        from quant.cli.kill_switch import _cancel_open_orders
        from quant.foundation.config import LoggingSettings, StateSettings
        from quant.state.sqlite_store import SQLiteStore

        settings = Settings(
            okx=OKXSettings(
                api_key="k", secret="s", passphrase="p", demo=True
            ),
            storage=StorageSettings(data_dir=tmp_path),
            state=StateSettings(db_path=tmp_path / "quant.db"),
            logging=LoggingSettings(),
        )
        store = SQLiteStore(tmp_path / "quant.db")
        signal = Signal(
            strategy_id="sma_cross",
            side="buy",
            order_type="market",
            inst_id="BTC-USDT",
            qty=Decimal("1"),
            ts=1_700_000_000_000,
            signal_id="sig-1",
        )
        store.save_signal(signal, "executed")
        store.save_order(
            Order(
                order_id="o-1",
                signal=signal,
                status="live",
                created_ts=1_700_000_000_000,
            )
        )
        store.close()

        with patch("quant.cli.kill_switch.OKXClient"), patch(
            "quant.cli.kill_switch.OkxBroker"
        ) as broker_cls:
            broker_cls.return_value.cancel_all_open_orders = _async_raise(
                RuntimeError("api down")
            )

            canceled = await _cancel_open_orders(settings, MagicMock())

        assert canceled == 0
        check = SQLiteStore(tmp_path / "quant.db")
        try:
            # 交易所没撤成功就不能谎报本地已撤，否则会留下真实挂单
            assert len(check.get_open_orders()) == 1
        finally:
            check.close()


def _async_return(value):
    async def _inner(*_args, **_kwargs):
        return value

    return _inner


def _async_raise(exc):
    async def _inner(*_args, **_kwargs):
        raise exc

    return _inner


# =====================================================================
# fetch_instruments.main 测试
# =====================================================================


class TestFetchInstrumentsMain:
    @patch("quant.cli.fetch_instruments.InstrumentFetcher")
    @patch("quant.cli.fetch_instruments.OKXClient")
    @patch("quant.cli.fetch_instruments.Settings.load")
    def test_normal_fetch(
        self, mock_load, mock_client_cls, mock_fetcher_cls, tmp_path
    ):
        mock_load.return_value = _make_settings(tmp_path)
        mock_client = MagicMock()
        mock_client_cls.return_value = mock_client
        mock_fetcher = MagicMock()
        mock_fetcher_cls.return_value = mock_fetcher
        mock_fetcher.fetch.return_value = 500

        args = _ns(
            inst_type="SPOT",
            config="config/config.toml",
        )

        ret = fetch_instruments_main(args)

        assert ret == 0
        mock_load.assert_called_once_with("config/config.toml")
        mock_client_cls.assert_called_once()
        mock_fetcher_cls.assert_called_once_with(mock_client, tmp_path)
        mock_fetcher.fetch.assert_called_once_with(inst_type="SPOT")
        mock_client.close.assert_called_once()

    @patch("quant.cli.fetch_instruments.Settings.load")
    def test_invalid_inst_type_returns_1(self, mock_load, tmp_path):
        mock_load.return_value = _make_settings(tmp_path)

        args = _ns(
            inst_type="INVALID",
            config="config/config.toml",
        )

        ret = fetch_instruments_main(args)
        assert ret == 1

    @patch("quant.cli.fetch_instruments.InstrumentFetcher")
    @patch("quant.cli.fetch_instruments.OKXClient")
    @patch("quant.cli.fetch_instruments.Settings.load")
    def test_exception_returns_1(
        self, mock_load, mock_client_cls, mock_fetcher_cls, tmp_path
    ):
        mock_load.return_value = _make_settings(tmp_path)
        mock_client = MagicMock()
        mock_client_cls.return_value = mock_client
        mock_fetcher = MagicMock()
        mock_fetcher_cls.return_value = mock_fetcher
        mock_fetcher.fetch.side_effect = RuntimeError("network error")

        args = _ns(
            inst_type="SPOT",
            config="config/config.toml",
        )

        ret = fetch_instruments_main(args)
        assert ret == 1
        mock_client.close.assert_called_once()

    @patch("quant.cli.fetch_instruments.InstrumentFetcher")
    @patch("quant.cli.fetch_instruments.OKXClient")
    @patch("quant.cli.fetch_instruments.Settings.load")
    def test_config_path_passed(
        self, mock_load, mock_client_cls, mock_fetcher_cls, tmp_path
    ):
        mock_load.return_value = _make_settings(tmp_path)
        mock_client = MagicMock()
        mock_client_cls.return_value = mock_client
        mock_fetcher = MagicMock()
        mock_fetcher_cls.return_value = mock_fetcher
        mock_fetcher.fetch.return_value = 10

        args = _ns(
            inst_type="SWAP",
            config="custom.toml",
        )

        ret = fetch_instruments_main(args)
        assert ret == 0
        mock_load.assert_called_once_with("custom.toml")


# =====================================================================
# fetch_funding_rates.main 测试
# =====================================================================


class TestFetchFundingRatesMain:
    @patch("quant.cli.fetch_funding_rates.FundingRateFetcher")
    @patch("quant.cli.fetch_funding_rates.OKXClient")
    @patch("quant.cli.fetch_funding_rates.Settings.load")
    def test_normal_fetch_new(
        self, mock_load, mock_client_cls, mock_fetcher_cls, tmp_path
    ):
        mock_load.return_value = _make_settings(tmp_path)
        mock_client = MagicMock()
        mock_client_cls.return_value = mock_client
        mock_fetcher = MagicMock()
        mock_fetcher_cls.return_value = mock_fetcher
        mock_fetcher.fetch.return_value = 1

        args = _ns(
            inst="BTC-USDT-SWAP",
            config="config/config.toml",
            start=None,
            end=None,
            max_pages=100,
        )

        ret = fetch_funding_rates_main(args)

        assert ret == 0
        mock_load.assert_called_once_with("config/config.toml")
        mock_client_cls.assert_called_once()
        mock_fetcher_cls.assert_called_once_with(mock_client, tmp_path)
        mock_fetcher.fetch.assert_called_once_with(
            inst_id="BTC-USDT-SWAP"
        )
        mock_client.close.assert_called_once()

    @patch("quant.cli.fetch_funding_rates.FundingRateFetcher")
    @patch("quant.cli.fetch_funding_rates.OKXClient")
    @patch("quant.cli.fetch_funding_rates.Settings.load")
    def test_normal_fetch_duplicate(
        self, mock_load, mock_client_cls, mock_fetcher_cls, tmp_path
    ):
        mock_load.return_value = _make_settings(tmp_path)
        mock_client = MagicMock()
        mock_client_cls.return_value = mock_client
        mock_fetcher = MagicMock()
        mock_fetcher_cls.return_value = mock_fetcher
        mock_fetcher.fetch.return_value = 0  # 已存在，无新增

        args = _ns(
            inst="BTC-USDT-SWAP",
            config="config/config.toml",
            start=None,
            end=None,
            max_pages=100,
        )

        ret = fetch_funding_rates_main(args)
        assert ret == 0  # 无新增也返回 0

    @patch("quant.cli.fetch_funding_rates.Settings.load")
    def test_empty_inst_returns_1(self, mock_load, tmp_path):
        mock_load.return_value = _make_settings(tmp_path)

        args = _ns(
            inst="",
            config="config/config.toml",
        )

        ret = fetch_funding_rates_main(args)
        assert ret == 1

    @patch("quant.cli.fetch_funding_rates.FundingRateFetcher")
    @patch("quant.cli.fetch_funding_rates.OKXClient")
    @patch("quant.cli.fetch_funding_rates.Settings.load")
    def test_exception_returns_1(
        self, mock_load, mock_client_cls, mock_fetcher_cls, tmp_path
    ):
        mock_load.return_value = _make_settings(tmp_path)
        mock_client = MagicMock()
        mock_client_cls.return_value = mock_client
        mock_fetcher = MagicMock()
        mock_fetcher_cls.return_value = mock_fetcher
        mock_fetcher.fetch.side_effect = RuntimeError("api error")

        args = _ns(
            inst="BTC-USDT-SWAP",
            config="config/config.toml",
            start=None,
            end=None,
            max_pages=100,
        )

        ret = fetch_funding_rates_main(args)
        assert ret == 1
        mock_client.close.assert_called_once()

    @patch("quant.cli.fetch_funding_rates.FundingRateFetcher")
    @patch("quant.cli.fetch_funding_rates.OKXClient")
    @patch("quant.cli.fetch_funding_rates.Settings.load")
    def test_history_mode_backfills_range(
        self, mock_load, mock_client_cls, mock_fetcher_cls, tmp_path
    ):
        """传 --start 时应走 fetch_history，且区间换算成 UTC ms。"""
        mock_load.return_value = _make_settings(tmp_path)
        mock_client_cls.return_value = MagicMock()
        mock_fetcher = MagicMock()
        mock_fetcher_cls.return_value = mock_fetcher
        mock_fetcher.fetch_history.return_value = 5

        args = _ns(
            inst="BTC-USDT-SWAP",
            config="config/config.toml",
            start="2024-01-01",
            end="2024-02-01",
            max_pages=10,
        )

        ret = fetch_funding_rates_main(args)

        assert ret == 0
        mock_fetcher.fetch.assert_not_called()
        kwargs = mock_fetcher.fetch_history.call_args.kwargs
        assert kwargs["inst_id"] == "BTC-USDT-SWAP"
        assert kwargs["start_ts"] == parse_date_to_ms("2024-01-01")
        assert kwargs["end_ts"] == parse_date_to_ms("2024-02-01")
        assert kwargs["max_pages"] == 10

    @patch("quant.cli.fetch_funding_rates.FundingRateFetcher")
    @patch("quant.cli.fetch_funding_rates.OKXClient")
    @patch("quant.cli.fetch_funding_rates.Settings.load")
    def test_history_mode_without_end_covers_now(
        self, mock_load, mock_client_cls, mock_fetcher_cls, tmp_path
    ):
        """只传 --start 时 end_ts 为 None（到最新）。"""
        mock_load.return_value = _make_settings(tmp_path)
        mock_client_cls.return_value = MagicMock()
        mock_fetcher = MagicMock()
        mock_fetcher_cls.return_value = mock_fetcher
        mock_fetcher.fetch_history.return_value = 5

        args = _ns(
            inst="BTC-USDT-SWAP",
            config="config/config.toml",
            start="2024-01-01",
            end=None,
            max_pages=100,
        )

        assert fetch_funding_rates_main(args) == 0
        assert mock_fetcher.fetch_history.call_args.kwargs["end_ts"] is None

    @patch("quant.cli.fetch_funding_rates.Settings.load")
    def test_end_without_start_returns_1(self, mock_load, tmp_path):
        mock_load.return_value = _make_settings(tmp_path)

        args = _ns(
            inst="BTC-USDT-SWAP",
            config="config/config.toml",
            start=None,
            end="2024-02-01",
            max_pages=100,
        )

        assert fetch_funding_rates_main(args) == 1

    @patch("quant.cli.fetch_funding_rates.Settings.load")
    def test_start_after_end_returns_1(self, mock_load, tmp_path):
        mock_load.return_value = _make_settings(tmp_path)

        args = _ns(
            inst="BTC-USDT-SWAP",
            config="config/config.toml",
            start="2024-02-01",
            end="2024-01-01",
            max_pages=100,
        )

        assert fetch_funding_rates_main(args) == 1


# =====================================================================
# fetch-open-interest CLI（P0 回归：settings.data 不存在曾致一调即崩）
# =====================================================================


class TestFetchOpenInterestCli:
    @patch("quant.cli.fetch_open_interest.OpenInterestFetcher")
    @patch("quant.cli.fetch_open_interest.OKXClient")
    @patch("quant.cli.fetch_open_interest.Settings.load")
    def test_uses_storage_data_dir_and_respects_config(
        self, mock_load, mock_client_cls, mock_fetcher_cls, tmp_path
    ):
        """数据目录取 settings.storage.data_dir，配置路径尊重 --config。"""
        settings = _make_settings(tmp_path)
        mock_load.return_value = settings
        mock_client_cls.return_value = MagicMock()
        mock_fetcher = MagicMock()
        mock_fetcher_cls.return_value = mock_fetcher
        mock_fetcher.fetch.side_effect = [1, 0]

        args = _ns(inst=["BTC-USDT-SWAP", "ETH-USDT-SWAP"], config="other.toml")

        ret = fetch_open_interest_main(args)

        assert ret == 0
        # 数据目录来自 settings.storage.data_dir（原先的 settings.data 不存在）
        assert mock_fetcher_cls.call_args.args[1] == settings.storage.data_dir
        # 配置路径尊重 --config（原先写死 config/config.toml）
        mock_load.assert_called_once_with("other.toml")
        assert mock_fetcher.fetch.call_count == 2

    @patch("quant.cli.fetch_open_interest.OpenInterestFetcher")
    @patch("quant.cli.fetch_open_interest.OKXClient")
    @patch("quant.cli.fetch_open_interest.Settings.load")
    def test_fetch_error_returns_1_and_closes_client(
        self, mock_load, mock_client_cls, mock_fetcher_cls, tmp_path
    ):
        """单品种拉取失败要返回 1，且 client 必须被关闭。"""
        mock_load.return_value = _make_settings(tmp_path)
        mock_client = MagicMock()
        mock_client_cls.return_value = mock_client
        mock_fetcher = MagicMock()
        mock_fetcher_cls.return_value = mock_fetcher
        mock_fetcher.fetch.side_effect = RuntimeError("api error")

        args = _ns(inst=["BTC-USDT-SWAP"], config="config/config.toml")

        assert fetch_open_interest_main(args) == 1
        mock_client.close.assert_called_once()


# =====================================================================
# cli.main 分发测试：新增两个子命令
# =====================================================================


class TestCliMainDispatchNew:
    @patch("quant.cli.fetch_instruments_main")
    def test_dispatch_fetch_instruments(self, mock_fetch_inst):
        mock_fetch_inst.return_value = 0
        ret = cli_main(
            ["fetch-instruments", "--inst-type", "SPOT"]
        )
        assert ret == 0
        mock_fetch_inst.assert_called_once()

    @patch("quant.cli.fetch_funding_rates_main")
    def test_dispatch_fetch_funding_rates(self, mock_fetch_funding):
        mock_fetch_funding.return_value = 0
        ret = cli_main(
            ["fetch-funding-rates", "--inst", "BTC-USDT-SWAP"]
        )
        assert ret == 0
        mock_fetch_funding.assert_called_once()


# =====================================================================
# 多周期支持：BAR_CHOICES / sync-candles 多周期
# =====================================================================


class TestMultiPeriodSupport:
    def test_bar_choices_include_6h_and_12h(self) -> None:
        assert "6H" in BAR_CHOICES
        assert "12H" in BAR_CHOICES

    @pytest.mark.parametrize("bar", ["6H", "12H"])
    def test_backtest_accepts_new_periods(self, bar: str) -> None:
        parser = build_parser()
        args = parser.parse_args(
            [
                "backtest",
                "--strategy", "sma_cross",
                "--inst", "BTC-USDT",
                "--bar", bar,
                "--start", "2024-01-01",
                "--end", "2024-02-01",
            ]
        )
        assert args.bar == bar

    @pytest.mark.parametrize("bar", ["6H", "12H"])
    def test_paper_report_accepts_new_periods(self, bar: str) -> None:
        parser = build_parser()
        args = parser.parse_args(["paper-report", "--bar", bar])
        assert args.bar == bar

    @pytest.mark.parametrize("bar", ["6H", "12H"])
    def test_run_engine_accepts_new_periods(self, bar: str) -> None:
        parser = build_parser()
        args = parser.parse_args(["run-engine", "--bar", bar])
        assert args.bar == bar

    def test_sync_candles_bar_is_free_form(self) -> None:
        """--bar 支持逗号分隔，不能是 argparse choices 受限。"""
        parser = build_parser()
        args = parser.parse_args(["sync-candles", "--bar", "1H,6H,12H"])
        assert args.bar == "1H,6H,12H"

    def test_sync_once_covers_every_inst_and_bar(self) -> None:
        calls: list[tuple[str, str]] = []
        first_sync_seen: list[int] = []

        class _Fetcher:
            def sync(
                self,
                inst: str,
                bar: str,
                lookback_bars: int = 3,
                *,
                first_sync_bars: int = 0,
            ) -> int:
                calls.append((inst, bar))
                first_sync_seen.append(first_sync_bars)
                return 1

        total = _sync_once(
            _Fetcher(),
            ["A", "B"],
            ["1H", "6H"],
            3,
            MagicMock(),
            first_sync_bars=300,
        )
        assert calls == [("A", "1H"), ("B", "1H"), ("A", "6H"), ("B", "6H")]
        assert total == 4
        assert first_sync_seen == [300, 300, 300, 300]

    def test_sync_once_continues_after_one_failure(self) -> None:
        class _Fetcher:
            def sync(
                self,
                inst: str,
                bar: str,
                lookback_bars: int = 3,
                *,
                first_sync_bars: int = 0,
            ) -> int:
                if inst == "BAD":
                    raise RuntimeError("boom")
                return 2

        log = MagicMock()
        total = _sync_once(_Fetcher(), ["BAD", "OK"], ["1H"], 3, log)
        assert total == 2
        log.error.assert_called_once()
