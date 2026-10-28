"""Step 1+2+3 端到端联调测试（真实 OKX + 全模块串联）。

验证：从配置加载 → 日志初始化 → OKXClient 构造 → 拉数据 → 转 Bar 模型 → 落盘到内存 →
      校验不可变性 → 跨调用一致性，整条数据链路无 bug。

与现有烟测的区别：
- 现有烟测各模块单独验证（foundation/models/okx 各一份）
- 本测试串联三步的所有模块，验证跨模块边界处的数据流转

运行：
    python3 tests/smoke/test_e2e_step1to3.py
"""
from __future__ import annotations

import os
import sys
from decimal import Decimal
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from quant.foundation.config import OKXSettings, Settings  # noqa: E402
from quant.foundation.errors import AuthError, QuantError  # noqa: E402
from quant.foundation.logger import get_logger, setup_logger  # noqa: E402
from quant.models import Instrument  # noqa: E402
from quant.okx.client import OKXClient  # noqa: E402
from quant.okx.rate_limiter import RateLimiter  # noqa: E402
from quant.okx.signer import sign  # noqa: E402


def _print(label: str, ok: bool, detail: str = "") -> None:
    mark = "PASS" if ok else "FAIL"
    print(f"[{mark}] {label}{f' — {detail}' if detail else ''}")


# =====================================================================
# 场景 1: Settings → OKXClient 跨模块配置传递
# =====================================================================


def scenario_1_config_to_client() -> bool:
    """验证 OKXSettings 字段正确传到 OKXClient 内部。"""
    print("\n=== 场景 1: Settings → OKXClient 跨模块配置传递 ===")
    env_path = ROOT / ".env"
    load_dotenv(env_path)
    api_key = os.environ["OKX_API_KEY"]
    secret = os.environ["OKX_SECRET"]
    passphrase = os.environ["OKX_PASSPHRASE"]

    # 用 OKXSettings 模型构造（验证 Step 1 config 模型）
    settings = OKXSettings(
        api_key=api_key, secret=secret, passphrase=passphrase, demo=False
    )
    # 用 OKXClient 接收（验证 Step 3 client 能正确吃 Settings）
    client = OKXClient(settings, sync_time_on_init=False)

    # 验证 client 内部确实持有正确值
    key_ok = client._s.api_key == api_key
    secret_ok = client._s.secret == secret
    pass_ok = client._s.passphrase == passphrase
    demo_ok = client._demo is False
    rate_limiter_ok = isinstance(client._rl, RateLimiter)
    offset_zero = client._server_time_offset_ms == 0  # 没调 sync_time 应为 0

    ok = all([key_ok, secret_ok, pass_ok, demo_ok, rate_limiter_ok, offset_zero])
    _print(
        "config → client 字段传递",
        ok,
        f"api_key={client._s.api_key[:6]}..., demo={client._demo}, "
        f"rate_limiter={type(client._rl).__name__}, "
        f"offset_ms={client._server_time_offset_ms}",
    )
    client.close()
    return ok


# =====================================================================
# 场景 2: Logger 脱敏 → OKXClient 内部日志不泄露凭证
# =====================================================================


def scenario_2_logger_redaction_in_client() -> bool:
    """验证 OKXClient 内部 INFO 日志不会泄露 api_key/secret。"""
    print("\n=== 场景 2: Logger 脱敏 + OKXClient 日志检查 ===")
    log_file = ROOT / "logs" / "e2e_test.log"
    log_file.parent.mkdir(parents=True, exist_ok=True)
    # 清掉旧日志
    if log_file.exists():
        log_file.unlink()
    setup_logger(level="DEBUG", file=str(log_file))

    # 用真凭证构造 client，但 sync_time_on_init=False 不联网
    load_dotenv(ROOT / ".env")
    settings = OKXSettings(
        api_key=os.environ["OKX_API_KEY"],
        secret=os.environ["OKX_SECRET"],
        passphrase=os.environ["OKX_PASSPHRASE"],
        demo=False,
    )
    log = get_logger("e2e.test")
    client = OKXClient(settings, sync_time_on_init=False)

    # 模拟一段可能泄露凭证的日志（生产可能这样写）
    log.info(f"client init api_key={settings.api_key} secret={settings.secret}")
    log.info(f"passphrase is {settings.passphrase}")
    log.debug(f"headers will include {client._headers('GET', '/x', 'ts', '')}")

    # 读日志文件，验证脱敏生效
    content = log_file.read_text(encoding="utf-8")
    real_key = settings.api_key
    real_secret = settings.secret
    real_pass = settings.passphrase

    key_leaked = real_key in content
    secret_leaked = real_secret in content
    pass_leaked = real_pass in content
    redaction_ok = "REDACTED" in content

    ok = (not key_leaked) and (not secret_leaked) and (not pass_leaked) and redaction_ok
    _print(
        "logger 脱敏 + client 日志",
        ok,
        f"key_leaked={key_leaked}, secret_leaked={secret_leaked}, "
        f"pass_leaked={pass_leaked}, has_REDACTED={redaction_ok}",
    )
    client.close()
    return ok


