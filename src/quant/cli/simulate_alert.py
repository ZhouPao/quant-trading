"""simulate-alert 命令：本地脚本模拟 TradingView 推送（spec §3 M3）。

用法：
  quant simulate-alert --side buy --qty 0.001
  quant simulate-alert --side sell --qty 0.001 --inst BTC-USDT

作用：向本地 webhook 发送一条 alert JSON，验证
      HTTP → 队列 → worker → 交易所 的完整链路。
"""
from __future__ import annotations

import json

import httpx

from quant.foundation.config import Settings
from quant.foundation.logger import get_logger, setup_logger


def _build_url(settings: Settings, override: str | None) -> str:
    if override:
        return override
    host = settings.webhook.host
    port = settings.webhook.port
    path = settings.webhook.path
    return f"http://{host}:{port}{path}"


def _build_payload(args) -> dict:
    payload = {
        "strategy_id": args.strategy,
        "side": args.side,
        "order_type": args.order_type,
        "inst_id": args.inst,
        "qty": args.qty,
    }
    if args.order_type == "limit":
        payload["price"] = args.price
    return payload


def main(args) -> int:
    """发送模拟 alert。返回退出码。"""
    settings = Settings.load(args.config)
    setup_logger(**settings.logging.model_dump())
    log = get_logger("cli.simulate_alert")

    if args.order_type == "limit" and not args.price:
        log.error("limit order requires --price")
        return 1

    url = _build_url(settings, args.url)
    payload = _build_payload(args)
    token = args.token if args.token is not None else settings.webhook.auth_token

    log.info(f"POST {url} payload={json.dumps(payload, ensure_ascii=False)}")
    if args.dry_run:
        log.info("dry run: request not sent")
        return 0

    headers = {"Content-Type": "application/json"}
    if token:
        headers["X-Auth-Token"] = token

    try:
        with httpx.Client(timeout=10.0) as client:
            resp = client.post(url, json=payload, headers=headers)
    except httpx.NetworkError as e:
        log.error(f"request failed: {e}")
        return 1

    log.info(f"response {resp.status_code}: {resp.text}")
    return 0 if resp.status_code == 200 else 1
