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
import math
import subprocess
import time
from collections.abc import Callable
from decimal import Decimal
from pathlib import Path

from pydantic import BaseModel, Field

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
from quant.foundation.config import Settings
from quant.foundation.logger import get_logger
from quant.models.bar import Bar
from quant.state.sqlite_store import SQLiteStore
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

MAX_COMBOS = 500          # 单次寻优的网格组合上限
MAX_EQUITY_POINTS = 2000  # 权益曲线降采样后最多点数
MAX_TRADES = 300          # 单次回测返回的成交明细上限

# 越小越好的指标，排序时反向
ASCENDING_METRICS = frozenset({"max_drawdown", "avg_holding_bars"})

METRIC_KEYS = (
    "total_return",
    "annual_return",
    "sharpe",
    "max_drawdown",
    "win_rate",
    "profit_factor",
    "trade_count",
    "avg_holding_bars",
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


class BacktestService:
    """看板回测服务：策略清单 + 单次回测 + 网格寻优。"""

    def __init__(self, settings: Settings, store: SQLiteStore | None = None) -> None:
        self._settings = settings
        self._store = store
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
        if combos > MAX_COMBOS:
            raise BacktestRequestError(
                f"参数组合过多：{combos} 组 > 上限 {MAX_COMBOS}，请缩小网格"
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
        )
        try:
            results = optimizer.search(bars, metric=req.metric)
        except ValueError as e:
            raise BacktestRequestError(str(e)) from e

        results.sort(
            key=lambda item: item[1][req.metric],
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


def _clean_metrics(metrics: dict[str, float]) -> dict[str, float | None]:
    """inf/nan 不是合法 JSON，统一转 null 让页面显示为「—」。"""
    return {
        key: (value if math.isfinite(value) else None)
        for key, value in metrics.items()
    }


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
    "METRIC_KEYS",
    "OptimizeRequest",
]
