"""看板内的回测 / 参数寻优（只计算，不落盘）。

与命令行同口径：区间解析、预热加载、成交模拟、网格搜索全部复用
`quant.backtest` 下的现有实现，这里只负责：

- 把 HTTP 入参转成回测参数
- 在 `asyncio.to_thread` 里跑计算，避免阻塞事件循环
- 用信号量串行化，避免多个请求同时打满 CPU
- 把结果裁成适合页面渲染的 JSON（权益曲线降采样、成交条数上限）

不写 CSV/HTML、不写 SQLite、不改 Redis，保持 dashboard 只读。
"""
from __future__ import annotations

import asyncio
import json
import math
import subprocess
import time
from collections.abc import Callable
from decimal import Decimal
from pathlib import Path

from pydantic import BaseModel, Field
from redis.asyncio import Redis

from quant.backtest.engine import Backtest, BacktestResult
from quant.backtest.optimizer import GridOptimizer
from quant.backtest.runner import (
    BacktestSetupError,
    build_matcher,
    load_bars,
    parse_grid,
    parse_params,
    parse_range,
)
from quant.backtest.walkforward import SegmentResult, WalkForward, WalkForwardResult
from quant.foundation.config import Settings
from quant.foundation.logger import get_logger
from quant.models.bar import Bar
from quant.state.sqlite_store import SQLiteStore
from quant.storage.validator import DataValidator
from quant.strategies.bindings import Binding, ensure_seed, write_cache
from quant.strategies.loader import StrategyConfigError
from quant.strategies.params import (
    ParamError,
    coerce_value,
    format_value,
    param_spec_payload,
    resolve_params,
    resolve_strategy,
)

log = get_logger("dashboard.backtest")

MAX_EQUITY_POINTS = 2000  # 权益曲线降采样后最多点数
MAX_TRADES = 300          # 单次回测返回的成交明细上限
MAX_N_JOBS = 8            # 寻优并行度上限，避免小机器被进程池拖死

# 越小越好的指标，排序时反向
ASCENDING_METRICS = frozenset(
    {
        "max_drawdown",
        "avg_holding_bars",
        "max_consecutive_losses",
        "max_consecutive_loss_amount",
    }
)

METRIC_KEYS = (
    "total_return",
    "annual_return",
    "sharpe",
    "max_drawdown",
    "win_rate",
    "profit_factor",
    "trade_count",
    "avg_holding_bars",
    # MS4-C §7 扩展指标（monthly_returns 是序列，不能作为排序指标）
    "calmar",
    "sortino",
    "max_consecutive_losses",
    "max_consecutive_loss_amount",
    "exposure_ratio",
    "turnover",
)


class BacktestRequestError(Exception):
    """入参或数据不满足回测条件，消息可直接展示给页面。"""


class StrategyParamError(Exception):
    """参数覆盖写入失败（未知字段、类型/越界、无 store），消息可直接展示。"""


class ParamUpdateRequest(BaseModel):
    """参数覆盖入参：只提交要改的键，未提交的保持原样。"""

    values: dict[str, object] = Field(default_factory=dict)
    reason: str = ""
    by: str = "owner"


class BacktestRequest(BaseModel):
    """单次回测入参。"""

    strategy: str
    start: str
    end: str
    inst: str | None = None
    bar: str = "1H"
    params: str = ""
    warmup_bars: int = Field(200, ge=0, le=5000)


class OptimizeRequest(BacktestRequest):
    """参数寻优入参，比回测多一个网格与排序指标。"""

    grid: str
    metric: str = "sharpe"
    top: int = Field(10, ge=1, le=50)
    n_jobs: int = Field(1, ge=1, le=MAX_N_JOBS)


class WalkforwardRequest(BacktestRequest):
    """滚动 IS/OOS 验证入参（MS4-C §8）。"""

    grid: str
    metric: str = "sharpe"
    is_bars: int = Field(1500, ge=10)
    oos_bars: int = Field(500, ge=10)
    step_bars: int | None = Field(None, ge=1)


class BindingUpdateRequest(BaseModel):
    """启停矩阵写入（MS4-C §4.3）：启用状态与周期都可缺省。

    `bar` 只在该组合尚未绑定时可省略（默认取 `[engine] bar`）；
    已绑定不同周期时改 `bar` 会被拒绝，必须先解绑。
    """

    enabled: bool | None = None
    bar: str | None = None


