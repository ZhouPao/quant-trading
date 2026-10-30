# quant-trading

个人加密货币量化交易平台。

**架构**：OKX 数据源 + TradingView Pine 回测 + Python 执行层（FastAPI + Redis + worker）
**范围**：个人使用，不对外提供服务

## 项目结构

```
quant-trading/
├── docs/                      # 设计文档（PRD / spec / 参考）
│   ├── requirements/          # 需求文档
│   ├── specs/                 # 详细设计文档
│   └── reference/             # OKX API 速查等参考
├── config/                    # 配置（不入 git；提供 .toml.example 模板）
│   └── strategies/            # 各策略风控参数
├── src/quant/
│   ├── foundation/            # 配置、日志、错误、通用类型
│   ├── models/                # 数据模型（Bar/Tick/Instrument/FundingRate）
│   ├── okx/                   # OKX REST/WS 客户端、签名、限频器
│   ├── storage/               # parquet 存储、SQLite、校验、对齐
│   ├── fetcher/               # 数据下载编排
│   ├── backtest/              # Python 回测引擎（M2 起）
│   ├── strategies/
│   │   ├── pine/              # Pine Script 源码（.pine 文件）
│   │   └── python/            # Python 策略副本
│   ├── webhook/               # FastAPI server + 信号解析 + 去重（M2 起）
│   ├── execution/             # worker + 下单执行 + 仓位管理（M2 起）
│   ├── risk/                  # 风控规则（M2 起）
│   ├── monitor/               # 账户/持仓监控 + 邮件告警 + 心跳（M4 起）
│   └── cli/                   # 命令行入口
├── tests/                     # 单元+集成+烟测
├── notebooks/                 # 策略研究
├── data/                      # 历史数据（parquet，不入 git）
├── logs/                      # 运行日志（不入 git）
├── pyproject.toml
├── .env.example
└── .gitignore
```

## 里程碑

> 里程碑编号统一为 **MS** 前缀（Milestone），与 PRD 的模块编号 M1–M10 区分。详见 [PRD §8](docs/requirements/2026-10-30-quant-trading-platform-requirements-v0.6.md)。

| 阶段 | 范围 | 状态 |
|---|---|---|
| MS1 数据底座 | OKX 客户端 + parquet 存储 + 校验/对齐 | 已交付 |
| MS2 回测 + Webhook 框架 | Python 回测 + webhook server + worker + 风控骨架 | 已交付 |
| MS3 模拟盘跑通 | 本地脚本模拟 TV alert，模拟盘端到端跑通 | 已交付 |
| MS4 告警 + 总览 + 可靠性 | 告警持久化、首页总览、风控参数体系、回测落库、执行可靠性、策略参数启停 | 进行中 |
| MS5 实盘接入 | 真 webhook、手工下单、三项对账、熔断与在线调阈值、告警静音 | 未开始 |
| MS6 上云 | systemd 托管 + 开机自启 | 未开始 |
| MS7+ 扩展 | 因子库、组合管理 | 候选 |

详见 [PRD v0.6](docs/requirements/2026-10-30-quant-trading-platform-requirements-v0.6.md) 与 [MS1 spec v1.2](docs/specs/2026-10-17-ms1-data-foundation-design.md)。

## 快速开始

```bash
# 1. 创建虚拟环境
python3.11 -m venv .venv
source .venv/bin/activate

# 2. 安装项目（M1 依赖）
pip install -e ".[m1,dev]"

# 3. 复制配置
cp config/config.toml.example config/config.toml
cp .env.example .env
# 编辑 .env 填入 OKX_API_KEY/SECRET/PASSPHRASE

# 4. 验证安装
python -c "import quant; print(quant.__version__)"
```
