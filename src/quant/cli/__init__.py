"""CLI: 命令行入口。M1 spec §11（fetch_candles/validate_data/align_data）。

入口：pyproject.toml [project.scripts] 定义的 quant 命令。
用法：
  quant fetch-candles --inst BTC-USDT --bar 1H --start 2024-01-01 --end 2024-12-31
  quant fetch-instruments --inst-type SPOT
  quant fetch-funding-rates --inst BTC-USDT-SWAP
  quant validate-data --inst BTC-USDT --bar 1H
  quant align-data --insts BTC-USDT,ETH-USDT --bar 1H --fill ffill
"""
from __future__ import annotations

import argparse
import sys

from quant.cli.align_data import main as align_data_main
from quant.cli.backtest import main as backtest_main
from quant.cli.backup import main as backup_main
from quant.cli.check_data import main as check_data_main
from quant.cli.check_live import main as check_live_main
from quant.cli.dashboard import main as dashboard_main
from quant.cli.doctor import main as doctor_main
from quant.cli.fetch_candles import main as fetch_candles_main
from quant.cli.fetch_funding_rates import main as fetch_funding_rates_main
from quant.cli.fetch_instruments import main as fetch_instruments_main
from quant.cli.fetch_open_interest import main as fetch_open_interest_main
from quant.cli.kill_switch import main as kill_switch_main
from quant.cli.monitor import main as monitor_main
from quant.cli.optimize import main as optimize_main
from quant.cli.paper_report import main as paper_report_main
from quant.cli.prune import main as prune_main
from quant.cli.research import main as research_main
from quant.cli.restore import main as restore_main
from quant.cli.run_engine import main as run_engine_main
from quant.cli.run_worker import main as run_worker_main
from quant.cli.serve_webhook import main as serve_webhook_main
from quant.cli.simulate_alert import main as simulate_alert_main
from quant.cli.supervisor import main as supervisor_main
from quant.cli.sync_candles import main as sync_candles_main
from quant.cli.test_email import main as test_email_main
from quant.cli.validate_data import main as validate_data_main
from quant.cli.walkforward import main as walk_forward_main

# OKX 支持的 K线周期；6H/12H 为本地扩展口径，OKX 原生返回
BAR_CHOICES = ["1m", "5m", "15m", "30m", "1H", "4H", "6H", "12H", "1D"]


def _add_backtest_args(sub, name: str) -> None:
    """backtest / optimize 共用的参数。"""
    p = sub.add_parser(name, help=f"{name}：本地历史数据策略回测")
    p.add_argument("--strategy", required=True, help="策略 ID，如 sma_cross")
    p.add_argument("--inst", required=True, help="instId，如 BTC-USDT")
    p.add_argument(
        "--bar",
        default="1H",
        choices=BAR_CHOICES,
        help="K线周期",
    )
    p.add_argument("--start", required=True, help="开始日期 YYYY-MM-DD（含）")
    p.add_argument("--end", required=True, help="结束日期 YYYY-MM-DD（含）")
    p.add_argument(
        "--params",
        default=None,
        help='覆盖策略参数，如 "fast_len=10;slow_len=50"',
    )
    p.add_argument(
        "--use-db-params",
        action="store_true",
        help=(
            "读取看板在线调参（strategy_params）；默认只用 toml 基线，"
            "保证同一条命令的回测结果可复现"
        ),
    )
    p.add_argument(
        "--warmup-bars",
        type=int,
        default=300,
        help="预热 K线数（不成交、不计入指标），默认 300（EMA200 需 201 根）",
    )
    p.add_argument(
        "--config-dir",
        default="config/strategies",
        help="策略配置目录",
    )
    p.add_argument(
        "--funding",
        action="store_true",
        help="计入永续资金费率成本（M4-15）；默认取配置 backtest.funding_rate_enabled",
    )
    p.add_argument(
        "--config", default="config/config.toml", help="配置文件路径"
    )
    if name == "backtest":
        p.add_argument(
            "--out-dir",
            default="reports/backtest",
            help="报告输出目录，默认 reports/backtest",
        )
        p.add_argument(
            "--no-report", action="store_true", help="只打印指标，不写文件"
        )


