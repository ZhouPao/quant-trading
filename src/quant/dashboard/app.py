"""运维看板 FastAPI 应用（M4 扩展）。

端点：
- GET /api/health    存活探针
- GET /api/overview  账户/指标/心跳/队列/kill-switch 汇总
- GET /api/positions 本地持仓 + monitor 市场快照
- GET /api/orders    最近订单
- GET /api/fills     最近成交
- GET /api/pnl       成交盈亏汇总（FIFO 回合，含手续费）
- GET /api/risk-limits 生效风控限额与当前已用额度（worker 发布 + 本地现算）
- GET /api/reports   模拟盘偏离日报（读取 reports/daily/*.csv）
- GET /api/quality   数据质量报告（可选 inst_id/bar_size，不传返回全部摘要）
- GET /api/alerts    告警列表（SQLite 为准，支持级别/规则/状态/时间区间与分页）
- POST /api/alerts/{id}/ack 确认告警（写操作）
- POST /api/alerts/silence  创建静音（写操作）
- DELETE /api/alerts/silence/{id} 解除静音（写操作）
- GET /api/dead-letters 死信列表（stage/since/pending_only + 分页）
- POST /api/dead-letters/{id}/replay 重放单条死信（写操作）
- POST /api/dead-letters/replay 批量重放（写操作）
- POST /api/dead-letters/{id}/discard 标记丢弃（写操作）
- GET /api/processes 7 个常驻进程健康
- GET /api/equity    权益时序（默认近 24 小时）
- GET /api/engine    策略引擎健康度（心跳/水位/补评估计数）
- GET /api/candles   K 线面板（本地 parquet + 成交 + 持仓均价）
- GET /api/strategies 可用策略清单（含 param_spec / 生效来源 / 可编辑字段）
- GET /api/strategies/{id}/params 单个策略的生效参数与来源
- PUT /api/strategies/{id}/params 保存参数覆盖并写审计（写操作）
- GET /api/strategies/{id}/param-changes 参数变更历史（分页）
- GET /api/strategies/{id}/bindings 启停矩阵（策略 × 品种）
- PUT /api/strategies/{id}/bindings/{inst_id} 启用/停用或新增绑定（写操作）
- POST /api/backtest  提交回测任务，立即返回 run_id（?sync=1 走旧同步路径）
- GET /api/backtest/runs 回测历史（筛选 + 分页）
- GET /api/backtest/compare 多组对比（同品种同周期）
- GET /api/backtest/{run_id} 状态 + 指标 + 产物清单
- GET /api/backtest/{run_id}/equity 降采样权益曲线
- GET /api/backtest/{run_id}/trades 成交明细（分页）
- GET /api/backtest/{run_id}/segments walk-forward 逐段 IS/OOS 明细
- POST /api/backtest/{run_id}/cancel 取消任务（写操作）
- POST /api/backtest/{run_id}/rerun  复制参数重跑（写操作）
- POST /api/optimize  参数网格寻优（只返回排名，不落盘）
- POST /api/walkforward 滚动 IS/OOS 验证任务，立即返回 run_id（?sync=1 同步）
- POST /api/kill-switch 切换紧急停止标志，激活时默认同时撤销交易所挂单（写操作）
- POST /api/risk/circuits/{inst_id}/resume 人工确认恢复熔断中的品种（写操作）
- POST /api/positions/reduce  手动减仓（写操作，直连 OKX，只减不开）
- POST /api/positions/flatten 一键全平（写操作，直连 OKX）

若前端构建产物存在（默认 web/dist），同一进程在 / 下托管静态页面。
手动减仓/全平以外全部只读；写操作都要求本机 Host、自定义头与
（配置了 token 时的）token。
"""
from __future__ import annotations

from contextlib import asynccontextmanager
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Literal

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from quant.dashboard.backtest import (
    BacktestRequest,
    BacktestRequestError,
    BacktestService,
    BindingUpdateRequest,
    OptimizeRequest,
    ParamUpdateRequest,
    StrategyParamError,
    WalkforwardRequest,
)
from quant.dashboard.manual import ManualTradeError, ManualTrader
from quant.dashboard.service import DashboardService
from quant.dashboard.tasks import RunNotFound, TaskRunner
from quant.foundation.errors import QuantError
from quant.foundation.logger import get_logger
from quant.risk.overrides import OverrideError
from quant.webhook.auth import verify_token

