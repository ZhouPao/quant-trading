"""Step 6 端到端烟测：CLI 命令真实拉取 + 校验 + 对齐（spec §11）。

本测试用真实 OKX API，通过 CLI 入口（quant.cli.main）串联全链路：
- quant fetch-candles：拉取 BTC-USDT 1H 数据
- quant validate-data：校验已落盘数据完整性
- quant align-data：多品种对齐（BTC-USDT + ETH-USDT）

运行：
    python3 tests/smoke/test_e2e_step6_cli.py
"""
from __future__ import annotations

import sys
import tempfile
import time
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from quant.cli import main as cli_main  # noqa: E402
from quant.foundation.logger import setup_logger  # noqa: E402
from quant.storage.bar_storage import BarStorage  # noqa: E402

BAR_SIZE_MS_1H = 3_600_000

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
    """创建临时 config.toml，指向临时 data_dir。"""
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
level = "INFO"
file = "{data_dir / 'test.log'}"
rotation = "10 MB"
retention = "1 days"
"""
    cfg_path = data_dir / "config.toml"
    cfg_path.write_text(config_text, encoding="utf-8")
    return cfg_path


def _scenario_1_fetch_candles() -> bool:
    """场景 1：CLI fetch-candles 拉取 7 天 BTC-USDT 1H 数据。"""
    print("\n=== 场景 1: CLI fetch-candles 拉取 7 天 BTC-USDT 1H ===")
    setup_logger(level="WARNING")  # 减少日志噪音

    with tempfile.TemporaryDirectory() as tmp:
        data_dir = Path(tmp)
        cfg = _make_config(data_dir)

        now_ms = int(time.time() * 1000)
        end_ts = now_ms - BAR_SIZE_MS_1H
        start_ts = end_ts - 7 * 24 * BAR_SIZE_MS_1H  # 7 天

        from datetime import UTC, datetime

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

        # 验证落盘
        storage = BarStorage(data_dir)
        bars = storage.load_bars("BTC-USDT", "1H", start_ts, end_ts)

        ok = ret == 0 and len(bars) > 100
        _print(
            "fetch-candles 拉取",
            ok,
            f"exit={ret}, bars={len(bars)}, "
            f"range={start_date}..{end_date}",
        )
        return ok and data_dir  # 返回 data_dir 给后续场景


def _scenario_2_fetch_candles_resume() -> bool:
    """场景 2：CLI fetch-candles 断点续传（第二次调用跳过已落盘）。"""
    print("\n=== 场景 2: CLI fetch-candles 断点续传 ===")
    setup_logger(level="WARNING")

    with tempfile.TemporaryDirectory() as tmp:
        data_dir = Path(tmp)
        cfg = _make_config(data_dir)

        now_ms = int(time.time() * 1000)
        end_ts = now_ms - BAR_SIZE_MS_1H
        start_ts = end_ts - 3 * 24 * BAR_SIZE_MS_1H  # 3 天

        from datetime import UTC, datetime

        start_date = datetime.fromtimestamp(start_ts / 1000, tz=UTC).strftime("%Y-%m-%d")
        end_date = datetime.fromtimestamp(end_ts / 1000, tz=UTC).strftime("%Y-%m-%d")

        # 第一次拉取
        ret1 = cli_main([
            "fetch-candles",
            "--inst", "BTC-USDT",
            "--bar", "1H",
            "--start", start_date,
            "--end", end_date,
            "--config", str(cfg),
        ])

        storage = BarStorage(data_dir)
        bars1 = storage.load_bars("BTC-USDT", "1H", start_ts, end_ts)

        # 第二次调同范围（应跳过）
        ret2 = cli_main([
            "fetch-candles",
            "--inst", "BTC-USDT",
            "--bar", "1H",
            "--start", start_date,
            "--end", end_date,
            "--config", str(cfg),
        ])

        bars2 = storage.load_bars("BTC-USDT", "1H", start_ts, end_ts)

        ok = ret1 == 0 and ret2 == 0 and len(bars1) > 50 and len(bars2) == len(bars1)
        _print(
            "fetch-candles 断点续传",
            ok,
            f"exit1={ret1} exit2={ret2} bars1={len(bars1)} bars2={len(bars2)}",
        )
    return ok


def _scenario_3_validate_data() -> bool:
    """场景 3：CLI validate-data 校验已落盘数据。"""
    print("\n=== 场景 3: CLI validate-data 校验数据 ===")
    setup_logger(level="WARNING")

    with tempfile.TemporaryDirectory() as tmp:
        data_dir = Path(tmp)
        cfg = _make_config(data_dir)

        now_ms = int(time.time() * 1000)
        end_ts = now_ms - BAR_SIZE_MS_1H
        start_ts = end_ts - 7 * 24 * BAR_SIZE_MS_1H

        from datetime import UTC, datetime

        start_date = datetime.fromtimestamp(start_ts / 1000, tz=UTC).strftime("%Y-%m-%d")
        end_date = datetime.fromtimestamp(end_ts / 1000, tz=UTC).strftime("%Y-%m-%d")

        # 先拉数据
        cli_main([
            "fetch-candles",
            "--inst", "BTC-USDT",
            "--bar", "1H",
            "--start", start_date,
            "--end", end_date,
            "--config", str(cfg),
        ])

        # 校验（全量模式，不传 start/end）
        ret = cli_main([
            "validate-data",
            "--inst", "BTC-USDT",
            "--bar", "1H",
            "--config", str(cfg),
        ])

        ok = ret == 0
        _print(
            "validate-data 校验",
            ok,
            f"exit={ret}（0=校验通过）",
        )
    return ok


def _scenario_4_align_data() -> bool:
    """场景 4：CLI align-data 多品种对齐（BTC-USDT + ETH-USDT）。"""
    print("\n=== 场景 4: CLI align-data 多品种对齐 ===")
    setup_logger(level="WARNING")

    with tempfile.TemporaryDirectory() as tmp:
        data_dir = Path(tmp)
        cfg = _make_config(data_dir)

        now_ms = int(time.time() * 1000)
        end_ts = now_ms - BAR_SIZE_MS_1H
        start_ts = end_ts - 7 * 24 * BAR_SIZE_MS_1H

        from datetime import UTC, datetime

        start_date = datetime.fromtimestamp(start_ts / 1000, tz=UTC).strftime("%Y-%m-%d")
        end_date = datetime.fromtimestamp(end_ts / 1000, tz=UTC).strftime("%Y-%m-%d")

        # 先拉 BTC 和 ETH 数据
        for inst in ["BTC-USDT", "ETH-USDT"]:
            cli_main([
                "fetch-candles",
                "--inst", inst,
                "--bar", "1H",
                "--start", start_date,
                "--end", end_date,
                "--config", str(cfg),
            ])

        # 对齐并输出 parquet
        out_path = data_dir / "aligned.parquet"
        ret = cli_main([
            "align-data",
            "--insts", "BTC-USDT,ETH-USDT",
            "--bar", "1H",
            "--fill", "ffill",
            "--start", start_date,
            "--end", end_date,
            "--output", str(out_path),
            "--config", str(cfg),
        ])

        file_exists = out_path.exists()
        ok = ret == 0 and file_exists
        _print(
            "align-data 多品种对齐",
            ok,
            f"exit={ret}, output_exists={file_exists}",
        )
    return ok


def _scenario_5_full_pipeline() -> bool:
    """场景 5：CLI 全链路 fetch → validate → align 串联。"""
    print("\n=== 场景 5: CLI 全链路 fetch → validate → align ===")
    setup_logger(level="WARNING")

    with tempfile.TemporaryDirectory() as tmp:
        data_dir = Path(tmp)
        cfg = _make_config(data_dir)

        now_ms = int(time.time() * 1000)
        end_ts = now_ms - BAR_SIZE_MS_1H
        start_ts = end_ts - 3 * 24 * BAR_SIZE_MS_1H  # 3 天

        from datetime import UTC, datetime

        start_date = datetime.fromtimestamp(start_ts / 1000, tz=UTC).strftime("%Y-%m-%d")
        end_date = datetime.fromtimestamp(end_ts / 1000, tz=UTC).strftime("%Y-%m-%d")

        # Step A: fetch BTC + ETH
        for inst in ["BTC-USDT", "ETH-USDT"]:
            ret_fetch = cli_main([
                "fetch-candles",
                "--inst", inst,
                "--bar", "1H",
                "--start", start_date,
                "--end", end_date,
                "--config", str(cfg),
            ])
            if ret_fetch != 0:
                _print("全链路 fetch", False, f"inst={inst} fetch failed exit={ret_fetch}")
                return False

        # Step B: validate BTC
        ret_validate = cli_main([
            "validate-data",
            "--inst", "BTC-USDT",
            "--bar", "1H",
            "--start", start_date,
            "--end", end_date,
            "--config", str(cfg),
        ])

        # Step C: align
        out_path = data_dir / "aligned_full.parquet"
        ret_align = cli_main([
            "align-data",
            "--insts", "BTC-USDT,ETH-USDT",
            "--bar", "1H",
            "--fill", "ffill",
            "--start", start_date,
            "--end", end_date,
            "--output", str(out_path),
            "--config", str(cfg),
        ])

        ok = ret_validate == 0 and ret_align == 0 and out_path.exists()
        _print(
            "全链路 fetch→validate→align",
            ok,
            f"validate={ret_validate} align={ret_align} output={out_path.exists()}",
        )
    return ok


def _scenario_6_fetch_instruments() -> bool:
    """场景 6：CLI fetch-instruments 拉取 SPOT 产品配置。"""
    print("\n=== 场景 6: CLI fetch-instruments 拉取 SPOT 产品 ===")
    setup_logger(level="WARNING")

    with tempfile.TemporaryDirectory() as tmp:
        data_dir = Path(tmp)
        cfg = _make_config(data_dir)

        ret = cli_main([
            "fetch-instruments",
            "--inst-type", "SPOT",
            "--config", str(cfg),
        ])

        # 验证落盘文件存在
        inst_file = data_dir / "instruments" / "SPOT.json"
        ok = ret == 0 and inst_file.exists()
        _print(
            "fetch-instruments 拉取",
            ok,
            f"exit={ret}, file_exists={inst_file.exists()}",
        )
    return ok


def _scenario_7_fetch_funding_rates() -> bool:
    """场景 7：CLI fetch-funding-rates 拉取 BTC-USDT-SWAP 资金费率。"""
    print("\n=== 场景 7: CLI fetch-funding-rates 拉取资金费率 ===")
    setup_logger(level="WARNING")

    with tempfile.TemporaryDirectory() as tmp:
        data_dir = Path(tmp)
        cfg = _make_config(data_dir)

        ret = cli_main([
            "fetch-funding-rates",
            "--inst", "BTC-USDT-SWAP",
            "--config", str(cfg),
        ])

        # 验证落盘文件存在
        funding_file = data_dir / "funding_rates" / "BTC-USDT-SWAP.json"
        ok = ret == 0 and funding_file.exists()
        _print(
            "fetch-funding-rates 拉取",
            ok,
            f"exit={ret}, file_exists={funding_file.exists()}",
        )
    return ok


def _scenario_8_full_5_command_chain() -> bool:
    """场景 8：全 5 命令串联 fetch-instruments→candles→funding→validate→align。"""
    print("\n=== 场景 8: 全 5 命令串联 ===")
    setup_logger(level="WARNING")

    with tempfile.TemporaryDirectory() as tmp:
        data_dir = Path(tmp)
        cfg = _make_config(data_dir)

        now_ms = int(time.time() * 1000)
        end_ts = now_ms - BAR_SIZE_MS_1H
        start_ts = end_ts - 3 * 24 * BAR_SIZE_MS_1H  # 3 天

        from datetime import UTC, datetime

        start_date = datetime.fromtimestamp(start_ts / 1000, tz=UTC).strftime("%Y-%m-%d")
        end_date = datetime.fromtimestamp(end_ts / 1000, tz=UTC).strftime("%Y-%m-%d")

        # Step A: fetch-instruments
        ret_inst = cli_main([
            "fetch-instruments",
            "--inst-type", "SPOT",
            "--config", str(cfg),
        ])
        if ret_inst != 0:
            _print("全 5 命令串联", False, f"fetch-instruments failed exit={ret_inst}")
            return False

        # Step B: fetch-candles (BTC + ETH)
        for inst in ["BTC-USDT", "ETH-USDT"]:
            ret_fetch = cli_main([
                "fetch-candles",
                "--inst", inst,
                "--bar", "1H",
                "--start", start_date,
                "--end", end_date,
                "--config", str(cfg),
            ])
            if ret_fetch != 0:
                _print("全 5 命令串联", False, f"fetch-candles {inst} failed exit={ret_fetch}")
                return False

        # Step C: fetch-funding-rates
        ret_funding = cli_main([
            "fetch-funding-rates",
            "--inst", "BTC-USDT-SWAP",
            "--config", str(cfg),
        ])

        # Step D: validate-data
        ret_validate = cli_main([
            "validate-data",
            "--inst", "BTC-USDT",
            "--bar", "1H",
            "--start", start_date,
            "--end", end_date,
            "--config", str(cfg),
        ])

        # Step E: align-data
        out_path = data_dir / "aligned_5cmd.parquet"
        ret_align = cli_main([
            "align-data",
            "--insts", "BTC-USDT,ETH-USDT",
            "--bar", "1H",
            "--fill", "ffill",
            "--start", start_date,
            "--end", end_date,
            "--output", str(out_path),
            "--config", str(cfg),
        ])

        ok = (
            ret_funding == 0
            and ret_validate == 0
            and ret_align == 0
            and out_path.exists()
        )
        _print(
            "全 5 命令串联",
            ok,
            f"inst={ret_inst} funding={ret_funding} "
            f"validate={ret_validate} align={ret_align} "
            f"output={out_path.exists()}",
        )
    return ok


def main() -> int:
    setup_logger(level="WARNING")
    print("=" * 60)
    print("Step 6+ CLI 端到端烟测（真实 OKX API）")
    print("=" * 60)

    scenarios = [
        _scenario_1_fetch_candles,
        _scenario_2_fetch_candles_resume,
        _scenario_3_validate_data,
        _scenario_4_align_data,
        _scenario_5_full_pipeline,
        _scenario_6_fetch_instruments,
        _scenario_7_fetch_funding_rates,
        _scenario_8_full_5_command_chain,
    ]

    for scenario in scenarios:
        try:
            scenario()
        except Exception as e:
            _print(scenario.__name__, False, f"异常: {type(e).__name__}: {e}")

    print("\n" + "=" * 60)
    print(f"总计: {_passed + _failed} 个场景, {_passed} 通过, {_failed} 失败")
    print("=" * 60)
    return 0 if _failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
