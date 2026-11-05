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

from pydantic import BaseModel, Field, field_validator

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
    #: 按周期覆盖保留天数（M10-3）：短周期数据量大、价值衰减快，
    #: 例如 {"1m": 30, "5m": 90, "1H": 365}；未列出的周期用 retention_days
    retention_by_bar: dict[str, int] = Field(default_factory=dict)
    retention_enabled: bool = True
    retention_at: str = "03:30"             # 本地时间 HH:MM
    retention_last_run_key: str = "quant:retention:last"
    # 死信是否双写 SQLite（灰度期回滚开关）。关掉后退回「只在 Redis」的旧行为，
    # 但看板死信列表与重放会没有数据可读。
    dead_letter_persist: bool = True


class FactorsSettings(BaseModel):
    """因子接入（M5-13）：默认关闭，不影响既有回测结果。

    `ids` 为空或 `enabled=false` 时，`Context.factors` 保持空字典，
    策略行为与接入前逐点一致。
    """

    enabled: bool = False
    ids: list[str] = Field(default_factory=list)


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
    #: HMAC-SHA256 共享密钥（X-Signature 头）；与 auth_token 二选一，
    #: auth_token 非空时优先。公网端点建议用 HMAC，避免 token 明文写进
    #: TradingView alert 正文而被平台日志留存。
    hmac_secret: str = ""
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
    #: 回测是否计入永续资金费率（M4-15）。开启前需先用 fetch-funding-rates
    #: 回补对应 `-SWAP` 合约的历史，否则回测会直接报错而不是静默忽略成本。
    funding_rate_enabled: bool = False


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
    # 服务端仓位计算（M8-14）：信号未带 qty 时由 worker 按
    # 「权益 × 单笔比例 ÷ 价格」算量。关掉即回滚到「qty 必填」。
    server_side_sizing: bool = True
    # 信号既没给 qty 也没给 size_pct 时使用的单笔目标占比（10%）
    default_size_pct: Decimal = Decimal("0.1")

    @field_validator("default_size_pct")
    @classmethod
    def _check_default_size_pct(cls, value: Decimal) -> Decimal:
        """0 或负数会让每一单算量都失败，属于配置错误，加载时就拒绝。"""
        if value <= 0:
            raise ValueError("default_size_pct must be positive")
        return value


class CandleSyncSettings(BaseModel):
    """K 线增量同步（定时把本地 parquet 追到最近已收盘 bar）。

    日报与回测都读本地 parquet，数据过期会让两者失真，故由 supervisor
    托管一个常驻同步进程。
    """

    insts: list[str] = Field(default_factory=lambda: ["BTC-USDT"])
    bar: str = "1H"
    bars: list[str] = Field(default_factory=list)   # 为空时回落为 [bar]
    #: 新周期首次同步补的历史根数（M2-12）；0 表示只拉 lookback_bars。
    #: 否则新周期只有几根 K 线，图表会被自适应撑成巨粗蜡烛。
    first_sync_bars: int = 300
    interval_seconds: int = 300             # 轮询间隔，0 表示跑一次即退出
    lookback_bars: int = 3                  # 每次回看几根，顺带补断点附近的洞

    @property
    def resolved_bars(self) -> list[str]:
        """实际要同步的周期列表：配置了 bars 就只用它，否则用单个 bar。"""
        return list(self.bars) if self.bars else [self.bar]


class QualitySettings(BaseModel):
    """数据质量报告（MS4-C §9）。

    candles 每轮同步后把校验结果并入 `data/quality/{inst}/{bar}/latest.json`，
    monitor 读这些文件判断是否有未修复缺口。
    """

    enabled: bool = True
    # 每轮回看多少根 K 线。窗口太小会看不到窗口外的旧缺口（旧缺口保持
    # 原状态，不会被误标为已修复），太大则每轮都要重读一批 parquet。
    window_bars: int = 5000
    # 未修复缺口告警冷却：数据缺口不会分钟级自愈，1 小时内只提醒一次
    alert_cooldown_seconds: int = 3600


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
    # --- 启停矩阵（MS4-C §4）---
    # multi_binding=False 是回滚开关：只跑下面 strategy/inst/bar 这一个组合
    multi_binding: bool = True
    # Redis 绑定缓存丢失时：db=回落读 SQLite；halt=直接停止新开仓
    bindings_fail_mode: str = "db"
    bindings_key: str = "quant:strategy:bindings"


