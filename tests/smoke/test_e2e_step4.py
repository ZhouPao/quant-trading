"""Step 4 端到端烟测：真实 OKX K线 → 落盘 → 校验 → 对齐 全链路。

验证：
  1) OKXClient.get_candles 取真实 K线
  2) BarStorage.save_bars 落盘 parquet
  3) BarStorage.load_bars 读回，与原数据一致
  4) DataValidator.validate 校验无问题
  5) InstrumentStorage.save_instruments 落盘 JSON
  6) DataAligner.align 多品种对齐，输出多列 DataFrame
  7) 重复写入同 ts，确认落盘去重生效
  8) 文件大小预算 < 200KB（1 年 1H 数据）

运行：
    python3 tests/smoke/test_e2e_step4.py
"""
from __future__ import annotations

import os
import sys
import tempfile
from decimal import Decimal
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from quant.foundation.config import OKXSettings  # noqa: E402
from quant.models.bar import Bar  # noqa: E402
from quant.okx.client import OKXClient  # noqa: E402
from quant.storage.aligner import DataAligner  # noqa: E402
from quant.storage.bar_storage import BarStorage  # noqa: E402
from quant.storage.instrument_storage import InstrumentStorage  # noqa: E402
from quant.storage.validator import DataValidator  # noqa: E402


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


# =====================================================================
# 场景 1: 真实 OKX K线 → BarStorage 落盘 → 读回 → 数据一致
# =====================================================================


def scenario_1_real_candles_to_parquet_roundtrip() -> bool:
    """真实 K线 → parquet 落盘 → 读回，验证字段无丢失。"""
    print("\n=== 场景 1: OKX K线 → parquet 落盘 → 读回 ===")
    client = _make_client()
    with tempfile.TemporaryDirectory() as tmp:
        storage = BarStorage(Path(tmp))
        try:
            # 取 BTC-USDT 1H 50 根
            bars = client.get_candles("BTC-USDT", "1H", limit=50)
            if len(bars) != 50:
                _print("拉取数量", False, f"got {len(bars)} != 50")
                return False

            # 落盘
            saved = storage.save_bars(bars)
            if saved != 50:
                _print("save_bars 数量", False, f"saved {saved} != 50")
                return False

            # 读回
            ts_min = min(b.ts for b in bars)
            ts_max = max(b.ts for b in bars)
            loaded = storage.load_bars("BTC-USDT", "1H", ts_min, ts_max)
            if len(loaded) != 50:
                _print("load_bars 数量", False, f"loaded {len(loaded)} != 50")
                return False

            # 字段一致（用 ts → close map 对比）
            orig_map = {b.ts: b.close for b in bars}
            loaded_map = {b.ts: b.close for b in loaded}
            fields_ok = all(orig_map[ts] == loaded_map[ts] for ts in orig_map)

            # 文件存在
            parquet_files = list(Path(tmp).rglob("*.parquet"))
            files_ok = len(parquet_files) >= 1

            ok = fields_ok and files_ok
            _print(
                "OKX → parquet → load roundtrip",
                ok,
                f"saved={saved}, loaded={len(loaded)}, "
                f"fields_match={fields_ok}, "
                f"parquet_files={len(parquet_files)}",
            )
            return ok
        except Exception as e:
            _print("场景 1 异常", False, f"{type(e).__name__}: {e}")
            return False
        finally:
            client.close()


# =====================================================================
# 场景 2: 重复写入同 ts → 去重生效
# =====================================================================


