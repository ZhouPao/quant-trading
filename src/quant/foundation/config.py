"""Foundation config: 全局配置加载（spec §7.3）。

设计：
- TOML 主配置（config/config.toml），支持 ${ENV_VAR} 与 ${ENV_VAR:-default} 引用
- .env 提供 OKX 凭证等敏感值（由 pydantic-settings 自动读取，本类不直接处理 .env）
- Settings 单例通过 Settings.load(path) 获取

使用：
    from quant.foundation.config import Settings
    s = Settings.load(Path("config/config.toml"))
    print(s.okx.api_key)        # 真值来自 .env 的 OKX_API_KEY
    print(s.storage.data_dir)   # Path("./data")
"""
from __future__ import annotations

import os
import re
import tomllib
from decimal import Decimal
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

# ${VAR} 或 ${VAR:-default}
_ENV_PATTERN = re.compile(r"^\$\{([A-Z_][A-Z0-9_]*)(?::-([^}]*))?\}$")


def _load_dotenv() -> None:
    """加载项目根目录 `.env`；python-dotenv 缺失时静默跳过。"""
    try:
        from dotenv import load_dotenv
    except ImportError:  # pragma: no cover
        return
    root = Path(__file__).resolve().parents[3]
    for candidate in (Path.cwd() / ".env", root / ".env"):
        if candidate.is_file():
            load_dotenv(candidate, override=False)
            return


class OKXSettings(BaseModel):
    api_key: str
    secret: str
    passphrase: str
    demo: bool = True


class StorageSettings(BaseModel):
    """本地数据存储与保留策略。

    retention_days 同时作用于 K线 parquet、SQLite 历史记录与日报 CSV；
    清理由 monitor 每日在 retention_at 后执行一次，也可 `quant prune` 手动触发。
    """

    data_dir: Path = Path("./data")
    retention_days: int = 365
    retention_enabled: bool = True
    retention_at: str = "03:30"             # 本地时间 HH:MM
    retention_last_run_key: str = "quant:retention:last"


class RateLimitSettings(BaseModel):
    timeout_seconds: float = 10.0


class LoggingSettings(BaseModel):
    level: str = "INFO"
    file: Path = Path("./logs/quant-trading.log")
    rotation: str = "10 MB"
    retention: str = "30 days"


class RedisSettings(BaseModel):
    """M2 起使用，M1 仅预留形状。"""

    url: str = "redis://localhost:6379/0"
    queue_key: str = "quant:signals"
    inflight_key: str = "quant:signals:inflight"
    dead_key: str = "quant:signals:dead"
    dedup_prefix: str = "quant:dedup:"
    dedup_ttl_seconds: int = 300


class WebhookSettings(BaseModel):
    """M2 起使用，M1 仅预留形状。"""

    host: str = "127.0.0.1"
    port: int = 8000
    path: str = "/webhook/tv"
    auth_token: str = ""
    ack_timeout_seconds: int = 5


class BacktestSettings(BaseModel):
    """回测默认参数（spec §12）。"""

    initial_cash: Decimal = Decimal("10000")
    commission_rate: Decimal = Decimal("0.001")
    slippage_bps: Decimal = Decimal("5")
    window_size: int = 200
    task_workers: int = 1           # 回测任务消费者数（CPU 密集，默认串行）
    artifacts_dir: Path = Path("./data/backtests")
    max_combos: int = 500           # 单次寻优的网格组合上限（CLI 与看板共用）


class ParamsSettings(BaseModel):
    """策略参数来源（MS4-C §12）。

    `resolve_from_db=False` 是回滚开关：完全按 toml 读，忽略 strategy_params。
    """

    resolve_from_db: bool = True


class StateSettings(BaseModel):
    """状态持久化（spec §12）。"""

    db_path: Path = Path("./data/state/quant.db")


class ExecutionSettings(BaseModel):
    """订单生命周期管理（F3.5/F3.7）。

    reconcile_interval_seconds: worker 运行期对账间隔（0=仅启动时对账一次）
    order_timeout_seconds: 挂单超时自动撤单阈值（0=关闭）
    """

    reconcile_interval_seconds: int = 60
    order_timeout_seconds: int = 300
    # 手动减仓的交易模式（看板直连 OKX 时使用）：cash=现货，cross=全仓合约
    td_mode: str = "cash"


class CandleSyncSettings(BaseModel):
    """K 线增量同步（定时把本地 parquet 追到最近已收盘 bar）。

    日报与回测都读本地 parquet，数据过期会让两者失真，故由 supervisor
    托管一个常驻同步进程。
    """

    insts: list[str] = Field(default_factory=lambda: ["BTC-USDT"])
    bar: str = "1H"
    bars: list[str] = Field(default_factory=list)   # 为空时回落为 [bar]
    interval_seconds: int = 300             # 轮询间隔，0 表示跑一次即退出
    lookback_bars: int = 3                  # 每次回看几根，顺带补断点附近的洞

    @property
    def resolved_bars(self) -> list[str]:
        """实际要同步的周期列表：配置了 bars 就只用它，否则用单个 bar。"""
        return list(self.bars) if self.bars else [self.bar]