# =====================================================================
# 场景 3: Signer → OKXClient._headers 签名实际被调用
# =====================================================================


def scenario_3_signer_used_in_client() -> bool:
    """验证 OKXClient._headers 内部确实调用了 sign()。"""
    print("\n=== 场景 3: Signer → OKXClient._headers 集成 ===")
    settings = OKXSettings(
        api_key="test-key", secret="test-secret",
        passphrase="test-pass", demo=False,
    )
    client = OKXClient(settings, sync_time_on_init=False)
    ts = "2024-01-01T10:30:00.000Z"
    path = "/api/v5/account/balance"

    # 通过 client._headers 拿到签名
    headers = client._headers("GET", path, ts, "")
    client_sig = headers["OK-ACCESS-SIGN"]

    # 独立用 signer.sign 计算
    expected_sig = sign(ts, "GET", path, "", "test-secret")

    ok = client_sig == expected_sig and client_sig != "" and len(client_sig) > 0
    _print(
        "client._headers 调用 sign()",
        ok,
        f"client_sig == signer.sign: {client_sig == expected_sig}, "
        f"sig_len={len(client_sig)}",
    )
    client.close()
    return ok


# =====================================================================
# 场景 4: OKXClient.get_history_candles → Bar 模型构造完整链路（真实联网）
# =====================================================================


def scenario_4_real_candles_to_bar_model() -> bool:
    """真实联网：OKXClient 拉数据 → Bar.from_okx_raw 转换 → 验证字段映射。"""
    print("\n=== 场景 4: OKXClient → Bar.from_okx_raw 真实数据链路 ===")
    load_dotenv(ROOT / ".env")
    settings = OKXSettings(
        api_key=os.environ["OKX_API_KEY"],
        secret=os.environ["OKX_SECRET"],
        passphrase=os.environ["OKX_PASSPHRASE"],
        demo=False,
    )
    client = OKXClient(settings)
    try:
        client.sync_time()
        bars = client.get_history_candles("BTC-USDT", "1H", limit=50)
    except Exception as e:
        _print("联网拉数据失败", False, f"{type(e).__name__}: {e}")
        client.close()
        return False
    client.close()

    if len(bars) != 50:
        _print("bar count", False, f"期望 50，实际 {len(bars)}")
        return False

    # 跨模块字段映射验证（OKX 响应字段顺序 → Bar 属性）
    b0 = bars[0]
    fields = ["ts", "open", "high", "low", "close", "vol", "vol_quote", "confirm",
              "inst_id", "bar_size"]
    values_ok = all(getattr(b0, f) is not None for f in fields)
    types_ok = (
        isinstance(b0.ts, int) and b0.ts > 0
        and isinstance(b0.open, Decimal) and b0.open > 0
        and isinstance(b0.high, Decimal) and b0.high > 0
        and isinstance(b0.low, Decimal) and b0.low > 0
        and isinstance(b0.close, Decimal) and b0.close > 0
        and isinstance(b0.vol, Decimal) and b0.vol > 0
        and isinstance(b0.vol_quote, Decimal) and b0.vol_quote > 0
        and isinstance(b0.confirm, int) and b0.confirm in (0, 1)
        and b0.inst_id == "BTC-USDT"
        and b0.bar_size == "1H"
    )
    # OHLC 内部一致性（high>=max(open,close), low<=min(open,close)）
    ohlc_ok = (
        b0.high >= max(b0.open, b0.close)
        and b0.low <= min(b0.open, b0.close)
        and b0.high >= b0.low
    )
    # 时间序列降序
    sorted_ok = all(bars[i].ts > bars[i + 1].ts for i in range(len(bars) - 1))
    # 不可变
    frozen_ok = True
    try:
        b0.close = Decimal("0")  # type: ignore[misc]
        frozen_ok = False
    except Exception:
        pass
    # 可哈希（能放 set）
    hashable_ok = len(set(bars)) == 50
    # Decimal 精度（不丢精度）
    precision_ok = (
        b0.close == Decimal(str(b0.close))  # 没变成 float
    )

    ok = all([values_ok, types_ok, ohlc_ok, sorted_ok, frozen_ok, hashable_ok, precision_ok])
    _print(
        "OKX → Bar.from_okx_raw 完整链路",
        ok,
        f"50 根，fields={values_ok}, types={types_ok}, "
        f"ohlc={ohlc_ok}, sorted={sorted_ok}, "
        f"frozen={frozen_ok}, hashable={hashable_ok}, "
        f"precision={precision_ok}; "
        f"first ts={b0.ts} close={b0.close}",
    )
    return ok


