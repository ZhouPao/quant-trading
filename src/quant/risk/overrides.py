"""阈值在线调整（M7-8）与变更审计（M7-14）。

覆盖值写 Redis hash ``quant:risk:overrides``（字段 ``{strategy}:{param}``），
审计写 SQLite ``risk_overrides``。两者分工明确：

- Redis 是**唯一生效源**：worker 每轮心跳重读一次，因此调完不重启即生效；
- SQLite 是**审计账本**：谁在什么时候把哪个字段从多少改到多少、为什么。

写入顺序是「先 Redis 后审计」，审计失败就把 Redis 回滚——宁可这次调整
整个失败，也不能留下「值改了但查不到谁改的」的状态。

覆盖值**不写回 toml**：配置文件仍是基线，覆盖是可随时清除的一层。
"""
from __future__ import annotations

import asyncio
import time
from decimal import Decimal, InvalidOperation

from redis.asyncio import Redis

from quant.foundation.logger import get_logger
from quant.risk.limits import INT_FIELDS, LIMIT_FIELDS
from quant.state.sqlite_store import SQLiteStore

log = get_logger("risk.overrides")

DEFAULT_KEY = "quant:risk:overrides"

#: 比例类字段的取值范围是 (0, 1]。`max_order_qty` 是名义价值（USDT），
#: 不在其中——把它当比例校验会让「单笔上限 5000」直接被拒。
PCT_FIELDS = frozenset(
    {"max_position_pct", "max_total_pct", "daily_loss_limit"}
)


class OverrideError(ValueError):
    """覆盖值不合法（字段名、类型、范围或缺少原因）。"""


