"""check-live 命令：实盘接入前的凭证与权限自检（M8-7）。

用法：
  quant check-live
  quant check-live --config config/config.toml

读 `[okx] demo` 判断当前连的是模拟盘还是实盘；实盘切换前先跑一次，
确认 Key「只开交易+读取、不开提币」，并核对子账户与 IP 绑定。
退出码 0 = 没有 fail 项（warn 只提示），1 = 有必须修正的项或接口失败。

注意：`demo=false` 才能验到实盘 Key；用实盘 Key 打模拟盘端点（或反之）
交易所会返回 50101，本命令会直接给出这一提示。
"""
from __future__ import annotations

from quant.foundation.config import Settings
from quant.foundation.errors import QuantError
from quant.foundation.logger import get_logger, setup_logger
from quant.okx.client import OKXClient
from quant.okx.live_check import (
    FAIL,
    WARN,
    LiveCheckItem,
    has_failures,
    run_checks,
)


def _report(log, items: list[LiveCheckItem]) -> None:
    for item in items:
        text = f"{item.name}: {item.detail}"
        if item.status == FAIL:
            log.error(f"[FAIL] {text}")
        elif item.status == WARN:
            log.warning(f"[WARN] {text}")
        else:
            log.info(f"[PASS] {text}")


def main(args) -> int:
    """执行实盘接入自检。返回退出码。"""
    settings = Settings.load(args.config)
    setup_logger(**settings.logging.model_dump())
    log = get_logger("cli.check_live")

    mode = "模拟盘" if settings.okx.demo else "实盘"
    log.info(f"当前配置连的是{mode}（[okx] demo={settings.okx.demo}）")

    client = OKXClient(settings.okx)
    try:
        items = run_checks(client)
    except QuantError as e:
        log.error(
            f"读取账户配置失败：{e}。50101 通常表示 Key 与当前环境不匹配"
            f"（{mode}端点只能使用 {mode}Key）"
        )
        return 1
    finally:
        client.close()

    _report(log, items)
    if has_failures(items):
        log.error("自检未通过：先在 OKX 后台修正 Key 权限，再切换实盘")
        return 1
    log.info("自检通过，可以切换实盘")
    return 0
