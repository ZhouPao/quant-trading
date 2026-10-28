# M2 Step 2 测试报告 — Backtest 引擎 + Matcher

> 日期：2026-10-18
> spec 对照：§4.3 事件驱动主循环、§4.4 成交模拟
> 上游报告：[Step 1 数据模型测试报告](./2026-10-18-step1-backtest-models-test-report.md)

---

## 1. 产出文件

| 文件 | 说明 |
|---|---|
| `src/quant/backtest/matcher.py` | Matcher 成交模拟器 |
| `src/quant/backtest/engine.py` | Backtest 事件驱动引擎 + BacktestResult |
| `src/quant/foundation/errors.py` | 新增 `NoFillError` |
| `src/quant/backtest/__init__.py` | 导出 Matcher/Backtest/BacktestResult |
| `tests/test_matcher.py` | 17 个测试 |
| `tests/test_engine.py` | 20 个测试 |

---

## 2. 测试结果

| 测试集 | 结果 |
|---|---|
| `tests/test_matcher.py` | 17 passed |
| `tests/test_engine.py` | 20 passed |
| 全量回归 | **360 passed** in 1.95s |
| ruff | All checks passed |

---

## 3. 关键设计决策

### 3.1 成交时序：下一根 bar 开盘执行

初版实现中，信号在产生的 bar 内立即成交，导致权益曲线在信号 bar 上就反映了持仓——等价于使用该 bar 收盘后的信息做估值，存在轻微"时间旅行"。

修正后：
1. 引擎维护 `pending` 信号
2. 下一根 bar 迭代**开始时**先执行挂起信号（按该 bar 开盘价成交）
3. 再更新窗口、回调 `on_bar`、记录权益

这样 `equity_curve[i]` 只包含截至 bar i 收盘的已知信息，且策略在 `on_bar` 中看到的持仓/现金已反映前一根信号的成交。

### 3.2 限价单成交价

按 spec §4.4 简化为"穿透即按限价成交"（买入 `low <= limit`，卖出 `high >= limit`）。不模拟开盘跳空改善价格，保证与 Pine 回测口径一致。

### 3.3 滑点参数语义

`Matcher.slippage_rate` 是**比率**（0.0005 = 5bp = 0.05%），买方向乘 `(1 + rate)`，卖方向乘 `(1 - rate)`。配置中的 `slippage_bps=5` 由上层换算为 `Decimal("5")/10000`。

---

## 4. 测试覆盖矩阵

| 模块 | 覆盖点 |
|---|---|
| Matcher 构造 | 默认值、负滑点、滑点≥1、负手续费 |
| 市价单 | 下一根开盘成交、买/卖滑点、零手续费、fee 精度 |
| 限价单 | 买/卖穿透、边界（low/high 恰等于限价）、未穿透 NoFillError |
| 一致性 | signal/bar 品种不一致 |
| 引擎输入 | 空 bars、乱序、重复 ts、多品种、非法 cash/window |
| 生命周期 | on_init/on_bar/on_trade/on_finish、params 传递、窗口裁剪 |
| 成交 | 下一根开盘、买入/卖出、手续费、已实现盈亏、空头开仓 |
| unfilled | 最后一根信号、限价未穿透 |
| 权益曲线 | 长度=bar 数、空仓=cash、持仓按收盘价估值 |

---

## 5. 已知限制

1. **单品种回测**：引擎拒绝多品种混合输入；跨品种回测留后续里程碑
2. **无部分成交/滑点模型**：限价单只有全成/不成两种结果
3. **drawdown_curve / metrics 为空**：Step 3 由 metrics 模块填充
