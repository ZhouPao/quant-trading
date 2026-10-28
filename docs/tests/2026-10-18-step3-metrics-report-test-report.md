# M2 Step 3 测试报告 — 绩效指标 + 报告生成

> 日期：2026-10-18
> spec 对照：§4.5 绩效指标、§4.6 报告生成
> 上游报告：[Step 2 引擎测试报告](./2026-10-18-step2-backtest-engine-test-report.md)

---

## 1. 产出文件

| 文件 | 说明 |
|---|---|
| `src/quant/backtest/metrics.py` | 绩效指标 + 回撤曲线（含 FIFO 回合配对） |
| `src/quant/backtest/report.py` | CSV + HTML 报告（内联 SVG，无额外依赖） |
| `src/quant/backtest/engine.py` | `run()` 结束时回填 metrics/drawdown_curve |
| `tests/test_metrics.py` | 19 个测试 |
| `tests/test_report.py` | 10 个测试 |

---

## 2. 测试结果

| 测试集 | 结果 |
|---|---|
| `tests/test_metrics.py` | 19 passed |
| `tests/test_report.py` | 10 passed |
| 全量回归 | **387 passed** in 2.03s |
| ruff | All checks passed |

---

## 3. 指标口径

| 指标 | 口径 |
|---|---|
| `total_return` | (期末权益 - 期初权益) / 期初权益 |
| `annual_return` | `(1+r)^(1/年数) - 1`；回测不足 1 天返回 0，避免指数爆炸 |
| `sharpe` | 按 UTC 日取每日最后权益，`mean/std * sqrt(365)`；交易日 < 3 返回 0 |
| `max_drawdown` | `max(1 - equity / running_max)` |
| `win_rate` | 盈利回合数 / 总回合数 |
| `profit_factor` | 总盈利 / 总亏损（含开仓+平仓手续费）；无亏损且有盈利时为 `inf` |
| `trade_count` | 已平仓回合数 |
| `avg_holding_bars` | FIFO 配对后的平均持仓 K线数 |

### 回合配对细节

- 同方向加仓 → 合并为一个 lot，平均成本按数量加权
- 反向成交 → 按 FIFO 消耗最早 lot，计算该部分已实现盈亏
- **手续费双向计入**：开仓手续费按 lot 单位成本暂存，平仓时随数量摊销；平仓手续费按平仓占比计入
- 反手 → 平仓部分结算为一个回合，剩余数量按成交价开新仓

---

## 4. 报告设计

### CSV

统一表结构，用 `record_type` 区分两类记录：

```
record_type,ts,inst_id,side,qty,price,fee,equity,drawdown
equity,1700000000000,,,,,,10000,0
trade,1700003600000,BTC-USDT,buy,1,100,0.1,,
```

按 ts 排序，可直接用 pandas 读取做二次分析。

### HTML

单文件、无外部依赖：
- 指标表（百分比/数值分别格式化，`inf` 安全显示）
- 权益曲线 + 回撤曲线：内联 SVG 折线图（不引入 matplotlib）
- 交易明细表：ts/品种/方向/数量/价格/手续费
- 空成交时显示"无成交"占位

---

## 5. 已知限制

1. **年化口径**：不足 1 天直接返回 0；回测周期很短时年化数字本身意义有限
2. **日频夏普**：按 UTC 日聚合，日内策略的夏普会失真；后续可按 bar 周期调整
3. **报告样式**：HTML 为极简样式，满足查看需求，不做交互式图表
