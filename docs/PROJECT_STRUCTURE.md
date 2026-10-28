# 项目结构说明

> 本文档解释 `quant-trading` 项目每个目录与关键文件的作用。
> 创建日期：2026-10-17
> 对应设计文档：[PRD v0.5](requirements/2026-10-17-quant-trading-platform-requirements.md) §6、[M1 spec v1.1](specs/2026-10-17-m1-data-foundation-design.md) §3

---

## 顶层目录树

```
quant-trading/
├── docs/                      # 文档
├── config/                    # 配置（不入 git）
├── src/                       # 源码
│   └── quant/
├── tests/                     # 测试
├── notebooks/                 # 策略研究笔记本
├── data/                      # 历史数据（不入 git）
├── logs/                      # 运行日志（不入 git）
├── pyproject.toml             # Python 包定义
├── .env.example               # 环境变量模板
├── .gitignore                 # git 忽略规则
└── README.md                  # 项目说明
```

---

## 各目录详细说明

### 1. `docs/` — 文档目录

存放所有设计与参考文档。**入 git**。

| 子目录 | 用途 |
|---|---|
| `docs/requirements/` | 需求文档（PRD），定义"做什么" |
| `docs/specs/` | 详细设计文档（spec），定义"怎么做" |
| `docs/reference/` | 外部 API 速查等参考材料（如 OKX API） |

**关键文件**：
- `docs/requirements/2026-10-17-quant-trading-platform-requirements.md` — PRD v0.5
- `docs/specs/2026-10-17-m1-data-foundation-design.md` — M1 详设 v1.1
- `docs/reference/OKX_API_REFERENCE.md` — OKX V5 API 速查

---

### 2. `config/` — 配置目录

存放运行时配置。**不入 git**（含敏感凭证）。

| 文件/子目录 | 用途 |
|---|---|
| `config/config.toml` | 全局配置：OKX 凭证、存储路径、限频、日志、Redis、webhook |
| `config/config.toml.example` | 配置模板（入 git），复制为 `config.toml` 后填真实值 |
| `config/strategies/` | 各策略风控参数（按 `strategy_id` 命名 toml 文件，如 `sma_cross.toml`） |

**设计依据**：spec §7。配置加载语法 `${ENV_VAR}` 引用环境变量；`${ENV_VAR:-default}` 提供默认值。

---

### 3. `src/quant/` — 源码主包

所有 Python 源码。采用 `src/` 布局避免导入歧义。`pip install -e .` 后通过 `import quant.xxx` 访问。

#### 3.1 `foundation/` — 基础设施

**职责**：配置加载、日志、错误定义、通用类型。**M1 范围**（spec §13 第1步）。

| 文件 | 用途 |
|---|---|
| `config.py` | `Settings` 类，基于 pydantic-settings，加载 `config.toml` + `.env` |
| `logger.py` | 日志器配置，含脱敏处理器（API Key 自动打码） |
| `errors.py` | 自定义异常类（`OKXError`/`RateLimitError`/`StorageError` 等） |
| `types.py` | 通用类型别名（`Timestamp`/`Symbol`/`UTC` 等） |

**下游依赖**：所有其他模块都依赖 foundation。

#### 3.2 `models/` — 数据模型

**职责**：定义不可变数据类型。**M1 范围**（spec §13 第2步）。

| 类 | 用途 | 消费方 |
|---|---|---|
| `Bar` | K线（OHLCV + 时间戳），frozen dataclass + Decimal | M2 回测、M3/M5 策略 |
| `Tick` | 实时行情快照 | M3/M5 实时策略 |
| `Instrument` | 产品配置（instId/tickSz/lotSz/minSz） | M3/M5 下单参数校验 |
| `FundingRate` | 资金费率 | 策略与监控 |

**设计依据**：spec §5。用 `frozen=True` + `Decimal` 保证精度与不可变。

#### 3.3 `okx/` — OKX 客户端

**职责**：封装 OKX V5 API。**M1 范围**（spec §13 第3-4步）。

| 文件 | 用途 |
|---|---|
| `client.py` | `OKXClient`：REST 客户端，自动签名、限频、重试、时间同步 |
| `signer.py` | HMAC-SHA256 签名实现（base64+secret+timestamp+method+path+body） |
| `rate_limiter.py` | `RateLimiter`：令牌桶，按端点组分桶，命中 50011 强制阻塞 2 秒 |
| `ws.py` | `WSClient`：WebSocket 客户端，自动重连、心跳 |
| `endpoints.py` | 端点常量与限频配置表 |

**设计依据**：spec §4。`OKXClient` 是 M3/M5 所有 API 调用的入口。

#### 3.4 `storage/` — 数据存储

