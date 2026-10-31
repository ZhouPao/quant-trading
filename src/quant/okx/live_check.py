"""实盘接入自检（M8-7）：Key 权限、子账户与 IP 绑定。

交易所不支持「读回」Key 的提币开关，但 `GET /api/v5/account/config` 会
返回 `perm`（权限列表）、`uid`/`mainUid`（是否子账户）与 `ip`（绑定 IP），
足够在切实盘前做一次硬性体检。

判定结果分三档：
- `pass`：符合要求
- `warn`：能跑，但建议按 PRD 收敛（主账户、未绑定 IP）
- `fail`：必须改配置后再跑（缺交易权限、开着提币权限）

CLI `quant check-live` 与 worker 实盘启动共用这一份判定，避免两处口径不一致。
"""
from __future__ import annotations

from dataclasses import dataclass

from quant.okx.client import OKXClient

PASS = "pass"
WARN = "warn"
FAIL = "fail"

#: acctLv → 人话（OKX 账户模式）
_ACCT_LEVEL = {
    "1": "简单交易模式",
    "2": "单币种保证金模式",
    "3": "跨币种保证金模式",
    "4": "组合保证金模式",
}


@dataclass(frozen=True)
class LiveCheckItem:
    """一条自检结论。"""

    name: str
    status: str
    detail: str


def _perms(config: dict) -> set[str]:
    raw = str(config.get("perm") or "")
    return {part.strip() for part in raw.split(",") if part.strip()}


def evaluate_account_config(config: dict) -> list[LiveCheckItem]:
    """按 `/account/config` 的返回判断实盘 Key 是否符合接入要求。

    字段缺失时按「未知」处理并给 warn，不因为交易所改字段名就误判为通过。
    """
    perms = _perms(config)
    has_trade = "trade" in perms
    has_withdraw = "withdraw" in perms

    items = [
        LiveCheckItem(
            "交易权限",
            PASS if has_trade else FAIL,
            f"perm={','.join(sorted(perms)) or '空'}"
            + ("" if has_trade else "；只读 Key 无法下单"),
        ),
        LiveCheckItem(
            "提币权限",
            FAIL if has_withdraw else PASS,
            "已开启提币权限，必须关闭后再跑实盘"
            if has_withdraw
            else "未开启提币权限",
        ),
    ]

    uid = str(config.get("uid") or "")
    main_uid = str(config.get("mainUid") or "")
    if uid and main_uid:
        if uid != main_uid:
            items.append(
                LiveCheckItem("子账户", PASS, f"uid={uid}（主账户 {main_uid}）")
            )
        else:
            items.append(
                LiveCheckItem(
                    "子账户",
                    WARN,
                    f"当前为主账户 {uid}，建议改用子账户隔离资金",
                )
            )
    else:
        items.append(LiveCheckItem("子账户", WARN, "接口未返回 uid/mainUid，请人工确认"))

    bound_ip = str(config.get("ip") or "").strip()
    if bound_ip:
        items.append(LiveCheckItem("IP 白名单", PASS, f"已绑定 {bound_ip}"))
    else:
        items.append(
            LiveCheckItem(
                "IP 白名单",
                WARN,
                "未返回绑定 IP；建议在 OKX 后台为 Key 绑定服务器 IP",
            )
        )

    level = str(config.get("acctLv") or "")
    items.append(
        LiveCheckItem(
            "账户模式",
            PASS,
            _ACCT_LEVEL.get(level, f"acctLv={level or '未知'}"),
        )
    )
    return items


def run_checks(client: OKXClient) -> list[LiveCheckItem]:
    """拉取账户配置并给出自检结论；接口失败时异常向上抛。"""
    return evaluate_account_config(client.get_account_config())


def has_failures(items: list[LiveCheckItem]) -> bool:
    return any(item.status == FAIL for item in items)
