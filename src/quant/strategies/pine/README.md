# Pine Script 源码目录

这里放 TradingView Pine **基线策略副本**（git 管理），用途是独立验证
`src/quant/strategies/python/` 里最基础的信号逻辑没有移植错。

## 基准口径（2026-11-04 决策）

生产执行与回测以 **Python + `config/strategies/*.toml` 为唯一基准**。
本目录的 `.pine` **不镜像生产参数，也不要求与 Python 逻辑逐行一致**：
Python 已扩展 EMA/ATR 趋势过滤、支撑压力止盈止损、手续费预留，Pine 未复刻；
参数默认值也不同（如 SMA 慢线 50 vs 生产 100）。

因此：

- 每个 `.pine` 文件头必须声明「基线策略副本」定位与已知差异；
- 参数名/默认值差异由 `tests/strategies/test_pine_python_consistency.py`
  **显式登记**，任何新增漂移都会让测试失败；
- Pine 偏离门禁只对「做过交叉验证的参数集」生效。没做过对比不等于对比
  通过，看板会显式提示「门禁未覆盖」，不能把 fail-open 当成安全。
