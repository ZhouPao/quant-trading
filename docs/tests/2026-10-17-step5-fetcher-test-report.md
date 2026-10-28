# Step 5 Fetcher 层测试报告

**模块**: `src/quant/fetcher/`
**测试日期**: 2026-10-17
**Python**: 3.13.13
**pytest**: 9.1.1
**ruff**: 0.17.1
**结果**: ✅ 全部通过（39 单元测试 + 5 端到端烟测）

---

## 1. 产出文件清单

| 文件路径 | 行数 | 内容 |
|---|---|---|
| [src/quant/fetcher/checkpoint.py](file:///Users/daxiongmao/Desktop/quant-trading/src/quant/fetcher/checkpoint.py) | ~85 | Checkpoint dataclass：JSON 持久化 + 原子写 + 容错 load_or_default |
| [src/quant/fetcher/candle_fetcher.py](file:///Users/daxiongmao/Desktop/quant-trading/src/quant/fetcher/candle_fetcher.py) | ~270 | CandleFetcher：断点续传 + 翻页 + 缺失补拉（max_attempts 防无限循环） |
| [src/quant/fetcher/instrument_fetcher.py](file:///Users/daxiongmao/Desktop/quant-trading/src/quant/fetcher/instrument_fetcher.py) | ~70 | InstrumentFetcher：拉 OKX 产品配置全量覆盖落盘 |
| [src/quant/fetcher/funding_rate_fetcher.py](file:///Users/daxiongmao/Desktop/quant-trading/src/quant/fetcher/funding_rate_fetcher.py) | ~115 | FundingRateFetcher：拉最近资金费率 + 按 ts 去重追加 |
| [src/quant/fetcher/__init__.py](file:///Users/daxiongmao/Desktop/quant-trading/src/quant/fetcher/__init__.py) | 1 | 模块说明 |
| [tests/test_fetcher.py](file:///Users/daxiongmao/Desktop/quant-trading/tests/test_fetcher.py) | ~280 | Checkpoint 18 个单元测试 |
| [tests/test_candle_fetcher.py](file:///Users/daxiongmao/Desktop/quant-trading/tests/test_candle_fetcher.py) | ~370 | CandleFetcher 17 个单元测试 |
| [tests/test_instrument_fetcher.py](file:///Users/daxiongmao/Desktop/quant-trading/tests/test_instrument_fetcher.py) | ~205 | InstrumentFetcher 11 个单元测试 |
| [tests/test_funding_rate_fetcher.py](file:///Users/daxiongmao/Desktop/quant-trading/tests/test_funding_rate_fetcher.py) | ~190 | FundingRateFetcher 11 个单元测试 |
| [tests/smoke/test_e2e_step5.py](file:///Users/daxiongmao/Desktop/quant-trading/tests/smoke/test_e2e_step5.py) | ~270 | 5 个端到端烟测（真实联网拉取） |

## 2. 测试结果汇总

### 2.1 全量回归 `pytest tests/ -v`

```
220 passed in 1.77s
```

- Step 1 Foundation: 22 个测试 ✓
- Step 2 Models: 30 个测试 ✓
- Step 3 OKX: 45 个测试 ✓
- Step 4 Storage: 66 个测试 ✓
- **Step 5 Fetcher: 57 个测试 ✓**（新增 = 18+17+11+11）

### 2.2 ruff 静态检查

```
ruff check src/ tests/
All checks passed!
```

### 2.3 端到端烟测 `python3 tests/smoke/test_e2e_step5.py`

```
== 总计: 5/5 通过 ==
```

---

## 3. 各组件测试详情

### 3.1 Checkpoint（src/quant/fetcher/checkpoint.py）

**核心能力**：dataclass + JSON 持久化 + 原子写

**测试用例（18 个）**：

| 类别 | 用例数 | 关键场景 |
|---|---|---|
| 序列化 roundtrip | 3 | to_json → from_json / save → load |
| 字段校验 | 4 | 缺 last_ts 抛 KeyError / updated_at 可空 |
| 文件持久化 | 3 | save 后 load 一致 / 跨实例读 |
| 容错 load_or_default | 5 | corrupt JSON / 不存在 / 缺字段 → 默认值 |
| 原子写 | 3 | tmp 文件不残留 / 写失败回退 / 重命名成功 |

### 3.2 CandleFetcher（src/quant/fetcher/candle_fetcher.py）

**核心能力**：断点续传 + 自动翻页 + 缺失补拉

**关键设计**：
- OKX `history-candles` 端点 `after` 参数语义：返回 ts < after 的更旧数据
- 翻页策略：第一页 `after=end_ts+1` 拿最新 page_size 根，后续 `after=oldest` 推进向旧
- 停止条件：返回空 / `oldest <= cursor`（覆盖到起点）/ `len(bars) < page_size`（无更多数据）
- 断点续传：读 ckpt，`cursor = max(start_ts, ckpt.last_ts + bar_size_ms)`
- 断点已超 end_ts 时直接跳过主循环，仅做校验
- 补拉循环：用 `DataValidator` 找 missing_ranges，最多 `max_attempts=3` 次防无限循环

**测试用例（17 个）**：

| 类别 | 用例数 | 关键场景 |
|---|---|---|
| 首次拉取 | 3 | 单页 / 空区间 / OKX 返回空停止 |
| 翻页 | 3 | 多页翻完 / 不足 page_size 停止 / 越界数据过滤 |
| 断点续传 | 2 | 从 last_ts+1bar 续拉 / cursor > end_ts 跳过 |
| 缺失补拉 | 4 | 无缺失通过 / 有 gap 补拉 / max_attempts 防死循环 / 真实 gap 停止 |
| 边界 | 5 | 未知 bar_size / 负 ts 抛 ValueError / 断点跨实例 / 不同 inst 分文件 / 不同 bar 分文件 |

**修复的 bug**：
1. **OKX 参数语义搞反**：原代码用 `before=oldest-1` 向旧方向翻页，但 OKX `before=N` 返回的是 ts > N 的数据（更新方向），导致每次返回同一批最新数据。改用 `after=oldest` 正确向旧方向翻页。
2. **断点已超 end_ts 时仍走主循环**：增加 `cursor > end_ts` 提前返回逻辑。
3. **缺负 ts 校验**：在 fetch 入口加 `start_ts/end_ts >= 0` 校验。

### 3.3 InstrumentFetcher（src/quant/fetcher/instrument_fetcher.py）

**核心能力**：全量覆盖落盘 + 过滤非 live 状态

**测试用例（11 个）**：

| 类别 | 用例数 | 关键场景 |
|---|---|---|
| 主流程 | 7 | 返回条数 / 非 live 过滤 / 空列表 / 落盘可读 / 全量覆盖 / SWAP / 按 inst_id 排序 |
| 边界 | 4 | 未知 inst_type 抛 ValueError / 未拉取 load 空 / 反序列化为真实对象 / 不同 inst 分文件 |

### 3.4 FundingRateFetcher（src/quant/fetcher/funding_rate_fetcher.py）

**核心能力**：拉最近一条资金费率 + 按 ts 去重追加 + 原子写

**简化说明**：OKX `/api/v5/public/funding-rate` 端点仅返回最近一条结算记录，无翻页支持。设计简化为：拉取 → 与已落盘数据按 ts 去重 → 追加写回（按 ts 升序）。spec §5.7 描述"模式同 CandleFetcher"在 M1 阶段做了简化处理。

**测试用例（11 个）**：

| 类别 | 用例数 | 关键场景 |
|---|---|---|
| 主流程 | 6 | 新记录返回 1 / 落盘 / OKX 返回 None / 同 ts 去重 / 不同 ts 追加排序 / 负费率反序列化 |
| 边界 | 5 | 空 inst_id 抛 ValueError / 未拉取 load 空 / 不同 inst 分文件 / 跨实例持久化 / 反序列化为真实对象 |

---

## 4. 端到端烟测详情（真实 OKX 拉取）

### 4.1 场景 1: CandleFetcher 首次拉 30 天 1H

- **操作**：拉近 30 天 BTC-USDT 1H 数据
- **结果**：拉取 720 根，断点文件生成，落盘数据可读回，无缺口（gaps=0）
- **状态**：✅ PASS

### 4.2 场景 2: CandleFetcher 断点续传

- **操作**：拉近 3 天 BTC-USDT 1H 数据，第二次调同范围
- **结果**：第一次拉 72 根，第二次跳过主循环返回 0（cursor > end_ts）
- **状态**：✅ PASS

### 4.3 场景 3: InstrumentFetcher 拉 SPOT

- **操作**：拉 OKX SPOT 全量产品
- **结果**：拉取 1184 个 live 产品，文件落盘可读回，包含 BTC-USDT
- **状态**：✅ PASS

### 4.4 场景 4: FundingRateFetcher 拉 BTC-USDT-SWAP

- **操作**：拉 BTC-USDT-SWAP 最近一条资金费率，第二次调用验证去重
- **结果**：第一次拉到 1 条（ts=1792252800000, rate=0.0001），第二次去重返回 0
- **状态**：✅ PASS

### 4.5 场景 5: Step 1-5 完整链路

- **操作**：foundation → models → okx → storage → fetcher 全链路串联
- **结果**：拉 7 天 1H 数据 168 根 + SPOT 1184 个产品，fetcher 落盘后可被 storage 读回，字段一致
- **状态**：✅ PASS

---

## 5. 已知限制

1. **FundingRateFetcher 仅支持最近一条**：OKX `/api/v5/public/funding-rate` 不支持翻页，无法拉历史资金费率序列。若需要历史资金费率，应使用 `/api/v5/public/funding-rate-history` 端点（M1 范围未纳入）。
2. **InstrumentFetcher 不分页**：依赖 OKXClient.get_instruments 一次拉取全部。OKX 实际可能分页返回，但 SPOT/SWAP 实测可一次拉完。
3. **CandleFetcher 翻页停止依赖 len(bars) < page_size**：如果 OKX 在某个边界返回正好 page_size 根但已无更旧数据，会多调一次 API 才停止（性能损耗可接受）。

---

## 6. 与 spec §5 对照

| spec 要求 | 实现位置 | 状态 |
|---|---|---|
| Checkpoint 断点持久化 | `checkpoint.py` Checkpoint dataclass | ✅ |
| CandleFetcher 断点续传 | `candle_fetcher.py` fetch + _load_checkpoint | ✅ |
| CandleFetcher 自动翻页 | `candle_fetcher.py` 主循环用 after 推进 | ✅ |
| CandleFetcher 缺失补拉 | `candle_fetcher.py` _validate_and_fill | ✅ |
| InstrumentFetcher 全量覆盖 | `instrument_fetcher.py` fetch | ✅ |
| FundingRateFetcher 拉资金费率 | `funding_rate_fetcher.py` fetch（简化版） | ✅ |
| 单元测试 mock OKXClient | tests/test_*.py 用 MagicMock | ✅ |
| 端到端烟测真实拉取 | tests/smoke/test_e2e_step5.py | ✅ |

---

## 7. M1 里程碑进度

| Step | 内容 | 状态 |
|---|---|---|
| 1 | Foundation（config/logger/errors/types） | ✅ 完成 |
| 2 | Models（Bar/Instrument/FundingRate/Tick） | ✅ 完成 |
| 3 | OKX（signer/rate_limiter/client） | ✅ 完成 |
| 4 | Storage（bar_storage/instrument_storage/validator/aligner） | ✅ 完成 |
| **5** | **Fetcher（checkpoint/candle/instrument/funding_rate）** | **✅ 完成** |

**M1 数据基础层全部完成。** 下一步可推进 M2（数据采集 → 信号计算 → 回测引擎）。