def build_parser() -> argparse.ArgumentParser:
    """构建顶层 argparse，包含三个 subcommand。"""
    parser = argparse.ArgumentParser(
        prog="quant",
        description="加密货币量化交易平台 CLI（M1 spec §11）",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    # fetch-candles
    p_fetch = sub.add_parser(
        "fetch-candles",
        help="拉取历史 K线并保存到 parquet（spec §11.1）",
    )
    p_fetch.add_argument("--inst", required=True, help="instId, 如 BTC-USDT")
    p_fetch.add_argument("--bar", default="1H", help="K线周期, 如 1H/1D")
    p_fetch.add_argument("--start", required=True, help="开始日期 YYYY-MM-DD")
    p_fetch.add_argument("--end", required=True, help="结束日期 YYYY-MM-DD")
    p_fetch.add_argument(
        "--config", default="config/config.toml", help="配置文件路径"
    )

    # fetch-instruments
    p_inst = sub.add_parser(
        "fetch-instruments",
        help="拉取 OKX 产品配置并落盘（spec §11.1）",
    )
    p_inst.add_argument(
        "--inst-type",
        required=True,
        help="产品类型: SPOT/SWAP/FUTURES/OPTION",
    )
    p_inst.add_argument(
        "--config", default="config/config.toml", help="配置文件路径"
    )

    # fetch-funding-rates
    p_funding = sub.add_parser(
        "fetch-funding-rates",
        help="拉取永续合约资金费率并落盘（spec §11.1）",
    )
    p_funding.add_argument(
        "--inst",
        required=True,
        help="永续合约 ID, 如 BTC-USDT-SWAP",
    )
    p_funding.add_argument(
        "--start",
        default=None,
        help="回补起点 YYYY-MM-DD（含）；不传则只拉最新一条",
    )
    p_funding.add_argument(
        "--end",
        default=None,
        help="回补终点 YYYY-MM-DD（含）；默认到最新",
    )
    p_funding.add_argument(
        "--max-pages",
        type=int,
        default=100,
        help="回补时最多翻页数（每页 100 条 ≈ 33 天）",
    )
    p_funding.add_argument(
        "--config", default="config/config.toml", help="配置文件路径"
    )

    # fetch-open-interest (M2-14)
    p_oi = sub.add_parser(
        "fetch-open-interest",
        help="拉取永续合约持仓量并落盘（M2-14）",
    )
    p_oi.add_argument(
        "--inst",
        required=True,
        nargs="+",
        help="永续合约 ID（可多个）, 如 BTC-USDT-SWAP ETH-USDT-SWAP",
    )
    p_oi.add_argument(
        "--config", default="config/config.toml", help="配置文件路径"
    )

    # validate-data
    p_validate = sub.add_parser(
        "validate-data",
        help="校验本地 K线数据完整性（spec §11.2）",
    )
    p_validate.add_argument("--inst", required=True, help="instId, 如 BTC-USDT")
    p_validate.add_argument("--bar", required=True, help="K线周期, 如 1H/1D")
    p_validate.add_argument(
        "--start", default=None, help="开始日期 YYYY-MM-DD（可选，不传则全量）"
    )
    p_validate.add_argument(
        "--end", default=None, help="结束日期 YYYY-MM-DD（可选，不传则全量）"
    )
    p_validate.add_argument(
        "--config", default="config/config.toml", help="配置文件路径"
    )

    # check-data
    p_check = sub.add_parser(
        "check-data",
        help="校验本地 K线并生成数据质量报告（MS4-C §9）",
    )
    p_check.add_argument("--inst", required=True, help="instId, 如 BTC-USDT")
    p_check.add_argument("--bar", required=True, help="K线周期, 如 1H/1D")
    p_check.add_argument(
        "--start", default=None, help="开始日期 YYYY-MM-DD（可选，不传则全量）"
    )
    p_check.add_argument(
        "--end", default=None, help="结束日期 YYYY-MM-DD（可选，不传则全量）"
    )
    p_check.add_argument(
        "--config", default="config/config.toml", help="配置文件路径"
    )

    # align-data
    p_align = sub.add_parser(
        "align-data",
        help="多品种 K线对齐（spec §11.2）",
    )
    p_align.add_argument(
        "--insts",
        required=True,
        help="逗号分隔的 instId 列表, 如 BTC-USDT,ETH-USDT",
    )
    p_align.add_argument("--bar", required=True, help="K线周期, 如 1H/1D")
    p_align.add_argument(
        "--fill",
        default="ffill",
        choices=["ffill", "nan", "interpolate"],
        help="缺失行填充策略",
    )
    p_align.add_argument(
        "--start", default=None, help="开始日期 YYYY-MM-DD（可选）"
    )
    p_align.add_argument(
        "--end", default=None, help="结束日期 YYYY-MM-DD（可选）"
    )
    p_align.add_argument(
        "--output", default=None, help="输出 parquet 文件路径（可选）"
    )
    p_align.add_argument(
        "--config", default="config/config.toml", help="配置文件路径"
    )

    # serve-webhook
    p_webhook = sub.add_parser(
        "serve-webhook",
        help="启动 TradingView webhook server（spec §6）",
    )
    p_webhook.add_argument(
        "--config", default="config/config.toml", help="配置文件路径"
    )

    # run-engine
    p_engine = sub.add_parser(
        "run-engine",
        help="本地策略实时信号引擎（不依赖 TradingView）",
    )
    p_engine.add_argument(
        "--strategy", default=None, help="策略 ID；默认取 config [engine] strategy"
    )
    p_engine.add_argument("--inst", default=None, help="产品 ID，如 BTC-USDT")
    p_engine.add_argument(
        "--bar",
        default=None,
        choices=BAR_CHOICES,
        help="K线周期",
    )
    p_engine.add_argument(
        "--interval", type=int, default=None, help="轮询间隔秒"
    )
    p_engine.add_argument(
        "--once", action="store_true", help="只评估一轮后退出（排查用）"
    )
    p_engine.add_argument(
        "--config", default="config/config.toml", help="配置文件路径"
    )

    # backtest
    _add_backtest_args(sub, "backtest")
    # optimize
    _add_backtest_args(sub, "optimize")
    sub.choices["optimize"].add_argument(
        "--grid",
        required=True,
        help='参数网格，如 "fast_len=5,10,20;slow_len=20,50"',
    )
    sub.choices["optimize"].add_argument(
        "--metric",
        default="sharpe",
        help="排序指标（max_drawdown 越小越好），默认 sharpe",
    )
    sub.choices["optimize"].add_argument(
        "--top", type=int, default=10, help="输出前 N 组，默认 10"
    )
    sub.choices["optimize"].add_argument(
        "--n-jobs",
        type=int,
        default=1,
        help="并行进程数，默认 1（串行）；>1 走进程池",
    )
    sub.choices["optimize"].add_argument(
        "--max-combos",
        type=int,
        default=None,
        help="组合数上限，默认取 config 里的 backtest.max_combos（500）",
    )

    # walk-forward
    _add_backtest_args(sub, "walk-forward")
    p_wf = sub.choices["walk-forward"]
    p_wf.add_argument(
        "--grid",
        required=True,
        help='参数网格，如 "slow_len=50,100;ema_filter=false,true"',
    )
    p_wf.add_argument(
        "--metric", default="sharpe", help="IS 段选参指标，默认 sharpe"
    )
    p_wf.add_argument(
        "--is-bars", type=int, default=1500, help="样本内根数，默认 1500"
    )
    p_wf.add_argument(
        "--oos-bars", type=int, default=500, help="样本外根数，默认 500"
    )
    p_wf.add_argument(
        "--step-bars",
        type=int,
        default=None,
        help="相邻段 IS 起点间隔，默认等于 --oos-bars（OOS 不重叠）",
    )

    # prune
    p_prune = sub.add_parser(
        "prune",
        help="按 storage.retention_days 清理历史 K线/状态/日报",
    )
    p_prune.add_argument(
        "--days", type=int, default=None, help="临时覆盖保留天数"
    )
    p_prune.add_argument(
        "--config", default="config/config.toml", help="配置文件路径"
    )

    # run-worker
    p_worker = sub.add_parser(
        "run-worker",
        help="启动信号消费 worker（spec §9）",
    )
    p_worker.add_argument(
        "--strategy",
        required=True,
        help="策略 ID，如 sma_cross；多个用逗号分隔（信号按 strategy_id 路由）",
    )
    p_worker.add_argument(
        "--config", default="config/config.toml", help="配置文件路径"
    )
    p_worker.add_argument(
        "--config-dir",
        default="config/strategies",
        help="策略配置目录（读取 [risk] 段）",
    )
    p_worker.add_argument(
        "--paper-mode",
        action="store_true",
        help="模拟盘验证模式：放宽仓位上限到 100%%，与回测 sizing 对齐",
    )
    p_worker.add_argument(
        "--inst",
        default="BTC-USDT",
        help="用于取最近收盘价的品种（M2 模拟市价）",
    )
    p_worker.add_argument(
        "--heartbeat-interval", type=int, default=60, help="心跳间隔秒"
    )
    p_worker.add_argument(
        "--broker",
        default="mock",
        choices=["mock", "okx"],
        help="执行器：mock=内存模拟，okx=OKX 模拟盘/实盘 REST 下单",
    )
    p_worker.add_argument(
        "--td-mode",
        default="cash",
        choices=["cash", "cross", "isolated"],
        help="OKX tdMode：现货 cash，合约 cross/isolated",
    )
    p_worker.add_argument(
        "--ws", action="store_true", help="用 WS tickers 实时价（否则用本地 K线收盘价）"
    )
    p_worker.add_argument(
        "--reconcile-interval",
        type=int,
        default=None,
        help="运行期对账间隔秒（默认取 config.execution；0=仅启动时对账）",
    )
    p_worker.add_argument(
        "--order-timeout",
        type=int,
        default=None,
        help="挂单超时自动撤单秒数（默认取 config.execution；0=关闭）",
    )

    # simulate-alert
    p_sim = sub.add_parser(
        "simulate-alert",
        help="本地模拟 TradingView 推送一条 alert（M3）",
    )
    p_sim.add_argument("--strategy", default="sma_cross", help="策略 ID")
    p_sim.add_argument(
        "--side", required=True, choices=["buy", "sell"], help="买卖方向"
    )
    p_sim.add_argument(
        "--order-type",
        default="market",
        choices=["market", "limit"],
        help="订单类型",
    )
    p_sim.add_argument(
        "--inst", default="BTC-USDT", help="产品 ID"
    )
    p_sim.add_argument("--qty", required=True, help="下单数量（基础币）")
    p_sim.add_argument("--price", default=None, help="限价单价格")
    p_sim.add_argument(
        "--url", default=None, help="webhook URL（默认取 config webhook 段）"
    )
    p_sim.add_argument(
        "--token", default=None, help="覆盖 X-Auth-Token（默认取配置）"
    )
    p_sim.add_argument(
        "--dry-run", action="store_true", help="只打印请求，不实际发送"
    )
    p_sim.add_argument(
        "--config", default="config/config.toml", help="配置文件路径"
    )

    # monitor
    p_mon = sub.add_parser(
        "monitor",
        help="启动账户/持仓/队列/心跳监控（M4）",
    )
    p_mon.add_argument(
        "--config", default="config/config.toml", help="配置文件路径"
    )

    # kill-switch
    p_kill = sub.add_parser(
        "kill-switch",
        help="一键紧急停止：停止新信号 + 撤所有挂单（spec F4.5）",
    )
    p_kill.add_argument(
        "--on", action="store_true", help="激活紧急停止"
    )
    p_kill.add_argument(
        "--off", action="store_true", help="解除紧急停止"
    )
    p_kill.add_argument(
        "--status", action="store_true", help="查询当前状态（默认）"
    )
    p_kill.add_argument("--reason", default="", help="激活原因")
    p_kill.add_argument(
        "--broker",
        default=None,
        choices=["okx"],
        help="激活时同时撤销挂单的执行器（仅 okx）",
    )
    p_kill.add_argument(
        "--config", default="config/config.toml", help="配置文件路径"
    )

    # dashboard
    p_dash = sub.add_parser(
        "dashboard",
        help="启动只读运维看板（M4 扩展）",
    )
    p_dash.add_argument(
        "--host",
        default=None,
        help="监听地址（默认取 config.dashboard，127.0.0.1）",
    )
    p_dash.add_argument(
        "--port",
        type=int,
        default=None,
        help="监听端口（默认取 config.dashboard，8080）",
    )
    p_dash.add_argument(
        "--web-dir",
        default=None,
        help="前端构建产物目录（默认 web/dist）",
    )
    p_dash.add_argument(
        "--config", default="config/config.toml", help="配置文件路径"
    )

    # sync-candles
    p_sync = sub.add_parser(
        "sync-candles",
        help="增量同步 K线到最近已收盘 bar（可常驻轮询）",
    )
    p_sync.add_argument(
        "--inst",
        default=None,
        help="逗号分隔的 instId；默认取 config [candles] insts",
    )
    p_sync.add_argument(
        "--bar",
        default=None,
        help="逗号分隔的周期；默认取 config [candles] bars",
    )
    p_sync.add_argument(
        "--loop", action="store_true", help="常驻轮询（供 supervisor 托管）"
    )
    p_sync.add_argument(
        "--interval",
        type=int,
        default=None,
        help="轮询间隔秒；默认取 config [candles] interval_seconds",
    )
    p_sync.add_argument(
        "--lookback-bars",
        type=int,
        default=None,
        help="每次回看几根，顺带补断点附近的洞",
    )
    p_sync.add_argument(
        "--config", default="config/config.toml", help="配置文件路径"
    )

    # test-email
    p_email = sub.add_parser(
        "test-email",
        help="发送一封测试邮件，验证 SMTP 告警链路",
    )
    p_email.add_argument(
        "--to",
        type=lambda s: [x.strip() for x in s.split(",") if x.strip()],
        default=None,
        help="逗号分隔的收件人；默认取 config [smtp] to_addrs",
    )
    p_email.add_argument(
        "--config", default="config/config.toml", help="配置文件路径"
    )

    # check-live
    p_live = sub.add_parser(
        "check-live",
        help="实盘接入自检：Key 权限（交易/提币）、子账户、IP 绑定",
    )
    p_live.add_argument(
        "--config", default="config/config.toml", help="配置文件路径"
    )

    # supervisor
    p_sup = sub.add_parser(
        "supervisor",
        help="一条命令拉起全部常驻进程，崩溃自动重启（M4 扩展）",
    )
    p_sup.add_argument(
        "--strategy", default="sma_cross",
        help="策略 ID（传给 run-worker；多个用逗号分隔）",
    )
    p_sup.add_argument(
        "--broker",
        default="okx",
        choices=["mock", "okx"],
        help="执行器（默认 okx，模拟盘）",
    )
    p_sup.add_argument(
        "--td-mode",
        default="cash",
        choices=["cash", "cross", "isolated"],
        help="OKX tdMode：现货 cash，合约 cross/isolated",
    )
    p_sup.add_argument(
        "--paper-mode",
        action="store_true",
        help="worker 放宽仓位上限到 100%%（模拟盘验证期）",
    )
    p_sup.add_argument(
        "--ws", action="store_true", help="worker 用 WS 实时价"
    )
    p_sup.add_argument(
        "--no-dashboard", action="store_true", help="不启动只读看板"
    )
    p_sup.add_argument(
        "--dry-run", action="store_true", help="只打印子进程命令，不启动"
    )
    p_sup.add_argument(
        "--config", default="config/config.toml", help="配置文件路径"
    )

    # paper-report
    p_paper = sub.add_parser(
        "paper-report",
        help="模拟盘 vs 回测偏离报告（M3）",
    )
    p_paper.add_argument("--strategy", default="sma_cross", help="策略 ID")
    p_paper.add_argument("--inst", default="BTC-USDT", help="产品 ID")
    p_paper.add_argument(
        "--bar", default="1H", choices=BAR_CHOICES,
        help="K线周期",
    )
    p_paper.add_argument(
        "--limit", type=int, default=500, help="最多读取的模拟盘成交笔数"
    )
    p_paper.add_argument(
        "--threshold", type=float, default=0.05, help="允许的最大偏离（默认 5%%）"
    )
    p_paper.add_argument(
        "--window-hours",
        type=int,
        default=24,
        help="回测对比时间窗（小时，默认 24）",
    )
    p_paper.add_argument(
        "--out", default=None, help="CSV 输出路径（可选）"
    )
    p_paper.add_argument(
        "--config-dir",
        default="config/strategies",
        help="策略配置目录（读取 class/params）",
    )
    p_paper.add_argument(
        "--config", default="config/config.toml", help="配置文件路径"
    )

    # backup
    p_backup = sub.add_parser(
        "backup",
        help="备份 SQLite 状态库与配置快照（M10-7，带 sha256 清单）",
    )
    p_backup.add_argument(
        "--out", default=None, help="输出根目录（默认 <data_dir>/backups）"
    )
    p_backup.add_argument(
        "--config", default="config/config.toml", help="配置文件路径"
    )

    # restore
    p_restore = sub.add_parser(
        "restore",
        help="从 quant backup 目录恢复状态库（M10-7，恢复前自动备份）",
    )
    p_restore.add_argument(
        "--backup", required=True, help="quant backup 生成的目录"
    )
    p_restore.add_argument(
        "--dry-run",
        action="store_true",
        help="只校验清单与版本，不写任何文件",
    )
    p_restore.add_argument(
        "--force",
        action="store_true",
        help="目标库 WAL 非空时仍然恢复（确认已停 dashboard/worker）",
    )
    p_restore.add_argument(
        "--config", default="config/config.toml", help="配置文件路径"
    )

    # doctor
    p_doctor = sub.add_parser(
        "doctor",
        help="部署自检：SQLite/Redis/凭证/SMTP/数据新鲜度/磁盘（M10-7）",
    )
    p_doctor.add_argument(
        "--json", action="store_true", help="输出机器可读 JSON"
    )
    p_doctor.add_argument(
        "--strict", action="store_true", help="存在 warn 也返回非零退出码"
    )
    p_doctor.add_argument(
        "--skip-redis", action="store_true", help="跳过 Redis 连通检查"
    )
    p_doctor.add_argument(
        "--config", default="config/config.toml", help="配置文件路径"
    )

    # research
    p_research = sub.add_parser(
        "research", help="研究数据导出：K线/成交/回测结果（M3-7）"
    )
    research_sub = p_research.add_subparsers(
        dest="research_command", required=True
    )
    p_export = research_sub.add_parser(
        "export", help="导出为 parquet/JSON 目录，供 notebook 使用"
    )
    p_export.add_argument("--inst", required=True, help="instId，如 BTC-USDT")
    p_export.add_argument(
        "--bar", default="1H", choices=BAR_CHOICES, help="K线周期"
    )
    p_export.add_argument(
        "--start", required=True, help="开始日期 YYYY-MM-DD（含当天）"
    )
    p_export.add_argument(
        "--end", required=True, help="结束日期 YYYY-MM-DD（含当天）"
    )
    p_export.add_argument(
        "--out",
        default=None,
        help="输出目录（默认 research/{inst}-{bar}）",
    )
    p_export.add_argument(
        "--run-id",
        dest="run_id",
        default=None,
        help="可选：导出该回测的成交与指标，而不是本地 fills",
    )
    p_export.add_argument(
        "--config", default="config/config.toml", help="配置文件路径"
    )

    return parser


def main(argv: list[str] | None = None) -> int:
    """CLI 主入口。返回退出码。"""
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.command == "backup":
        return backup_main(args)
    elif args.command == "restore":
        return restore_main(args)
    elif args.command == "research":
        return research_main(args)
    elif args.command == "doctor":
        return doctor_main(args)
    elif args.command == "fetch-candles":
        return fetch_candles_main(args)
    elif args.command == "fetch-instruments":
        return fetch_instruments_main(args)
    elif args.command == "fetch-funding-rates":
        return fetch_funding_rates_main(args)
    elif args.command == "fetch-open-interest":
        return fetch_open_interest_main(args)
    elif args.command == "validate-data":
        return validate_data_main(args)
    elif args.command == "check-data":
        return check_data_main(args)
    elif args.command == "check-live":
        return check_live_main(args)
    elif args.command == "align-data":
        return align_data_main(args)
    elif args.command == "serve-webhook":
        return serve_webhook_main(args)
    elif args.command == "run-worker":
        return run_worker_main(args)
    elif args.command == "run-engine":
        return run_engine_main(args)
    elif args.command == "prune":
        return prune_main(args)
    elif args.command == "backtest":
        return backtest_main(args)
    elif args.command == "optimize":
        return optimize_main(args)
    elif args.command == "walk-forward":
        return walk_forward_main(args)
    elif args.command == "simulate-alert":
        return simulate_alert_main(args)
    elif args.command == "paper-report":
        return paper_report_main(args)
    elif args.command == "monitor":
        return monitor_main(args)
    elif args.command == "kill-switch":
        return kill_switch_main(args)
    elif args.command == "dashboard":
        return dashboard_main(args)
    elif args.command == "supervisor":
        return supervisor_main(args)
    elif args.command == "sync-candles":
        return sync_candles_main(args)
    elif args.command == "test-email":
        return test_email_main(args)
    else:  # pragma: no cover
        parser.print_help()
        return 1


if __name__ == "__main__":
    sys.exit(main())
