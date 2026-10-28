"""Step 3 OKX 客户端集成烟测（spec §10.3）。

第一次真正联网调用 OKX REST API，验证：
1. 时间同步：拿到 OKX 服务端时间，|offset_ms| < 15000ms
2. 历史K线：拉 100 根 BTC-USDT 1H，字段完整、不可变
3. 产品配置：拉 SPOT 产品列表，包含 BTC-USDT
4. 资金费率：拉 BTC-USDT-SWAP 当前资金费率
5. 限频器：连发 25 次请求触发令牌桶阻塞（不触发 50011，本地阻塞）
6. 错误处理：请求不存在的 inst_id 应返回空 data 或业务错误码

运行：
    python3 tests/smoke/test_okx.py

依赖：
    .env 文件含 OKX_API_KEY / OKX_SECRET / OKX_PASSPHRASE
"""
from __future__ import annotations

import os
import sys
import time
from decimal import Decimal
from pathlib import Path

from dotenv import load_dotenv

# 把项目根加到 sys.path（用脚本运行）
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from quant.foundation.config import OKXSettings  # noqa: E402
from quant.models import Bar, FundingRate, Instrument  # noqa: E402
from quant.okx.client import OKXClient  # noqa: E402
from quant.okx.rate_limiter import RateLimiter  # noqa: E402


def _print(label: str, ok: bool, detail: str = "") -> None:
    mark = "PASS" if ok else "FAIL"
    print(f"[{mark}] {label}{f' — {detail}' if detail else ''}")


def _step1_sync_time(client: OKXClient) -> bool:
    """1. 时间同步。"""
    print("\n=== Step 1: 时间同步 ===")
    try:
        offset_ms = client.sync_time()
    except Exception as e:
        _print("sync_time", False, f"抛异常: {e}")
        return False

    ok = abs(offset_ms) < 15000
    _print(
        "sync_time",
        ok,
        f"offset_ms={offset_ms}（要求 |offset|<15000ms）",
    )
    return ok


def _step2_history_candles(client: OKXClient) -> bool:
    """2. 历史K线拉取。"""
    print("\n=== Step 2: 历史K线拉取 BTC-USDT 1H ===")
    try:
        t0 = time.perf_counter()
        bars = client.get_history_candles("BTC-USDT", "1H", limit=100)
        elapsed_ms = (time.perf_counter() - t0) * 1000
    except Exception as e:
        _print("get_history_candles", False, f"抛异常: {e}")
        return False

    if len(bars) != 100:
        _print("candles count", False, f"期望 100 根，实际 {len(bars)}")
        return False

    # 字段完整性
    b0 = bars[0]
    fields_ok = all(
        getattr(b0, f) is not None
        for f in ["ts", "open", "high", "low", "close", "vol", "vol_quote", "confirm"]
    )
    # 类型正确
    types_ok = (
        isinstance(b0, Bar)
        and isinstance(b0.ts, int)
        and isinstance(b0.open, Decimal)
        and isinstance(b0.close, Decimal)
    )
    # OHLC 合法
    ohlc_ok = (
        b0.high >= max(b0.open, b0.close)
        and b0.low <= min(b0.open, b0.close)
        and b0.high >= b0.low
    )
    # 不可变
    frozen_ok = True
    try:
        b0.close = Decimal("0")  # type: ignore[misc]
        frozen_ok = False
    except Exception:
        pass
    # 时间序列降序（OKX history-candles 默认从新到旧）
    sorted_ok = all(bars[i].ts > bars[i + 1].ts for i in range(len(bars) - 1))

    ok = fields_ok and types_ok and ohlc_ok and frozen_ok and sorted_ok
    _print(
        "get_history_candles",
        ok,
        f"100 根，{elapsed_ms:.0f}ms，"
        f"first ts={b0.ts}, close={b0.close}, "
        f"fields={fields_ok}, types={types_ok}, "
        f"ohlc={ohlc_ok}, frozen={frozen_ok}, sorted={sorted_ok}",
    )
    return ok


def _step3_instruments(client: OKXClient) -> bool:
    """3. 产品配置。"""
    print("\n=== Step 3: 产品配置 SPOT ===")
    try:
        instruments = client.get_instruments("SPOT")
    except Exception as e:
        _print("get_instruments", False, f"抛异常: {e}")
        return False

    if len(instruments) == 0:
        _print("instruments count", False, "返回空列表")
        return False

    # 找 BTC-USDT
    btc_usdt = next((i for i in instruments if i.inst_id == "BTC-USDT"), None)
    if btc_usdt is None:
        _print("find BTC-USDT", False, "未找到 BTC-USDT")
        return False

    # 类型与字段
    type_ok = isinstance(btc_usdt, Instrument)
    spot_ok = btc_usdt.inst_type == "SPOT"
    no_settle_ok = btc_usdt.settle_ccy == ""  # SPOT 应无 settle_ccy
    base_ok = btc_usdt.base_ccy == "BTC"
    quote_ok = btc_usdt.quote_ccy == "USDT"

    ok = type_ok and spot_ok and no_settle_ok and base_ok and quote_ok
    _print(
        "get_instruments",
        ok,
        f"SPOT 共 {len(instruments)} 个产品，"
        f"BTC-USDT: inst_type={btc_usdt.inst_type}, "
        f"base={btc_usdt.base_ccy}, quote={btc_usdt.quote_ccy}, "
        f"settle='{btc_usdt.settle_ccy}', tick_sz={btc_usdt.tick_sz}",
    )
    return ok