# =====================================================================
# 场景 5: OKXClient.get_instruments → Instrument 模型构造（真实联网）
# =====================================================================


def scenario_5_real_instruments_to_model() -> bool:
    """真实联网：OKX → Instrument 模型，验证 SPOT/SWAP 规则。"""
    print("\n=== 场景 5: OKXClient → Instrument 模型真实链路 ===")
    load_dotenv(ROOT / ".env")
    settings = OKXSettings(
        api_key=os.environ["OKX_API_KEY"],
        secret=os.environ["OKX_SECRET"],
        passphrase=os.environ["OKX_PASSPHRASE"],
        demo=False,
    )
    client = OKXClient(settings, sync_time_on_init=False)
    try:
        spot_instruments = client.get_instruments("SPOT")
        swap_instruments = client.get_instruments("SWAP")
    except Exception as e:
        _print("联网失败", False, f"{type(e).__name__}: {e}")
        client.close()
        return False
    client.close()

    if not spot_instruments or not swap_instruments:
        _print(
            "instruments count", False,
            f"SPOT={len(spot_instruments)} SWAP={len(swap_instruments)}",
        )
        return False

    # 找 BTC-USDT（SPOT）和 BTC-USDT-SWAP（SWAP）
    btc_spot = next((i for i in spot_instruments if i.inst_id == "BTC-USDT"), None)
    btc_swap = next((i for i in swap_instruments if i.inst_id == "BTC-USDT-SWAP"), None)
    if btc_spot is None or btc_swap is None:
        _print(
            "find BTC pairs", False,
            f"spot_found={btc_spot is not None}, swap_found={btc_swap is not None}",
        )
        return False

    # SPOT 规则：settle_ccy 必须 == ""（Step 2 校验）
    spot_settle_empty = btc_spot.settle_ccy == ""
    spot_ct_val_one = btc_spot.ct_val == Decimal("1")
    spot_type = btc_spot.inst_type == "SPOT"
    spot_base = btc_spot.base_ccy == "BTC"
    spot_quote = btc_spot.quote_ccy == "USDT"

    # SWAP 规则：settle_ccy 非空（如 BTC 或 USDT）
    swap_settle_nonempty = btc_swap.settle_ccy != ""
    swap_ct_val_positive = btc_swap.ct_val > 0
    swap_type = btc_swap.inst_type == "SWAP"

    # 精度
    spot_tick_positive = btc_spot.tick_sz > 0
    spot_lot_positive = btc_spot.lot_sz > 0
    swap_tick_positive = btc_swap.tick_sz > 0

    # 不可变
    spot_frozen = True
    try:
        btc_spot.state = "dead"  # type: ignore[misc]
        spot_frozen = False
    except Exception:
        pass

    # 类型
    type_ok = isinstance(btc_spot, Instrument) and isinstance(btc_swap, Instrument)

    ok = all([
        spot_settle_empty, spot_ct_val_one, spot_type, spot_base, spot_quote,
        swap_settle_nonempty, swap_ct_val_positive, swap_type,
        spot_tick_positive, spot_lot_positive, swap_tick_positive,
        spot_frozen, type_ok,
    ])
    _print(
        "OKX → Instrument (SPOT+SWAP) 完整链路",
        ok,
        f"SPOT 共 {len(spot_instruments)} 个，SWAP 共 {len(swap_instruments)} 个；"
        f"BTC-USDT: settle='{btc_spot.settle_ccy}', ct_val={btc_spot.ct_val}, "
        f"tick_sz={btc_spot.tick_sz}; "
        f"BTC-USDT-SWAP: settle='{btc_swap.settle_ccy}', "
        f"ct_val={btc_swap.ct_val}",
    )
    return ok


# =====================================================================
# 场景 6: 错误链路：构造 client 用错凭证，应该抛 AuthError 而非崩溃
# =====================================================================


