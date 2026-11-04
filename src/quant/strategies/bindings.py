"""策略启停矩阵：绑定模型与 Redis/SQLite 两级读取（MS4-C §4）。

真值在 SQLite `strategy_bindings`，Redis hash 只是给引擎每轮读的缓存：
看板写入后重建缓存，引擎下一轮（≤ interval_seconds）即生效，不重启进程。

缓存键 `quant:strategy:bindings`，field 为 `{strategy_id}:{inst_id}`，
value 为 JSON `{"bar": "1H", "enabled": 1}`。**禁用项也写进缓存**，
这样「hash 不存在」才等价于「缓存丢了」，不会把「全部禁用」误判成缓存故障。
**全部解绑则写一个哨兵 field**：空绑定是用户有意状态，同样不能当成缓存故障，
否则回落 DB 时 `ensure_seed` 会把解绑掉的组合复活、halt 模式还会误触发急停。
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass

from redis.asyncio import Redis

from quant.foundation.config import Settings
from quant.foundation.logger import get_logger
from quant.state.sqlite_store import SQLiteStore

log = get_logger("strategies.bindings")

#: 「绑定表已初始化过」的持久标记（存 SQLite app_meta）
SEED_META_KEY = "bindings_seeded"
#: 空绑定的哨兵 field：hash 存在但没有任何有效绑定
EMPTY_FIELD = "__empty__"


class BindingsUnavailable(Exception):
    """绑定读取彻底失败（缓存与库都读不到），调用方必须停止新开仓。"""


@dataclass(frozen=True, slots=True)
class Binding:
    """一个「策略 × 品种」组合；bar 不参与唯一性。"""

    strategy_id: str
    inst_id: str
    bar: str
    enabled: bool = True

    @property
    def key(self) -> tuple[str, str]:
        return (self.strategy_id, self.inst_id)

    @property
    def field(self) -> str:
        return f"{self.strategy_id}:{self.inst_id}"

    def payload(self) -> dict[str, object]:
        return {
            "strategy_id": self.strategy_id,
            "inst_id": self.inst_id,
            "bar": self.bar,
            "enabled": self.enabled,
        }


def _from_row(row: dict) -> Binding:
    return Binding(
        strategy_id=str(row["strategy_id"]),
        inst_id=str(row["inst_id"]),
        bar=str(row["bar"]),
        enabled=bool(row["enabled"]),
    )


def initial_binding(settings: Settings) -> Binding:
    """首次启动时由 `[engine] strategy/inst/bar` 生成的初始绑定。"""
    cfg = settings.engine
    return Binding(cfg.strategy, cfg.inst, cfg.bar, True)


def ensure_seed(store: SQLiteStore, settings: Settings, now_ms: int) -> bool:
    """仅在「从未初始化」时种入初始绑定；返回是否真的写了。

    「表为空」既可能是首次启动，也可能是用户主动解绑了全部组合（P0）。
    后者是有意状态，必须靠持久标记区分：否则看板每次读矩阵、引擎每次回落
    DB 都会把刚解绑掉的组合复活并重新开仓评估。
    """
    if store.get_meta(SEED_META_KEY):
        return False
    if store.list_bindings():
        # 存量库（v11 之前已有绑定）：只补标记，绝不改动现有数据
        store.set_meta(SEED_META_KEY, "1")
        return False
    binding = initial_binding(settings)
    store.upsert_binding(
        binding.strategy_id,
        binding.inst_id,
        binding.bar,
        binding.enabled,
        now_ms,
    )
    store.set_meta(SEED_META_KEY, "1")
    log.info(
        f"seeded initial binding strategy={binding.strategy_id} "
        f"inst={binding.inst_id} bar={binding.bar}"
    )
    return True


def read_db(store: SQLiteStore, strategy_id: str | None = None) -> list[Binding]:
    return [_from_row(row) for row in store.list_bindings(strategy_id)]


async def write_cache(redis: Redis, key: str, bindings: list[Binding]) -> None:
    """重建缓存：先删后写，保证已解绑的组合不会残留。"""
    pipe = redis.pipeline()
    pipe.delete(key)
    if bindings:
        pipe.hset(
            key,
            mapping={
                item.field: json.dumps(
                    {"bar": item.bar, "enabled": int(item.enabled)},
                    sort_keys=True,
                )
                for item in bindings
            },
        )
    else:
        # 空绑定也要让 hash 存在：删键会让 read_cache 判成「缓存丢了」，
        # 回落 DB 后补种复活、halt 模式误触发急停（P0）
        pipe.hset(key, EMPTY_FIELD, "1")
    await pipe.execute()


async def read_cache(redis: Redis, key: str) -> list[Binding] | None:
    """读缓存；键不存在或内容损坏时返回 None（等价于「缓存丢了」）。"""
    raw = await redis.hgetall(key)
    if not raw:
        return None
    if len(raw) == 1 and EMPTY_FIELD in raw:
        # 合法的全空绑定（用户主动解绑），不是缓存丢失
        return []
    out: list[Binding] = []
    for field, value in raw.items():
        if field == EMPTY_FIELD:
            continue
        strategy_id, _, inst_id = str(field).partition(":")
        if not strategy_id or not inst_id:
            log.warning(f"binding cache field malformed: {field!r}")
            return None
        try:
            payload = json.loads(value)
            out.append(
                Binding(
                    strategy_id=strategy_id,
                    inst_id=inst_id,
                    bar=str(payload["bar"]),
                    enabled=bool(payload["enabled"]),
                )
            )
        except (TypeError, ValueError, KeyError):
            log.warning(f"binding cache value malformed: {field!r}={value!r}")
            return None
    return out


async def read_bindings(
    redis: Redis,
    store: SQLiteStore,
    settings: Settings,
    strategy_id: str | None = None,
) -> list[Binding]:
    """引擎每轮调用：缓存优先，`bindings_fail_mode` 决定回落策略。

    Raises:
        BindingsUnavailable: 缓存与数据库都读不到，调用方必须停止新开仓
    """
    cfg = settings.engine
    if not cfg.multi_binding:
        # 回滚开关：完全按配置的单组合跑，不读表也不读缓存
        binding = initial_binding(settings)
        return [binding] if strategy_id in (None, binding.strategy_id) else []

    try:
        cached = await read_cache(redis, cfg.bindings_key)
    except Exception as e:  # noqa: BLE001
        log.warning(f"read bindings cache failed: {e}")
        cached = None
    if cached is not None:
        return [b for b in cached if strategy_id in (None, b.strategy_id)]

    if cfg.bindings_fail_mode == "halt":
        # 严格模式：缓存丢了就停，宁可少交易也不猜
        raise BindingsUnavailable("bindings cache lost (fail_mode=halt)")

    try:
        rows = read_db(store, strategy_id)
        if not rows:
            # 只有全量读才补种；单策略查询补种会把别的策略的初始绑定漏掉
            if strategy_id is None and ensure_seed(store, settings, _now_ms()):
                rows = read_db(store, None)
            elif strategy_id is not None:
                return []
    except Exception as e:  # noqa: BLE001
        raise BindingsUnavailable(f"read bindings from db failed: {e}") from e

    try:
        await write_cache(
            redis, cfg.bindings_key, rows if strategy_id is None else read_db(store)
        )
    except Exception as e:  # noqa: BLE001
        # 缓存重建失败不影响本轮，只是下一轮还要回落读库
        log.warning(f"rebuild bindings cache failed: {e}")
    return rows


def _now_ms() -> int:
    return int(time.time() * 1000)


__all__ = [
    "Binding",
    "BindingsUnavailable",
    "ensure_seed",
    "initial_binding",
    "read_bindings",
    "read_cache",
    "read_db",
    "write_cache",
]
