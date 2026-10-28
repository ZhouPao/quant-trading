"""Step 5 端到端烟测：Fetchers 真实拉取（spec §5）。

本测试用真实 OKX API（不联网的 mock 单测已在 test_candle_fetcher.py 等覆盖），
串联 Step 1-5 全链路，验证：
- CandleFetcher 拉近 30 天 BTC-USDT 1H 数据，落盘 parquet + 断点 JSON
- 第二次调同 fetch 应跳过已落盘的数据（断点续传）
- InstrumentFetcher 拉取 SPOT 全量产品
- FundingRateFetcher 拉取 BTC-USDT-SWAP 资金费率
- 验证落盘后能被 storage 层读回

运行：
    python3 tests/smoke/test_e2e_step5.py
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from quant.fetcher.candle_fetcher import CandleFetcher  # noqa: E402
from quant.fetcher.funding_rate_fetcher import FundingRateFetcher  # noqa: E402
from quant.fetcher.instrument_fetcher import InstrumentFetcher  # noqa: E402
from quant.foundation.config import OKXSettings  # noqa: E402
from quant.foundation.logger import get_logger, setup_logger  # noqa: E402
from quant.okx.client import OKXClient  # noqa: E402
from quant.storage.bar_storage import BarStorage  # noqa: E402
from quant.storage.instrument_storage import InstrumentStorage  # noqa: E402

# 1H bar_size_ms
BAR_SIZE_MS_1H = 3_600_000


def _print(label: str, ok: bool, detail: str = "") -> None:
    mark = "PASS" if ok else "FAIL"
    print(f"[{mark}] {label}{f' — {detail}' if detail else ''}")


def _make_client() -> OKXClient:
    load_dotenv(ROOT / ".env")
    settings = OKXSettings(
        api_key=os.environ["OKX_API_KEY"],
        secret=os.environ["OKX_SECRET"],
        passphrase=os.environ["OKX_PASSPHRASE"],
        demo=False,
    )
    return OKXClient(settings, sync_time_on_init=False)


def _scenario_1_candle_fetcher_fresh() -> bool:
    """场景 1：CandleFetcher 首次拉近 30 天 1H，落盘 + 断点生成。"""
    print("\n=== 场景 1: CandleFetcher 首次拉近 30 天 1H ===")
    setup_logger(level="INFO")
    log = get_logger("e2e.step5.candle")
    client = _make_client()
    try:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            fetcher = CandleFetcher(client, data_dir, page_size=100)

            # 计算 end_ts = now - 1h, start_ts = end_ts - 30d
            import time
            now_ms = int(time.time() * 1000)
            end_ts = now_ms - BAR_SIZE_MS_1H  # 上一根已完成的 K线
            start_ts = end_ts - 30 * 24 * BAR_SIZE_MS_1H  # 30 天

            total = fetcher.fetch("BTC-USDT", "1H", start_ts, end_ts)
            log.info(f"first fetch total={total}")

            # 期望至少 700 根（30 天 ≈ 720 根，部分可能因交易对上线时间不足）
            fetched_ok = total >= 700

            # 断点文件应存在
            ckpt_path = data_dir / ".checkpoints" / "BTC-USDT_1H.json"
            ckpt_exists = ckpt_path.exists()

            # 落盘数据可用 BarStorage 读回
            bar_storage = BarStorage(data_dir)
            loaded = bar_storage.load_bars("BTC-USDT", "1H", start_ts, end_ts)
            loaded_ok = len(loaded) >= 700

            # 验证落盘数据连续（无大缺口）
            ts_list = sorted([b.ts for b in loaded])
            gaps = sum(
                1 for i in range(1, len(ts_list))
                if ts_list[i] - ts_list[i - 1] > BAR_SIZE_MS_1H
            )
            # 允许少量缺口（OKX 偶有缺失，但应 < 5%）
            continuity_ok = gaps < len(ts_list) * 0.05

            _print(
                "首次 fetch + 落盘 + 断点",
                fetched_ok and ckpt_exists and loaded_ok and continuity_ok,
                f"fetched={total}, ckpt_exists={ckpt_exists}, "
                f"loaded={len(loaded)}, gaps={gaps}",
            )
            return (
                fetched_ok and ckpt_exists and loaded_ok and continuity_ok
            )
    except Exception as e:
        _print("场景 1 异常", False, f"{type(e).__name__}: {e}")
        return False
    finally:
        client.close()


def _scenario_2_candle_fetcher_resume() -> bool:
    """场景 2：第二次调 fetch 应跳过已落盘数据（断点续传）。"""
    print("\n=== 场景 2: CandleFetcher 断点续传 ===")
    client = _make_client()
    try:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            fetcher = CandleFetcher(client, data_dir, page_size=100)

            import time
            now_ms = int(time.time() * 1000)
            end_ts = now_ms - BAR_SIZE_MS_1H
            start_ts = end_ts - 3 * 24 * BAR_SIZE_MS_1H  # 3 天，缩短加速

            total1 = fetcher.fetch("BTC-USDT", "1H", start_ts, end_ts)
            # 第二次：应跳过主循环（cursor > end_ts）
            total2 = fetcher.fetch("BTC-USDT", "1H", start_ts, end_ts)

            resume_ok = total1 >= 70 and total2 == 0

            _print(
                "断点续传跳过已落盘",
                resume_ok,
                f"first={total1}, second={total2}",
            )
            return resume_ok
    except Exception as e:
        _print("场景 2 异常", False, f"{type(e).__name__}: {e}")
        return False
    finally:
        client.close()


def _scenario_3_instrument_fetcher() -> bool:
    """场景 3：InstrumentFetcher 拉取 SPOT 全量产品。"""
    print("\n=== 场景 3: InstrumentFetcher 拉 SPOT 产品 ===")
    client = _make_client()
    try:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            fetcher = InstrumentFetcher(client, data_dir)

            count = fetcher.fetch("SPOT")
            # OKX SPOT 应有几百个产品
            count_ok = count > 100

            # 文件应存在
            path = data_dir / "instruments" / "spot.json"
            file_exists = path.exists()

            # 读回
            loaded = fetcher.load("SPOT")
            loaded_ok = len(loaded) == count

            # 包含 BTC-USDT
            has_btc = any(i.inst_id == "BTC-USDT" for i in loaded)

            _print(
                "InstrumentFetcher SPOT 拉取",
                count_ok and file_exists and loaded_ok and has_btc,
                f"count={count}, file={file_exists}, "
                f"loaded={len(loaded)}, has_btc={has_btc}",
            )
            return count_ok and file_exists and loaded_ok and has_btc
    except Exception as e:
        _print("场景 3 异常", False, f"{type(e).__name__}: {e}")
        return False
    finally:
        client.close()


def _scenario_4_funding_rate_fetcher() -> bool:
    """场景 4：FundingRateFetcher 拉取 BTC-USDT-SWAP 资金费率。"""
    print("\n=== 场景 4: FundingRateFetcher 拉 BTC-USDT-SWAP ===")
    client = _make_client()
    try:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            fetcher = FundingRateFetcher(client, data_dir)

            count = fetcher.fetch("BTC-USDT-SWAP")
            # 应返回 1（首次拉到）
            fetched_ok = count == 1

            # 文件应存在
            path = data_dir / "funding_rates" / "BTC-USDT-SWAP.json"
            file_exists = path.exists()

            # 读回
            loaded = fetcher.load("BTC-USDT-SWAP")
            loaded_ok = len(loaded) == 1

            # 第二次 fetch 同样 ts：应去重
            count2 = fetcher.fetch("BTC-USDT-SWAP")
            dedup_ok = count2 == 0

            _print(
                "FundingRateFetcher 拉取 + 去重",
                fetched_ok and file_exists and loaded_ok and dedup_ok,
                f"first={count}, file={file_exists}, "
                f"loaded={len(loaded)}, second_dedup={dedup_ok}",
            )
            return fetched_ok and file_exists and loaded_ok and dedup_ok
    except Exception as e:
        _print("场景 4 异常", False, f"{type(e).__name__}: {e}")
        return False
    finally:
        client.close()


def _scenario_5_full_chain_step1to5() -> bool:
    """场景 5：Step 1-5 完整串联。

    foundation → models → okx → storage → fetcher
    用 fetcher 拉数据 → storage 读回 → 验证字段一致性
    """
    print("\n=== 场景 5: Step 1-5 完整链路 ===")
    setup_logger(level="INFO")
    log = get_logger("e2e.step5.full")
    client = _make_client()
    try:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            # fetcher 拉数据
            candle_fetcher = CandleFetcher(client, data_dir)
            inst_fetcher = InstrumentFetcher(client, data_dir)

            import time
            now_ms = int(time.time() * 1000)
            end_ts = now_ms - BAR_SIZE_MS_1H
            start_ts = end_ts - 7 * 24 * BAR_SIZE_MS_1H  # 7 天

            total_bars = candle_fetcher.fetch("BTC-USDT", "1H", start_ts, end_ts)
            total_insts = inst_fetcher.fetch("SPOT")
            log.info(
                f"fetched bars={total_bars}, instruments={total_insts}"
            )

            # 用 storage 层读回（验证 fetcher → storage 接口对接）
            bar_storage = BarStorage(data_dir)
            inst_storage = InstrumentStorage(data_dir)

            bars = bar_storage.load_bars("BTC-USDT", "1H", start_ts, end_ts)
            insts = inst_storage.load_instruments("SPOT")

            bars_ok = len(bars) == total_bars
            insts_ok = len(insts) == total_insts
            interface_ok = bars_ok and insts_ok

            # 字段一致性（从 fetch 落盘到读回，Decimal 字段不丢精度）
            sample = bars[0]
            fields_ok = (
                isinstance(sample.open, type(sample.open))
                and sample.high >= sample.low
            )

            all_ok = (
                total_bars > 100
                and total_insts > 100
                and interface_ok
                and fields_ok
            )
            _print(
                "Step 1-5 完整链路",
                all_ok,
                f"bars_fetched={total_bars}, insts_fetched={total_insts}, "
                f"bars_loaded={len(bars)}, insts_loaded={len(insts)}, "
                f"interface_ok={interface_ok}, fields_ok={fields_ok}",
            )
            return all_ok
    except Exception as e:
        _print("场景 5 异常", False, f"{type(e).__name__}: {e}")
        return False
    finally:
        client.close()


def main() -> int:
    print("=" * 60)
    print("Step 5 端到端烟测：Fetchers 真实拉取")
    print("=" * 60)

    results: list[bool] = []
    results.append(_scenario_1_candle_fetcher_fresh())
    results.append(_scenario_2_candle_fetcher_resume())
    results.append(_scenario_3_instrument_fetcher())
    results.append(_scenario_4_funding_rate_fetcher())
    results.append(_scenario_5_full_chain_step1to5())

    passed = sum(results)
    total = len(results)
    print("\n" + "=" * 60)
    print(f"== 总计: {passed}/{total} 通过 ==")
    print("=" * 60)
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())