class BacktestService:
    """看板回测服务：策略清单 + 单次回测 + 网格寻优 + 启停矩阵。"""

    def __init__(
        self,
        settings: Settings,
        store: SQLiteStore | None = None,
        redis: Redis | None = None,
    ) -> None:
        self._settings = settings
        self._store = store
        self._redis = redis
        self._config_dir = Path(settings.engine.config_dir)
        self._lock = asyncio.Semaphore(1)

    # ------------------------------------------------------------------
    # 公开 API
    # ------------------------------------------------------------------

    async def strategies(self) -> list[dict[str, object]]:
        """扫描策略配置目录，返回 [{id, params}]。非法配置跳过并记日志。"""
        return await asyncio.to_thread(self._scan_strategies)

    async def params(self, strategy_id: str) -> dict[str, object]:
        """单个策略的生效参数、来源与可编辑字段（MS4-C §11.1）。"""
        return await asyncio.to_thread(self._strategy_detail, strategy_id)

    async def set_params(
        self,
        strategy_id: str,
        req: ParamUpdateRequest,
    ) -> dict[str, object]:
        """保存参数覆盖并写审计（MS4-C §3.3）。"""
        return await asyncio.to_thread(
            self._set_params, strategy_id, req
        )

    async def param_changes(
        self, strategy_id: str, limit: int = 50, offset: int = 0
    ) -> dict[str, object]:
        return await asyncio.to_thread(
            self._param_changes, strategy_id, limit, offset
        )

    async def bindings(self, strategy_id: str) -> dict[str, object]:
        """启停矩阵（MS4-C §4）：策略 × 品种的当前绑定。"""
        return await asyncio.to_thread(self._bindings_sync, strategy_id)

    async def set_binding(
        self, strategy_id: str, inst_id: str, req: BindingUpdateRequest
    ) -> dict[str, object]:
        """启用/停用或新增绑定，并重建 Redis 缓存让引擎下一轮生效。"""
        payload = await asyncio.to_thread(
            self._set_binding_sync, strategy_id, inst_id, req
        )
        await self._rebuild_binding_cache()
        return payload

    async def run(self, req: BacktestRequest) -> dict[str, object]:
        async with self._lock:
            started = time.monotonic()
            payload, _result = await asyncio.to_thread(self.build, req)
        payload["elapsed_ms"] = int((time.monotonic() - started) * 1000)
        return payload

    async def build_async(
        self,
        req: BacktestRequest,
        should_stop: Callable[[], bool] | None = None,
    ) -> tuple[dict[str, object], BacktestResult]:
        """任务队列用的入口：与同步接口共用同一把信号量，避免打满 CPU。"""
        async with self._lock:
            return await asyncio.to_thread(self.build, req, should_stop)

    async def optimize(self, req: OptimizeRequest) -> dict[str, object]:
        async with self._lock:
            started = time.monotonic()
            payload = await asyncio.to_thread(self._optimize_sync, req)
        payload["elapsed_ms"] = int((time.monotonic() - started) * 1000)
        return payload

    async def walkforward(self, req: WalkforwardRequest) -> dict[str, object]:
        async with self._lock:
            started = time.monotonic()
            payload, _result = await asyncio.to_thread(self._walkforward_sync, req)
        payload["elapsed_ms"] = int((time.monotonic() - started) * 1000)
        return payload

    def check_walkforward(self, req: WalkforwardRequest) -> dict:
        """提交前的廉价校验（策略、日期、网格、组合上限）。"""
        self._walkforward_inputs(req)
        _cls, params, _start_ts, _end_ts = self._resolve(req)
        return params

    async def build_walkforward_async(
        self,
        req: WalkforwardRequest,
        should_stop: Callable[[], bool] | None = None,
    ) -> tuple[dict[str, object], WalkForwardResult]:
        async with self._lock:
            return await asyncio.to_thread(
                self._walkforward_sync, req, should_stop
            )

    # ------------------------------------------------------------------
    # 同步实现（在线程里跑）
    # ------------------------------------------------------------------

    def _scan_strategies(self) -> list[dict[str, object]]:
        out: list[dict[str, object]] = []
        for path in sorted(self._config_dir.glob("*.toml")):
            try:
                strategy_cls, params, sources = resolve_strategy(
                    path.stem,
                    self._config_dir,
                    self._store,
                    resolve_from_db=self._settings.params.resolve_from_db,
                )
            except (StrategyConfigError, ParamError) as e:
                log.warning(f"skip strategy config {path}: {e}")
                continue
            spec = param_spec_payload(strategy_cls)
            out.append(
                {
                    "id": path.stem,
                    "params": _jsonable_params(params),
                    "param_spec": spec,
                    "sources": sources,
                    # 未在 PARAM_SPEC 声明的参数（sr_* / commission_rate）只读
                    "editable": [item["name"] for item in spec],
                }
            )
        return out

    def _strategy_detail(self, strategy_id: str) -> dict[str, object]:
        """从策略清单里挑出单个；不存在时报错而不是返回空对象。"""
        for item in self._scan_strategies():
            if item["id"] == strategy_id:
                return item
        raise StrategyParamError(f"strategy {strategy_id} not found")

    def _set_params(
        self, strategy_id: str, req: ParamUpdateRequest
    ) -> dict[str, object]:
        if self._store is None:
            raise StrategyParamError("store not configured, cannot persist params")
        if not req.values:
            raise StrategyParamError("values is empty")

        try:
            strategy_cls, _params, _sources = resolve_strategy(
                strategy_id, self._config_dir
            )
        except (StrategyConfigError, ParamError) as e:
            raise StrategyParamError(str(e)) from e

        spec_map = strategy_cls.PARAM_SPEC
        unknown = sorted(name for name in req.values if name not in spec_map)
        if unknown:
            raise StrategyParamError(
                f"参数不可在线修改（未在 PARAM_SPEC 声明）：{', '.join(unknown)}"
            )

        parsed: dict[str, object] = {}
        for name, raw in req.values.items():
            try:
                parsed[name] = coerce_value(spec_map[name], raw)
            except ParamError as e:
                raise StrategyParamError(f"{name}: {e}") from e

        current, _ = resolve_params(
            strategy_id,
            self._config_dir,
            self._store,
            resolve_from_db=self._settings.params.resolve_from_db,
        )
        commit = _git_commit()
        now = _now_ms()
        changed: list[str] = []
        for name, value in parsed.items():
            if name in current and current[name] == value:
                continue  # 与当前生效值一致：不写库，也不产生审计噪音
            if self._store.set_strategy_param(
                strategy_id,
                name,
                format_value(spec_map[name], value),
                now,
                reason=req.reason,
                changed_by=req.by.strip() or "owner",
                git_commit=commit,
            ):
                changed.append(name)

        detail = self._strategy_detail(strategy_id)
        detail["changed"] = changed
        return detail

    def _param_changes(
        self, strategy_id: str, limit: int, offset: int
    ) -> dict[str, object]:
        if self._store is None:
            raise StrategyParamError("store not configured")
        rows, total = self._store.list_param_changes(strategy_id, limit, offset)
        return {"changes": rows, "total": total}

    def _bindings_sync(self, strategy_id: str) -> dict[str, object]:
        if self._store is None:
            raise StrategyParamError("store not configured")
        # 未知策略在参数面板就该报错，别让空矩阵看起来像「没绑定」
        self._strategy_detail(strategy_id)
        ensure_seed(self._store, self._settings, _now_ms())
        rows = self._store.list_bindings(strategy_id)
        return {
            "strategy_id": strategy_id,
            "default_bar": self._settings.engine.bar,
            # false 时引擎只跑 [engine] 单组合，页面需要提示矩阵未生效
            "multi_binding": self._settings.engine.multi_binding,
            "bindings": [_binding_payload(row) for row in rows],
        }

    def _set_binding_sync(
        self, strategy_id: str, inst_id: str, req: BindingUpdateRequest
    ) -> dict[str, object]:
        """写绑定；周期冲突必须先解绑（M3-11 的硬要求）。"""
        if self._store is None:
            raise StrategyParamError("store not configured")
        self._strategy_detail(strategy_id)

        inst = inst_id.strip()
        if not inst:
            raise StrategyParamError("inst_id is empty")
        bar = (req.bar or "").strip() or None
        if bar is not None:
            try:
                DataValidator._bar_size_ms(bar)  # type: ignore[attr-defined]
            except ValueError as e:
                raise StrategyParamError(f"bar {bar!r} invalid") from e

        existing = self._store.get_binding(strategy_id, inst)
        if existing is None:
            bar = bar or self._settings.engine.bar
            enabled = True if req.enabled is None else req.enabled
        else:
            if bar is not None and bar != existing["bar"]:
                raise StrategyParamError(
                    f"该组合已绑定 {existing['bar']}，请先解绑再改周期"
                )
            bar = str(existing["bar"])
            enabled = (
                bool(existing["enabled"]) if req.enabled is None else req.enabled
            )

        self._store.upsert_binding(strategy_id, inst, bar, enabled, _now_ms())
        row = self._store.get_binding(strategy_id, inst)
        return {"binding": _binding_payload(row or {})}

    async def _rebuild_binding_cache(self) -> None:
        """重建 Redis 绑定缓存；失败时删键，让引擎回落读 SQLite。"""
        if self._redis is None or self._store is None:
            return
        rows = await asyncio.to_thread(self._store.list_bindings)
        bindings = [
            Binding(
                str(row["strategy_id"]),
                str(row["inst_id"]),
                str(row["bar"]),
                bool(row["enabled"]),
            )
            for row in rows
        ]
        key = self._settings.engine.bindings_key
        try:
            await write_cache(self._redis, key, bindings)
        except Exception as e:  # noqa: BLE001
            log.warning(f"rebuild bindings cache failed: {e}; invalidate key")
            try:
                await self._redis.delete(key)
            except Exception as err:  # noqa: BLE001
                log.warning(f"invalidate bindings cache failed: {err}")

    def check(self, req: BacktestRequest) -> dict:
        """提交任务前的廉价校验（配置存在、日期合法），返回合并后的参数。"""
        _cls, params, _start_ts, _end_ts = self._resolve(req)
        return params

    def build(
        self,
        req: BacktestRequest,
        should_stop: Callable[[], bool] | None = None,
    ) -> tuple[dict[str, object], BacktestResult]:
        """同步跑完一次回测，返回 (页面 JSON, 原始结果)。

        原始结果供任务落库时写产物（equity/trades/report），页面 JSON 只是它的
        裁剪版；两者必须来自同一次计算，否则「看到的」和「存下的」会不一致。
        """
        inst = req.inst or self._settings.engine.inst
        warmup, bars, strategy_cls, params = self._prepare(req, inst)
        try:
            result = Backtest(
                strategy_cls(),
                build_matcher(self._settings),
                initial_cash=self._settings.backtest.initial_cash,
                window_size=self._settings.backtest.window_size,
                params=params,
                warmup_bars=warmup,
            ).run(bars, should_stop=should_stop)
        except ValueError as e:
            raise BacktestRequestError(str(e)) from e

        trades = result.trades[-MAX_TRADES:]
        payload: dict[str, object] = {
            "kind": "backtest",
            "strategy": req.strategy,
            "inst": inst,
            "bar": req.bar,
            "start": req.start,
            "end": req.end,
            "params": _jsonable_params(params),
            "bar_count": len(bars),
            "warmup_count": len(warmup),
            "trade_count": len(result.trades),
            "trades_truncated": len(result.trades) > len(trades),
            "unfilled_signals": len(result.unfilled_signals),
            "final_equity": str(result.equity_curve[-1][1]),
            "metrics": _clean_metrics(result.metrics),
            "equity": _equity_points(result.equity_curve),
            "trades": [
                {
                    "ts": fill.ts,
                    "side": fill.side,
                    "qty": str(fill.qty),
                    "price": str(fill.price),
                    "fee": str(fill.fee),
                }
                for fill in trades
            ],
        }
        return payload, result

    def _optimize_sync(self, req: OptimizeRequest) -> dict[str, object]:
        if req.metric not in METRIC_KEYS:
            raise BacktestRequestError(
                f"unknown metric {req.metric!r}; available: {list(METRIC_KEYS)}"
            )
        inst = req.inst or self._settings.engine.inst
        try:
            grid = parse_grid(req.grid)
        except BacktestSetupError as e:
            raise BacktestRequestError(str(e)) from e

        combos = math.prod(len(values) for values in grid.values())
        limit = self._settings.backtest.max_combos
        if combos > limit:
            raise BacktestRequestError(
                f"参数组合过多：{combos} 组 > 上限 {limit}，请缩小网格"
            )

        warmup, bars, strategy_cls, params = self._prepare(req, inst)
        optimizer = GridOptimizer(
            lambda _params: strategy_cls(),
            grid,
            initial_cash=self._settings.backtest.initial_cash,
            matcher=build_matcher(self._settings),
            window_size=self._settings.backtest.window_size,
            warmup_bars=warmup,
            base_params=params,
            strategy_cls=strategy_cls,
        )
        try:
            results = optimizer.search(bars, metric=req.metric, n_jobs=req.n_jobs)
        except ValueError as e:
            raise BacktestRequestError(str(e)) from e

        results.sort(
            key=lambda item: _sort_rank(item[1][req.metric]),
            reverse=req.metric not in ASCENDING_METRICS,
        )
        top = results[: req.top]
        return {
            "kind": "optimize",
            "strategy": req.strategy,
            "inst": inst,
            "bar": req.bar,
            "start": req.start,
            "end": req.end,
            "grid": grid,
            "metric": req.metric,
            "n_jobs": req.n_jobs,
            "bar_count": len(bars),
            "warmup_count": len(warmup),
            "combo_count": combos,
            "skipped_count": combos - len(results),
            "results": [
                {
                    "rank": index + 1,
                    "params": {
                        name: str(params_[name]) for name in grid
                    },
                    "metrics": _clean_metrics(metrics),
                }
                for index, (params_, metrics) in enumerate(top)
            ],
        }

    def _walkforward_sync(
        self,
        req: WalkforwardRequest,
        should_stop: Callable[[], bool] | None = None,
    ) -> tuple[dict[str, object], WalkForwardResult]:
        inst, grid = self._walkforward_inputs(req)
        warmup, bars, strategy_cls, params = self._prepare(req, inst)

        walker = WalkForward(
            lambda _params: strategy_cls(),
            grid,
            initial_cash=self._settings.backtest.initial_cash,
            matcher=build_matcher(self._settings),
            window_size=self._settings.backtest.window_size,
            # 段内预热与回测口径一致：只用段起点之前的历史
            warmup_bars=req.warmup_bars,
            base_params=params,
            metric=req.metric,
        )
        try:
            result = walker.run(
                warmup + bars,
                is_bars=req.is_bars,
                oos_bars=req.oos_bars,
                step_bars=req.step_bars or req.oos_bars,
                start_index=len(warmup),
                should_stop=should_stop,
            )
        except ValueError as e:
            raise BacktestRequestError(str(e)) from e

        metrics: dict[str, object] = {
            "compounded_return": result.compounded_return,
            "profitable_segments": result.profitable_segments,
            "avg_oos_sharpe": result.avg_oos_sharpe,
            "total_oos_trades": result.total_oos_trades,
            "modal_params": (
                json.dumps(_jsonable_params(result.modal_params), sort_keys=True)
                if result.segments
                else None
            ),
        }
        return (
            {
                "kind": "walkforward",
                "strategy": req.strategy,
                "inst": inst,
                "bar": req.bar,
                "start": req.start,
                "end": req.end,
                "params": _jsonable_params(params),
                "grid": grid,
                "metric": req.metric,
                "is_bars": req.is_bars,
                "oos_bars": req.oos_bars,
                "step_bars": req.step_bars or req.oos_bars,
                "bar_count": len(bars),
                "warmup_count": len(warmup),
                "segment_count": len(result.segments),
                "metrics": _clean_metrics(metrics),
                "segments": [_segment_payload(item) for item in result.segments],
            },
            result,
        )

    def _walkforward_inputs(
        self, req: WalkforwardRequest
    ) -> tuple[str, dict[str, list]]:
        """校验并与寻优共用同一套指标 / 组合上限约束。"""
        if req.metric not in METRIC_KEYS:
            raise BacktestRequestError(
                f"unknown metric {req.metric!r}; available: {list(METRIC_KEYS)}"
            )
        inst = req.inst or self._settings.engine.inst
        try:
            grid = parse_grid(req.grid)
        except BacktestSetupError as e:
            raise BacktestRequestError(str(e)) from e
        if not grid:
            raise BacktestRequestError("grid is empty")

        combos = math.prod(len(values) for values in grid.values())
        limit = self._settings.backtest.max_combos
        if combos > limit:
            raise BacktestRequestError(
                f"参数组合过多：{combos} 组 > 上限 {limit}，请缩小网格"
            )
        return inst, grid

    def _resolve(self, req: BacktestRequest) -> tuple[type, dict, int, int]:
        """解析策略类、合并参数与回测区间；不加载 K 线。"""
        try:
            # 先校验策略配置（廉价），避免数据不足的错误盖住配置写错
            strategy_cls, params, _sources = resolve_strategy(
                req.strategy,
                self._config_dir,
                self._store,
                overrides=parse_params(req.params),
                resolve_from_db=self._settings.params.resolve_from_db,
            )
            start_ts, end_ts = parse_range(req.start, req.end)
        except (BacktestSetupError, StrategyConfigError, ParamError) as e:
            raise BacktestRequestError(str(e)) from e
        return strategy_cls, params, start_ts, end_ts

    def _prepare(
        self, req: BacktestRequest, inst: str
    ) -> tuple[list[Bar], list[Bar], type, dict]:
        """解析区间、加载预热与回测段、读取策略类与参数（toml→DB→请求）。"""
        strategy_cls, params, start_ts, end_ts = self._resolve(req)
        try:
            warmup, bars = load_bars(
                self._settings,
                inst,
                req.bar,
                start_ts,
                end_ts,
                req.warmup_bars,
            )
        except BacktestSetupError as e:
            raise BacktestRequestError(str(e)) from e
        return warmup, bars, strategy_cls, params


