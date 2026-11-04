"""回测任务队列（MS4-C §5.2 / §10.4）。

设计取舍：

- **单消费者**：回测是 CPU 密集，并发只会互相拖慢；并行应该发生在寻优内部的
  进程池，而不是任务之间。
- **状态写 SQLite，产物写文件**：状态是权威（重启后能查到「跑过什么」），
  产物（equity/trades/report）落在 `data/backtests/{run_id}/`，库里只登记元数据。
- **取消粒度是一根 bar**：`Backtest.run(should_stop=...)` 每根 bar 检查一次，
  成本相对回测本身可忽略；取消中的任务不写指标与产物，避免留下半份结果。
- **重启不自动重跑**：上次进程留下的 running 一律标 failed（`error="interrupted"`），
  因为重跑会生成第二份产物，静默覆盖用户对上一次结果的预期。
"""
from __future__ import annotations

import asyncio
import json
import math
import time
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import pandas as pd

from quant.backtest.batch import BatchResult
from quant.backtest.engine import BacktestResult
from quant.backtest.report import ReportGenerator
from quant.backtest.runner import parse_grid, parse_range
from quant.backtest.walkforward import WalkForwardResult
from quant.dashboard.backtest import (
    BacktestRequest,
    BacktestRequestError,
    BacktestService,
    BatchRequest,
    WalkforwardRequest,
)
from quant.foundation.config import Settings
from quant.foundation.gitinfo import git_commit
from quant.foundation.logger import get_logger
from quant.monitor.alert_service import AlertEvent, AlertService
from quant.state.sqlite_store import SQLiteStore

log = get_logger("dashboard.tasks")

PENDING = "pending"
RUNNING = "running"
DONE = "done"
FAILED = "failed"
CANCELED = "canceled"

TERMINAL_STATUSES = frozenset({DONE, FAILED, CANCELED})

#: 单次对比最多 run 数：ids 来自查询参数，不设上限会拉爆产物/指标读取（P1）
COMPARE_MAX_RUNS = 8

#: 指标表里以整数存的列，其余数值列统一按文本存（避免浮点误差扩散到审计）。
_INT_METRIC_COLUMNS = frozenset(
    {
        "trade_count",
        "max_consecutive_losses",
        "profitable_segments",
        "total_oos_trades",
    }
)

#: 需要反序列化回对象的 JSON 列（前端拿到的是结构而不是字符串）
_JSON_METRIC_COLUMNS = frozenset({"monthly_returns", "modal_params"})


class RunNotFound(KeyError):
    """run_id 不存在。"""


