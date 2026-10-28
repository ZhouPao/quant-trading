"""Models Step 2 集成烟测。

验证：
1. 用 OKX 官方文档给出的真实 K 线返回格式构造 Bar，字段映射正确
2. Tick/Instrument/FundingRate 用真实字段构造通过校验
3. 不可变性 + Decimal 精度
4. 跨模型批量构造 + 字段访问性能

不依赖网络（不真实调 OKX REST），只用 OKX 文档约定的真实字段格式做离线烟测，
避免在 Step 2 阶段还没有 OKXClient 时就联网。

运行：
    python3 tests/smoke/test_models.py
"""
from __future__ import annotations

import sys
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

# 加载 .env（虽然本烟测不直接用凭证，但保持习惯）
ENV_FILE = Path(__file__).resolve().parents[2] / ".env"
if ENV_FILE.exists():
    import os
    for line in ENV_FILE.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        v = v.strip().strip("'").strip('"')
        os.environ.setdefault(k, v)


def main() -> int:
    from quant.models import Bar, FundingRate, Instrument, Tick

    print("== 1. 用 OKX 真实 K 线格式构造 Bar ==")
    # OKX /api/v5/market/candles 真实返回示例（节选）：
    # data 字段是 list of list[str]，每根 K 线 9 个字符串
    okx_candles_response = [
        [
            "1697148800000",  # ts（开盘时间，UTC ms）
            "27650.5",  # o
            "27700.8",  # h
            "27580.1",  # l
            "27690.3",  # c
            "1250.5",  # vol（基础币）
            "34567890.1",  # volCcy（被忽略）
            "34650000.0",  # volCcyQuote（计价币）
            "1",  # confirm
        ],
        [
            "1697145200000",
            "27600.0",
            "27660.0",
            "27590.0",
            "27650.5",
            "980.2",
            "27000000.0",
            "27100000.0",
            "1",
        ],
    ]
    bars = [
        Bar.from_okx_raw(raw, inst_id="BTC-USDT", bar_size="1H")
        for raw in okx_candles_response
    ]
    print(f"  构造 {len(bars)} 根 Bar")
    print(f"  Bar[0]: ts={bars[0].ts} open={bars[0].open} close={bars[0].close}")
    print(f"  Bar[0]: vol_quote={bars[0].vol_quote} (==volCcyQuote 字段)")
    print(f"  Bar[0].is_closed() = {bars[0].is_closed()}")
    assert bars[0].vol_quote == Decimal("34650000.0")
    assert bars[0].is_closed() is True
    # 验证时间顺序：后一根比前一根晚
    assert bars[0].ts > bars[1].ts

    print("\n== 2. Tick 真实盘口格式 ==")
    # OKX /api/v5/market/ticker 返回示例字段
    tick = Tick(
        ts=1697148800123,
        inst_id="BTC-USDT",
        bid_px=Decimal("27690.2"),
        bid_sz=Decimal("0.5"),
        ask_px=Decimal("27690.5"),
        ask_sz=Decimal("0.8"),
        last_px=Decimal("27690.3"),
        last_sz=Decimal("0.1"),
    )
    print(f"  spread = {tick.ask_px - tick.bid_px}")
    assert tick.ask_px > tick.bid_px
    assert tick.inst_id == "BTC-USDT"

    print("\n== 3. Instrument 真实产品配置 ==")
    # OKX /api/v5/public/instruments?instType=SPOT 真实返回字段（节选）
    spot = Instrument(
        inst_id="BTC-USDT",
        inst_type="SPOT",
        base_ccy="BTC",
        quote_ccy="USDT",
        settle_ccy="",
        tick_sz=Decimal("0.1"),
        lot_sz=Decimal("0.00000001"),
        min_sz=Decimal("0.00001"),
        ct_val=Decimal("1"),
        state="live",
        list_time=1600000000000,
    )
    swap = Instrument(
        inst_id="BTC-USDT-SWAP",
        inst_type="SWAP",
        base_ccy="BTC",
        quote_ccy="USDT",
        settle_ccy="USDT",
        tick_sz=Decimal("0.1"),
        lot_sz=Decimal("0.0001"),
        min_sz=Decimal("0.0001"),
        ct_val=Decimal("0.01"),  # 永续合约每张 0.01 BTC
        state="live",
        list_time=1600000000000,
    )
    print(f"  SPOT is_spot={spot.is_spot()} ct_val={spot.ct_val}")
    print(f"  SWAP is_swap={swap.is_swap()} ct_val={swap.ct_val}")
    assert spot.is_spot() and spot.ct_val == Decimal("1")
    assert swap.is_swap() and swap.ct_val == Decimal("0.01")

    print("\n== 4. FundingRate 真实资金费率 ==")
    # OKX /api/v5/public/funding-rate 真实返回字段（节选）
    fr = FundingRate(
        inst_id="BTC-USDT-SWAP",
        ts=1697148800000,
        rate=Decimal("0.00015"),  # 0.015%
        next_funding_ts=1697148800000 + 28800000,  # 8h 后
    )
    print(f"  rate={fr.rate} ({fr.rate * 100}%) next_funding_ts={fr.next_funding_ts}")
    assert fr.rate == Decimal("0.00015")
    assert fr.next_funding_ts > fr.ts

    print("\n== 5. Decimal 精度验证（避免 float 误差）==")
    # 真实场景：高波动 K 线，价格有 8 位小数
    precise_bar = Bar.from_okx_raw(
        [
            "1697148800000",
            "0.12345678",
            "0.12345689",  # 比开高 1 个最小单位
            "0.12345670",  # 比开低 8 个最小单位
            "0.12345685",
            "1000",
            "0",
            "123.456789",
            "1",
        ],
        inst_id="PEPE-USDT",
        bar_size="1H",
    )
    diff = precise_bar.high - precise_bar.low
    print(f"  PEPE 高低差 = {diff}")
    assert diff == Decimal("0.00000019")  # 不会被 float 误差污染

    print("\n== 6. 批量构造性能（1000 根 Bar）==")
    import time
    raw_list = [okx_candles_response[0].copy() for _ in range(1000)]
    # 调整 ts 让每根独立合法
    for i, r in enumerate(raw_list):
        r[0] = str(1697148800000 + i * 3600000)

    t0 = time.perf_counter()
    bars_1k = [Bar.from_okx_raw(r, "BTC-USDT", "1H") for r in raw_list]
    elapsed_ms = (time.perf_counter() - t0) * 1000
    print(f"  1000 根 Bar 构造耗时: {elapsed_ms:.1f}ms ({elapsed_ms / 1000:.3f}ms/bar)")
    assert len(bars_1k) == 1000
    # 内存友好：slots=True 不应有 __dict__
    assert not hasattr(bars_1k[0], "__dict__")

    # 验证 ts 升序
    for i in range(1, len(bars_1k)):
        assert bars_1k[i].ts > bars_1k[i - 1].ts

    print("\n== 7. 不可变性 + 哈希（可放入 set / dict key）==")
    # frozen dataclass 默认可哈希
    b1 = bars[0]
    b2 = Bar.from_okx_raw(okx_candles_response[0], "BTC-USDT", "1H")
    assert b1 == b2
    assert b1 is not b2  # 不同实例但相等
    assert hash(b1) == hash(b2)
    print(f"  hash(b1) == hash(b2): {hash(b1) == hash(b2)}")

    # 不可变
    try:
        b1.close = Decimal("99999")
    except Exception as e:
        print(f"  FrozenInstanceError 正确抛出: {type(e).__name__}")

    print("\n== 全部通过 ==")
    return 0


if __name__ == "__main__":
    sys.exit(main())
