"""看板手动减仓/平仓（只减仓，不开仓）。

与 `service.py` 的只读职责分开：本模块会真实下单并写 SQLite。

执行路径是直连 OKX（不经信号队列），因此这里自己兜住三件事：
1. 下单前向交易所查可用量，实际下单量 = min(请求量, 可用量)；
2. 合约单必须带 `reduceOnly`，避免方向算错时开出反向新仓；
3. 同品种用 Redis 短锁串行化，防止连点重复下单。

数量口径：对外一律用「基础币数量」；合约在内部按 `ctVal` 换算成张数，
回执里同时给出 `contracts`，便于对照交易所界面。
"""
from __future__ import annotations

import asyncio
import json
import time
import uuid
from dataclasses import replace
from decimal import ROUND_DOWN, Decimal, InvalidOperation

from redis.asyncio import Redis

from quant.backtest.order import Fill
from quant.backtest.signal import Signal
from quant.execution.okx_broker import OkxBroker
from quant.foundation.config import Settings
from quant.foundation.errors import QuantError
from quant.foundation.logger import get_logger
from quant.models.instrument import Instrument
from quant.okx.client import OKXClient
from quant.state.sqlite_store import SQLiteStore

log = get_logger("dashboard.manual")

# 秒级互斥，够覆盖一次市价单的往返
LOCK_TTL_SECONDS = 5
# 单次平仓最多处理的品种数，防御异常数据导致的超长循环
MAX_FLATTEN_ITEMS = 20