class EngineSettings(BaseModel):
    """本地策略实时信号引擎（不依赖 TradingView）。

    每根 K 线收盘后跑一次本地策略，信号写 Redis 队列交给 worker 执行。
    """

    enabled: bool = True
    strategy: str = "sma_cross"
    inst: str = "BTC-USDT"
    bar: str = "1H"
    interval_seconds: int = 60             # 轮询间隔；每根 bar 只评估一次
    warmup_bars: int = 200                 # 回放窗口，需覆盖策略最慢周期
    max_catchup_bars: int = 24             # 单轮最多补评估几根漏掉的 bar
    config_dir: Path = Path("./config/strategies")
    cash_fallback: Decimal = Decimal("10000")  # 读不到账户可用 USDT 时的兜底
    last_bar_key: str = "quant:engine:last-bar"
    # 每轮评估后发布的状态 JSON（看板只读），键后缀 :{strategy}:{inst}
    status_key: str = "quant:engine:status"
    status_timeout_seconds: int = 180      # 超过视为引擎离线


class DashboardSettings(BaseModel):
    """只读运维看板（M4 扩展）。默认只监听本机。"""

    host: str = "127.0.0.1"
    port: int = 8080
    web_dir: Path = Path("./web/dist")
    token: str = ""                         # 写操作 token；留空则只允许本机调用


class ReportSettings(BaseModel):
    """模拟盘日报（偏离自动比对）。

    由 monitor 在本地时间到达 `at` 后生成一次，结果写 CSV 并可选发邮件。
    """

    enabled: bool = False
    at: str = "08:00"                       # 本地时间 HH:MM
    strategy: str = "sma_cross"
    inst: str = "BTC-USDT"
    bar: str = "1H"
    window_hours: int = 24
    threshold: float = 0.05                 # 最大允许偏离
    limit: int = 500                        # 读取的模拟盘成交上限
    config_dir: Path = Path("./config/strategies")
    out_dir: Path = Path("./reports/daily")
    email: bool = True                      # SMTP 未配置时自动降级为仅日志
    last_run_key: str = "quant:daily-report:last"


class SmtpSettings(BaseModel):
    """邮件告警（spec §12.4，M4）。

    未配置 host/username/to_addrs 时告警降级为仅日志，不抛错。
    """

    host: str = ""
    port: int = 465
    username: str = ""
    password: str = ""
    from_addr: str = ""
    to_addrs: list[str] = Field(default_factory=list)
    use_ssl: bool = True


class MonitorSettings(BaseModel):
    """监控告警（spec §12.4，M4）。"""

    interval_seconds: int = 60
    worker_heartbeat_key: str = "quant:heartbeat:worker"
    worker_heartbeat_timeout_seconds: int = 120
    # engine 状态键（含 :{strategy}:{inst} 后缀）；空=不检查 engine 心跳
    engine_status_key: str = ""
    engine_timeout_seconds: int = 180
    monitor_heartbeat_key: str = "quant:heartbeat:monitor"
    monitor_heartbeat_timeout_seconds: int = 300
    queue_backlog_threshold: int = 50
    margin_ratio_threshold: float = 0.1
    balance_change_threshold: float = 0.10
    cancel_rate_threshold: float = 0.20
    consecutive_loss_threshold: int = 3
    api_error_rate_threshold: float = 0.50
    # OKX /account/positions 不接受 SPOT（现货持仓在 balance details，且无强平价），
    # 强平价距离仅对合约有意义，故默认只查 SWAP/FUTURES。
    position_inst_types: list[str] = Field(
        default_factory=lambda: ["SWAP", "FUTURES"]
    )
    # 现货持仓对账：/account/positions 不含 SPOT，改从 balance details 按
    # {ccy}-{quote} 折算成持仓；置空则关闭现货对账（只监控合约）。
    spot_quote_ccy: str = "USDT"
    # 策略记账数量超过交易所余额的告警阈值（超出比例）。本地少于交易所是
    # 正常的（外部转入、空投），反向才说明记账多了、卖出会因可用量不足被拒。
    position_drift_tolerance: float = 0.005
    alive_email_hours: int = 4
    # 上次存活邮件时间戳；存 Redis 以便 monitor 重启后不重复发送
    alive_email_key: str = "quant:monitor:alive-last"
    # 只读看板：monitor 每轮发布快照与告警，dashboard 进程只读这些键
    dashboard_snapshot_key: str = "quant:dashboard:snapshot"
    dashboard_alerts_key: str = "quant:dashboard:alerts"
    dashboard_alert_keep: int = 200
    # 同一规则在冷却窗口内只发布一次，避免持续状态刷屏（0=不去重）
    dashboard_alert_cooldown_seconds: int = 600
    # 告警落库总开关；置 false 退回「只写 Redis」的旧行为（MS4-A §13 回滚用）
    alerts_persist: bool = True
    # worker 每轮发布生效风控参数（键后缀 :{strategy}），看板只读
    risk_limits_key: str = "quant:risk:limits"
    risk_limits_ttl_seconds: int = 300