def scenario_6_wrong_credentials_error_chain() -> bool:
    """用错误凭证调鉴权端点，应抛 AuthError 而非 RuntimeError/ValueError。"""
    print("\n=== 场景 6: 错误凭证 → AuthError 异常链路 ===")
    settings = OKXSettings(
        api_key="wrong_key_xxxxxx",
        secret="wrong_secret_xxxxxx",
        passphrase="wrong_pass_xxxxxx",
        demo=False,
    )
    client = OKXClient(settings, sync_time_on_init=False)
    try:
        # 调一个需要鉴权的端点（/account/balance）
        client.get("/api/v5/account/balance")
        _print("wrong creds should raise", False, "未抛异常")
        return False
    except AuthError as e:
        # 期望的错误类型
        ok = e.code in ("50101", "50111", "50113", "50105", "50103")
        _print(
            "wrong creds → AuthError",
            ok,
            f"code={e.code}, msg={e.msg[:50] if e.msg else ''!r}, "
            f"type={type(e).__name__}",
        )
        return ok
    except QuantError as e:
        _print(
            "wrong creds → AuthError",
            False,
            f"抛了 QuantError 但不是 AuthError: {type(e).__name__}: {e}",
        )
        return False
    except Exception as e:
        _print(
            "wrong creds → AuthError",
            False,
            f"抛了非预期异常: {type(e).__name__}: {e}",
        )
        return False
    finally:
        client.close()


# =====================================================================
# 场景 7: Settings.load 从 TOML 加载（如 config.toml 不存在则跳过）
# =====================================================================


def scenario_7_settings_load_from_toml() -> bool | None:
    """如果存在 config/config.toml，验证 Settings.load 真能用。"""
    print("\n=== 场景 7: Settings.load 从 TOML 加载（可选）===")
    toml_path = ROOT / "config" / "config.toml"
    if not toml_path.exists():
        _print(
            "config.toml 跳过",
            True,
            "config/config.toml 不存在（仅有 .env），跳过本场景",
        )
        return True  # 不算失败

    try:
        s = Settings.load(toml_path)
        ok = bool(s.okx.api_key) and bool(s.okx.secret) and bool(s.okx.passphrase)
        _print(
            "Settings.load TOML",
            ok,
            f"api_key_len={len(s.okx.api_key)}, demo={s.okx.demo}, "
            f"data_dir={s.storage.data_dir}",
        )
        return ok
    except Exception as e:
        _print("Settings.load TOML", False, f"抛异常: {type(e).__name__}: {e}")
        return False


# =====================================================================
# 场景 8: 连续多次调用，验证限频器不卡、时间偏移稳定
# =====================================================================


def scenario_8_repeated_calls_stable() -> bool:
    """连续 5 次调 OKX API（不触发限频），验证 client 状态稳定。"""
    print("\n=== 场景 8: 连续 5 次调用，状态稳定性 ===")
    load_dotenv(ROOT / ".env")
    settings = OKXSettings(
        api_key=os.environ["OKX_API_KEY"],
        secret=os.environ["OKX_SECRET"],
        passphrase=os.environ["OKX_PASSPHRASE"],
        demo=False,
    )
    client = OKXClient(settings, sync_time_on_init=False)
    try:
        client.sync_time()
        offset_1 = client._server_time_offset_ms

        # 连续拉 5 次小数据
        results = []
        for _ in range(5):
            bars = client.get_candles("BTC-USDT", "1H", limit=1)
            results.append(len(bars) == 1)
        # 再次校时
        client.sync_time()
        offset_2 = client._server_time_offset_ms

        all_ok = all(results)
        offset_stable = abs(offset_1 - offset_2) < 2000  # 两次校时间偏差应小于 2s

        ok = all_ok and offset_stable
        _print(
            "5 次连续调用 + offset 稳定",
            ok,
            f"5 次 candle 拉取: {sum(results)}/5 成功, "
            f"offset_1={offset_1}ms, offset_2={offset_2}ms, "
            f"diff={abs(offset_1 - offset_2)}ms",
        )
        return ok
    except Exception as e:
        _print("连续调用失败", False, f"{type(e).__name__}: {e}")
        return False
    finally:
        client.close()


def main() -> int:
    print("=" * 60)
    print("Step 1+2+3 端到端联调测试")
    print("=" * 60)

    results: list[bool] = []
    results.append(scenario_1_config_to_client())
    results.append(scenario_2_logger_redaction_in_client())
    results.append(scenario_3_signer_used_in_client())
    results.append(scenario_4_real_candles_to_bar_model())
    results.append(scenario_5_real_instruments_to_model())
    results.append(scenario_6_wrong_credentials_error_chain())
    results.append(scenario_7_settings_load_from_toml())
    results.append(scenario_8_repeated_calls_stable())

    passed = sum(results)
    total = len(results)
    print("\n" + "=" * 60)
    print(f"== 总计: {passed}/{total} 通过 ==")
    print("=" * 60)
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())