class ManualTradeError(Exception):
    """手动交易失败；`status` 直接映射 HTTP 状态码。"""

    def __init__(self, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.status = status


def _dec(raw: object) -> Decimal | None:
    if raw is None or raw == "":
        return None
    try:
        value = Decimal(str(raw))
    except (InvalidOperation, ValueError):
        return None
    return value if value.is_finite() else None


def floor_to(value: Decimal, step: Decimal) -> Decimal:
    """按步长向下取整（OKX 的 lotSz 要求）。"""
    if step <= 0:
        return value
    return (value / step).to_integral_value(rounding=ROUND_DOWN) * step


def _is_contract(inst_id: str) -> bool:
    """是否合约品种：SWAP/FUTURES 形如 BTC-USDT-SWAP、BTC-USD-251226，
    至少两个连字符；现货只有 BASE-QUOTE 一个连字符。"""
    return inst_id.count("-") >= 2


class ManualTrader:
    """手动减仓/平仓执行器。"""

    def __init__(
        self,
        settings: Settings,
        store: SQLiteStore,
        redis: Redis | None = None,
    ) -> None:
        self._settings = settings
        self._store = store
        self._redis = redis
        self._td_mode = settings.execution.td_mode
        self._client: OKXClient | None = None
        self._broker: OkxBroker | None = None

    # ------------------------------------------------------------------
    # 基础设施
    # ------------------------------------------------------------------

    def _ensure_client(self) -> tuple[OKXClient, OkxBroker]:
        """懒加载 OKX 客户端：看板启动不依赖网络，首次操作才建连。"""
        okx = self._settings.okx
        if not (okx.api_key and okx.secret and okx.passphrase):
            raise ManualTradeError(
                "未配置 OKX 凭据，无法手动下单；请在 config.toml 填写 "
                "[okx] api_key/secret/passphrase",
                status=503,
            )
        if self._client is None:
            self._client = OKXClient(okx)
            self._broker = OkxBroker(self._client, td_mode=self._td_mode)
        assert self._broker is not None
        return self._client, self._broker

    async def _instrument(self, inst_id: str) -> Instrument:
        """按 instId 精确查询产品配置（ctVal/lotSz/minSz）。

        复用 client 的进程内缓存：早期版本在这里自己按 SPOT → SWAP →
        FUTURES 试，类型不对时 OKX 返回 51001 而非空列表，导致合约查配置
        直接抛错。
        """
        client, _ = self._ensure_client()
        instrument = await asyncio.to_thread(client.get_instrument, inst_id)
        if instrument is None:
            raise ManualTradeError(f"交易所查不到产品配置：{inst_id}", status=404)
        return instrument

    async def _available(self, inst_id: str, instrument: Instrument) -> Decimal:
        """查询交易所当前可减仓数量（基础币口径）。"""
        client, _ = self._ensure_client()
        if instrument.inst_type == "SPOT":
            balance = await asyncio.to_thread(client.get_balance)
            details = balance.get("details") or []
            for item in details:
                if str(item.get("ccy", "")).upper() != instrument.base_ccy.upper():
                    continue
                for field in ("availBal", "availEq", "cashBal", "eq"):
                    value = _dec(item.get(field))
                    if value is not None:
                        return max(value, Decimal("0"))
            return Decimal("0")

        rows = await asyncio.to_thread(client.get_positions, instrument.inst_type)
        for row in rows:
            if str(row.get("instId", "")) != inst_id:
                continue
            contracts = _dec(row.get("pos")) or Decimal("0")
            if contracts == 0:
                return Decimal("0")
            return abs(contracts) * instrument.ct_val
        return Decimal("0")

    def _local_qty(self, inst_id: str) -> Decimal:
        try:
            return abs(self._store.get_position(inst_id).qty)
        except Exception as e:  # noqa: BLE001
            log.warning(f"read local position failed inst={inst_id}: {e}")
            return Decimal("0")

    async def _lock(self, inst_id: str) -> bool:
        if self._redis is None:
            return True
        try:
            return bool(
                await self._redis.set(
                    f"quant:manual:lock:{inst_id}",
                    "1",
                    nx=True,
                    ex=LOCK_TTL_SECONDS,
                )
            )
        except Exception as e:  # noqa: BLE001
            # Redis 不可用时不阻断交易，宁可少一层保护也不要操作不了
            log.warning(f"manual lock unavailable inst={inst_id}: {e}")
            return True

    async def _unlock(self, inst_id: str) -> None:
        if self._redis is None:
            return
        try:
            await self._redis.delete(f"quant:manual:lock:{inst_id}")
        except Exception as e:  # noqa: BLE001
            log.warning(f"manual unlock failed inst={inst_id}: {e}")

    # ------------------------------------------------------------------
    # 对外操作
    # ------------------------------------------------------------------

    async def reduce(
        self,
        inst_id: str,
        *,
        ratio: Decimal | None,
        qty: Decimal | None,
        reason: str,
    ) -> dict[str, object]:
        """按比例或指定数量减仓。

        Args:
            ratio: 0 < ratio <= 1，按「本地持仓 × ratio」下单
            qty: 基础币数量；与 ratio 二选一
        """
        if not inst_id:
            raise ManualTradeError("inst_id 不能为空")
        if not reason.strip():
            raise ManualTradeError("必须填写操作原因")
        if (ratio is None) == (qty is None):
            raise ManualTradeError("ratio 与 qty 必须且只能提供一个")
        if ratio is not None and not (Decimal("0") < ratio <= Decimal("1")):
            raise ManualTradeError("ratio 必须在 (0, 1] 区间")
        if qty is not None and qty <= 0:
            raise ManualTradeError("qty 必须为正数")

        if not await self._lock(inst_id):
            raise ManualTradeError(f"{inst_id} 有正在处理的手动操作，请稍后重试", 409)
        try:
            return await self._reduce_locked(
                inst_id, ratio=ratio, qty=qty, reason=reason
            )
        finally:
            await self._unlock(inst_id)

    async def flatten(self, *, reason: str) -> dict[str, object]:
        """一键全平：逐个品种减到 0，单项失败不影响其它。"""
        if not reason.strip():
            raise ManualTradeError("必须填写操作原因")

        targets = await self._flatten_targets()
        if not targets:
            return {
                "ok": True,
                "ts": int(time.time() * 1000),
                "results": [],
                "total": 0,
                "succeeded": 0,
                "failed": 0,
            }

        results: list[dict[str, object]] = []
        for inst_id in targets[:MAX_FLATTEN_ITEMS]:
            if not await self._lock(inst_id):
                results.append(
                    {
                        "inst_id": inst_id,
                        "ok": False,
                        "error": "该品种有正在处理的操作",
                    }
                )
                continue
            try:
                results.append(
                    await self._reduce_locked(
                        inst_id,
                        ratio=Decimal("1"),
                        qty=None,
                        reason=reason,
                        allow_full=True,
                    )
                )
            except ManualTradeError as e:
                results.append(
                    {"inst_id": inst_id, "ok": False, "error": str(e)}
                )
            except QuantError as e:
                results.append(
                    {"inst_id": inst_id, "ok": False, "error": str(e)}
                )
            finally:
                await self._unlock(inst_id)

        succeeded = sum(1 for r in results if r.get("ok"))
        return {
            "ok": succeeded == len(results),
            "ts": int(time.time() * 1000),
            "results": results,
            "total": len(results),
            "succeeded": succeeded,
            "failed": len(results) - succeeded,
        }

    async def _flatten_targets(self) -> list[str]:
        """待平品种：本地持仓 ∪ 快照合约多头。

        现货只认本地记账：快照里的现货是账户全部余额（可能含 ETH/OKB 等
        与策略无关的资产），直接平会连带卖掉它们。
        """
        targets: list[str] = []
        try:
            for pos in await asyncio.to_thread(self._store.get_all_positions):
                if pos.qty > 0:
                    targets.append(pos.inst_id)
        except Exception as e:  # noqa: BLE001
            log.warning(f"read local positions failed: {e}")

        for inst_id in await self._snapshot_longs():
            if not _is_contract(inst_id):
                continue
            if inst_id not in targets:
                targets.append(inst_id)
        return targets

    async def _snapshot_longs(self) -> list[str]:
        """monitor 快照里的多头品种。

        本地持仓可能落后于交易所（例如策略刚成交、或 worker 重启前没落库），
        所以合约全平还要参考快照，避免漏平。现货由调用方过滤。
        """
        if self._redis is None:
            return []
        try:
            raw = await self._redis.get(self._settings.monitor.dashboard_snapshot_key)
        except Exception as e:  # noqa: BLE001
            log.warning(f"read snapshot failed: {e}")
            return []
        if not raw:
            return []
        try:
            payload = json.loads(raw)
        except (TypeError, ValueError):
            return []
        longs: list[str] = []
        for item in payload.get("positions") or []:
            if not isinstance(item, dict):
                continue
            inst_id = str(item.get("inst_id") or "")
            qty = _dec(item.get("qty"))
            if inst_id and qty is not None and qty > 0:
                longs.append(inst_id)
        return longs

    # ------------------------------------------------------------------
    # 内部实现
    # ------------------------------------------------------------------

    async def _reduce_locked(
        self,
        inst_id: str,
        *,
        ratio: Decimal | None,
        qty: Decimal | None,
        reason: str,
        allow_full: bool = False,
    ) -> dict[str, object]:
        instrument = await self._instrument(inst_id)
        available = await self._available(inst_id, instrument)
        if available <= 0:
            raise ManualTradeError(f"{inst_id} 交易所可用持仓为 0，无需减仓")

        if ratio is not None:
            if allow_full:
                # 全平以交易所可用量为准：本地记录可能落后或偏小，
                # 按本地数量下单会在交易所留下残余仓位
                base_qty = available
            else:
                local = self._local_qty(inst_id)
                if local <= 0:
                    raise ManualTradeError(
                        f"{inst_id} 本地无持仓记录，无法按比例减仓"
                    )
                base_qty = local * ratio
        else:
            assert qty is not None
            base_qty = qty

        target = min(base_qty, available)

        contracts: Decimal | None = None
        if instrument.inst_type == "SPOT":
            submitted = floor_to(target, instrument.lot_sz)
            ok_size = submitted > 0 and submitted >= instrument.min_sz
        else:
            # 合约 sz 是张数：基础币 / 面值 再按 lotSz 取整
            raw_contracts = (
                target / instrument.ct_val
                if instrument.ct_val > 0
                else target
            )
            contracts = floor_to(raw_contracts, instrument.lot_sz)
            submitted = contracts * instrument.ct_val
            # minSz 的单位是张，不是基础币
            ok_size = contracts > 0 and contracts >= instrument.min_sz

        if not ok_size:
            raise ManualTradeError(
                f"{inst_id} 可下单量 {contracts if contracts is not None else submitted} "
                f"低于最小下单量 {instrument.min_sz}（lotSz={instrument.lot_sz}）"
            )

        signal = Signal(
            strategy_id="manual",
            side="sell",
            order_type="market",
            inst_id=inst_id,
            # 全链路基础币口径：合约的 sz/张数换算由 broker 负责
            qty=submitted,
            ts=int(time.time() * 1000),
            signal_id=f"manual-{uuid.uuid4().hex[:20]}",
        )

        _, broker = self._ensure_client()
        result = await broker.execute(
            signal,
            submitted,
            reduce_only=instrument.inst_type != "SPOT",
        )

        # 先落 signal 再落 order：orders 只有 signal_id，看板的品种/方向/
        # 类型/数量都靠 JOIN signals 取回，缺这行就会显示为空
        await asyncio.to_thread(self._store.save_signal, signal, "executed")
        await asyncio.to_thread(self._store.save_order, result.order)
        fill = result.fill
        if fill is not None:
            # broker 已把合约张数折成基础币，这里直接用
            normalized = Fill(
                order_id=fill.order_id,
                inst_id=inst_id,
                side="sell",
                qty=fill.qty,
                price=fill.price,
                fee=fill.fee,
                ts=fill.ts,
            )
            await asyncio.to_thread(
                self._store.update_order_fill, result.order.order_id, normalized
            )
            await asyncio.to_thread(self._store.save_fill, normalized)
            position = await asyncio.to_thread(self._store.get_position, inst_id)
            # 交易所成交量可能大于本地记录（本地落后于交易所）：只结平已知
            # 持仓，否则 apply_fill 会把本地持仓翻成反向空头；
            # 本地完全没有记录时保持空仓，不凭空建仓
            closable = position.qty if position.qty > 0 else Decimal("0")
            if closable > 0:
                applied = (
                    replace(normalized, qty=closable)
                    if normalized.qty > closable
                    else normalized
                )
                position.apply_fill(applied)
                await asyncio.to_thread(self._store.update_position, position)

        log.warning(
            f"manual reduce inst={inst_id} reason={reason!r} "
            f"submitted={submitted} status={result.order.status} "
            f"order={result.order.order_id}"
        )

        return {
            "inst_id": inst_id,
            "inst_type": instrument.inst_type,
            "ok": True,
            "ts": int(time.time() * 1000),
            "available_qty": str(available),
            "submitted_qty": str(submitted),
            "contracts": str(contracts) if contracts is not None else None,
            "order_id": result.order.order_id,
            "status": result.order.status,
            "fill": (
                {
                    "qty": str(fill.qty),
                    "price": str(fill.price),
                    "fee": str(fill.fee),
                    "ts": fill.ts,
                }
                if fill is not None
                else None
            ),
            "error": None,
        }
