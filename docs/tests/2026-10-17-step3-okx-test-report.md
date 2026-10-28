# Step 3 OKX 客户端层测试报告

**模块**: `src/quant/okx/`
**测试日期**: 2026-10-17
**Python**: 3.13.13
**pytest**: 9.1.1
**ruff**: 0.17.1
**结果**: ✅ 全部通过（首次真实联网调用 OKX REST API 成功）

---

## 1. 产出文件清单

| 文件路径 | 行数 | 内容 |
|---|---|---|
| `src/quant/okx/signer.py` | 48 | HMAC-SHA256 签名纯函数 |
| `src/quant/okx/rate_limiter.py` | 109 | 令牌桶限频器（7 个端点组） |
| `src/quant/okx/client.py` | 340 | OKXClient REST 客户端（签名/限频/重试/时间同步） |
| `src/quant/okx/__init__.py` | 13 | 模块导出 |
| `tests/test_okx.py` | 470 | 45 个单元测试 |
| `tests/smoke/test_okx.py` | 290 | 6 步真实联网烟测 |

## 2. 测试结果汇总

### 2.1 单元测试 `pytest tests/test_okx.py -v`

```
45 passed in 1.14s
```

| 测试分组 | 用例数 | 覆盖点 |
|---|---|---|
| **TestSigner** | 8 | 确定性、HMAC-SHA256 标准库交叉验证、POST+body、含 query string、方法大小写不敏感、不同输入不同签名、Base64 字符集、空 secret 稳定 |
| **TestRateLimiter** | 8 | 未知组拒绝、桶满立即拿、桶空阻塞、acquire 超时、penalize 阻塞、penalize 清空令牌且惩罚期不补充、随时间补充、不同组独立 |
| **TestOKXClientGroup** | 8 | market/public/account/trade_order/trade_batch/trade_cancel/history/unknown 8 种路径映射 |
| **TestOKXClientTimestamp** | 2 | ISO 8601 格式、时间偏差校正 |
| **TestOKXClientHeaders** | 3 | 必需头、demo 模式 x-simulated-trading、签名正确性 |
| **TestOKXClientErrorHandling** | 9 | 成功返回 data、50101 鉴权立即抛、51008 余额不足、50011 限频重试+penalize、5xx 指数退避重试、5xx 重试耗尽、网络错误重试、网络错误耗尽、未知码 ValueError |
| **TestOKXClientHighLevelMethods** | 7 | get_history_candles→Bar、空数据→[]、get_instruments→Instrument、get_funding_rate 空→None、get_funding_rate→FundingRate、sync_time 成功、sync_time 失败抛 NetworkError |

### 2.2 集成烟测 `python3 tests/smoke/test_okx.py`（真实联网）

```
== 总计: 6/6 通过 ==
```

| 步骤 | 验证项 | 结果 |
|---|---|---|
| 1 | 时间同步：调 `/api/v5/public/time`，|offset_ms|<15000ms | ✅ offset=-114ms |
| 2 | 历史 K 线：调 `/api/v5/market/history-candles` 拉 100 根 BTC-USDT 1H | ✅ 100 根，356ms，fields/types/ohlc/frozen/sorted 全通过 |
| 3 | 产品配置：调 `/api/v5/public/instruments?instType=SPOT` | ✅ 1184 个 SPOT 产品，BTC-USDT 字段完整 |
| 4 | 资金费率：调 `/api/v5/public/funding-rate?instId=BTC-USDT-SWAP` | ✅ rate=0.0001 (0.0100%)，next_funding_ts > ts |
| 5 | 限频器本地阻塞：连发 25 次，第 21 个应阻塞 ~100ms | ✅ 前 20 个 0ms，第 21 个 102ms |
| 6 | 错误处理：请求不存在的 inst_id `FAKE-FAKE-FAKE` | ✅ OKX 返回 51001，重试 3 次后抛 ServerError |

### 2.3 Lint 检查 `ruff check src/quant/ tests/`

```
All checks passed!
```

### 2.4 全量回归 `pytest tests/test_foundation.py tests/test_models.py tests/test_okx.py`

```
97 passed in 1.14s
```

Step 1 (18) + Step 2 (34) + Step 3 (45) = 97 个单元测试，无回归。

## 3. 修复过程记录

