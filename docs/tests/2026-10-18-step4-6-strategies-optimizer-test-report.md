# M2 Step 4-6 测试报告 — 双副本策略 + 参数寻优

> 日期：2026-10-18
> spec 对照：§5 策略双副本、§4.7 参数寻优、§14.3 真实烟测
> 上游报告：[Step 3 指标与报告测试报告](./2026-10-18-step3-metrics-report-test-report.md)

---

## 1. 产出文件

| 文件 | 说明 |
|---|---|
| `src/quant/strategies/pine/sma_cross.pine` | SMA 金叉 Pine 源 |
| `src/quant/strategies/pine/rsi.pine` | RSI Pine 源 |
| `src/quant/strategies/python/sma_cross.py` | SMA Python 副本 |
| `src/quant/strategies/python/rsi.py` | RSI Python 副本（Wilder 平滑） |
| `config/strategies/sma_cross.toml` | SMA 参数 + 风控参数 |
| `config/strategies/rsi.toml` | RSI 参数 + 风控参数 |
| `src/quant/backtest/optimizer.py` | GridOptimizer 网格寻优 |
| `tests/strategies/test_sma_cross.py` | 10 个测试 |
| `tests/strategies/test_rsi.py` | 14 个测试 |
| `tests/test_optimizer.py` | 9 个测试 |
| `tests/smoke/test_e2e_m2_backtest.py` | 真实数据回测烟测 |

---

## 2. 测试结果

| 测试集 | 结果 |
|---|---|
| `tests/strategies/` | 24 passed |
| `tests/test_optimizer.py` | 9 passed |
| 全量回归 | **420 passed** in 1.94s |
| ruff | All checks passed |
| 真实数据烟测 | **3/3 PASS** |

---

## 3. 真实数据烟测

拉取最近 30 天 BTC-USDT 1H（712 根），手续费 0.1%、滑点 5bp：

| 策略 | 成交笔数 | 总收益 | 最大回撤 |
|---|---|---|---|
| SMA Cross (10/50) | 24 | -3.21% | 10.26% |
| RSI (14, 30/70) | 7 | +3.01% | 4.45% |

两个策略均成功生成 CSV + HTML 报告，8 个指标齐全。

---

## 4. 关键对齐点

### SMA Cross

| Pine | Python |
|---|---|
| `ta.sma(close, n)` | `sum(closes[-n:]) / n` |
| `ta.crossover(fast, slow)` | `prev_fast <= prev_slow and fast > slow` |
| `strategy.entry("long")` | `side="buy"` |
| `strategy.close("long")` | `side="sell", qty=position.qty` |
| 默认 `pyramiding=0` | 仅在 `position.is_flat` 时买入 |

- 窗口需要 `slow_len + 1` 根：多出的 1 根用于计算前一组 fast/slow
- 下单量 = `cash / (price * (1 + commission_rate))`，避免手续费导致现金为负

### RSI

| Pine | Python |
|---|---|
| `ta.rsi(close, period)` | Wilder 平滑（RMA，alpha=1/period） |
| 首次 RMA 种子 | 前 period 个涨跌的简单平均 |
| `avg_loss == 0` | RSI = 100（有涨幅）/ 50（完全无波动） |
| `r < oversold` 开多 | 空仓时买入 |
| `r > overbought` 平多 | 持多时卖出全部 |

数值验证：`100,102,101,103`（period=2）→ RSI 66.67 → 85.71，与 Wilder 公式手算一致。

---

## 5. 参数寻优

- 穷举 `itertools.product` 全组合，逐组构造策略 + 回测
- 按指定指标**降序**排序（如 `sharpe` / `total_return`）
- 参数只允许标量常量（int/float/str/bool/Decimal）；callable/对象直接拒绝
- 未来函数防护：引擎只向策略暴露当前及历史 bar；参数不得为可调用对象
- `n_jobs` 保留接口，M2 串行执行

---

## 6. 已知限制

1. **双回测偏离检测未执行**：需要用户从 TradingView 导出 `trades.csv` 作为基准（spec §17.2 开放问题 1）
2. **RSI 数值精度**：Decimal 除法保留 28 位，与 Pine float64 存在微小差异（远小于 5% 阈值）
3. **寻优无并行**：`n_jobs > 1` 仍串行，M2 数据量下可接受