class DashboardSettings(BaseModel):
    """只读运维看板（M4 扩展）。默认只监听本机。"""

    host: str = "127.0.0.1"
    port: int = 8080
    web_dir: Path = Path("./web/dist")
    token: str = ""                         # 写操作 token；留空则只允许本机调用
    # M10-6：进程启停。demo=true 时默认允许；实盘必须显式打开这个开关，
    # 避免一个误点把实盘 worker 停掉。
    allow_live_process_control: bool = False


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
    # 无 TTL 的「最后一次心跳」时间戳：monitor 重启时据此判断上次停了多久。
    # 带 TTL 的心跳键在长时间停机后已过期，读不到任何线索（M9-4）。
    monitor_last_beat_key: str = "quant:heartbeat:monitor:last"
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
    # 规则级启停（M9-2）：列出的规则整条停用，既不告警也不发邮件。
    # 静音（M9-7/M9-10）是临时压噪，这里是「这条规则我不要」，用途不同。
    disabled_rules: list[str] = Field(default_factory=list)
    # 告警保留分档（M9-12）：按级别给不同天数，0 表示该级别不自动清理
    alert_retention_error_days: int = 90
    alert_retention_warning_days: int = 30
    alert_retention_info_days: int = 7
    # worker 每轮发布生效风控参数（键后缀 :{strategy}），看板只读
    risk_limits_key: str = "quant:risk:limits"
    risk_limits_ttl_seconds: int = 300
    # 在线阈值覆盖层（hash，字段 {strategy}:{param}）；worker 每轮心跳重读，
    # 因此看板调整后不重启即生效（M7-8）
    risk_overrides_key: str = "quant:risk:overrides"


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
    # 极端行情熔断（M7-6）：单根 (high-low)/open 严格大于阈值即按品种暂停。
    # 设 circuit_enabled=false 可整体回滚到「无熔断」的旧行为。
    circuit_enabled: bool = True
    circuit_amplitude_pct: Decimal = Decimal("0.10")
    circuit_cooldown_seconds: int = 900
    # 冷却结束后是否必须人工在看板确认才恢复（M7-12）。
    # 置 false 退回「TTL 到期自动恢复」的旧行为（灰度回滚用）。
    circuit_resume_requires_confirm: bool = True
    # 下单前预检（M7-7）：精度、最小下单量、最小名义金额、现货余额。
    # 只对直连交易所（okx broker）生效，模拟撮合不做校验。
    preflight_enabled: bool = True
    # 额外的单笔最小名义金额（USDT）；0 表示只按交易所 minSz 判定
    preflight_min_notional_usdt: Decimal = Decimal("0")
    # 余额安全垫，给手续费与滑点留余量
    preflight_balance_buffer_pct: Decimal = Decimal("0.02")
    # 余额缓存秒数：每单现查会吃满限频
    preflight_balance_cache_seconds: float = 15.0


class Settings(BaseModel):
    """项目全局配置。

    通过 Settings.load(path) 从 TOML 加载；${ENV} 引用会被展开。
    敏感字段（如 OKX 凭证）必须通过环境变量提供（.env 由调用方加载）。
    """

    okx: OKXSettings
    storage: StorageSettings = Field(default_factory=StorageSettings)
    factors: FactorsSettings = Field(default_factory=FactorsSettings)
    rate_limit: RateLimitSettings = Field(default_factory=RateLimitSettings)
    logging: LoggingSettings = Field(default_factory=LoggingSettings)
    redis: RedisSettings = Field(default_factory=RedisSettings)
    webhook: WebhookSettings = Field(default_factory=WebhookSettings)
    backtest: BacktestSettings = Field(default_factory=BacktestSettings)
    params: ParamsSettings = Field(default_factory=ParamsSettings)
    state: StateSettings = Field(default_factory=StateSettings)
    execution: ExecutionSettings = Field(default_factory=ExecutionSettings)
    candles: CandleSyncSettings = Field(default_factory=CandleSyncSettings)
    quality: QualitySettings = Field(default_factory=QualitySettings)
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
    "FactorsSettings",
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