**职责**：本地数据持久化与校验。**M1 范围**（spec §13 第5-6步）。

| 文件 | 用途 |
|---|---|
| `bar_storage.py` | `BarStorage`：parquet 读写，按 UTC 年份分文件，自动去重 |
| `instrument_storage.py` | 产品配置 JSON 存取 |
| `sqlite_store.py` | SQLite 状态持久化（M2 起用于信号/订单/持仓） |
| `validator.py` | `DataValidator`：K线连续性、重复、OHLC 合法性校验 |
| `aligner.py` | `DataAligner`：多品种 K线按统一时间轴对齐 |

**设计依据**：spec §6。

#### 3.5 `fetcher/` — 数据下载编排

**职责**：业务编排层，串联 OKX 客户端与存储。**M1 范围**（spec §13 第7步）。

| 类 | 用途 |
|---|---|
| `CandleFetcher` | 断点续传、自动翻页、缺失补拉 K线 |
| `InstrumentFetcher` | 拉取并缓存产品配置 |
| `FundingRateFetcher` | 拉取资金费率历史 |

**设计依据**：spec §6.7。

#### 3.6 `backtest/` — 回测引擎

**职责**：Python 端回测引擎，与 TradingView Pine 双回测交叉验证。**M2 范围**（spec §12.1）。

- 事件驱动：按时间顺序回放 `Bar`/`Tick`
- 模拟成交（市价/限价/滑点/手续费）
- 绩效指标（夏普/最大回撤/胜率等）
- 与 Pine 回测结果对比，偏离 > 5% 禁止上实盘

**依赖**：`models.Bar`、`storage.BarStorage`

#### 3.7 `strategies/` — 策略源码

**职责**：策略源码双副本（Pine + Python）。**M2 范围**。

| 子目录 | 用途 |
|---|---|
| `strategies/pine/` | TradingView Pine Script 源码（`.pine` 文件，git 管理） |
| `strategies/python/` | Python 策略副本，与 Pine 逻辑严格对齐 |

**规则**：每个策略在 Pine 与 Python 中各一份；代码评审确认逻辑一致；双回测结果偏离阈值控制。

#### 3.8 `webhook/` — Webhook 服务

**职责**：FastAPI server 接收 TV alert，解析、去重、入队。**M2 范围**（spec §12.2）。

| 文件 | 用途 |
|---|---|
| `server.py` | `WebhookServer`：FastAPI 应用工厂，端点 `POST /webhook/tv` |
| `parser.py` | alert JSON 解析为 `Signal` |
| `queue.py` | `SignalQueue`：Redis LIST + BRPOP 封装，含 inflight 与死信队列 |
| `dedup.py` | `Deduper`：Redis SET + hash + TTL 5min 防重发 |

**设计依据**：PRD F3.2/F3.3、spec §12.2。

#### 3.9 `execution/` — 执行层

**职责**：worker 进程消费队列，调风控、下单、持久化。**M2 范围**（spec §12.2）。

| 文件 | 用途 |
|---|---|
| `worker.py` | `Worker`：BRPOP → 风控 → 下单 → 持久化主循环 |
| `executor.py` | `Executor`：调用 OKX 下单 API |
| `account.py` | `read_account_field()`：读 monitor 发布的账户快照，engine 算仓位、worker 定风控基数共用 |
| `position_manager.py` | 仓位跟踪与 reconciliation |

**依赖**：`okx.OKXClient`、`risk.RiskChecker`、`storage.sqlite_store`

#### 3.10 `risk/` — 风控

**职责**：风控规则。**M2 范围**（spec §12.2）。

| 类 | 用途 |
|---|---|
| `RiskChecker` | 单品种最大持仓、总仓位上限、单笔最大量、日亏损、连续亏损 |
| `RiskConfig` | 风控参数（从 `config/strategies/{strategy_id}.toml` 加载） |
| `RiskDecision` | 校验结果（通过/拒绝/降仓） |

`RiskChecker.check(..., equity=...)` 的权益基数由调用方传入（worker 取 monitor
快照的 `total_equity`），未传入时退回构造参数 `initial_equity`。

**设计依据**：PRD F3.4/F4.3。

#### 3.11 `monitor/` — 监控告警

**职责**：独立进程，监控账户/持仓/队列/心跳，邮件告警。**M4 范围**（spec §12.4）。

| 类 | 用途 |
|---|---|
| `Monitor` | 监控账户余额、持仓、Redis 队列长度、worker 心跳、死信队列 |
| `EmailAlerter` | SMTP 邮件告警 |
| `Heartbeat` | 进程心跳写入与超时检测 |

**设计依据**：PRD F5.1-F5.6、spec §12.4。

#### 3.12 `cli/` — 命令行入口

