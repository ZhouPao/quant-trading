"""kill-switch 命令：一键紧急停止（spec F4.5）。

用法：
  quant kill-switch --on [--reason "..."] [--broker okx]   # 激活 + 撤所有挂单
  quant kill-switch --off                                    # 解除
  quant kill-switch --status                                 # 查询状态

激活后：worker 拒绝一切新信号（入死信）；若指定 --broker okx，
同时撤销本地记录中所有 pending 挂单。
"""
from __future__ import annotations

import asyncio

from redis.asyncio import Redis

from quant.execution.kill_switch import KillSwitch
from quant.execution.okx_broker import OkxBroker
from quant.foundation.config import Settings
from quant.foundation.logger import get_logger, setup_logger
from quant.okx.client import OKXClient
from quant.state.sqlite_store import SQLiteStore


async def _cancel_open_orders(settings: Settings, log) -> int:
    """撤销交易所侧全部挂单，并把本地未终结订单标记为 canceled。

    以**交易所的挂单列表**为准，而不是本地 `orders` 表：本地漏记或状态
    漂移时，只撤本地记录会留下真正裸露在交易所的挂单。撤完再把本地状态
    同步过来，避免下次启动时对账重复处理。
    """
    store = SQLiteStore(settings.state.db_path)
    client = OKXClient(settings.okx)
    broker = OkxBroker(client)
    try:
        try:
            canceled = await broker.cancel_all_open_orders()
        except Exception as e:  # noqa: BLE001
            log.error(f"cancel all open orders failed: {e}")
            return 0

        for order in store.get_open_orders():
            order_id = str(order.get("order_id") or "")
            if order_id:
                await asyncio.to_thread(
                    store.update_order_status, order_id, "canceled"
                )
        log.info(f"canceled {canceled} open orders on exchange")
        return canceled
    finally:
        client.close()
        store.close()


async def _run(args, settings: Settings) -> int:
    log = get_logger("cli.kill_switch")
    redis = Redis.from_url(settings.redis.url, decode_responses=True)
    ks = KillSwitch(redis)
    try:
        if args.on:
            await ks.enable(args.reason or "")
            log.warning(
                f"kill-switch ENABLED reason={args.reason or '(none)'}"
            )
            if args.broker == "okx":
                await _cancel_open_orders(settings, log)
            return 0

        if args.off:
            await ks.disable()
            log.info("kill-switch DISABLED")
            return 0

        # --status（默认）
        active = await ks.is_active()
        reason = await ks.reason()
        if active:
            log.warning(f"kill-switch ACTIVE reason={reason or '(none)'}")
        else:
            log.info("kill-switch INACTIVE")
        return 0
    finally:
        await redis.aclose()


def main(args) -> int:
    """kill-switch 主入口。"""
    settings = Settings.load(args.config)
    setup_logger(**settings.logging.model_dump())
    try:
        return asyncio.run(_run(args, settings))
    except KeyboardInterrupt:
        return 0