| 阶段 | 问题 | 修复 |
|---|---|---|
| 第 1 次 ruff | `client.py` 用 `timezone.utc` 不符合 UP017 | 改为 `datetime.UTC` |
| 第 1 次 ruff | `__enter__` 返回类型 `"OKXClient"` 带引号（UP037） | 去引号 |
| 第 1 次 ruff | `elapsed = "?"` 变量未使用（F841） | 删除 |
| 第 1 次 ruff | `tests/test_okx.py` import 顺序乱（I001） | `ruff --fix` 自动排序 |
| 第 1 次 ruff | 测试中 `RateLimitError` 导入未用（F401） | 删除 |
| 第 1 次 ruff | 测试中两行超 100 字符（E501） | 拆行 |
| 第 1 次 pytest | `test_penalize_clears_tokens` 失败：惩罚期内 _refill 仍在补充令牌 | 修 `_refill`：惩罚期内不补充 |
| 第 1 次烟测 | `get_instruments` 抛 `decimal.ConversionSyntax` | SPOT 响应 `ctVal=""` 是空字符串，`Decimal("")` 抛错；改用 `raw.get("ctVal") or "1"` |

## 4. 验证的 spec 章节

| spec 章节 | 内容 | 验证方式 |
|---|---|---|
| §4.1 Signer | HMAC-SHA256 + Base64 + prehash 顺序 | 8 个单元测试（含标准库交叉验证） |
| §4.2 RateLimiter | 7 个端点组令牌桶 + penalize + acquire 超时 | 8 个单元测试 + 烟测第 5 步 |
| §4.3 OKXClient | 自动签名/限频/重试/错误码分发/时间同步 | 29 个单元测试 + 烟测 6 步 |
| §9 错误码 | 50011 重试 + 2s penalize、50101 鉴权立即抛、51008 余额不足、5xx 指数退避 | 单元测试 9 个错误码分发用例 |
| §10.3 烟测场景 | 真实 OKX 调用 | 6 步全过 |

## 5. 性能数据

| 场景 | 结果 |
|---|---|
| 97 个单元测试 | 1.14s |
| 真实调 OKX `sync_time` | 30ms |
| 真实拉 100 根 BTC-USDT 1H K线 | 356ms（含网络） |
| 真实拉 1184 个 SPOT 产品 | 1043ms |
| 真实拉 BTC-USDT-SWAP 资金费率 | 302ms |
| 限频器第 21 个令牌阻塞 | 102ms（理论 100ms） |

## 6. 真实联网验证的关键发现

| 发现 | 说明 |
|---|---|
| OKX 服务端时间偏差 -114ms | 本地与服务端时间差很小，签名不会因时间偏差失败 |
| BTC-USDT 1H 第一根 K线 ts=1792238400000 | 对应 2026-10-17 12:00 UTC，是当前小时 |
| SPOT 产品共 1184 个 | OKX 上线了大量币种 |
| BTC-USDT-SWAP 资金费率 0.0100% | 年化约 11%，多空双方按费率方向支付 |
| OKX 错误码 51001 | 不存在的 inst_id；当前实现当作 5xx 重试 3 次后抛 ServerError；spec 未明确要求区分业务码与服务端码，保持当前实现 |

## 7. 已知短板（后续补）

| 短板 | 计划补的时机 |
|---|---|
| WebSocket 客户端 | spec §4.4，M1 末或 M2 实现 |
| 51001 等业务错误码应立即抛不重试 | spec 未明确，先观察实际使用是否需要 |
| mypy 严格类型检查 | M1 收尾时 |
| 真实触发 50011 验证服务端限频处理 | 难以稳定触发，单元测试已覆盖逻辑 |

## 8. 复现命令

```bash
# 单元测试
python3 -m pytest tests/test_okx.py -v

# 集成烟测（真实联网，需要 .env）
python3 tests/smoke/test_okx.py

# Lint
python3 -m ruff check src/quant/okx/ tests/test_okx.py tests/smoke/test_okx.py

# 全量回归
python3 -m pytest tests/test_foundation.py tests/test_models.py tests/test_okx.py -v
```

---

**结论**: Step 3 OKX 客户端层完成，真正能联网拉数据，可进入 Step 4 `storage/`（parquet 持久化）。