class TaskRunner:
    """回测任务队列：登记 → 执行 → 落库。"""

    def __init__(
        self,
        store: SQLiteStore,
        settings: Settings,
        service: BacktestService,
        alerts: AlertService | None = None,
    ) -> None:
        self._store = store
        self._settings = settings
        self._service = service
        # 回测失败进告警（M9-11）；None 表示调用方没接告警（单测常用）
        self._alerts = alerts
        self._queue: asyncio.Queue[tuple[str, BacktestRequest]] = asyncio.Queue()
        self._worker: asyncio.Task | None = None
        self._canceled: set[str] = set()

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------

    async def recover(self) -> int:
        """启动时把上次残留的 pending/running 任务标为 failed。"""
        count = self._store.fail_interrupted_runs(_now_ms())
        if count:
            log.warning(f"marked {count} interrupted backtest run(s) as failed")
        return count

    async def close(self) -> None:
        if self._worker is not None:
            self._worker.cancel()
            try:
                await self._worker
            except asyncio.CancelledError:
                pass
            self._worker = None

    # ------------------------------------------------------------------
    # 提交 / 取消
    # ------------------------------------------------------------------

    async def submit(self, req: BacktestRequest) -> str:
        """登记 pending 并唤醒消费者，立即返回 run_id。

        Raises:
            BacktestRequestError: 策略配置或日期非法（不排队，直接报错）
        """
        params = await asyncio.to_thread(self._service.check, req)
        inst = req.inst or self._settings.engine.inst
        start_ts, end_ts = _range_of(req)
        now = _now_ms()
        run_id = self._next_run_id(req.strategy, inst, now)

        self._store.create_backtest_run(
            run_id,
            strategy_id=req.strategy,
            inst_id=inst,
            bar=req.bar,
            start_ts=start_ts,
            end_ts=end_ts,
            params=json.dumps(_jsonable(params), sort_keys=True),
            kind="backtest",
            status=PENDING,
            created_at=now,
            warmup_bars=req.warmup_bars,
            git_commit=git_commit(),
        )
        await self._queue.put((run_id, req))
        self._ensure_worker()
        return run_id

    async def submit_walkforward(self, req: WalkforwardRequest) -> str:
        """登记 walk-forward 任务；params 存「基础参数 + 网格 + 切分」便于重跑。"""
        base = await asyncio.to_thread(self._service.check_walkforward, req)
        inst = req.inst or self._settings.engine.inst
        start_ts, end_ts = _range_of(req)
        now = _now_ms()
        run_id = self._next_run_id(req.strategy, inst, now)

        self._store.create_backtest_run(
            run_id,
            strategy_id=req.strategy,
            inst_id=inst,
            bar=req.bar,
            start_ts=start_ts,
            end_ts=end_ts,
            params=json.dumps(
                {
                    "base": _jsonable(base),
                    "grid": _grid_payload(parse_grid(req.grid)),
                    "metric": req.metric,
                    "is_bars": req.is_bars,
                    "oos_bars": req.oos_bars,
                    "step_bars": req.step_bars,
                },
                sort_keys=True,
            ),
            kind="walkforward",
            status=PENDING,
            created_at=now,
            warmup_bars=req.warmup_bars,
            git_commit=git_commit(),
        )
        await self._queue.put((run_id, req))
        self._ensure_worker()
        return run_id

    async def submit_batch(self, req: BatchRequest) -> str:
        """登记批量回测任务；params 存「基础参数 + 网格 + 品种/周期」。"""
        base = await asyncio.to_thread(self._service.check_batch, req)
        start_ts, end_ts = parse_range(req.start, req.end)
        # 多品种共用一个 run_id：inst 列放不下组合，用 "batch" 占位，
        # 具体品种与周期在 params 与产物里
        now = _now_ms()
        run_id = self._next_run_id(req.strategy, "batch", now)

        self._store.create_backtest_run(
            run_id,
            strategy_id=req.strategy,
            inst_id=",".join(_clean_list(req.insts)),
            bar=",".join(_clean_list(req.bars)),
            start_ts=start_ts,
            end_ts=end_ts,
            params=json.dumps(
                {
                    "base": _jsonable(base),
                    "grid": (
                        _grid_payload(parse_grid(req.grid))
                        if req.grid.strip()
                        else {}
                    ),
                    "insts": _clean_list(req.insts),
                    "bars": _clean_list(req.bars),
                    "metric": req.metric,
                },
                sort_keys=True,
            ),
            kind="batch",
            status=PENDING,
            created_at=now,
            warmup_bars=req.warmup_bars,
            git_commit=git_commit(),
        )
        await self._queue.put((run_id, req))
        self._ensure_worker()
        return run_id

    async def rerun(self, run_id: str) -> str:
        """按旧任务的参数与区间重跑，返回新 run_id（旧结果保留）。"""
        row = self._require(run_id)
        req = _request_from_row(row)
        if isinstance(req, BatchRequest):
            return await self.submit_batch(req)
        if isinstance(req, WalkforwardRequest):
            return await self.submit_walkforward(req)
        return await self.submit(req)

    async def cancel(self, run_id: str) -> str:
        """pending 直接取消；running 置标志，下一根 bar 退出。"""
        row = self._require(run_id)
        status = row["status"]
        if status in TERMINAL_STATUSES:
            return status
        if status == PENDING:
            # 队列里那条任务被取出后会发现状态已不是 pending 而直接跳过
            self._store.update_run_status(run_id, CANCELED, finished_at=_now_ms())
            return CANCELED
        self._canceled.add(run_id)
        return "canceling"

    # ------------------------------------------------------------------
    # 查询
    # ------------------------------------------------------------------

    def get(self, run_id: str) -> dict[str, Any]:
        row = self._require(run_id)
        metrics = self._store.get_backtest_metrics(run_id) or {}
        metrics.pop("run_id", None)
        return {
            **_run_payload(row),
            "metrics": _metrics_payload(metrics),
            "artifacts": [
                {
                    "kind": item["kind"],
                    "size": item["size"],
                    "created_at": item["created_at"],
                }
                for item in self._store.list_backtest_artifacts(run_id)
            ],
        }

    def list_runs(
        self,
        limit: int = 50,
        offset: int = 0,
        *,
        strategy_id: str | None = None,
        inst_id: str | None = None,
        bar: str | None = None,
        status: str | None = None,
        kind: str | None = None,
        since_ms: int | None = None,
        until_ms: int | None = None,
    ) -> dict[str, Any]:
        rows, total = self._store.list_backtest_runs(
            limit,
            offset,
            strategy_id=strategy_id,
            inst_id=inst_id,
            bar=bar,
            status=status,
            kind=kind,
            since_ms=since_ms,
            until_ms=until_ms,
        )
        return {
            "total": total,
            "runs": [
                {
                    **_run_payload(row),
                    "metrics": _metrics_payload(
                        self._store.get_backtest_metrics(row["run_id"]) or {}
                    ),
                }
                for row in rows
            ],
        }

    def compare(self, run_ids: list[str]) -> dict[str, Any]:
        """多组对比：品种/周期/区间一致且均为已完成 run，避免拿苹果比橘子。"""
        # 重复 id 会产生重复行、污染最优标注，先去重再校验数量（P2）
        run_ids = list(dict.fromkeys(run_ids))
        if len(run_ids) < 2:
            raise ValueError("compare needs at least 2 run ids")
        if len(run_ids) > COMPARE_MAX_RUNS:
            raise ValueError(f"compare allows at most {COMPARE_MAX_RUNS} run ids")
        rows = [self._require(run_id) for run_id in run_ids]
        for row in rows:
            # pending/failed/canceled 没有可比指标，进对比会误导最优标注（P1）
            if row["status"] != DONE:
                raise ValueError(
                    f"{row['run_id']} 状态为 {row['status']}，只有已完成的回测可对比"
                )
        first = rows[0]
        for row in rows[1:]:
            # kind 不同（backtest / walkforward / batch）的 metrics 键完全不同，
            # 同表排名会拿不可比指标当「最优」（P1）
            if row["kind"] != first["kind"]:
                raise ValueError(
                    f"{row['run_id']} 是 {row['kind']}，与 {first['run_id']} "
                    f"的 {first['kind']} 类型不一致，无法对比"
                )
            if row["inst_id"] != first["inst_id"] or row["bar"] != first["bar"]:
                raise ValueError(
                    f"{row['run_id']} ({row['inst_id']} {row['bar']}) 与 "
                    f"{first['run_id']} ({first['inst_id']} {first['bar']}) "
                    f"品种或周期不一致，无法对比"
                )
            # M3-18 要求同品种+同周期+同区间，原先只实现前两项（P1）
            if (row["start_ts"], row["end_ts"]) != (
                first["start_ts"],
                first["end_ts"],
            ):
                raise ValueError(
                    f"{row['run_id']} 与 {first['run_id']} 回测区间不一致，无法对比"
                )
        return {
            "inst_id": first["inst_id"],
            "bar": first["bar"],
            "runs": [
                {
                    **_run_payload(row),
                    "metrics": _metrics_payload(
                        self._store.get_backtest_metrics(row["run_id"]) or {}
                    ),
                }
                for row in rows
            ],
        }

    def equity(self, run_id: str, limit: int = 2000) -> list[dict[str, Any]]:
        """读取降采样后的权益曲线（产物缺失时返回空列表）。"""
        path = self._artifact_path(run_id, "equity")
        if path is None:
            return []
        frame = _read_parquet(path)
        if frame.empty:
            return []
        step = max(1, -(-len(frame) // limit))
        indices = list(range(0, len(frame), step))
        # 等距采样会漏掉最后一点（如 N=100/limit=60 → 止于 98），导致曲线
        # 终点权益与 final_equity 不符、对比图形态失真（P1）。显式补尾点。
        if indices[-1] != len(frame) - 1:
            indices.append(len(frame) - 1)
        sampled = frame.iloc[indices]
        return [
            {"ts": int(row.ts), "equity": str(row.equity), "drawdown": str(row.drawdown)}
            for row in sampled.itertuples()
        ]

    def trades(self, run_id: str, limit: int = 100, offset: int = 0) -> dict[str, Any]:
        path = self._artifact_path(run_id, "trades")
        if path is None:
            return {"total": 0, "trades": []}
        frame = _read_parquet(path)
        total = len(frame)
        # 「最近 N 笔」= 尾部窗口：原先正序取 offset:offset+limit 是最早的
        # N 笔，与 UI 文案相反（P1）。offset 从最新往旧偏移，返回仍按时间正序。
        end = max(0, total - offset)
        start = max(0, end - limit)
        window = frame.iloc[start:end]
        return {
            "total": total,
            "trades": [dict(row) for row in window.to_dict("records")],
        }

    def segments(self, run_id: str) -> list[dict[str, Any]]:
        """读取 walk-forward 逐段明细（非 walkforward 任务返回空列表）。"""
        path = self._artifact_path(run_id, "segments")
        if path is None:
            return []
        frame = _read_parquet(path)
        rows = []
        for item in frame.to_dict("records"):
            rows.append(
                {
                    **item,
                    "params": json.loads(item["params"]),
                    "is_metrics": json.loads(item["is_metrics"]),
                    "oos_metrics": json.loads(item["oos_metrics"]),
                }
            )
        return rows

    def batches(self, run_id: str) -> list[dict[str, Any]]:
        """读取批量回测逐格结果（非 batch 任务返回空列表）。"""
        path = self._artifact_path(run_id, "batch")
        if path is None:
            return []
        frame = _read_parquet(path)
        return [
            {
                **item,
                "params": json.loads(item["params"]),
                "metrics": _metrics_payload(json.loads(item["metrics"])),
            }
            for item in frame.to_dict("records")
        ]

    # ------------------------------------------------------------------
    # 执行
    # ------------------------------------------------------------------

    def _ensure_worker(self) -> None:
        if self._worker is None or self._worker.done():
            self._worker = asyncio.create_task(self._consume())

    async def _consume(self) -> None:
        while True:
            run_id, req = await self._queue.get()
            try:
                await self._execute(run_id, req)
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001 - 单任务失败不能拖垮队列
                log.exception(f"backtest run failed run_id={run_id}: {e}")
                self._store.update_run_status(
                    run_id, FAILED, finished_at=_now_ms(), error=str(e)
                )
                await self._notify_failure(run_id, str(e))
            finally:
                self._queue.task_done()

    async def _execute(self, run_id: str, req: BacktestRequest) -> None:
        row = self._store.get_backtest_run(run_id)
        if row is None or row["status"] != PENDING:
            return  # 排队期间被取消
        self._store.update_run_status(run_id, RUNNING, started_at=_now_ms())

        should_stop = lambda: run_id in self._canceled  # noqa: E731
        try:
            if isinstance(req, BatchRequest):
                payload, result = await self._service.build_batch_async(
                    req, should_stop
                )
            elif isinstance(req, WalkforwardRequest):
                payload, result = await self._service.build_walkforward_async(
                    req, should_stop
                )
            else:
                payload, result = await self._service.build_async(req, should_stop)
        except BacktestRequestError as e:
            self._store.update_run_status(
                run_id, FAILED, finished_at=_now_ms(), error=str(e)
            )
            await self._notify_failure(run_id, str(e))
            return

        if run_id in self._canceled:
            self._canceled.discard(run_id)
            self._store.update_run_status(run_id, CANCELED, finished_at=_now_ms())
            return

        now = _now_ms()
        try:
            artifacts = await asyncio.to_thread(
                self._write_artifacts, run_id, result
            )
        except OSError as e:
            # 磁盘写不进去时宁可整单 failed，也不要落一份半截产物
            error = f"artifact write failed: {e}"
            self._store.update_run_status(
                run_id, FAILED, finished_at=now, error=error
            )
            await self._notify_failure(run_id, error)
            return

        # 指标 + 产物清单 + 状态必须一次事务写入：原先三步各自 commit，
        # 中途失败会留下「有产物无指标」或「有指标无 artifact」的中间态（P1）
        self._store.complete_backtest_run(
            run_id,
            _metric_row(payload),
            [(kind, str(path), _size(path)) for kind, path in artifacts],
            _now_ms(),
        )

    async def _notify_failure(self, run_id: str, error: str) -> None:
        """回测失败进告警（M9-11）。

        业务 ID 是 run_id：同一个任务只在失败分支里发布一次，不重复告警；
        告警链路本身出错不能影响任务状态落库，故整体吞异常。
        """
        if self._alerts is None:
            return
        try:
            row = await asyncio.to_thread(self._store.get_backtest_run, run_id)
            await self._alerts.publish(
                AlertEvent(
                    rule="backtest_failed",
                    level="ERROR",
                    message=f"回测任务失败 {run_id}: {error[:200]}",
                    ts=_now_ms(),
                    inst_id=(row or {}).get("inst_id"),
                    strategy_id=(row or {}).get("strategy_id"),
                )
            )
        except Exception as e:  # noqa: BLE001
            log.warning(f"publish backtest_failed alert failed: {e}")

    def _write_artifacts(
        self,
        run_id: str,
        result: BacktestResult | WalkForwardResult | BatchResult,
    ) -> list[tuple[str, Path]]:
        if isinstance(result, BatchResult):
            return self._write_batch_artifacts(run_id, result)
        if isinstance(result, WalkForwardResult):
            return self._write_walkforward_artifacts(run_id, result)
        return self._write_backtest_artifacts(run_id, result)

    def _write_walkforward_artifacts(
        self, run_id: str, result: WalkForwardResult
    ) -> list[tuple[str, Path]]:
        """逐段 IS/OOS 明细；没有权益曲线，故不产出 equity/trades/report。"""
        out_dir = Path(self._settings.backtest.artifacts_dir) / run_id
        out_dir.mkdir(parents=True, exist_ok=True)

        segments_path = out_dir / "segments.parquet"
        pd.DataFrame(
            [
                {
                    "index": item.segment.index,
                    "is_start": item.is_range[0],
                    "is_end": item.is_range[1],
                    "oos_start": item.oos_range[0],
                    "oos_end": item.oos_range[1],
                    "params": json.dumps(_jsonable(item.params), sort_keys=True),
                    "is_metrics": json.dumps(_jsonable(item.is_metrics)),
                    "oos_metrics": json.dumps(_jsonable(item.oos_metrics)),
                }
                for item in result.segments
            ],
            columns=[
                "index",
                "is_start",
                "is_end",
                "oos_start",
                "oos_end",
                "params",
                "is_metrics",
                "oos_metrics",
            ],
        ).to_parquet(segments_path, index=False)
        return [("segments", segments_path)]

    def _write_batch_artifacts(
        self, run_id: str, result: BatchResult
    ) -> list[tuple[str, Path]]:
        """逐格结果：成功行带指标，失败行带原因，同一张表便于页面直接渲染。"""
        out_dir = Path(self._settings.backtest.artifacts_dir) / run_id
        out_dir.mkdir(parents=True, exist_ok=True)

        batch_path = out_dir / "batch.parquet"
        pd.DataFrame(
            [
                {
                    "inst": cell.inst,
                    "bar": cell.bar,
                    "params": json.dumps(_jsonable(cell.params), sort_keys=True),
                    "metrics": json.dumps(_jsonable(dict(cell.metrics))),
                    "error": cell.error,
                }
                for cell in (*result.cells, *result.failed)
            ],
            columns=["inst", "bar", "params", "metrics", "error"],
        ).to_parquet(batch_path, index=False)
        return [("batch", batch_path)]

    def _write_backtest_artifacts(
        self, run_id: str, result: BacktestResult
    ) -> list[tuple[str, Path]]:
        out_dir = Path(self._settings.backtest.artifacts_dir) / run_id
        out_dir.mkdir(parents=True, exist_ok=True)

        equity_path = out_dir / "equity.parquet"
        pd.DataFrame(
            [
                {
                    "ts": ts,
                    "equity": str(equity),
                    "drawdown": (
                        str(result.drawdown_curve[i][1])
                        if i < len(result.drawdown_curve)
                        else "0"
                    ),
                }
                for i, (ts, equity) in enumerate(result.equity_curve)
            ]
        ).to_parquet(equity_path, index=False)

        trades_path = out_dir / "trades.parquet"
        pd.DataFrame(
            [
                {
                    "ts": fill.ts,
                    "inst_id": fill.inst_id,
                    "side": fill.side,
                    "qty": str(fill.qty),
                    "price": str(fill.price),
                    "fee": str(fill.fee),
                }
                for fill in result.trades
            ],
            columns=["ts", "inst_id", "side", "qty", "price", "fee"],
        ).to_parquet(trades_path, index=False)

        report_path = out_dir / "report.html"
        ReportGenerator().to_html(result, report_path)
        return [
            ("equity", equity_path),
            ("trades", trades_path),
            ("report", report_path),
        ]

    # ------------------------------------------------------------------
    # 辅助
    # ------------------------------------------------------------------

    def _require(self, run_id: str) -> dict:
        row = self._store.get_backtest_run(run_id)
        if row is None:
            raise RunNotFound(run_id)
        return row

    def _next_run_id(self, strategy: str, inst: str, now_ms: int) -> str:
        date = datetime.fromtimestamp(now_ms / 1000, UTC).strftime("%Y%m%d")
        prefix = f"{date}-{strategy}-{inst}-"
        seq = self._store.count_runs_with_prefix(prefix) + 1
        return f"{prefix}{seq:03d}"

    def _artifact_path(self, run_id: str, kind: str) -> Path | None:
        for item in self._store.list_backtest_artifacts(run_id):
            if item["kind"] == kind:
                path = Path(item["path"])
                return path if path.is_file() else None
        return None


def _now_ms() -> int:
    return int(time.time() * 1000)


def _range_of(req: BacktestRequest) -> tuple[int, int]:
    return parse_range(req.start, req.end)


def _jsonable(params: dict) -> dict:
    return {
        key: (str(value) if isinstance(value, Decimal) else value)
        for key, value in params.items()
    }


def _request_from_row(row: dict) -> BacktestRequest:
    data = json.loads(row["params"] or "{}")
    common = {
        "strategy": row["strategy_id"],
        "start": _date(row["start_ts"]),
        "end": _date(row["end_ts"] - 1),
        "inst": row["inst_id"],
        "bar": row["bar"],
        "warmup_bars": row["warmup_bars"],
    }
    if row["kind"] == "batch":
        # batch 的 params 存的是「基础参数 + 网格 + 品种/周期」结构
        return BatchRequest(
            strategy=row["strategy_id"],
            insts=list(data.get("insts") or []),
            bars=list(data.get("bars") or []),
            start=common["start"],
            end=common["end"],
            params=data.get("base") or {},
            grid=_grid_text(data.get("grid") or {}),
            metric=data.get("metric", "sharpe"),
            warmup_bars=row["warmup_bars"],
        )
    if row["kind"] == "walkforward":
        # walkforward 的 params 存的是「基础参数 + 网格 + 切分」结构
        return WalkforwardRequest(
            **common,
            params=data.get("base") or {},
            grid=_grid_text(data.get("grid") or {}),
            metric=data.get("metric", "sharpe"),
            is_bars=int(data.get("is_bars", 1500)),
            oos_bars=int(data.get("oos_bars", 500)),
            step_bars=data.get("step_bars"),
        )
    # 直接传原 JSON 对象：原先拼成 "k=v" 再由 parse_params 猜类型，
    # Decimal 会变 float、字符串 "10" 会变 int、值含 ;/= 直接崩（P1）
    return BacktestRequest(**common, params=data)


def _grid_text(grid: dict) -> str:
    return ";".join(
        f"{name}={','.join(str(value) for value in values)}"
        for name, values in grid.items()
    )


def _grid_payload(grid: dict) -> dict:
    return {name: list(values) for name, values in grid.items()}


def _clean_list(values: list[str]) -> list[str]:
    """去空白与重复、保持顺序（与 `batch.expand_cells` 的规则一致）。"""
    return list(dict.fromkeys(item.strip() for item in values if item.strip()))


def _date(ts: int) -> str:
    return datetime.fromtimestamp(ts / 1000, UTC).strftime("%Y-%m-%d")


def _metric_row(payload: dict) -> dict:
    row: dict[str, Any] = {}
    for key, value in (payload.get("metrics") or {}).items():
        if value is None:
            row[key] = None
        elif key in _INT_METRIC_COLUMNS:
            row[key] = int(value)
        else:
            row[key] = str(value)
    return row


def _metrics_payload(row: dict) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in row.items():
        if key == "run_id":
            continue
        # None 保留在键位里（inf/NaN 置空），前端统一渲染为 "—"
        if value is None:
            out[key] = None
        elif key in _INT_METRIC_COLUMNS:
            out[key] = int(value)
        elif key in _JSON_METRIC_COLUMNS:
            try:
                out[key] = json.loads(value)
            except (TypeError, ValueError):
                out[key] = value
        else:
            try:
                number = float(value)
            except (TypeError, ValueError):
                out[key] = value
            else:
                # inf/nan 不是合法 JSON：Starlette 的 JSONResponse 会直接
                # 抛 ValueError → /runs、/compare、/runs/{id} 全 500（P1）
                out[key] = number if math.isfinite(number) else None
    return out


def _run_payload(row: dict) -> dict[str, Any]:
    return {
        "run_id": row["run_id"],
        "strategy": row["strategy_id"],
        "inst": row["inst_id"],
        "bar": row["bar"],
        "start": _date(row["start_ts"]),
        "end": _date(row["end_ts"] - 1),
        "params": json.loads(row["params"] or "{}"),
        "warmup_bars": row["warmup_bars"],
        "kind": row["kind"],
        "status": row["status"],
        "created_at": row["created_at"],
        "started_at": row["started_at"],
        "finished_at": row["finished_at"],
        "error": row["error"],
    }


def _size(path: Path) -> int | None:
    try:
        return path.stat().st_size
    except OSError:
        return None


def _read_parquet(path: Path) -> pd.DataFrame:
    """读产物 parquet；文件损坏/被清理时返回空表并按「无数据」降级（P2）。"""
    try:
        return pd.read_parquet(path)
    except Exception as e:  # noqa: BLE001
        log.warning(f"read parquet failed path={path}: {e}")
        return pd.DataFrame()


__all__ = [
    "CANCELED",
    "DONE",
    "FAILED",
    "PENDING",
    "RUNNING",
    "TERMINAL_STATUSES",
    "RunNotFound",
    "TaskRunner",
]