def scenario_2_dedup_on_overwrite() -> bool:
    """两次写入相同 ts → 文件里只保留最新。"""
    print("\n=== 场景 2: 重复写入同 ts 去重 ===")
    client = _make_client()
    with tempfile.TemporaryDirectory() as tmp:
        storage = BarStorage(Path(tmp))
        try:
            bars = client.get_candles("BTC-USDT", "1H", limit=10)
            if len(bars) != 10:
                _print("拉取数量", False, f"got {len(bars)} != 10")
                return False

            # 第一次写
            storage.save_bars(bars)
            # 第二次写同样 ts，但改 vol 为 999（避免 OHLC 校验）
            modified = []
            for b in bars:
                modified.append(Bar(
                    ts=b.ts, open=b.open, high=b.high, low=b.low,
                    close=b.close,
                    vol=Decimal("999.0"),  # 改 vol
                    vol_quote=b.vol_quote, confirm=b.confirm,
                    inst_id=b.inst_id, bar_size=b.bar_size,
                ))
            saved = storage.save_bars(modified)
            if saved != 0:
                _print("第二次 save_bars", False, f"saved {saved} != 0（同 ts 应为净增 0）")
                return False

            # 读回验证 vol 全是 999
            ts_min = min(b.ts for b in bars)
            ts_max = max(b.ts for b in bars)
            loaded = storage.load_bars("BTC-USDT", "1H", ts_min, ts_max)
            if len(loaded) != 10:
                _print("读回数量", False, f"loaded {len(loaded)} != 10")
                return False
            all_999 = all(b.vol == Decimal("999.0") for b in loaded)

            _print(
                "同 ts 覆盖后只保留最新",
                all_999,
                f"loaded={len(loaded)}, all_vol_999={all_999}",
            )
            return all_999
        except Exception as e:
            _print("场景 2 异常", False, f"{type(e).__name__}: {e}")
            return False
        finally:
            client.close()


# =====================================================================
# 场景 3: DataValidator 校验真实 K线
# =====================================================================


def scenario_3_validator_on_real_bars() -> bool:
    """真实 K线应通过 DataValidator 校验。"""
    print("\n=== 场景 3: DataValidator 校验真实 K线 ===")
    client = _make_client()
    try:
        bars = client.get_candles("BTC-USDT", "1H", limit=100)
        if len(bars) != 100:
            _print("拉取数量", False, f"got {len(bars)} != 100")
            return False

        validator = DataValidator()
        report = validator.validate(bars)

        # 真实 K线可能存在未收盘 K线（confirm=0），不报 missing
        # 也不应有 duplicates 或 invalid_ohlc
        no_dups = len(report.duplicates) == 0
        no_invalid = len(report.invalid_ohlc) == 0
        no_errors = len(report.errors) == 0

        ok = no_dups and no_invalid and no_errors
        _print(
            "真实 K线校验通过",
            ok,
            f"bars={len(bars)}, duplicates={len(report.duplicates)}, "
            f"missing={len(report.missing)}, "
            f"invalid_ohlc={len(report.invalid_ohlc)}, "
            f"errors={len(report.errors)}",
        )
        return ok
    except Exception as e:
        _print("场景 3 异常", False, f"{type(e).__name__}: {e}")
        return False
    finally:
        client.close()


# =====================================================================
# 场景 4: InstrumentStorage 真实产品列表落盘
# =====================================================================


def scenario_4_instruments_persist() -> bool:
    """真实 OKX 产品列表 → JSON 落盘 → 读回。"""
    print("\n=== 场景 4: OKX 产品 → JSON 落盘 → 读回 ===")
    client = _make_client()
    with tempfile.TemporaryDirectory() as tmp:
        storage = InstrumentStorage(Path(tmp))
        try:
            spot = client.get_instruments("SPOT")
            if len(spot) == 0:
                _print("SPOT 产品数", False, "0")
                return False

            # 落盘
            saved = storage.save_instruments("SPOT", spot)
            if saved != len(spot):
                _print("save 数量", False, f"{saved} != {len(spot)}")
                return False

            # 读回
            loaded = storage.load_instruments("SPOT")
            if len(loaded) != len(spot):
                _print("load 数量", False, f"{loaded} != {spot}")
                return False

            # 找一个 inst_id 验证 load_one
            target = spot[0].inst_id
            one = storage.load_one(target, "SPOT")
            one_ok = one is not None and one.inst_id == target

            # 文件存在
            json_file = Path(tmp) / "instruments" / "spot.json"
            file_ok = json_file.exists()

            # 字段一致
            orig_map = {i.inst_id: i for i in spot}
            loaded_map = {i.inst_id: i for i in loaded}
            fields_ok = all(
                orig_map[iid].tick_sz == loaded_map[iid].tick_sz
                for iid in list(orig_map)[:5]
            )

            ok = saved == len(spot) and one_ok and file_ok and fields_ok
            _print(
                "OKX 产品 → JSON → load roundtrip",
                ok,
                f"saved={saved}, loaded={len(loaded)}, "
                f"load_one={one_ok}, file={file_ok}, "
                f"fields_match={fields_ok}",
            )
            return ok
        except Exception as e:
            _print("场景 4 异常", False, f"{type(e).__name__}: {e}")
            return False
        finally:
            client.close()