def _jsonable_params(params: dict) -> dict:
    """Decimal 不是合法 JSON，转字符串让页面按原样展示。"""
    return {
        key: (str(value) if isinstance(value, Decimal) else value)
        for key, value in params.items()
    }


def _segment_payload(item: SegmentResult) -> dict[str, object]:
    """逐段 IS/OOS 结果，列名与 segments.parquet 保持一致。"""
    return {
        "index": item.segment.index,
        "is_start": item.is_range[0],
        "is_end": item.is_range[1],
        "oos_start": item.oos_range[0],
        "oos_end": item.oos_range[1],
        "params": _jsonable_params(item.params),
        "is_metrics": _clean_metrics(item.is_metrics),
        "oos_metrics": _clean_metrics(item.oos_metrics),
    }


def _binding_payload(row: dict) -> dict[str, object]:
    """绑定行转页面结构；`enabled` 统一成 bool，SQLite 存的是 0/1。"""
    return {
        "strategy_id": row.get("strategy_id"),
        "inst_id": row.get("inst_id"),
        "bar": row.get("bar"),
        "enabled": bool(row.get("enabled", 0)),
        "updated_at": row.get("updated_at"),
    }


def _sort_rank(value: float | int | None) -> float:
    """None 视为最差，保证排序不因 None 抛 TypeError。"""
    return float("-inf") if value is None else float(value)