def _step4_funding_rate(client: OKXClient) -> bool:
    """4. 资金费率。"""
    print("\n=== Step 4: 资金费率 BTC-USDT-SWAP ===")
    try:
        fr = client.get_funding_rate("BTC-USDT-SWAP")
    except Exception as e:
        _print("get_funding_rate", False, f"抛异常: {e}")
        return False

    if fr is None:
        _print("get_funding_rate", False, "返回 None（可能 SWAP 暂无费率）")
        return False

    type_ok = isinstance(fr, FundingRate)
    rate_ok = abs(fr.rate) < Decimal("1")
    next_ts_ok = fr.next_funding_ts > fr.ts if fr.next_funding_ts and fr.ts else True

    ok = type_ok and rate_ok and next_ts_ok
    _print(
        "get_funding_rate",
        ok,
        f"inst_id={fr.inst_id}, rate={fr.rate} ({float(fr.rate)*100:.4f}%), "
        f"ts={fr.ts}, next_ts={fr.next_funding_ts}, "
        f"type={type_ok}, |rate|<1={rate_ok}, next>now={next_ts_ok}",
    )
    return ok


def _step5_rate_limiter_local_blocking() -> bool:
    """5. 限频器本地阻塞验证（不触发 50011）。

    capacity=20 + refill=10/s：连续发 25 次后第 21-25 次必须阻塞。
    """
    print("\n=== Step 5: 限频器本地阻塞 ===")
    rl = RateLimiter()
    # 拿光 public 桶的 20 个令牌
    t0 = time.monotonic()
    for _ in range(20):
        rl.acquire("public", timeout=1.0)
    elapsed_first_20 = time.monotonic() - t0
    # 第 21 个应阻塞约 0.1s（refill=10/s, 1 个令牌需 0.1s）
    t1 = time.monotonic()
    rl.acquire("public", timeout=2.0)
    elapsed_21st = time.monotonic() - t1

    first_20_fast = elapsed_first_20 < 0.05
    blocked_21st = 0.05 < elapsed_21st < 0.5

    ok = first_20_fast and blocked_21st
    _print(
        "rate_limiter local block",
        ok,
        f"前 20 个耗时 {elapsed_first_20*1000:.0f}ms (<50ms), "
        f"第 21 个阻塞 {elapsed_21st*1000:.0f}ms (50-500ms)",
    )
    return ok


def _step6_error_handling(client: OKXClient) -> bool:
    """6. 错误处理：请求不存在的 inst_id。"""
    print("\n=== Step 6: 错误处理 ===")
    try:
        bars = client.get_history_candles("FAKE-FAKE-FAKE", "1H", limit=1)
        # OKX 通常返回空 data
        empty_ok = len(bars) == 0
        _print(
            "fake inst_id returns empty",
            empty_ok,
            f"返回 {len(bars)} 根 K线（应为 0）",
        )
        return empty_ok
    except Exception as e:
        # 也可能返回业务错误码，但不应崩溃
        _print(
            "fake inst_id handled gracefully",
            True,
            f"抛业务异常: {type(e).__name__}: {e}",
        )
        return True


def main() -> int:
    # 加载 .env
    env_path = ROOT / ".env"
    if not env_path.exists():
        print(f"ERROR: .env not found at {env_path}")
        return 2
    load_dotenv(env_path)

    api_key = os.environ.get("OKX_API_KEY", "")
    secret = os.environ.get("OKX_SECRET", "")
    passphrase = os.environ.get("OKX_PASSPHRASE", "")
    if not api_key or not secret or not passphrase:
        print("ERROR: OKX_API_KEY / OKX_SECRET / OKX_PASSPHRASE not set in .env")
        return 2

    # 构造 Settings 与 Client
    settings = OKXSettings(
        api_key=api_key,
        secret=secret,
        passphrase=passphrase,
        demo=False,
    )
    print(f"API Key: {api_key[:6]}...{api_key[-4:]} ({len(api_key)} chars)")
    print("Mode: LIVE (demo=False)")

    results: list[bool] = []
    client = OKXClient(settings, sync_time_on_init=False)
    try:
        results.append(_step1_sync_time(client))
        results.append(_step2_history_candles(client))
        results.append(_step3_instruments(client))
        results.append(_step4_funding_rate(client))
        results.append(_step5_rate_limiter_local_blocking())
        results.append(_step6_error_handling(client))
    finally:
        client.close()

    passed = sum(results)
    total = len(results)
    print(f"\n== 总计: {passed}/{total} 通过 ==")
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())