# =====================================================================
# 场景 5: DataAligner 多品种对齐
# =====================================================================


def scenario_5_aligner_multi_instrument() -> bool:
    """BTC + ETH 真实 K线 → 对齐输出。"""
    print("\n=== 场景 5: DataAligner 多品种对齐 ===")
    client = _make_client()
    try:
        btc = client.get_candles("BTC-USDT", "1H", limit=50)
        eth = client.get_candles("ETH-USDT", "1H", limit=50)
        if len(btc) != 50 or len(eth) != 50:
            _print("拉取数量", False, f"btc={len(btc)}, eth={len(eth)}")
            return False

        aligner = DataAligner()
        df = aligner.align({"BTC-USDT": btc, "ETH-USDT": eth}, fill="ffill")

        # 行数 = 50（取并集，这里 ts 应一致）
        rows_ok = len(df) == 50
        # 列包含两品种
        cols_ok = (
            "BTC-USDT_close" in df.columns
            and "ETH-USDT_close" in df.columns
        )
        # 没有全 NaN 的行
        no_all_nan = not df[["BTC-USDT_close", "ETH-USDT_close"]].isna().all(axis=1).any()
        # BTC close 在合理范围（>1000 USD）
        btc_close_valid = (df["BTC-USDT_close"].dropna() > 1000).all()

        ok = rows_ok and cols_ok and no_all_nan and btc_close_valid
        _print(
            "BTC + ETH 对齐",
            ok,
            f"rows={len(df)}, cols={len(df.columns)}, "
            f"both_present={cols_ok}, no_all_nan={no_all_nan}, "
            f"btc>1000={btc_close_valid}",
        )
        return ok
    except Exception as e:
        _print("场景 5 异常", False, f"{type(e).__name__}: {e}")
        return False
    finally:
        client.close()


# =====================================================================
# 场景 6: 全链路串联 OKX → 落盘 → 校验 → 对齐
# =====================================================================


def scenario_6_full_pipeline() -> bool:
    """端到端：OKX → save → load → validate → align。"""
    print("\n=== 场景 6: 全链路 OKX → save → load → validate → align ===")
    client = _make_client()
    with tempfile.TemporaryDirectory() as tmp:
        storage = BarStorage(Path(tmp))
        validator = DataValidator()
        aligner = DataAligner()
        try:
            # 1) 拉 BTC + ETH
            btc = client.get_candles("BTC-USDT", "1H", limit=30)
            eth = client.get_candles("ETH-USDT", "1H", limit=30)
            if len(btc) != 30 or len(eth) != 30:
                _print("拉取", False, f"btc={len(btc)}, eth={len(eth)}")
                return False

            # 2) 落盘
            n_btc = storage.save_bars(btc)
            n_eth = storage.save_bars(eth)
            save_ok = n_btc == 30 and n_eth == 30

            # 3) 读回
            ts_min = min(b.ts for b in btc)
            ts_max = max(b.ts for b in btc)
            btc_loaded = storage.load_bars("BTC-USDT", "1H", ts_min, ts_max)
            eth_loaded = storage.load_bars("ETH-USDT", "1H", ts_min, ts_max)
            load_ok = len(btc_loaded) == 30 and len(eth_loaded) == 30

            # 4) 校验
            btc_report = validator.validate(btc_loaded)
            eth_report = validator.validate(eth_loaded)
            validate_ok = (
                len(btc_report.invalid_ohlc) == 0
                and len(eth_report.invalid_ohlc) == 0
            )

            # 5) 对齐
            df = aligner.align(
                {"BTC-USDT": btc_loaded, "ETH-USDT": eth_loaded},
                fill="ffill",
            )
            align_ok = len(df) == 30

            ok = save_ok and load_ok and validate_ok and align_ok
            _print(
                "OKX → save → load → validate → align",
                ok,
                f"save={save_ok}, load={load_ok}, "
                f"validate={validate_ok}, align={align_ok}; "
                f"df_rows={len(df)}, df_cols={len(df.columns)}",
            )
            return ok
        except Exception as e:
            _print("场景 6 异常", False, f"{type(e).__name__}: {e}")
            return False
        finally:
            client.close()