def _now_ms() -> int:
    return int(time.time() * 1000)


def _git_commit() -> str | None:
    """当前代码版本，供参数审计追溯（MS4-C §3.3）；取不到就留空。"""
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            capture_output=True,
            text=True,
            timeout=2,
            check=False,
            cwd=Path(__file__).resolve().parents[3],
        )
    except (OSError, subprocess.SubprocessError):
        return None
    commit = out.stdout.strip()
    return commit or None


def _clean_metrics(
    metrics: dict[str, float | int | str | None],
) -> dict[str, float | int | str | None]:
    """inf/nan 不是合法 JSON，统一转 null 让页面显示为「—」。

    非数值（如 monthly_returns 的 JSON 文本）原样保留。
    """
    cleaned: dict[str, float | int | str | None] = {}
    for key, value in metrics.items():
        if value is None or isinstance(value, (str, int)):
            cleaned[key] = value
        elif math.isfinite(value):
            cleaned[key] = float(value)
        else:
            cleaned[key] = None
    return cleaned


def _equity_points(
    curve: list[tuple[int, object]], limit: int = MAX_EQUITY_POINTS
) -> list[dict[str, object]]:
    """权益曲线降采样：超出上限时等步取点，并保证保留最后一根。"""
    if not curve:
        return []
    if len(curve) <= limit:
        selected = curve
    else:
        step = math.ceil(len(curve) / limit)
        selected = curve[::step]
        if selected[-1][0] != curve[-1][0]:
            selected = [*selected, curve[-1]]
    return [{"ts": ts, "equity": str(equity)} for ts, equity in selected]


__all__ = [
    "ASCENDING_METRICS",
    "BacktestRequest",
    "BacktestRequestError",
    "BacktestService",
    "BindingUpdateRequest",
    "METRIC_KEYS",
    "OptimizeRequest",
    "ParamUpdateRequest",
    "StrategyParamError",
    "WalkforwardRequest",
]
