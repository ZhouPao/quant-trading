# 研究用 Notebook 模板（M3-7）

这个目录只放**离线研究**模板。原则：**notebook 不直连 SQLite / Redis / OKX**，
只消费 `quant research export` 导出的目录——研究代码出问题不会影响实盘进程。

## 1. 先导出数据

```bash
quant research export \
  --inst BTC-USDT --bar 1H \
  --start 2026-09-01 --end 2026-09-10 \
  --out research/BTC-USDT-1H

# 想连回测成交与指标一起看：
quant research export ... --run-id <run_id> --out research/btc-run
```

导出目录结构：

```
research/BTC-USDT-1H/
├── bars.parquet      # K 线（数值是 Decimal 文本，读取后需转数值）
├── trades.parquet    # 成交（--run-id 时来自回测产物，否则来自本地 fills）
├── backtest.json     # 仅 --run-id：参数/指标/区间
└── manifest.json     # 导出时间、条数、区间、run_id（校验数据版本）
```

## 2. 启动

```bash
# 若尚未安装 jupyter：
uv pip install jupyterlab

uv run jupyter lab notebooks/
```

## 3. 模板

`explore.ipynb` 是零额外依赖的模板（只用 pandas）：

1. 设置 `DATA_DIR`（默认 `research/BTC-USDT-1H`，可用环境变量覆盖）
2. 读取 `bars.parquet`，检查行数、时间范围、缺失
3. 读取 `trades.parquet`，按方向统计笔数/数量/手续费/名义
4. 读取 `backtest.json`（存在时），打印参数与指标

环境变量覆盖：

```bash
QUANT_RESEARCH_DIR=research/btc-run uv run jupyter lab notebooks/
```

## 4. 注意事项

- **金额/价格是字符串**：导出时保留 Decimal 文本（避免浮点误差），
  读取后需要 `pd.to_numeric(..., errors="coerce")`；不要在导出前转 float。
- **区间是左闭右开**：`--start` 当天 00:00 到 `--end` 次日 00:00。
- **画图**：模板默认不画图（避免依赖 matplotlib）。需要时自行
  `uv pip install matplotlib`，然后
  `bars.set_index("ts")["close"].plot()`。
- **可复现**：`manifest.json` 里的 `exported_at`/`run_id` 是数据版本锚点，
  写研究结论时建议一并记录。