class RiskDefaults(BaseModel):
    """全局风控默认值（config.toml `[risk]`，MS4-B §13）。

    限额字段默认 ``None`` 表示「全局层未配置」，**不参与取严**，由代码
    默认值兜底。若把代码默认值写进全局层，就等于把所有策略隐式钳制在
    默认值以内——那是一次没人要求的收紧。
    """

    max_position_pct: Decimal | None = None
    max_total_pct: Decimal | None = None
    max_order_qty: Decimal | None = None
    daily_loss_limit: Decimal | None = None
    consecutive_loss_limit: int | None = None
    # 权益快照缺失时是否允许退回 backtest.initial_cash 做风控基数。
    # 默认 False：monitor 挂掉时按初始资金算比例会把真实敞口放大数倍。
    allow_initial_equity_fallback: bool = False
    # 快照最大可接受年龄；超龄视为不可用（不硬编码，monitor 周期可调）
    snapshot_max_age_ms: int = 180_000
    # 风控事件是否落库（灰度期回滚开关）
    event_log: bool = True


class Settings(BaseModel):
    """项目全局配置。

    通过 Settings.load(path) 从 TOML 加载；${ENV} 引用会被展开。
    敏感字段（如 OKX 凭证）必须通过环境变量提供（.env 由调用方加载）。
    """

    okx: OKXSettings
    storage: StorageSettings = Field(default_factory=StorageSettings)
    rate_limit: RateLimitSettings = Field(default_factory=RateLimitSettings)
    logging: LoggingSettings = Field(default_factory=LoggingSettings)
    redis: RedisSettings = Field(default_factory=RedisSettings)
    webhook: WebhookSettings = Field(default_factory=WebhookSettings)
    backtest: BacktestSettings = Field(default_factory=BacktestSettings)
    params: ParamsSettings = Field(default_factory=ParamsSettings)
    state: StateSettings = Field(default_factory=StateSettings)
    execution: ExecutionSettings = Field(default_factory=ExecutionSettings)
    candles: CandleSyncSettings = Field(default_factory=CandleSyncSettings)
    engine: EngineSettings = Field(default_factory=EngineSettings)
    smtp: SmtpSettings = Field(default_factory=SmtpSettings)
    monitor: MonitorSettings = Field(default_factory=MonitorSettings)
    dashboard: DashboardSettings = Field(default_factory=DashboardSettings)
    report: ReportSettings = Field(default_factory=ReportSettings)
    risk: RiskDefaults = Field(default_factory=RiskDefaults)

    @classmethod
    def load(cls, config_path: Path = Path("config/config.toml")) -> Settings:
        """从 TOML 文件加载配置，并展开 ${ENV} 引用。

        - 自动加载项目根目录 `.env`（已存在的环境变量优先，不会被覆盖）
        - 单一 ${VAR}：必须已设置，否则 ValueError
        - ${VAR:-default}：未设置时使用 default
        """
        _load_dotenv()
        with open(config_path, "rb") as f:
            data = tomllib.load(f)
        data = cls._expand_env(data)
        return cls(**data)

    @staticmethod
    def _expand_env(d: Any) -> Any:
        """递归展开 dict 中的 ${ENV} 引用。"""
        if isinstance(d, dict):
            return {k: Settings._expand_env(v) for k, v in d.items()}
        if isinstance(d, list):
            return [Settings._expand_env(x) for x in d]
        if isinstance(d, str):
            m = _ENV_PATTERN.match(d)
            if not m:
                return d
            var_name, default = m.group(1), m.group(2)
            val = os.environ.get(var_name)
            if val is None or val == "":
                if default is not None:
                    return default
                raise ValueError(f"env var {var_name} not set")
            return val
        return d


__all__ = [
    "Settings",
    "OKXSettings",
    "StorageSettings",
    "RateLimitSettings",
    "LoggingSettings",
    "RedisSettings",
    "WebhookSettings",
    "ParamsSettings",
    "ExecutionSettings",
    "CandleSyncSettings",
    "EngineSettings",
    "DashboardSettings",
    "ReportSettings",
    "SmtpSettings",
    "MonitorSettings",
]