class RiskOverrideService:
    """在线阈值覆盖的读写入口。"""

    def __init__(
        self,
        store: SQLiteStore,
        redis: Redis,
        *,
        key: str = DEFAULT_KEY,
    ) -> None:
        self._store = store
        self._redis = redis
        self._key = key

    # ------------------------------------------------------------------
    # 写
    # ------------------------------------------------------------------

    async def set(
        self,
        strategy_id: str,
        param: str,
        value: str | int | float | Decimal,
        *,
        reason: str,
        by: str = "dashboard",
    ) -> dict[str, object]:
        """写入/更新一条在线覆盖。

        Raises:
            OverrideError: 字段名未知、值非法、或缺少变更原因。
        """
        text = _validate(param, value, reason)
        field = f"{strategy_id}:{param}"
        previous = await self._redis.hget(self._key, field)
        old_value = _decode(previous)
        now = int(time.time() * 1000)

        await self._redis.hset(self._key, field, text)
        try:
            await asyncio.to_thread(
                self._store.insert_risk_override,
                strategy_id,
                param,
                text,
                changed_at=now,
                changed_by=by,
                reason=reason.strip(),
                old_value=old_value,
            )
        except Exception:
            # 审计落不了库就撤销这次改动，避免「生效了但没人知道谁改的」
            if old_value is None:
                await self._redis.hdel(self._key, field)
            else:
                await self._redis.hset(self._key, field, old_value)
            raise

        log.warning(
            f"risk override set strategy={strategy_id} param={param} "
            f"{old_value} -> {text} by={by} reason={reason.strip()}"
        )
        return {
            "strategy_id": strategy_id,
            "param": param,
            "old_value": old_value,
            "new_value": text,
            "reason": reason.strip(),
            "changed_by": by,
            "changed_at": now,
        }

    async def clear(
        self, strategy_id: str, param: str, *, by: str = "dashboard"
    ) -> dict[str, object]:
        """清除某个字段的覆盖，回落到配置层；无覆盖时返回 cleared=False。"""
        if param not in LIMIT_FIELDS:
            raise OverrideError(
                f"unknown risk param {param!r}; expected one of "
                f"{', '.join(LIMIT_FIELDS)}"
            )
        field = f"{strategy_id}:{param}"
        previous = _decode(await self._redis.hget(self._key, field))
        active = await asyncio.to_thread(
            self._store.get_active_risk_override, strategy_id, param
        )
        if previous is None and active is None:
            return {
                "strategy_id": strategy_id,
                "param": param,
                "cleared": False,
                "reason": "no active override",
            }

        now = int(time.time() * 1000)
        await self._redis.hdel(self._key, field)
        try:
            revoked = await asyncio.to_thread(
                self._store.revoke_risk_overrides,
                strategy_id,
                param,
                revoked_at=now,
                revoked_by=by,
            )
        except Exception:
            # 审计没改成，覆盖值就不能先失效：否则「谁撤的」无从查起
            if previous is not None:
                await self._redis.hset(self._key, field, previous)
            raise

        log.warning(
            f"risk override cleared strategy={strategy_id} param={param} "
            f"by={by} revoked={revoked}"
        )
        return {
            "strategy_id": strategy_id,
            "param": param,
            "cleared": True,
            "old_value": previous
            or (str(active["new_value"]) if active else None),
            "revoked": revoked,
            "cleared_at": now,
        }

    # ------------------------------------------------------------------
    # 读
    # ------------------------------------------------------------------

    async def effective(self, strategy_id: str) -> dict[str, str]:
        """当前生效的覆盖：``{param: value}``。"""
        raw = await self._redis.hgetall(self._key)
        prefix = f"{strategy_id}:"
        result: dict[str, str] = {}
        for key, value in raw.items():
            text = _decode(key) or ""
            if not text.startswith(prefix):
                continue
            name = text[len(prefix) :]
            if name in LIMIT_FIELDS:
                result[name] = _decode(value) or ""
        return result

    async def list(
        self,
        *,
        strategy_id: str | None = None,
        limit: int = 50,
        offset: int = 0,
        include_revoked: bool = True,
    ) -> dict[str, object]:
        """生效覆盖 + 变更历史。"""
        effective = (
            await self.effective(strategy_id)
            if strategy_id is not None
            else {}
        )
        items, total = await asyncio.to_thread(
            self._store.list_risk_overrides,
            limit,
            offset,
            strategy_id=strategy_id,
            include_revoked=include_revoked,
        )
        return {
            "ts": int(time.time() * 1000),
            "strategy_id": strategy_id,
            "effective": effective,
            "items": items,
            "total": total,
        }


def _decode(raw: object) -> str | None:
    if raw is None:
        return None
    if isinstance(raw, bytes):
        return raw.decode("utf-8", errors="replace")
    return str(raw)


def _validate(
    param: str, value: str | int | float | Decimal, reason: str
) -> str:
    """校验并归一化覆盖值，返回写入 Redis 的字符串。"""
    if param not in LIMIT_FIELDS:
        raise OverrideError(
            f"unknown risk param {param!r}; expected one of "
            f"{', '.join(LIMIT_FIELDS)}"
        )
    if not str(reason or "").strip():
        # 审计要求每条变更都写原因：没有原因的调整等于没人负责
        raise OverrideError("reason is required for risk override")

    if param in INT_FIELDS:
        try:
            number = int(value)
        except (TypeError, ValueError) as e:
            raise OverrideError(f"{param} must be an integer") from e
        if number < 1:
            raise OverrideError(f"{param} must be >= 1")
        return str(number)

    try:
        number = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as e:
        raise OverrideError(f"{param} must be a number") from e
    if not number.is_finite() or number <= 0:
        raise OverrideError(f"{param} must be a positive finite number")
    if param in PCT_FIELDS and number > 1:
        # 比例类字段超过 100% 不是「放宽」，而是配置错误
        raise OverrideError(f"{param} must be within (0, 1]")
    return str(number)


__all__ = [
    "DEFAULT_KEY",
    "OverrideError",
    "PCT_FIELDS",
    "RiskOverrideService",
]
