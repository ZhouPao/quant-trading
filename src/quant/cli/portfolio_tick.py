"""``quant portfolio-tick``：一次组合评估（快照生产者 + 再平衡入口）。

默认 **dry-run**：只写组合快照、评估是否该调仓，不向 M8 队列发任何信号。
``--execute`` 才会把 ``RebalancePlan`` 交给 ``PortfolioRebalanceService``
（唯一 M8 队列），并要求 ``[portfolio] enabled=true``。

接自动调度用 cron / systemd timer 周期调用即可，supervisor 的 7 个常驻
进程清单不变。示例：
    quant portfolio-tick --equity 10000              # 只写快照 + 看计划
    quant portfolio-tick --execute                   # 真正提交调仓单
"""
from __future__ import annotations

import asyncio
import json
import time
from decimal import Decimal, InvalidOperation

from redis.asyncio import Redis

from quant.dashboard.portfolio_view import PortfolioView
from quant.foundation.config import Settings
from quant.foundation.logger import get_logger
from quant.portfolio import (
    PortfolioRebalanceService,
    RebalancePlan,
    load_portfolios,
)
from quant.queue.signal_queue import SignalQueue
from quant.state.sqlite_store import SQLiteStore

log = get_logger("cli.portfolio_tick")


def _equity(store: SQLiteStore, override: str | None) -> Decimal | None:
    if override is not None:
        try:
            return Decimal(override)
        except (InvalidOperation, ValueError):
            return None
    rows = store.get_equity_history(0, 1)
    if not rows:
        return None
    try:
        return Decimal(str(rows[-1]["total_equity"]))
    except (KeyError, InvalidOperation, ValueError):
        return None


def _pick(settings: Settings):
    configs = load_portfolios(settings.portfolio.config_dir)
    active = settings.portfolio.active
    if active:
        return next((c for c in configs if c.portfolio_id == active), None)
    return configs[0] if configs else None


async def _submit(
    settings: Settings, store: SQLiteStore, config, plan: RebalancePlan
) -> list[dict[str, object]]:
    redis = Redis.from_url(settings.redis.url, decode_responses=True)
    try:
        queue = SignalQueue(
            redis,
            pending_key=settings.redis.queue_key,
            inflight_key=settings.redis.inflight_key,
            dead_key=settings.redis.dead_key,
        )
        service = PortfolioRebalanceService(
            config, store, queue, enabled=settings.portfolio.enabled
        )
        return [
            {
                "unit_key": r.unit_key,
                "signal_id": r.signal_id,
                "status": r.status,
                "reason": r.reason,
            }
            for r in await service.submit(plan)
        ]
    finally:
        await redis.aclose()


def main(args) -> int:
    settings = Settings.load(args.config)
    store = SQLiteStore(settings.state.db_path)
    try:
        equity = _equity(store, args.equity)
        if equity is None or equity <= 0:
            print(
                json.dumps(
                    {
                        "ok": False,
                        "reason": (
                            "读不到有效总权益；用 --equity 显式指定，"
                            "或先让 monitor 写 equity_snapshots"
                        ),
                    },
                    ensure_ascii=False,
                    indent=2,
                )
            )
            return 1

        view = PortfolioView(store, settings)
        snap, plan, reason = view.tick(equity, int(time.time() * 1000))
        if reason or snap is None:
            print(json.dumps({"ok": False, "reason": reason}, ensure_ascii=False))
            return 1

        payload: dict[str, object] = {
            "ok": True,
            "portfolio_id": snap.portfolio_id,
            "ts": snap.ts,
            "equity": str(snap.equity),
            "drawdown": str(snap.drawdown),
            "unit_equity": {k: str(v) for k, v in snap.unit_equity.items()},
            "snapshot_note": "单元权益按目标权重分摊总权益，非逐单元真实持仓估值",
            "enabled": settings.portfolio.enabled,
            "plan": (
                None
                if plan is None
                else {
                    "reason": plan.reason,
                    "deltas": [
                        {
                            "unit_key": d.unit_key,
                            "side": d.side,
                            "amount": str(d.amount),
                            "target_weight": str(d.target_weight),
                        }
                        for d in plan.deltas
                    ],
                }
            ),
            "submitted": [],
            "execute": bool(args.execute),
        }
        if args.execute:
            if not settings.portfolio.enabled:
                payload["ok"] = False
                payload["reason"] = "[portfolio] enabled=false，拒绝提交调仓单"
            elif plan is not None:
                config = _pick(settings)
                if config is None:
                    payload["reason"] = "组合配置丢失，跳过提交"
                else:
                    payload["submitted"] = asyncio.run(
                        _submit(settings, store, config, plan)
                    )
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        log.info(
            f"portfolio tick done id={snap.portfolio_id} equity={snap.equity} "
            f"plan={'yes' if plan else 'no'} execute={bool(args.execute)}"
        )
        return 0
    finally:
        store.close()
