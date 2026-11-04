"""research 命令：把研究所需数据导出成独立目录（M3-7）。

用法：
  quant research export --inst BTC-USDT --bar 1H \
      --start 2026-09-01 --end 2026-09-10 --out research/btc
  quant research export ... --run-id <run_id> --out research/btc

产物：
- `bars.parquet`   区间内 K 线（数值保持 Decimal 文本，避免浮点误差）
- `trades.parquet` 成交：`--run-id` 时取该回测产物，否则取本地 fills
- `backtest.json`  该 run 的参数/指标/区间（仅 `--run-id`）
- `manifest.json`  导出时间、条数、过滤条件（便于 notebook 校验数据版本）
"""
from __future__ import annotations

import json
import shutil
import time
from pathlib import Path

import pandas as pd

from quant.backtest.runner import BacktestSetupError, parse_range
from quant.foundation.config import Settings
from quant.foundation.logger import get_logger, setup_logger
from quant.state.sqlite_store import SQLiteStore
from quant.storage.bar_storage import BarStorage


def _bars_frame(bars) -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "ts": bar.ts,
                "open": str(bar.open),
                "high": str(bar.high),
                "low": str(bar.low),
                "close": str(bar.close),
                "vol": str(bar.vol),
                "vol_quote": str(bar.vol_quote),
                "confirm": bar.confirm,
                "inst_id": bar.inst_id,
                "bar_size": bar.bar_size,
            }
            for bar in bars
        ]
    )


def _fills_frame(fills) -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "ts": fill.ts,
                "inst_id": fill.inst_id,
                "side": fill.side,
                "qty": str(fill.qty),
                "price": str(fill.price),
                "fee": str(fill.fee),
                "order_id": fill.order_id,
            }
            for fill in fills
        ]
    )


def main(args) -> int:
    """执行导出，返回退出码。"""
    settings = Settings.load(args.config)
    setup_logger(**settings.logging.model_dump())
    log = get_logger("cli.research")

    if getattr(args, "research_command", None) != "export":
        log.error("usage: quant research export --inst ... --bar ... --start ... --end ...")
        return 2

    try:
        start_ts, end_ts = parse_range(args.start, args.end)
    except BacktestSetupError as e:
        log.error(str(e))
        return 1

    out = Path(args.out or f"research/{args.inst}-{args.bar}")
    out.mkdir(parents=True, exist_ok=True)

    bars = BarStorage(settings.storage.data_dir).load_bars(
        args.inst, args.bar, start_ts, end_ts
    )
    if not bars:
        log.error(
            f"no bars for {args.inst} {args.bar} in {args.start}..{args.end}"
        )
        return 1
    bars_path = out / "bars.parquet"
    _bars_frame(bars).to_parquet(bars_path, index=False)

    trades_path: Path | None = None
    backtest_path: Path | None = None
    store = SQLiteStore(settings.state.db_path)
    try:
        if args.run_id:
            artifacts = {
                item["kind"]: Path(item["path"])
                for item in store.list_backtest_artifacts(args.run_id)
            }
            source = artifacts.get("trades")
            if source is not None and source.is_file():
                trades_path = out / "trades.parquet"
                shutil.copy2(source, trades_path)
            detail = store.get_backtest_run(args.run_id)
            metrics = store.get_backtest_metrics(args.run_id) or {}
            backtest_path = out / "backtest.json"
            backtest_path.write_text(
                json.dumps(
                    {"run": detail, "metrics": metrics},
                    ensure_ascii=False,
                    indent=2,
                    default=str,
                ),
                encoding="utf-8",
            )
        else:
            fills = [
                fill
                for fill in store.get_all_fills()
                if fill.inst_id == args.inst and start_ts <= fill.ts < end_ts
            ]
            trades_path = out / "trades.parquet"
            _fills_frame(fills).to_parquet(trades_path, index=False)
    finally:
        store.close()

    manifest = {
        "exported_at": int(time.time() * 1000),
        "inst": args.inst,
        "bar": args.bar,
        "start_ts": start_ts,
        "end_ts": end_ts,
        "bars": len(bars),
        "run_id": args.run_id,
        "files": [
            path.name
            for path in (bars_path, trades_path, backtest_path)
            if path is not None
        ],
    }
    (out / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    log.info(f"research exported dir={out} bars={len(bars)}")
    print(f"导出完成：{out}")
    for name in manifest["files"]:
        print(f"  {name}")
    return 0