log = get_logger("dashboard.app")

MAX_LIMIT = 200
LOCAL_HOSTS = {"127.0.0.1", "localhost", "::1"}


class KillSwitchRequest(BaseModel):
    action: Literal["on", "off"]
    reason: str = ""
    # 激活时默认同时撤掉交易所挂单；置 false 只拦新开仓（M7-4）
    cancel_orders: bool = True


class ReduceRequest(BaseModel):
    """手动减仓：ratio 与 qty 二选一。"""

    inst_id: str
    ratio: float | None = None
    qty: str | None = None
    reason: str


class FlattenRequest(BaseModel):
    """一键全平：必须手动输入 FLATTEN 才提交。"""

    reason: str
    confirm: str


class AckRequest(BaseModel):
    """告警确认：记录操作人。"""

    by: str = "owner"


class SilenceRequest(BaseModel):
    """创建静音：inst_id 为空表示该规则全部品种。"""

    rule: str
    inst_id: str | None = None
    hours: float = 4
    reason: str = ""
    by: str = "owner"


class ReplayBatchRequest(BaseModel):
    """批量重放死信。"""

    ids: list[str]


class RiskOverrideRequest(BaseModel):
    """阈值在线调整：值用字符串传，避免浮点丢精度。"""

    strategy_id: str
    param: str
    value: str
    reason: str
    by: str = "dashboard"


def _host_only(raw: str) -> str:
    """从 Host 头里取出主机名（兼容 `host:port` 与 `[::1]:port`）。"""
    if raw.startswith("["):
        return raw.split("]")[0].lstrip("[")
    return raw.split(":")[0]


def _guard_write(request: Request, token: str) -> None:
    """写操作守卫：本机 Host + 自定义头 + 可选 token。

    自定义头会让浏览器跨站请求先发预检，而本应用不返回任何 CORS 头，
    因此能挡住「恶意网页静默停掉交易」这类 CSRF；Host 限制则挡 DNS 重绑定。
    """
    host = _host_only(request.headers.get("host", ""))
    if host not in LOCAL_HOSTS:
        raise HTTPException(
            status_code=403, detail="dashboard only accepts local requests"
        )
    if request.headers.get("X-Dashboard-Request") != "1":
        raise HTTPException(
            status_code=400, detail="missing X-Dashboard-Request header"
        )
    if token and not verify_token(
        request.headers.get("X-Dashboard-Token", ""), token
    ):
        raise HTTPException(status_code=401, detail="invalid dashboard token")


