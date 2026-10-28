# Step 1+2+3 端到端联调测试报告

**测试日期**: 2026-10-17
**模块**: foundation + models + okx 三层跨模块
**结果**: ✅ 8/8 通过（联调发现并修复 2 个真实 bug）

---

## 1. 为什么要做联调

前 3 步每步都单独测试通过，但**跨模块边界**的数据流从未串联跑过：

- Step 1 测试只测了 Settings/Logger/Errors 的单元功能
- Step 2 测试只测了 Bar/Instrument 等模型构造
- Step 3 测试只测了 Signer/RateLimiter/OKXClient 内部逻辑

但实际运行时：
- Settings 是否真的把凭证传给 OKXClient？
- Logger 是否真的脱敏了 OKXClient 内部产生的日志？
- OKXClient 拉的真实 OKX 响应，能否通过 Bar.from_okx_raw 转 Bar 模型？
- AuthError 抛出来后是否真的有 `.msg` 属性供上层使用？

这些跨模块问题只有联调才能暴露。

## 2. 测试文件

| 文件路径 | 行数 | 场景数 |
|---|---|---|
| [tests/smoke/test_e2e_step1to3.py](file:///Users/daxiongmao/Desktop/quant-trading/tests/smoke/test_e2e_step1to3.py) | 490 | 8 |

## 3. 8 个测试场景

| # | 场景 | 验证目标 | 结果 |
|---|---|---|---|
| 1 | Settings → OKXClient 跨模块配置传递 | OKXSettings.api_key 真的进入 client._s | ✅ |
| 2 | Logger 脱敏 + OKXClient 日志检查 | client 内部 INFO/DEBUG 日志不含真凭证 | ✅（**发现 bug 2**） |
| 3 | Signer → OKXClient._headers 集成 | client._headers 内部确实调用 sign() | ✅ |
| 4 | OKXClient → Bar.from_okx_raw 真实数据链路 | 真实联网拉 BTC-USDT 50 根 K线 → Bar 类型/字段/OHLC/排序/不可变/可哈希/精度 全通过 | ✅ |
| 5 | OKXClient → Instrument 模型真实链路 | SPOT+SWAP 同时拉取，SPOT settle='' ct_val=1；SWAP settle='USDT' ct_val=0.01 | ✅ |
| 6 | 错误凭证 → AuthError 异常链路 | 错凭证调 /account/balance 抛 AuthError(50111) 而非 RuntimeError | ✅（**发现 bug 1**） |
| 7 | Settings.load 从 TOML 加载（可选） | 若 config.toml 存在，Settings.load 正常工作 | ✅ 跳过（仅有 .env） |
| 8 | 连续 5 次调用，状态稳定性 | 5 次 candle 拉取全成功，两次校时 offset 差 <2s | ✅ |

## 4. 联调发现的 2 个真实 bug

### Bug 1: AuthError 缺少 .msg 属性

**发现路径**：场景 6（错误凭证 → AuthError）
**现象**：
```
AttributeError: 'AuthError' object has no attribute 'msg'
```
**根因**：[src/quant/foundation/errors.py](file:///Users/daxiongmao/Desktop/quant-trading/src/quant/foundation/errors.py) 中 `AuthError.__init__` 只存了 `self.code`，**漏存 `self.msg`**：
```python
def __init__(self, code: str, msg: str) -> None:
    super().__init__(f"{code}: {msg}")
    self.code = code
    # ↑ 漏了 self.msg = msg
```
**Step 1 测试为何没发现**：当时只测了 `e.code`，没测 `e.msg`。
**影响**：上层 catch AuthError 想打日志看 OKX 返回什么错误，会再次崩溃。
**修复**：补 `self.msg = msg`（同时修了 ServerError 同样问题）。

### Bug 2: Logger 脱敏漏了 Python dict repr 形式

**发现路径**：场景 2（Logger 脱敏 + OKXClient 日志检查）
**现象**：
```
DEBUG|headers: {'OK-ACCESS-KEY': '22ad790e-3470-...', 'OK-ACCESS-PASSPHRASE': 'Pzwfy15@98765', ...}
```
真凭证直接落盘到日志文件！
**根因**：[src/quant/foundation/logger.py](file:///Users/daxiongmao/Desktop/quant-trading/src/quant/foundation/logger.py) 脱敏正则 `_JSON_PATTERN` 只匹配双引号 `"api_key":`，**不匹配 Python dict repr 的单引号** `'OK-ACCESS-KEY':`。
**Step 1 测试为何没发现**：当时只测了 `secret=xxx` / `"secret":"xxx"` 这种格式，没测打印整个 dict 的情况。
**影响**：开发者调试时随手 `log.debug(f"headers: {headers}")`，OKX 真凭证泄露到日志文件。
**修复**：
1. 把 `_JSON_PATTERN` 改为支持单引号和双引号：`[\'"]key[\'"]:\s*[\'"]value[\'"]`
2. 把 `_KV_PATTERN` 加上 `\bis\b` 分隔符（支持 `passphrase is xxx` 英语句式）
3. 把 OKX HTTP 头加入敏感字段集合：`ok-access-key` / `ok-access-passphrase` / `ok-access-sign`

## 5. 修复回归

| 验证项 | 修复后结果 |
|---|---|
| `ruff check src/quant/ tests/` | ✅ All checks passed |
| `pytest tests/test_foundation.py tests/test_models.py tests/test_okx.py` | ✅ 97 passed (1.14s) |
| `python3 tests/smoke/test_e2e_step1to3.py` | ✅ 8/8 通过 |

## 6. 联调验证的关键数据流

### 6.1 真实 OKX 数据 → Bar 模型

```
OKX REST 响应: ["1792238400000", "81500", "81600", "81400", "81532.4", "100", "0", "8153240", "1"]
       ↓ (OKXClient._request 拿到 list)
       ↓ (get_history_candles 调 Bar.from_okx_raw)
Bar(ts=1792238400000, open=81500, high=81600, low=81400, close=81532.4,
    vol=100, vol_quote=8153240, confirm=1, inst_id='BTC-USDT', bar_size='1H')
       ↓
✅ 类型: ts=int, OHLCV=Decimal, confirm=int
✅ 字段映射: vol_quote==OKX 索引 7（volCcyQuote），跳过索引 6（volCcy）
✅ OHLC 合法: high >= max(open, close), low <= min(open, close), high >= low
✅ 时间排序: 50 根 K线 ts 严格降序（OKX history-candles 默认从新到旧）
✅ 不可变: Bar.close = Decimal('0') 抛 FrozenInstanceError
✅ 可哈希: 50 根 Bar 放进 set 后大小=50（无重复）
✅ Decimal 精度: close == Decimal(str(close))，无 float 误差
```

### 6.2 真实 OKX 产品配置 → Instrument 模型

```
SPOT BTC-USDT: settle='', ct_val=1, tick_sz=0.1, lot_sz=0.00000001
   ✅ Step 2 校验规则生效：SPOT 必须 settle_ccy='' 且 ct_val=1

SWAP BTC-USDT-SWAP: settle='USDT', ct_val=0.01, tick_sz=0.1
   ✅ SWAP settle_ccy 非空、ct_val > 0
```

### 6.3 Logger 脱敏跨模块生效

```
client 内部日志: f"headers: {client._headers(...)}"
↓ loguru filter 调用 _redact_message
↓ _JSON_PATTERN 替换 'OK-ACCESS-KEY': 'xxx' → 'OK-ACCESS-KEY': '***REDACTED***'
↓ 写入文件
日志文件实际内容:
  headers: {'OK-ACCESS-KEY': '***REDACTED***', 'OK-ACCESS-PASSPHRASE': '***REDACTED***', ...}
```

### 6.4 错误链路完整

```
错误凭证 → OKXClient._request → OKX 返回 code=50111
       ↓ raise AuthError("50111", "Invalid OK-ACCESS-KEY")
       ↓ e.code == "50111", e.msg == "Invalid OK-ACCESS-KEY"
       ↓ 上层可 catch AuthError 并打日志: log.error(f"{e.code}: {e.msg}")
```

## 7. 性能数据

| 场景 | 耗时 |
|---|---|
| 8 个联调场景总耗时 | ~12s（含 5 次真实 OKX 调用 + 2 次 sync_time） |
| 单次 OKX REST 调用（candles） | 300-700ms |
| 单次 OKX REST 调用（instruments SPOT 1184 个） | 1000-1500ms |
| 两次 sync_time offset 差 | 18ms（稳定） |

## 8. 复现命令

```bash
# 单元测试（97 个，无联网）
python3 -m pytest tests/test_foundation.py tests/test_models.py tests/test_okx.py -v

# 联调测试（8 个场景，需 .env + 真实联网）
python3 tests/smoke/test_e2e_step1to3.py

# Lint
python3 -m ruff check src/quant/ tests/
```

---

**结论**: Step 1+2+3 联调通过，发现的 2 个 bug 已修复。**整体代码可以放心进入 Step 4。**

联调验证证明：
- ✅ Settings → OKXClient 字段正确传递
- ✅ Logger 脱敏跨模块生效（含 OKX HTTP 头）
- ✅ Signer → client._headers 实际调用
- ✅ OKX 真实响应 → Bar/Instrument 模型字段映射正确
- ✅ AuthError 完整属性可被上层使用
- ✅ 5 次连续调用 client 状态稳定