**职责**：CLI 命令。**M1 范围**（spec §11）。

| 命令 | 用途 |
|---|---|
| `fetch_candles` | 拉取 K线（spec §11.1） |
| `validate_data` | 校验本地数据完整性（spec §11.2） |
| `align_data` | 多品种对齐（spec §11.2） |

**入口**：`pyproject.toml` 中 `[project.scripts]` 定义 `quant = "quant.cli:main"`。

---

### 4. `tests/` — 测试

**职责**：单元 + 集成 + 烟测。**入 git**。

| 子结构 | 用途 |
|---|---|
| `tests/unit/` | 单元测试（spec §10） |
| `tests/integration/` | 集成测试，用 respx mock HTTP、本地 Redis |
| `tests/smoke/` | 烟测（真实 OKX 调用，CI 跳过） |

**设计依据**：spec §10。pytest 配置在 `pyproject.toml` `[tool.pytest.ini_options]`。

---

### 5. `notebooks/` — 策略研究

**职责**：Jupyter 笔记本，用于策略原型探索、数据分析。**入 git**（但 `.ipynb_checkpoints` 不入）。

- 不承载生产代码；探索结果成熟后迁移到 `strategies/python/` 或 `backtest/`

---

### 6. `data/` — 历史数据

**职责**：本地 parquet 文件存储。**不入 git**。

| 子结构 | 用途 |
|---|---|
| `data/candles/{inst_id}/{bar}/` | K线按品种与周期分目录，每年一个 parquet 文件 |
| `data/instruments/` | 产品配置 JSON |
| `data/funding/` | 资金费率历史 |

**设计依据**：spec §6.3-6.4。

---

### 7. `logs/` — 运行日志

**职责**：运行时日志文件。**不入 git**。

- 主日志文件：`quant-trading.log`，10MB rotation，保留 30 天
- 配置在 `config.toml [logging]` 段

---

## 关键文件说明

### `pyproject.toml`

Python 包定义。**入 git**。

- **`[project]`**：包名 `quant-trading`、版本、Python 版本要求
- **`[project.optional-dependencies]`**：按里程碑分组（`m1`/`m2`/`dev`），避免一开始装所有依赖
- **`[project.scripts]`**：定义 `quant` 命令行入口
- **`[tool.setuptools.packages.find]`**：声明 `src/` 布局
- **`[tool.pytest.ini_options]`**：pytest 配置
- **`[tool.ruff]`**：代码风格配置

### `.env.example`

环境变量模板。**入 git**。

- OKX API 凭证：`OKX_API_KEY`/`OKX_SECRET`/`OKX_PASSPHRASE`
- M2 起使用：`REDIS_URL`/`WEBHOOK_AUTH_TOKEN`

复制为 `.env` 后填真实值；`.env` 不入 git。

### `.gitignore`

git 忽略规则。**入 git**。

主要忽略：
- 敏感文件：`.env`、`config/config.toml`、`config/strategies/*.toml`
- 运行时数据：`data/`、`logs/`、`*.db`
- Python 缓存：`__pycache__/`、`*.pyc`、`.pytest_cache/`
- 虚拟环境：`.venv/`、`venv/`
- IDE 文件：`.vscode/`、`.idea/`、`.DS_Store`

---

## 目录与里程碑映射

| 目录 | M1 | M2 | M3 | M4 | M5 |
|---|---|---|---|---|---|
| `foundation/` | ✅ 实现 | 用 | 用 | 用 | 用 |
| `models/` | ✅ 实现 | 用 | 用 | 用 | 用 |
| `okx/` | ✅ 实现 | - | 用 | 用 | 用 |
| `storage/` | ✅ 实现 | 扩展 SQLite | 用 | 用 | 用 |
| `fetcher/` | ✅ 实现 | - | - | - | - |
| `backtest/` | - | ✅ 实现 | 用 | - | - |
| `strategies/` | - | ✅ 实现 | 用 | - | 用 |
| `webhook/` | - | ✅ 实现 | 用 | 监控 | 用 |
| `execution/` | - | ✅ 实现 | 用 | - | 用 |
| `risk/` | - | ✅ 实现 | 用 | - | 用 |
| `monitor/` | - | - | - | ✅ 实现 | 用 |
| `cli/` | ✅ 实现 | 扩展 | - | - | - |

✅ = 实现；用 = 复用；- = 不涉及

---

## 参考资料

- [PRD v0.5](requirements/2026-10-17-quant-trading-platform-requirements.md) §6 项目结构
- [M1 spec v1.1](specs/2026-10-17-m1-data-foundation-design.md) §3 模块清单、§12 接口契约
- [OKX API 速查](reference/OKX_API_REFERENCE.md)
