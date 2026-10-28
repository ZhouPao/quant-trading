# Step 7 烟测报告 — CLI 端到端真实拉取

> 日期：2026-10-17
> spec 对照：§11 CLI 命令 + §13 Step 11 烟测
> 上一份报告：[Step 6 CLI 测试报告](./2026-10-17-step6-cli-test-report.md)

---

## 1. 产出文件清单

| 文件 | 行数 | 说明 |
|---|---|---|
| `config/config.toml` | 36 | 运行时配置文件（从 example 复制，引用 .env 凭证） |
| `tests/smoke/test_e2e_step6_cli.py` | ~290 | 5 场景端到端烟测脚本 |
| `src/quant/cli/validate_data.py` | 修复 | 年份→时间戳转换 bug 修复 |

---

## 2. 测试结果汇总

| 测试集 | 结果 |
|---|---|
| 单元测试 `tests/` | **309 passed** in 1.59s |
| ruff 代码风格 | **All checks passed** |
| CLI 端到端烟测 | **5/5 PASS** |

---

## 3. 烟测场景详情

### 场景 1：CLI fetch-candles 拉取 7 天 BTC-USDT 1H

**命令**：
```bash
quant fetch-candles --inst BTC-USDT --bar 1H --start 2026-10-10 --end 2026-10-17
```

**结果**：PASS
- exit=0
- 拉取 169 根 K线（落盘），读回 155 根（范围过滤）
- 断点 JSON 生成，验证 0 错误

### 场景 2：CLI fetch-candles 断点续传

**命令**：
```bash
# 第一次拉取 3 天
quant fetch-candles --inst BTC-USDT --bar 1H --start 2026-10-14 --end 2026-10-17
# 第二次调同范围
quant fetch-candles --inst BTC-USDT --bar 1H --start 2026-10-14 --end 2026-10-17
```

**结果**：PASS
- exit1=0, exit2=0
- bars1=59, bars2=59（第二次净增 0 根，断点跳过）
- 日志：`cursor > end_ts, skip main loop, only validate`

### 场景 3：CLI validate-data 校验数据

**命令**：
```bash
quant validate-data --inst BTC-USDT --bar 1H  # 全量校验
```

**结果**：PASS
- exit=0
- ValidationReport: errors=0, duplicates=0, missing=0, invalid_ohlc=0

### 场景 4：CLI align-data 多品种对齐

**命令**：
```bash
quant align-data --insts BTC-USDT,ETH-USDT --bar 1H --fill ffill \
  --start 2026-10-10 --end 2026-10-17 --output aligned.parquet
```

**结果**：PASS
- exit=0
- 对齐完成：169 行 x 10 列
- output_exists=True

### 场景 5：CLI 全链路 fetch → validate → align

**命令**：串联调用三个子命令
```bash
quant fetch-candles --inst BTC-USDT --bar 1H --start ... --end ...
quant fetch-candles --inst ETH-USDT --bar 1H --start ... --end ...
quant validate-data --inst BTC-USDT --bar 1H --start ... --end ...
quant align-data --insts BTC-USDT,ETH-USDT --bar 1H --fill ffill --output aligned_full.parquet
```

**结果**：PASS
- validate=0, align=0
- BTC: 73 根, ETH: 73 根
- aligned_full.parquet 生成成功

---

## 4. 修复的 Bug

### Bug：validate-data 年份→时间戳转换错误

**现象**：场景 3 第一次运行 FAIL，日志显示 `加载到 0 根 K线`。

**根因**：`validate_data.py` 的"全量校验"分支用 `int(years[0] * 1e9)` 把年份转 ms，但这是错误的数学近似：
- `2026 * 1e9 = 2,026,000,000,000` → 对应 2034 年的时间戳
- 正确的 2026-01-01 UTC ms = `1,767,220,800,000`

**修复**：改用 `datetime(year, 1, 1, tzinfo=UTC).timestamp() * 1000` 精确转换。

**验证**：修复后场景 3 PASS，309 个单元测试无回归。

---

## 5. spec 对照表

| spec 条目 | 烟测验证 | 状态 |
|---|---|---|
| §11.1 fetch-candles 命令 | 场景 1 + 2 | PASS |
| §11.2 validate-data 命令 | 场景 3 + 5 | PASS |
| §11.3 align-data 命令 | 场景 4 + 5 | PASS |
| §13 Step 11 端到端烟测 | 全部 5 场景 | PASS |
| 断点续传（§5.5） | 场景 2 | PASS |
| 数据校验（§7） | 场景 3 + 5 | PASS |
| 多品种对齐（§8） | 场景 4 + 5 | PASS |

---

## 6. M1 里程碑完成状态

| Step | 内容 | 状态 |
|---|---|---|
| 1 | Foundation（配置/日志/错误/类型） | 完成 |
| 2 | Models（Bar/Instrument/FundingRate/Tick） | 完成 |
| 3 | OKX 客户端层（signer/rate_limiter/client） | 完成 |
| 4 | Storage（bar_storage/validator/aligner/instrument_storage） | 完成 |
| 5 | Fetcher（candle/instrument/funding_rate/checkpoint） | 完成 |
| 6 | CLI（fetch_candles/validate_data/align_data） | 完成 |
| 7 | 真实烟测 | **完成** |

**M1 里程碑：数据底座全部完成。** 下一步进入 M2（回测引擎 + Webhook 框架）。

---

## 7. 已知限制

1. **Parquet Decimal 存储**：当前 Decimal 以 string 存储，体积偏大但 M1 数据量可接受
2. **网络依赖**：烟测需联网调用真实 OKX API，CI 环境需跳过
3. **模拟盘 vs 实盘**：当前 config.toml 设 `demo=false`（拉历史数据不需要模拟盘），M3 实盘时改回 true
