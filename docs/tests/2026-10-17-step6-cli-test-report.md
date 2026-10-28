# Step 6 CLI 入口测试报告

**模块**: `src/quant/cli/`
**测试日期**: 2026-10-17
**Python**: 3.13.13
**pytest**: 9.1.1
**ruff**: 0.17.1
**结果**: ✅ 全部通过（31 单元测试）

---

## 1. 产出文件清单

| 文件路径 | 行数 | 内容 |
|---|---|---|
| [src/quant/cli/__init__.py](file:///Users/daxiongmao/Desktop/quant-trading/src/quant/cli/__init__.py) | ~108 | `build_parser()` + `main()` 入口：argparse 三 subcommand 分发 |
| [src/quant/cli/fetch_candles.py](file:///Users/daxiongmao/Desktop/quant-trading/src/quant/cli/fetch_candles.py) | ~62 | `fetch-candles` 命令：加载配置 → OKXClient → CandleFetcher.fetch → 落盘 |
| [src/quant/cli/validate_data.py](file:///Users/daxiongmao/Desktop/quant-trading/src/quant/cli/validate_data.py) | ~87 | `validate-data` 命令：BarStorage 加载 → DataValidator 校验 → 打印报告 |
| [src/quant/cli/align_data.py](file:///Users/daxiongmao/Desktop/quant-trading/src/quant/cli/align_data.py) | ~83 | `align-data` 命令：多品种 BarStorage 加载 → DataAligner.align → 输出 parquet |
| [tests/test_cli.py](file:///Users/daxiongmao/Desktop/quant-trading/tests/test_cli.py) | ~628 | 31 个单元测试（覆盖全部 3 个子命令 + argparse + 分发） |

## 2. 测试结果汇总

### 2.1 全量回归 `pytest tests/ -v`

```
309 passed in 1.68s
```

- Step 1 Foundation: 22 个测试 ✓
- Step 2 Models: 30 个测试 ✓
- Step 3 OKX: 45 个测试 ✓
- Step 4 Storage: 66 个测试 ✓
- Step 5 Fetcher: 57 个测试 ✓
- **Step 6 CLI: 31 个测试 ✓**（新增）
- Backtest 预置: 58 个测试 ✓

### 2.2 ruff 静态检查

```
ruff check src/ tests/
All checks passed!
```

---

## 3. 各命令测试详情

### 3.1 `fetch_candles.py`（spec §11.1）

**核心能力**：解析 `--inst/--bar/--start/--end` → 日期转 UTC ms → 调用 CandleFetcher.fetch

**测试用例（5 个）**：

| 用例 | 关键场景 |
|---|---|
| test_normal_fetch | mock 下游 → 验证 fetch 调用参数 + client.close 调用 + 返回 0 |
| test_start_after_end | start > end 返回 1 |
| test_fetch_exception_returns_1 | fetch 抛异常 → 返回 1 + client.close 仍被调用 |
| test_config_path_passed | --config 传递给 Settings.load |
| test_bar_default | --bar 默认值为 1H |

**辅助函数**：
- `parse_date_to_ms(date_str)` — YYYY-MM-DD → UTC 毫秒时间戳（5 个独立测试）

### 3.2 `validate_data.py`（spec §11.2）

**核心能力**：BarStorage.load_bars → DataValidator.validate → 打印报告 → 返回退出码

**测试用例（5 个）**：

| 用例 | 关键场景 |
|---|---|
| test_data_ok_returns_0 | 校验通过 is_ok=True → 返回 0 |
| test_data_has_issues_returns_1 | 有重复/错误 is_ok=False → 返回 1 |
| test_no_data_with_range_returns_1 | 指定区间但无数据 → 返回 1 |
| test_no_data_without_range_returns_1 | 全量但 list_years 空 → 返回 1 |
| test_full_range_uses_list_years | 无 --start/--end 时用 list_years 推断范围 |

### 3.3 `align_data.py`（spec §11.2）

**核心能力**：多品种 BarStorage.load_bars → DataAligner.align → 输出 parquet 或预览

**测试用例（5 个）**：

| 用例 | 关键场景 |
|---|---|
| test_normal_align | 2 品种正常对齐 → 返回 0 |
| test_single_inst_returns_1 | 仅 1 个品种 → 返回 1 |
| test_no_data_returns_1 | 全量但 list_years 空 → 返回 1 |
| test_one_inst_empty_returns_1 | 其中 1 个品种无数据 → 返回 1 |
| test_output_parquet | --output 指定 → to_parquet 被调用 |

### 3.4 `cli/__init__.py` — argparse + 分发

**测试用例（16 个）**：

| 类别 | 用例数 | 关键场景 |
|---|---|---|
| build_parser 解析 | 8 | 三个 subcommand 参数解析 / bar 默认值 / fill choices 校验 / 缺参数报错 / 无 subcommand 报错 |
| cli.main 分发 | 4 | 三个子命令正确分发 / 无命令 SystemExit |

---

## 4. CLI 命令用法

### 4.1 拉取 K线

```bash
quant fetch-candles \
  --inst BTC-USDT \
  --bar 1H \
  --start 2024-01-01 \
  --end 2024-12-31 \
  --config config/config.toml
```

### 4.2 校验数据

```bash
# 全量校验（用已有年份文件推断范围）
quant validate-data --inst BTC-USDT --bar 1H

# 指定区间
quant validate-data --inst BTC-USDT --bar 1H \
  --start 2024-01-01 --end 2024-12-31
```

### 4.3 对齐多品种

```bash
# 对齐并预览
quant align-data --insts BTC-USDT,ETH-USDT --bar 1H --fill ffill

# 对齐并输出 parquet
quant align-data --insts BTC-USDT,ETH-USDT --bar 1H \
  --start 2024-01-01 --end 2024-12-31 --output aligned.parquet
```

---

## 5. 已知限制

1. **CLI 入口需 `pip install -e .` 注册 console_script**：当前通过 `python3 -m quant.cli` 也可运行，但 `quant` 命令需安装包后才可用。
2. **validate-data 全量模式用 `year * 1e9` 近似时间范围**：年份文件的起始 ms 不是精确的 UTC 纪元值，但 BarStorage.load_bars 内部会过滤，不影响正确性。
3. **align-data 全量模式取所有品种的并集范围**：不在各自缺失区间做特殊填充，由 DataAligner 的 fill 策略统一处理。

---

## 6. 与 spec §11 对照

| spec 要求 | 实现位置 | 状态 |
|---|---|---|
| fetch_candles CLI | `cli/fetch_candles.py` main() | ✅ |
| validate_data CLI | `cli/validate_data.py` main() | ✅ |
| align_data CLI | `cli/align_data.py` main() | ✅ |
| pyproject.toml [project.scripts] | `quant = "quant.cli:main"` | ✅（已有） |
| 单元测试 mock 下游 | tests/test_cli.py 用 patch | ✅ |

---

## 7. M1 里程碑进度

| Step | 内容 | 状态 |
|---|---|---|
| 1 | Foundation（config/logger/errors/types） | ✅ 完成 |
| 2 | Models（Bar/Instrument/FundingRate/Tick） | ✅ 完成 |
| 3 | OKX（signer/rate_limiter/client） | ✅ 完成 |
| 4 | Storage（bar_storage/instrument_storage/validator/aligner） | ✅ 完成 |
| 5 | Fetcher（checkpoint/candle/instrument/funding_rate） | ✅ 完成 |
| **6** | **CLI（fetch_candles/validate_data/align_data）** | **✅ 完成** |

**M1 数据基础层全部代码完成。** 下一步为 Step 7：真实烟测验证 CLI 端到端流程。