# =====================================================================
# 场景 7: F1.5 文件大小预算（1 年 1H 数据 < 200KB）
# =====================================================================


def scenario_7_file_size_budget() -> bool:
    """实测 1 年 1H 数据 parquet 文件大小，对照 spec F1.5 (200KB) 预算。

    spec §15.2 把\"是否换 parquet DECIMAL 类型\"列为 M1 烟测后决策的开放问题。
    实测：string + zstd + row_group_size=8192，1 年(8760) ≈ 580KB，超 spec 预算 2.9x。
    决策（用户确认 2026-10-17）：M1 保留 string 设计（保精度），spec F1.5 列为已知偏差，
    M2 启动时评估是否切 parquet DECIMAL 类型。
    """
    print("\n=== 场景 7: 文件大小预算（spec F1.5 偏差记录） ===")
    client = _make_client()
    with tempfile.TemporaryDirectory() as tmp:
        storage = BarStorage(Path(tmp))
        try:
            bars = client.get_candles("BTC-USDT", "1H", limit=300)
            if len(bars) < 100:
                _print("拉取", False, f"only got {len(bars)} bars")
                return False

            storage.save_bars(bars)
            parquet_files = list(Path(tmp).rglob("*.parquet"))
            if not parquet_files:
                _print("parquet 文件", False, "未生成")
                return False

            total_size = sum(p.stat().st_size for p in parquet_files)
            n_bars = len(bars)
            # 直接外推到 8760 行（zstd 压缩率随数据量提升，实际更小）
            estimated_year_size = total_size * 8760 / n_bars
            budget_kb = 200
            actual_kb = estimated_year_size / 1024
            deviation_pct = (actual_kb - budget_kb) / budget_kb * 100

            # 已知偏差：保留 string 存储，M2 评估切 DECIMAL
            # 烟测通过条件：文件能正确生成 + 实测体积 < 1MB（合理上限）
            reasonable_upper_kb = 1024
            ok = actual_kb < reasonable_upper_kb

            _print(
                "文件大小（spec 偏差已知）",
                ok,
                f"{n_bars} 根 = {total_size}B ({total_size/n_bars:.1f}B/根), "
                f"估算 1 年(8760) ≈ {actual_kb:.1f}KB, "
                f"spec F1.5 预算 = {budget_kb}KB, "
                f"偏差 = +{deviation_pct:.0f}%; "
                f"M2 决策：评估切 parquet DECIMAL 类型",
            )
            return ok
        except Exception as e:
            _print("场景 7 异常", False, f"{type(e).__name__}: {e}")
            return False
        finally:
            client.close()


def main() -> int:
    print("=" * 60)
    print("Step 4 端到端烟测：真实 OKX → 落盘 → 校验 → 对齐")
    print("=" * 60)

    results: list[bool] = []
    results.append(scenario_1_real_candles_to_parquet_roundtrip())
    results.append(scenario_2_dedup_on_overwrite())
    results.append(scenario_3_validator_on_real_bars())
    results.append(scenario_4_instruments_persist())
    results.append(scenario_5_aligner_multi_instrument())
    results.append(scenario_6_full_pipeline())
    results.append(scenario_7_file_size_budget())

    passed = sum(results)
    total = len(results)
    print("\n" + "=" * 60)
    print(f"== 总计: {passed}/{total} 通过 ==")
    print("=" * 60)
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())