def create_app(
    service: DashboardService,
    web_dir: Path | None = None,
    manual: ManualTrader | None = None,
) -> FastAPI:
    backtests = BacktestService(service.settings, service.store, service.redis)
    tasks = TaskRunner(service.store, service.settings, backtests)

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        # 上次进程留下的 running 任务不会自己动，启动时统一标 failed
        await tasks.recover()
        try:
            yield
        finally:
            await tasks.close()

    app = FastAPI(
        title="quant-trading dashboard",
        description="只读运维看板（含紧急停止与手动减仓）",
        docs_url=None,
        redoc_url=None,
        lifespan=lifespan,
    )

    def _trader() -> ManualTrader:
        if manual is None:
            raise HTTPException(
                status_code=503, detail="手动交易未启用（缺少 OKX 客户端）"
            )
        return manual

    async def _cancel_open_orders() -> dict[str, object]:
        """急停时撤掉全部挂单；失败只回报，不改变已置的停止标志。"""
        if manual is None:
            return {"ok": False, "error": "手动交易未启用（缺少 OKX 客户端）"}
        try:
            detail = await manual.cancel_open_orders()
        except Exception as e:  # noqa: BLE001
            log.warning(f"kill switch cancel failed: {e}")
            return {"ok": False, "error": str(e)}
        return {"ok": True, **detail}

    @app.get("/api/health")
    async def health() -> dict:
        return {"status": "ok"}

    @app.get("/api/overview")
    async def overview() -> JSONResponse:
        return JSONResponse(await service.overview())

    @app.get("/api/positions")
    async def positions() -> JSONResponse:
        return JSONResponse(await service.positions())

    @app.get("/api/orders")
    async def orders(
        limit: int = Query(50, ge=1, le=MAX_LIMIT),
    ) -> JSONResponse:
        return JSONResponse(await service.orders(limit))

    @app.get("/api/fills")
    async def fills(
        limit: int = Query(50, ge=1, le=MAX_LIMIT),
    ) -> JSONResponse:
        return JSONResponse(await service.fills(limit))

    @app.get("/api/reports")
    async def reports(limit: int = 20) -> JSONResponse:
        return JSONResponse(await service.reports(min(max(limit, 1), MAX_LIMIT)))

    @app.get("/api/quality")
    async def quality(
        inst_id: str | None = Query(None),
        bar_size: str | None = Query(None),
    ) -> JSONResponse:
        return JSONResponse(await service.quality(inst_id, bar_size))

    @app.get("/api/pnl")
    async def pnl() -> JSONResponse:
        return JSONResponse(await service.pnl())

    @app.get("/api/risk-limits")
    async def risk_limits() -> JSONResponse:
        return JSONResponse(await service.risk_limits())

    @app.get("/api/risk/overrides")
    async def risk_overrides(
        strategy_id: str | None = Query(None),
        limit: int = Query(50, ge=1, le=MAX_LIMIT),
        offset: int = Query(0, ge=0),
        include_revoked: bool = Query(True),
    ) -> JSONResponse:
        return JSONResponse(
            await service.risk_overrides(
                strategy_id=strategy_id,
                limit=limit,
                offset=offset,
                include_revoked=include_revoked,
            )
        )

    @app.post("/api/risk/overrides")
    async def set_risk_override(
        request: Request, req: RiskOverrideRequest
    ) -> JSONResponse:
        _guard_write(request, service.settings.dashboard.token)
        try:
            result = await service.set_risk_override(
                req.strategy_id,
                req.param,
                req.value,
                reason=req.reason,
                by=req.by,
            )
        except OverrideError as e:
            # 字段名/取值/原因不合法属于调用方问题，不是服务故障
            raise HTTPException(status_code=400, detail=str(e)) from e
        return JSONResponse(result)

    @app.delete("/api/risk/overrides/{strategy_id}/{param}")
    async def clear_risk_override(
        request: Request,
        strategy_id: str,
        param: str,
        by: str = Query("dashboard"),
    ) -> JSONResponse:
        _guard_write(request, service.settings.dashboard.token)
        try:
            result = await service.clear_risk_override(
                strategy_id, param, by=by
            )
        except OverrideError as e:
            raise HTTPException(status_code=400, detail=str(e)) from e
        return JSONResponse(result)

    @app.get("/api/alerts")
    async def alerts(
        limit: int = Query(50, ge=1, le=MAX_LIMIT),
        offset: int = Query(0, ge=0),
        level: str | None = Query(None),
        rule: str | None = Query(None),
        state: str | None = Query(None),
        since: int | None = Query(None),
        until: int | None = Query(None),
    ) -> JSONResponse:
        return JSONResponse(
            await service.alerts(
                limit,
                offset,
                level=level,
                rule=rule,
                state=state,
                since_ms=since,
                until_ms=until,
            )
        )

    @app.post("/api/alerts/{alert_id}/ack")
    async def ack_alert(
        request: Request, alert_id: str, req: AckRequest
    ) -> JSONResponse:
        _guard_write(request, service.settings.dashboard.token)
        return JSONResponse(
            await service.ack_alert(alert_id, req.by.strip() or "owner")
        )

    @app.post("/api/alerts/silence")
    async def create_silence(
        request: Request, req: SilenceRequest
    ) -> JSONResponse:
        _guard_write(request, service.settings.dashboard.token)
        return JSONResponse(
            await service.create_silence(
                req.rule,
                inst_id=req.inst_id,
                hours=req.hours,
                reason=req.reason,
                by=req.by.strip() or "owner",
            )
        )

    @app.delete("/api/alerts/silence/{silence_id}")
    async def revoke_silence(
        request: Request, silence_id: str
    ) -> JSONResponse:
        _guard_write(request, service.settings.dashboard.token)
        return JSONResponse(await service.revoke_silence(silence_id))

    @app.get("/api/dead-letters")
    async def dead_letters(
        limit: int = Query(50, ge=1, le=MAX_LIMIT),
        offset: int = Query(0, ge=0),
        stage: str | None = Query(None),
        since: int | None = Query(None),
        pending_only: bool = Query(False),
    ) -> JSONResponse:
        return JSONResponse(
            await service.dead_letters(
                limit,
                offset,
                stage=stage,
                since=since,
                only_pending=pending_only,
            )
        )

    @app.post("/api/dead-letters/replay")
    async def replay_dead_letters(
        request: Request, req: ReplayBatchRequest
    ) -> JSONResponse:
        _guard_write(request, service.settings.dashboard.token)
        return JSONResponse(await service.replay_dead_letters(req.ids))

    @app.post("/api/dead-letters/{dead_id}/replay")
    async def replay_dead_letter(
        request: Request, dead_id: str
    ) -> JSONResponse:
        _guard_write(request, service.settings.dashboard.token)
        return JSONResponse(await service.replay_dead_letter(dead_id))

    @app.post("/api/dead-letters/{dead_id}/discard")
    async def discard_dead_letter(
        request: Request, dead_id: str
    ) -> JSONResponse:
        _guard_write(request, service.settings.dashboard.token)
        return JSONResponse(await service.discard_dead_letter(dead_id))

    @app.get("/api/processes")
    async def processes() -> JSONResponse:
        return JSONResponse(await service.processes())

    @app.get("/api/equity")
    async def equity(
        hours: int = Query(24, ge=1, le=720),
    ) -> JSONResponse:
        return JSONResponse(await service.equity(hours))

    @app.get("/api/engine")
    async def engine() -> JSONResponse:
        return JSONResponse(await service.engine())

    @app.get("/api/freshness")
    async def freshness() -> JSONResponse:
        return JSONResponse(await service.freshness())

    @app.get("/api/candles")
    async def candles(
        inst: str | None = Query(None, description="默认取 engine.inst"),
        bar: str | None = Query(None, description="默认取 engine.bar"),
        limit: int = Query(300, ge=20, le=1000),
    ) -> JSONResponse:
        return JSONResponse(await service.candles(inst, bar, limit))

    @app.get("/api/strategies")
    async def strategies() -> JSONResponse:
        return JSONResponse({"strategies": await backtests.strategies()})

    @app.get("/api/strategies/{strategy_id}/params")
    async def strategy_params(strategy_id: str) -> JSONResponse:
        try:
            return JSONResponse(await backtests.params(strategy_id))
        except StrategyParamError as e:
            raise HTTPException(status_code=404, detail=str(e)) from e

    @app.put("/api/strategies/{strategy_id}/params")
    async def update_strategy_params(
        request: Request, strategy_id: str, req: ParamUpdateRequest
    ) -> JSONResponse:
        _guard_write(request, service.settings.dashboard.token)
        try:
            return JSONResponse(await backtests.set_params(strategy_id, req))
        except StrategyParamError as e:
            raise HTTPException(status_code=400, detail=str(e)) from e

    @app.get("/api/strategies/{strategy_id}/param-changes")
    async def strategy_param_changes(
        strategy_id: str,
        limit: int = Query(50, ge=1, le=MAX_LIMIT),
        offset: int = Query(0, ge=0),
    ) -> JSONResponse:
        try:
            return JSONResponse(
                await backtests.param_changes(strategy_id, limit, offset)
            )
        except StrategyParamError as e:
            raise HTTPException(status_code=404, detail=str(e)) from e

    @app.get("/api/strategies/{strategy_id}/bindings")
    async def strategy_bindings(strategy_id: str) -> JSONResponse:
        try:
            return JSONResponse(await backtests.bindings(strategy_id))
        except StrategyParamError as e:
            raise HTTPException(status_code=404, detail=str(e)) from e

    @app.put("/api/strategies/{strategy_id}/bindings/{inst_id}")
    async def update_strategy_binding(
        request: Request,
        strategy_id: str,
        inst_id: str,
        req: BindingUpdateRequest,
    ) -> JSONResponse:
        _guard_write(request, service.settings.dashboard.token)
        try:
            return JSONResponse(
                await backtests.set_binding(strategy_id, inst_id, req)
            )
        except StrategyParamError as e:
            raise HTTPException(status_code=400, detail=str(e)) from e

    @app.post("/api/backtest")
    async def backtest(
        req: BacktestRequest, sync: bool = Query(False)
    ) -> JSONResponse:
        if sync:
            # 兼容旧前端的一版路径；前端切换后可删
            try:
                return JSONResponse(await backtests.run(req))
            except BacktestRequestError as e:
                raise HTTPException(status_code=400, detail=str(e)) from e
        try:
            run_id = await tasks.submit(req)
        except BacktestRequestError as e:
            raise HTTPException(status_code=400, detail=str(e)) from e
        return JSONResponse({"run_id": run_id, "status": "pending"})

    @app.post("/api/walkforward")
    async def walkforward(
        req: WalkforwardRequest, sync: bool = Query(False)
    ) -> JSONResponse:
        if sync:
            try:
                return JSONResponse(await backtests.walkforward(req))
            except BacktestRequestError as e:
                raise HTTPException(status_code=400, detail=str(e)) from e
        try:
            run_id = await tasks.submit_walkforward(req)
        except BacktestRequestError as e:
            raise HTTPException(status_code=400, detail=str(e)) from e
        return JSONResponse({"run_id": run_id, "status": "pending"})

    @app.get("/api/backtest/runs")
    async def backtest_runs(
        limit: int = Query(50, ge=1, le=MAX_LIMIT),
        offset: int = Query(0, ge=0),
        strategy: str | None = Query(None),
        inst: str | None = Query(None),
        bar: str | None = Query(None),
        status: str | None = Query(None),
        kind: str | None = Query(None),
        since: int | None = Query(None),
        until: int | None = Query(None),
    ) -> JSONResponse:
        return JSONResponse(
            tasks.list_runs(
                limit,
                offset,
                strategy_id=strategy,
                inst_id=inst,
                bar=bar,
                status=status,
                kind=kind,
                since_ms=since,
                until_ms=until,
            )
        )

    @app.get("/api/backtest/compare")
    async def backtest_compare(
        ids: str = Query(..., description="逗号分隔的 run_id"),
    ) -> JSONResponse:
        run_ids = [item.strip() for item in ids.split(",") if item.strip()]
        try:
            return JSONResponse(tasks.compare(run_ids))
        except RunNotFound as e:
            raise HTTPException(status_code=404, detail=f"run not found: {e}") from e
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e)) from e

    @app.get("/api/backtest/{run_id}")
    async def backtest_detail(run_id: str) -> JSONResponse:
        try:
            return JSONResponse(tasks.get(run_id))
        except RunNotFound as e:
            raise HTTPException(status_code=404, detail=f"run not found: {e}") from e

    @app.get("/api/backtest/{run_id}/equity")
    async def backtest_equity(
        run_id: str, limit: int = Query(2000, ge=20, le=10000)
    ) -> JSONResponse:
        try:
            tasks.get(run_id)
        except RunNotFound as e:
            raise HTTPException(status_code=404, detail=f"run not found: {e}") from e
        return JSONResponse({"points": tasks.equity(run_id, limit)})

    @app.get("/api/backtest/{run_id}/trades")
    async def backtest_trades(
        run_id: str,
        limit: int = Query(100, ge=1, le=MAX_LIMIT),
        offset: int = Query(0, ge=0),
    ) -> JSONResponse:
        try:
            tasks.get(run_id)
        except RunNotFound as e:
            raise HTTPException(status_code=404, detail=f"run not found: {e}") from e
        return JSONResponse(tasks.trades(run_id, limit, offset))

    @app.get("/api/backtest/{run_id}/segments")
    async def backtest_segments(run_id: str) -> JSONResponse:
        try:
            tasks.get(run_id)
        except RunNotFound as e:
            raise HTTPException(status_code=404, detail=f"run not found: {e}") from e
        return JSONResponse({"segments": tasks.segments(run_id)})

    @app.post("/api/backtest/{run_id}/cancel")
    async def backtest_cancel(request: Request, run_id: str) -> JSONResponse:
        _guard_write(request, service.settings.dashboard.token)
        try:
            return JSONResponse({"run_id": run_id, "status": await tasks.cancel(run_id)})
        except RunNotFound as e:
            raise HTTPException(status_code=404, detail=f"run not found: {e}") from e

    @app.post("/api/backtest/{run_id}/rerun")
    async def backtest_rerun(request: Request, run_id: str) -> JSONResponse:
        _guard_write(request, service.settings.dashboard.token)
        try:
            new_id = await tasks.rerun(run_id)
        except RunNotFound as e:
            raise HTTPException(status_code=404, detail=f"run not found: {e}") from e
        except BacktestRequestError as e:
            raise HTTPException(status_code=400, detail=str(e)) from e
        return JSONResponse({"run_id": new_id, "status": "pending"})

    @app.post("/api/optimize")
    async def optimize(req: OptimizeRequest) -> JSONResponse:
        try:
            return JSONResponse(await backtests.optimize(req))
        except BacktestRequestError as e:
            raise HTTPException(status_code=400, detail=str(e)) from e

    @app.post("/api/kill-switch")
    async def kill_switch(
        request: Request, req: KillSwitchRequest
    ) -> JSONResponse:
        _guard_write(request, service.settings.dashboard.token)
        reason = req.reason.strip()
        try:
            result = await service.set_kill_switch(req.action == "on", reason)
        except Exception as e:  # noqa: BLE001
            log.warning(f"kill switch write failed: {e}")
            raise HTTPException(
                status_code=503, detail="redis unavailable"
            ) from e
        # 写操作留痕：谁在什么时候因为什么停了策略
        log.warning(f"kill switch -> {req.action} (reason={reason!r})")

        # 先置标志再撤单：撤单失败也要保持「停止」状态，绝不因撤单失败而恢复交易
        if req.action == "on" and req.cancel_orders:
            result["cancel"] = await _cancel_open_orders()
        return JSONResponse(result)

    @app.post("/api/risk/circuits/{inst_id}/resume")
    async def resume_circuit(request: Request, inst_id: str) -> JSONResponse:
        _guard_write(request, service.settings.dashboard.token)
        try:
            result = await service.resume_circuit(inst_id)
        except Exception as e:  # noqa: BLE001
            log.warning(f"circuit resume failed inst={inst_id}: {e}")
            raise HTTPException(
                status_code=503, detail="redis unavailable"
            ) from e
        if result.get("resumed"):
            log.warning(f"circuit breaker resumed by operator inst={inst_id}")
        return JSONResponse(result)

    @app.post("/api/positions/reduce")
    async def reduce_position(
        request: Request, req: ReduceRequest
    ) -> JSONResponse:
        _guard_write(request, service.settings.dashboard.token)
        try:
            qty = Decimal(req.qty) if req.qty else None
        except (InvalidOperation, ValueError) as e:
            raise HTTPException(status_code=400, detail=f"qty 非法：{e}") from e
        try:
            result = await _trader().reduce(
                req.inst_id.strip(),
                ratio=Decimal(str(req.ratio)) if req.ratio is not None else None,
                qty=qty,
                reason=req.reason,
            )
        except ManualTradeError as e:
            raise HTTPException(status_code=e.status, detail=str(e)) from e
        except QuantError as e:
            # 交易所拒单/网络异常：502 让前端区分「请求本身有问题」
            raise HTTPException(
                status_code=502, detail=f"交易所下单失败：{e}"
            ) from e
        return JSONResponse(result)

    @app.post("/api/positions/flatten")
    async def flatten_positions(
        request: Request, req: FlattenRequest
    ) -> JSONResponse:
        _guard_write(request, service.settings.dashboard.token)
        if req.confirm.strip().upper() != "FLATTEN":
            raise HTTPException(
                status_code=400, detail="确认词不匹配：请输入 FLATTEN"
            )
        try:
            result = await _trader().flatten(reason=req.reason)
        except ManualTradeError as e:
            raise HTTPException(status_code=e.status, detail=str(e)) from e
        except QuantError as e:
            raise HTTPException(
                status_code=502, detail=f"交易所下单失败：{e}"
            ) from e
        return JSONResponse(result)

    index = web_dir / "index.html" if web_dir is not None else None
    if index is not None and index.is_file():
        app.mount("/", StaticFiles(directory=str(web_dir), html=True), name="web")
    else:
        log.warning(
            f"web assets not found at {web_dir}, serving API only "
            f"(run `npm run build` in web/)"
        )

        @app.get("/")
        async def root() -> dict:
            return {
                "service": "quant-trading dashboard",
                "hint": "前端未构建：cd web && npm install && npm run build",
                "api": [
                    "/api/overview",
                    "/api/positions",
                    "/api/orders",
                    "/api/fills",
                    "/api/alerts",
                    "/api/equity",
                    "/api/candles",
                    "/api/strategies",
                    "/api/strategies/{id}/params",
                    "/api/strategies/{id}/param-changes",
                    "/api/strategies/{id}/bindings",
                    "/api/backtest",
                    "/api/backtest/runs",
                    "/api/optimize",
                    "/api/walkforward",
                    "/api/processes",
                ],
            }

    return app


__all__ = ["create_app"]
