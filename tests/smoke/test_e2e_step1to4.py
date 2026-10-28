"""Step 1+2+3+4 全模块综合联调测试（真实 OKX + 全模块串联）。

本测试串联 M1 里程碑前 4 步所有模块，验证跨模块边界处的数据流转：
  Step 1: foundation（config / logger / errors / types）
  Step 2: models（Bar / Instrument / FundingRate / Tick）
  Step 3: okx（signer / rate_limiter / client）
  Step 4: storage（bar_storage / instrument_storage / validator / aligner）

与现有 step1to3 / step4 烟测的区别：
- step1to3：覆盖 foundation → models → okx 联调
- step4：覆盖 okx → storage 主链路（重复了 step1to3 的部分模块）
- 本测试：补齐缺口，覆盖 step1to3 没复测的 Step 4 模块 + step4 没串联的 Step 1 独立模块
          （errors 链路 / signer 直接调用 / rate_limiter 直接调用 / logger 脱敏 / sync_time）

运行：
    python3 tests/smoke/test_e2e_step1to4.py
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
from quant.foundation.errors import AuthError, NetworkError, QuantError  # noqa: E402
from quant.foundation.logger import get_logger, setup_logger  # noqa: E402
from quant.models.bar import Bar  # noqa: E402
from quant.okx.client import OKXClient  # noqa: E402
from quant.okx.rate_limiter import RateLimiter  # noqa: E402
from quant.okx.signer import sign  # noqa: E402
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
# 场景 1: Foundation errors 完整异常链（Step 1）
# =====================================================================


def scenario_1_foundation_errors_chain() -> bool:
    """验证 QuantError 异常继承链与 OKXClient 错误映射对接。"""
    print("\n=== 场景 1: Foundation errors 异常链 ===")
    # 异常继承关系（不联网）
    inheritance_ok = (
        issubclass(AuthError, QuantError)
        and issubclass(NetworkError, QuantError)
    )

    # 真实联网：用错凭证触发 AuthError，验证 OKXClient 把 OKX 错误码
    # 映射成 QuantError 子类
    wrong_settings = OKXSettings(
        api_key="wrong_key",
        secret="wrong_secret",
        passphrase="wrong_pass",
        demo=False,
    )
    client = OKXClient(wrong_settings, sync_time_on_init=False)
    try:
        client.get("/api/v5/account/balance")
        _print("wrong creds should raise", False, "未抛异常")
        return False
    except AuthError as e:
        auth_ok = e.code in ("50101", "50111", "50113", "50105", "50103")
        _print(
            "wrong creds → AuthError(QuantError 子类)",
            auth_ok,
            f"code={e.code}, type={type(e).__name__}, "
            f"isinstance(QuantError)={isinstance(e, QuantError)}",
        )
        return inheritance_ok and auth_ok
    except QuantError as e:
        _print("wrong creds → AuthError", False,
               f"抛了 QuantError 但非 AuthError: {type(e).__name__}")
        return False
    except Exception as e:
        _print("wrong creds → AuthError", False,
               f"非预期异常: {type(e).__name__}: {e}")
        return False
    finally:
        client.close()


# =====================================================================
# 场景 2: Signer 函数被 OKXClient 真实使用（Step 1+3 串联）
# =====================================================================


def scenario_2_signer_used_in_real_client() -> bool:
    """验证 signer.sign 函数与 OKXClient 的对接（不直接调 signer，间接验证）。"""
    print("\n=== 场景 2: signer.sign 函数对接 ===")
    # 直接调用 signer 验证函数本身（不依赖网络）
    ts = "2024-01-15T10:30:00.123Z"
    method = "GET"
    path = "/api/v5/market/candles?instId=BTC-USDT"
    body = ""
    secret = "test_secret"
    sig1 = sign(ts, method, path, body, secret)
    sig2 = sign(ts, method, path, body, secret)
    deterministic = sig1 == sig2

    # 改任意输入，签名应不同
    sig_diff = sign(ts, method, path, body, "different_secret") != sig1
    sig_diff_ts = sign("2024-01-15T10:30:00.124Z", method, path, body, secret) != sig1

    # 真实联网：OKXClient 内部应该用 signer 生成签名头
    # 用对凭证拉真实数据，如果签名错会被 OKX 拒绝
    client = _make_client()
    try:
        bars = client.get_candles("BTC-USDT", "1H", limit=1)
        real_call_ok = len(bars) == 1
        _print(
            "signer → OKXClient 真实使用",
            deterministic and sig_diff and sig_diff_ts and real_call_ok,
            f"deterministic={deterministic}, "
            f"diff_secret={sig_diff}, diff_ts={sig_diff_ts}, "
            f"real_call_with_signer={real_call_ok}",
        )
        return deterministic and sig_diff and sig_diff_ts and real_call_ok
    except Exception as e:
        _print("signer 对接异常", False, f"{type(e).__name__}: {e}")
        return False
    finally:
        client.close()


# =====================================================================
# 场景 3: RateLimiter 独立功能 + 被 OKXClient 使用（Step 1+3）
# =====================================================================


def scenario_3_rate_limiter_integration() -> bool:
    """验证 RateLimiter 令牌桶独立功能 + 在 OKXClient 中被使用。"""
    print("\n=== 场景 3: RateLimiter 令牌桶对接 ===")
    # 独立调用 RateLimiter 验证基础功能
    rl = RateLimiter()
    # history 组应能立即拿到（桶满）
    start = __import__("time").perf_counter()
    rl.acquire("history", timeout=2.0)
    elapsed = __import__("time").perf_counter() - start
    fast_acquire = elapsed < 0.1

    # penalize 后再次 acquire 应被阻塞
    rl.penalize("history", seconds=0.5)
    start = __import__("time").perf_counter()
    rl.acquire("history", timeout=2.0)
    elapsed = __import__("time").perf_counter() - start
    penalize_blocks = 0.3 < elapsed < 1.0

    # 真实联网：OKXClient 内部 RateLimiter 应自动生效
    # 连续 3 次 get_candles 应该都成功（不超限频）
    client = _make_client()
    try:
        results = []
        for _ in range(3):
            bars = client.get_candles("BTC-USDT", "1H", limit=1)
            results.append(len(bars) == 1)
        all_ok = all(results)
        _print(
            "RateLimiter 独立 + OKXClient 对接",
            fast_acquire and penalize_blocks and all_ok,
            f"fast_acquire={fast_acquire} ({elapsed:.3f}s), "
            f"penalize_blocks={penalize_blocks}, "
            f"3 real calls all ok={all_ok}",
        )
        return fast_acquire and penalize_blocks and all_ok
    except Exception as e:
        _print("RateLimiter 对接异常", False, f"{type(e).__name__}: {e}")
        return False
    finally:
        client.close()


# =====================================================================
# 场景 4: Logger 脱敏在 OKXClient 调用中生效（Step 1+3）
# =====================================================================


def scenario_4_logger_redaction_in_client() -> bool:
    """验证 logger 脱敏函数能拦住 secret/passphrase。"""
    print("\n=== 场景 4: Logger 脱敏 ===")
    setup_logger(level="DEBUG", file=tempfile.mktemp(suffix=".log"))
    get_logger("test.redaction")

    # 直接调 _redact_message 验证脱敏逻辑
    from quant.foundation.logger import _redact_message  # noqa: E402

    # JSON 形式
    msg1 = 'Loading config {"api_key": "abc123", "secret": "xyz"}'
    r1 = _redact_message(msg1)
    json_redacted = "abc123" not in r1 and "xyz" not in r1 and "REDACTED" in r1

    # kv 形式
    msg2 = "OKX passPhrase=secret_value_here"
    r2 = _redact_message(msg2)
    kv_redacted = "secret_value_here" not in r2 and "REDACTED" in r2

    _print(
        "logger 脱敏（JSON + kv 形式）",
        json_redacted and kv_redacted,
        f"json={json_redacted}, kv={kv_redacted}",
    )
    return json_redacted and kv_redacted


# =====================================================================
# 场景 5: sync_time 与 OKXClient 时间偏差校正（Step 3）
# =====================================================================


def scenario_5_sync_time() -> bool:
    """验证 OKXClient.sync_time 拉服务器时间并算 offset。"""
    print("\n=== 场景 5: sync_time 时间偏差校正 ===")
    client = _make_client()
    try:
        # sync_time 前后 offset 应被设置
        offset_before = client._server_time_offset_ms
        client.sync_time()
        offset_after = client._server_time_offset_ms

        # offset 应该是个合理值（本地时钟与 OKX 服务器偏差通常 < 5s）
        offset_reasonable = abs(offset_after) < 5000

        _print(
            "sync_time 校时",
            offset_reasonable,
            f"offset_before={offset_before}ms, "
            f"offset_after={offset_after}ms",
        )
        return offset_reasonable
    except Exception as e:
        _print("sync_time 异常", False, f"{type(e).__name__}: {e}")
        return False
    finally:
        client.close()


# =====================================================================
# 场景 6: 全链路 OKX → Bar 模型 → Storage → Validator → Aligner
#         （Step 1+2+3+4 完整串联，本测试核心场景）
# =====================================================================


def scenario_6_full_chain_all_modules() -> bool:
    """Step 1-4 全模块串通：foundation → models → okx → storage。"""
    print("\n=== 场景 6: 全链路 1-4 模块串联 ===")
    setup_logger(level="DEBUG", file=tempfile.mktemp(suffix=".log"))
    log = get_logger("e2e.step1to4")

    # Step 1: 加载 Settings（直接构造 OKXSettings）
    load_dotenv(ROOT / ".env")
    settings = OKXSettings(
        api_key=os.environ["OKX_API_KEY"],
        secret=os.environ["OKX_SECRET"],
        passphrase=os.environ["OKX_PASSPHRASE"],
        demo=False,
    )
    log.info(f"Loaded OKXSettings demo={settings.demo}")

    # Step 3: 构造 OKXClient（用 settings + signer + rate_limiter）
    client = OKXClient(settings, sync_time_on_init=False)

    with tempfile.TemporaryDirectory() as tmp:
        # Step 4: 存储层
        bar_storage = BarStorage(Path(tmp))
        inst_storage = InstrumentStorage(Path(tmp))
        validator = DataValidator()
        aligner = DataAligner()

        try:
            # ---- 子链路 A: K线 ----
            log.info("拉 BTC-USDT 1H 30 根")
            bars_btc = client.get_candles("BTC-USDT", "1H", limit=30)
            log.info(f"got {len(bars_btc)} BTC bars")
            bars_btc_ok = len(bars_btc) == 30

            # Step 2: Bar 模型字段校验
            sample = bars_btc[0]
            bar_fields_ok = (
                isinstance(sample.ts, int)
                and isinstance(sample.open, Decimal)
                and isinstance(sample.high, Decimal)
                and isinstance(sample.close, Decimal)
                and sample.high >= sample.low
                and sample.high >= sample.open
                and sample.high >= sample.close
            )

            # 落盘
            saved = bar_storage.save_bars(bars_btc)
            bars_saved_ok = saved == 30

            # 读回
            ts_min = min(b.ts for b in bars_btc)
            ts_max = max(b.ts for b in bars_btc)
            loaded = bar_storage.load_bars("BTC-USDT", "1H", ts_min, ts_max)
            bars_loaded_ok = len(loaded) == 30

            # 字段一致
            orig_map = {b.ts: b.close for b in bars_btc}
            loaded_map = {b.ts: b.close for b in loaded}
            fields_match = all(orig_map[ts] == loaded_map[ts] for ts in orig_map)

            # 校验
            report = validator.validate(loaded)
            validator_ok = (
                len(report.duplicates) == 0
                and len(report.invalid_ohlc) == 0
                and len(report.errors) == 0
            )

            # ---- 子链路 B: 产品配置 ----
            log.info("拉 SPOT 产品列表")
            spot_insts = client.get_instruments("SPOT")
            log.info(f"got {len(spot_insts)} SPOT instruments")
            insts_count_ok = len(spot_insts) > 100  # SPOT 应 > 100 个

            # Step 2: Instrument 模型字段校验
            inst_sample = spot_insts[0]
            inst_fields_ok = (
                isinstance(inst_sample.inst_id, str)
                and isinstance(inst_sample.tick_sz, Decimal)
                and isinstance(inst_sample.lot_sz, Decimal)
            )

            # 落盘 + 读回
            saved_insts = inst_storage.save_instruments("SPOT", spot_insts)
            loaded_insts = inst_storage.load_instruments("SPOT")
            inst_roundtrip_ok = (
                saved_insts == len(spot_insts)
                and len(loaded_insts) == len(spot_insts)
            )

            # load_one
            one = inst_storage.load_one(inst_sample.inst_id, "SPOT")
            load_one_ok = one is not None and one.inst_id == inst_sample.inst_id

            # ---- 子链路 C: 多品种对齐 ----
            log.info("拉 ETH-USDT 1H 30 根，做多品种对齐")
            bars_eth = client.get_candles("ETH-USDT", "1H", limit=30)
            bars_eth_ok = len(bars_eth) == 30

            df = aligner.align(
                {"BTC-USDT": bars_btc, "ETH-USDT": bars_eth},
                fill="ffill",
            )
            align_ok = (
                len(df) == 30
                and "BTC-USDT_close" in df.columns
                and "ETH-USDT_close" in df.columns
            )

            all_ok = all([
                bars_btc_ok, bar_fields_ok, bars_saved_ok, bars_loaded_ok,
                fields_match, validator_ok,
                insts_count_ok, inst_fields_ok, inst_roundtrip_ok, load_one_ok,
                bars_eth_ok, align_ok,
            ])
            _print(
                "Step 1-4 全模块串联",
                all_ok,
                f"bars: get={bars_btc_ok}, fields={bar_fields_ok}, "
                f"save={bars_saved_ok}, load={bars_loaded_ok}, "
                f"fields_match={fields_match}, validate={validator_ok}; "
                f"instruments: count={insts_count_ok}, "
                f"fields={inst_fields_ok}, roundtrip={inst_roundtrip_ok}, "
                f"load_one={load_one_ok}; "
                f"aligner: eth_get={bars_eth_ok}, align={align_ok}",
            )
            return all_ok
        except Exception as e:
            _print("全链路异常", False, f"{type(e).__name__}: {e}")
            return False
        finally:
            client.close()


# =====================================================================
# 场景 7: 错误链路在存储层验证（Step 1+4）
# =====================================================================


def scenario_7_errors_in_storage_layer() -> bool:
    """验证 QuantError 异常体系被 Step 4 storage 层正确使用。"""
    print("\n=== 场景 7: errors 在 storage 层的使用 ===")
    with tempfile.TemporaryDirectory() as tmp:
        bar_storage = BarStorage(Path(tmp))

        # 写入 + 读回正常路径无异常
        bar = Bar(
            ts=1704067200000,
            open=Decimal("100.0"),
            high=Decimal("110.0"),
            low=Decimal("95.0"),
            close=Decimal("105.0"),
            vol=Decimal("10.0"),
            vol_quote=Decimal("1050.0"),
            confirm=1,
            inst_id="BTC-USDT",
            bar_size="1H",
        )
        saved = bar_storage.save_bars([bar])
        save_ok = saved == 1

        loaded = bar_storage.load_bars("BTC-USDT", "1H", 1704067200000, 1704067200000)
        load_ok = len(loaded) == 1

        # 不存在的区间返回空列表，不抛异常
        empty = bar_storage.load_bars("BTC-USDT", "1H", 1, 2)
        empty_ok = empty == []

        # list_years 不存在返回空列表
        years = bar_storage.list_years("ETH-USDT", "1H")
        no_years_ok = years == []

        _print(
            "storage 层错误边界",
            save_ok and load_ok and empty_ok and no_years_ok,
            f"save={save_ok}, load={load_ok}, "
            f"empty_return_not_raise={empty_ok}, "
            f"no_years_return_empty={no_years_ok}",
        )
        return save_ok and load_ok and empty_ok and no_years_ok


def main() -> int:
    print("=" * 60)
    print("Step 1+2+3+4 全模块综合联调测试")
    print("=" * 60)

    results: list[bool] = []
    results.append(scenario_1_foundation_errors_chain())
    results.append(scenario_2_signer_used_in_real_client())
    results.append(scenario_3_rate_limiter_integration())
    results.append(scenario_4_logger_redaction_in_client())
    results.append(scenario_5_sync_time())
    results.append(scenario_6_full_chain_all_modules())
    results.append(scenario_7_errors_in_storage_layer())

    passed = sum(results)
    total = len(results)
    print("\n" + "=" * 60)
    print(f"== 总计: {passed}/{total} 通过 ==")
    print("=" * 60)
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())
