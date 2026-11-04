"""凭证状态只读探测（M10-2）。

- **OKX**：复用 `okx.live_check.run_checks`（权限 / 子账户 / IP），
  只返回结论，**永不回显 key/secret/passphrase**。
- **SMTP**：只判断是否配置齐全，不真连（避免每次刷新打一次 SMTP）。
- 探测结果缓存 60s：看板每 15s 轮询一次，不缓存会把 OKX 限频吃满。
- 探测失败只降级为「不可用 + 原因」，不影响设置页其它内容。
"""
from __future__ import annotations

import time
from threading import Lock

from quant.foundation.config import Settings
from quant.foundation.errors import QuantError
from quant.foundation.logger import get_logger
from quant.okx.client import OKXClient
from quant.okx.live_check import run_checks

log = get_logger("dashboard.credentials")

CACHE_TTL_SECONDS = 60.0
#: 单进程 dashboard 内缓存（key -> (monotonic, payload)）
_cache: dict[str, tuple[float, dict]] = {}
_lock = Lock()


def clear_cache() -> None:
    """清空探测缓存（测试与手动刷新用）。"""
    with _lock:
        _cache.clear()


def _missing_okx(settings: Settings) -> list[str]:
    return [
        name
        for name in ("api_key", "secret", "passphrase")
        if not getattr(settings.okx, name)
    ]


def okx_status(settings: Settings, *, force: bool = False) -> dict[str, object]:
    """OKX 凭证/权限状态；探测失败不抛异常。"""
    now = time.monotonic()
    with _lock:
        cached = _cache.get("okx")
        if not force and cached and now - cached[0] < CACHE_TTL_SECONDS:
            return cached[1]

    checked_at = int(time.time() * 1000)
    missing = _missing_okx(settings)
    if missing:
        result: dict[str, object] = {
            "configured": False,
            "demo": settings.okx.demo,
            "error": f"缺凭证：{', '.join(missing)}",
            "items": [],
            "checked_at": checked_at,
        }
    else:
        try:
            client = OKXClient(settings.okx)
            try:
                items = run_checks(client)
            finally:
                client.close()
        except QuantError as e:
            # 鉴权失败 / 环境不匹配（50101）等：给出原因而不是抛给页面
            result = {
                "configured": True,
                "demo": settings.okx.demo,
                "error": str(e),
                "items": [],
                "checked_at": checked_at,
            }
        except Exception as e:  # noqa: BLE001 - 网络异常都要降级
            log.warning(f"okx credential probe failed: {e}")
            result = {
                "configured": True,
                "demo": settings.okx.demo,
                "error": str(e),
                "items": [],
                "checked_at": checked_at,
            }
        else:
            result = {
                "configured": True,
                "demo": settings.okx.demo,
                "error": None,
                "items": [
                    {
                        "name": item.name,
                        "status": item.status,
                        "detail": item.detail,
                    }
                    for item in items
                ],
                "checked_at": checked_at,
            }

    with _lock:
        _cache["okx"] = (now, result)
    return result


def smtp_status(settings: Settings) -> dict[str, object]:
    """SMTP 是否配置齐全（不真连）。"""
    smtp = settings.smtp
    missing: list[str] = []
    if not smtp.host:
        missing.append("host")
    if not smtp.username:
        missing.append("username")
    if not smtp.to_addrs:
        missing.append("to_addrs")
    return {
        "configured": not missing,
        "host": smtp.host,
        "port": smtp.port,
        "recipients": len(smtp.to_addrs),
        "missing": missing,
    }


def credential_status(
    settings: Settings, *, force: bool = False
) -> dict[str, object]:
    """设置页「凭证状态」卡片的数据源。"""
    return {
        "okx": okx_status(settings, force=force),
        "smtp": smtp_status(settings),
    }


__all__ = [
    "CACHE_TTL_SECONDS",
    "clear_cache",
    "credential_status",
    "okx_status",
    "smtp_status",
]
