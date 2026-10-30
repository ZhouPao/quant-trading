"""SQLite 状态持久化（spec §11）。

表：
- signals：信号及处理状态
- orders：订单及成交信息
- fills：成交明细（用于盈亏/风控统计与崩溃恢复）
- positions：当前持仓
- equity_snapshots：账户权益时序（回撤峰值与看板曲线）
- alerts / alert_silences：告警与静音（v2 起，见 MS4-A 详设 §4）

所有金额以 TEXT 存储 Decimal 字符串，避免浮点误差。

结构变更走 `_MIGRATIONS` + `PRAGMA user_version`：`_SCHEMA` 视为 v1 基线，
`_MIGRATIONS[i]` 负责把库升到 `i + 2`。
"""
from __future__ import annotations

import sqlite3
import threading
from decimal import Decimal
from pathlib import Path

from quant.backtest.order import Fill, Order
from quant.backtest.position import Position
from quant.backtest.signal import Signal
from quant.foundation.errors import StateError
from quant.foundation.logger import get_logger
from quant.state.order_state import can_transition

log = get_logger("state.sqlite")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS signals (
    signal_id TEXT PRIMARY KEY,
    strategy_id TEXT NOT NULL,
    side TEXT NOT NULL,
    order_type TEXT NOT NULL,
    inst_id TEXT NOT NULL,
    qty TEXT NOT NULL,
    price TEXT,
    ts INTEGER NOT NULL,
    status TEXT NOT NULL,
    created_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS orders (
    order_id TEXT PRIMARY KEY,
    signal_id TEXT REFERENCES signals(signal_id),
    status TEXT NOT NULL,
    created_ts INTEGER NOT NULL,
    filled_ts INTEGER,
    filled_price TEXT,
    filled_qty TEXT,
    fee TEXT
);

CREATE TABLE IF NOT EXISTS fills (
    order_id TEXT NOT NULL,
    inst_id TEXT NOT NULL,
    side TEXT NOT NULL,
    qty TEXT NOT NULL,
    price TEXT NOT NULL,
    fee TEXT NOT NULL,
    ts INTEGER NOT NULL,
    PRIMARY KEY (order_id, ts)
);

CREATE TABLE IF NOT EXISTS positions (
    inst_id TEXT PRIMARY KEY,
    qty TEXT NOT NULL,
    avg_price TEXT NOT NULL,
    realized_pnl TEXT NOT NULL,
    updated_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS equity_snapshots (
    ts INTEGER PRIMARY KEY,
    total_equity TEXT NOT NULL,
    avail_usdt TEXT NOT NULL,
    frozen_usdt TEXT NOT NULL,
    unrealized_pnl TEXT NOT NULL,
    margin_ratio TEXT
);
"""

# 结构变更历史。_SCHEMA 即 v1，`_MIGRATIONS[i]` 对应「升到版本 i + 2」，
# 故枚举从 2 开始（MS4-A 用 v2/v3，MS4-B 用 v4/v5，MS4-C 用 v6）。
#
# 每段脚本必须幂等（IF NOT EXISTS 等）：SQLite 的 DDL 与 user_version 写入
# 不在同一事务里，中途崩溃会从同一版本重放。
_MIGRATIONS: list[str] = [
    # v1 -> v2：告警持久化（MS4-A §4.2/§4.3）+ 删除从未被调用的死表
    """
    CREATE TABLE IF NOT EXISTS alerts (
        alert_id     TEXT PRIMARY KEY,
        rule         TEXT NOT NULL,
        level        TEXT NOT NULL,
        message      TEXT NOT NULL,
        fingerprint  TEXT NOT NULL,
        inst_id      TEXT,
        strategy_id  TEXT,
        state        TEXT NOT NULL DEFAULT 'open',
        suppressed   INTEGER NOT NULL DEFAULT 0,
        created_at   INTEGER NOT NULL,
        acked_at     INTEGER,
        acked_by     TEXT,
        resolved_at  INTEGER
    );
    CREATE INDEX IF NOT EXISTS idx_alerts_created ON alerts(created_at DESC);
    CREATE INDEX IF NOT EXISTS idx_alerts_state ON alerts(state, created_at DESC);
    CREATE INDEX IF NOT EXISTS idx_alerts_rule ON alerts(rule, created_at DESC);

    CREATE TABLE IF NOT EXISTS alert_silences (
        silence_id  TEXT PRIMARY KEY,
        rule        TEXT NOT NULL,
        inst_id     TEXT,
        until_ts    INTEGER NOT NULL,
        reason      TEXT,
        created_by  TEXT NOT NULL,
        created_at  INTEGER NOT NULL,
        revoked_at  INTEGER
    );
    CREATE INDEX IF NOT EXISTS idx_silences_active
        ON alert_silences(revoked_at, until_ts);

    DROP TABLE IF EXISTS heartbeat;
    """,
    # v2 -> v3：事件流 UNION 查询所需的索引（MS4-A §8.1）
    """
    CREATE INDEX IF NOT EXISTS idx_signals_ts ON signals(ts);
    CREATE INDEX IF NOT EXISTS idx_orders_created ON orders(created_ts);
    CREATE INDEX IF NOT EXISTS idx_fills_ts ON fills(ts);
    """,
    # v3 -> v4：风控事件与在线覆盖（MS4-B §11.1/§11.2）
    """
    CREATE TABLE IF NOT EXISTS risk_events (
        event_id     TEXT PRIMARY KEY,
        ts           INTEGER NOT NULL,
        strategy_id  TEXT,
        inst_id      TEXT NOT NULL,
        signal_id    TEXT NOT NULL,
        decision     TEXT NOT NULL,
        rule         TEXT,
        limit_name   TEXT,
        limit_value  TEXT,
        actual_value TEXT,
        qty_before   TEXT,
        qty_after    TEXT,
        equity       TEXT,
        reason       TEXT
    );
    CREATE INDEX IF NOT EXISTS idx_risk_events_ts ON risk_events(ts DESC);
    CREATE INDEX IF NOT EXISTS idx_risk_events_rule
        ON risk_events(rule, ts DESC);
    CREATE INDEX IF NOT EXISTS idx_risk_events_signal
        ON risk_events(signal_id);

    CREATE TABLE IF NOT EXISTS risk_overrides (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        strategy_id TEXT NOT NULL,
        param       TEXT NOT NULL,
        old_value   TEXT,
        new_value   TEXT NOT NULL,
        reason      TEXT,
        source      TEXT NOT NULL,
        changed_by  TEXT NOT NULL,
        changed_at  INTEGER NOT NULL,
        revoked_at  INTEGER,
        revoked_by  TEXT
    );
    CREATE INDEX IF NOT EXISTS idx_overrides_effective
        ON risk_overrides(strategy_id, param, revoked_at);
    """,
]

#: 当前期望的 schema 版本，供启动自检与测试断言。
SCHEMA_VERSION = 1 + len(_MIGRATIONS)


def _apply_migrations(conn: sqlite3.Connection) -> None:
    """建基线表并依次应用未执行的迁移。"""
    conn.executescript(_SCHEMA)
    row = conn.execute("PRAGMA user_version").fetchone()
    version = int(row[0]) if row else 0
    for target, script in enumerate(_MIGRATIONS, start=2):
        if version >= target:
            continue
        conn.executescript(script)
        # PRAGMA 不支持参数绑定；target 来自 enumerate，必为整数
        conn.execute(f"PRAGMA user_version = {target}")
        conn.commit()
    conn.commit()


class SQLiteStore:
    """信号/订单/成交/持仓/心跳的状态存储。

    线程安全：内部共享一个连接 + Lock。worker 为异步，调用处用 to_thread。
    """

    def __init__(self, db_path: Path | str) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        try:
            self._conn = sqlite3.connect(
                str(self.db_path), check_same_thread=False
            )
            self._conn.row_factory = sqlite3.Row
            with self._lock:
                _apply_migrations(self._conn)
            self._conn.commit()
        except sqlite3.Error as e:
            raise StateError(f"failed to open sqlite db {self.db_path}: {e}") from e

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # ------------------------------------------------------------------
    # signals
    # ------------------------------------------------------------------

    def save_signal(self, signal: Signal, status: str, now_ms: int = 0) -> None:
        """插入或更新信号状态。"""
        sql = """
        INSERT INTO signals (signal_id, strategy_id, side, order_type, inst_id,
                             qty, price, ts, status, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(signal_id) DO UPDATE SET status=excluded.status
        """
        self._execute(
            sql,
            (
                signal.signal_id or f"{signal.strategy_id}-{signal.ts}",
                signal.strategy_id,
                signal.side,
                signal.order_type,
                signal.inst_id,
                str(signal.qty),
                str(signal.price) if signal.price is not None else None,
                signal.ts,
                status,
                now_ms or signal.ts,
            ),
        )

    def get_signal(self, signal_id: str) -> dict | None:
        row = self._query_one(
            "SELECT * FROM signals WHERE signal_id = ?", (signal_id,)
        )
        return dict(row) if row else None

    def get_signal_status(self, signal_id: str) -> str | None:
        row = self._query_one(
            "SELECT status FROM signals WHERE signal_id = ?", (signal_id,)
        )
        return row["status"] if row else None

    # ------------------------------------------------------------------
    # orders / fills
    # ------------------------------------------------------------------

    def save_order(self, order: Order) -> None:
        sql = """
        INSERT INTO orders (order_id, signal_id, status, created_ts)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(order_id) DO UPDATE SET status=excluded.status
        """
        self._execute(
            sql,
            (
                order.order_id,
                order.signal.signal_id,
                order.status,
                order.created_ts,
            ),
        )

    def update_order_fill(
        self, order_id: str, fill: Fill, status: str = "filled"
    ) -> bool:
        """**累加**成交并重算均价，同时推进状态。

        部分成交会分多次到达（REST 分批回包或 WS 逐笔推送）。旧实现直接
        覆盖 `filled_qty/filled_price`，第二次部分成交会把第一次的量冲掉，
        导致本地成交量偏小、均价错误，进而影响持仓与盈亏。

        Args:
            fill: 本次新增的成交（**增量**，不是累计量）
            status: 推进到的目标状态；`partially_filled` 或 `filled`

        Returns:
            状态是否被推进（成交累加总是执行）。
        """
        row = self._query_one(
            "SELECT status, filled_qty, filled_price, fee FROM orders "
            "WHERE order_id = ?",
            (order_id,),
        )
        if row is None:
            raise StateError(f"order not found: {order_id}")

        prev_qty = Decimal(str(row["filled_qty"] or "0"))
        prev_px = Decimal(str(row["filled_price"] or "0"))
        prev_fee = Decimal(str(row["fee"] or "0"))

        total_qty = prev_qty + fill.qty
        if total_qty > 0:
            avg_px = (
                prev_qty * prev_px + fill.qty * fill.price
            ) / total_qty
        else:
            avg_px = fill.price

        current = str(row["status"])
        target = status if can_transition(current, status) else current
        if target != status:
            log.warning(
                f"illegal transition {order_id}: {current} -> {status}, ignored"
            )

        self._execute(
            """
            UPDATE orders SET status=?, filled_ts=?, filled_price=?,
                              filled_qty=?, fee=?
            WHERE order_id=?
            """,
            (
                target,
                fill.ts,
                str(avg_px),
                str(total_qty),
                str(prev_fee + fill.fee),
                order_id,
            ),
        )
        return target != current

    def get_order_fill_state(self, order_id: str) -> dict:
        """订单的累计成交状态（部分成交时用于算增量）。"""
        row = self._query_one(
            "SELECT status, filled_qty, filled_price, fee FROM orders "
            "WHERE order_id = ?",
            (order_id,),
        )
        if row is None:
            return {
                "status": "",
                "filled_qty": Decimal("0"),
                "filled_price": Decimal("0"),
                "fee": Decimal("0"),
            }
        return {
            "status": str(row["status"]),
            "filled_qty": Decimal(str(row["filled_qty"] or "0")),
            "filled_price": Decimal(str(row["filled_price"] or "0")),
            "fee": Decimal(str(row["fee"] or "0")),
        }

    def update_order_status(self, order_id: str, status: str) -> bool:
        """更新订单状态（对账以远端为准时使用）。

        走状态机校验：非法迁移（如 `filled → pending`）只记 WARNING 并保持
        原状态，不抛异常——否则一轮对账会被单个脏数据整体打断。

        Returns:
            是否实际写入。
        """
        row = self._query_one(
            "SELECT status FROM orders WHERE order_id = ?", (order_id,)
        )
        if row is None:
            return False
        current = str(row["status"])
        if not can_transition(current, status):
            log.warning(
                f"illegal transition {order_id}: {current} -> {status}, ignored"
            )
            return False
        if current == status:
            return False
        self._execute(
            "UPDATE orders SET status = ? WHERE order_id = ?",
            (status, order_id),
        )
        return True

    #: 仍需对账的订单状态（未终结且有成交/挂单可能）。
    OPEN_ORDER_STATUSES = ("pending", "live", "partially_filled")

    def get_open_orders(self) -> list[dict]:
        """返回本地未终结订单（含 inst_id/created_ts，供对账使用）。

        必须覆盖 pending/live/partially_filled 三态：只看 pending 会漏掉
        已进挂单簿（live）与部分成交（partially_filled）的订单，那些恰恰
        是最需要继续跟踪的。
        """
        placeholders = ", ".join("?" for _ in self.OPEN_ORDER_STATUSES)
        rows = self._query_all(
            f"""
            SELECT o.order_id, o.signal_id, o.status, o.created_ts, s.inst_id
            FROM orders o
            LEFT JOIN signals s ON o.signal_id = s.signal_id
            WHERE o.status IN ({placeholders})
            ORDER BY o.created_ts
            """,
            self.OPEN_ORDER_STATUSES,
        )
        return [dict(r) for r in rows]

    def get_order_status_counts(self) -> dict[str, int]:
        """按订单状态统计数量（监控撤单率用）。"""
        rows = self._query_all(
            "SELECT status, COUNT(*) AS n FROM orders GROUP BY status"
        )
        return {r["status"]: int(r["n"]) for r in rows}

    def get_recent_orders(self, n: int) -> list[dict]:
        """最近 n 笔订单（含信号上下文），按创建时间倒序。"""
        rows = self._query_all(
            """
            SELECT o.order_id, o.status, o.created_ts, o.filled_ts,
                   o.filled_price, o.filled_qty, o.fee,
                   s.strategy_id, s.inst_id, s.side, s.order_type,
                   s.qty, s.price
            FROM orders o
            LEFT JOIN signals s ON o.signal_id = s.signal_id
            ORDER BY o.created_ts DESC
            LIMIT ?
            """,
            (n,),
        )
        return [dict(r) for r in rows]

    def save_fill(self, fill: Fill) -> None:
        self._execute(
            """
            INSERT OR REPLACE INTO fills
                (order_id, inst_id, side, qty, price, fee, ts)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                fill.order_id,
                fill.inst_id,
                fill.side,
                str(fill.qty),
                str(fill.price),
                str(fill.fee),
                fill.ts,
            ),
        )

    def get_recent_trades(self, n: int) -> list[Fill]:
        """最近 n 笔成交（按 ts 升序返回）。"""
        rows = self._query_all(
            "SELECT * FROM fills ORDER BY ts DESC LIMIT ?", (n,)
        )
        rows = list(reversed(rows))
        return [
            Fill(
                order_id=r["order_id"],
                inst_id=r["inst_id"],
                side=r["side"],
                qty=Decimal(r["qty"]),
                price=Decimal(r["price"]),
                fee=Decimal(r["fee"]),
                ts=int(r["ts"]),
            )
            for r in rows
        ]

    # ------------------------------------------------------------------
    # positions
    # ------------------------------------------------------------------

    def get_position(self, inst_id: str) -> Position:
        row = self._query_one(
            "SELECT * FROM positions WHERE inst_id = ?", (inst_id,)
        )
        if row is None:
            return Position(inst_id=inst_id)
        return Position(
            inst_id=row["inst_id"],
            qty=Decimal(row["qty"]),
            avg_price=Decimal(row["avg_price"]),
            realized_pnl=Decimal(row["realized_pnl"]),
        )

    def update_position(self, position: Position) -> None:
        self._execute(
            """
            INSERT INTO positions (inst_id, qty, avg_price, realized_pnl, updated_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(inst_id) DO UPDATE SET
                qty=excluded.qty,
                avg_price=excluded.avg_price,
                realized_pnl=excluded.realized_pnl,
                updated_at=excluded.updated_at
            """,
            (
                position.inst_id,
                str(position.qty),
                str(position.avg_price),
                str(position.realized_pnl),
                0,
            ),
        )

    def get_all_positions(self) -> list[Position]:
        rows = self._query_all("SELECT * FROM positions")
        return [
            Position(
                inst_id=r["inst_id"],
                qty=Decimal(r["qty"]),
                avg_price=Decimal(r["avg_price"]),
                realized_pnl=Decimal(r["realized_pnl"]),
            )
            for r in rows
        ]

    # ------------------------------------------------------------------
    # pnl statistics
    # ------------------------------------------------------------------

    def get_today_realized_pnl(self, day_start_ms: int) -> Decimal:
        """自 day_start_ms 起的回合已实现盈亏（FIFO 配对，含手续费）。"""
        fills = self._fills_since(day_start_ms)
        return _realized_pnl(fills)

    def get_all_fills(self) -> list[Fill]:
        """全部成交（按 ts 升序）。用于看板 FIFO 回合盈亏统计。"""
        return self._fills_since(0)

    # ------------------------------------------------------------------
    # risk events / overrides (MS4-B)
    # ------------------------------------------------------------------

    def insert_risk_event(
        self,
        *,
        event_id: str,
        ts: int,
        inst_id: str,
        signal_id: str,
        decision: str,
        strategy_id: str | None = None,
        rule: str | None = None,
        limit_name: str | None = None,
        limit_value: str | None = None,
        actual_value: str | None = None,
        qty_before: str | None = None,
        qty_after: str | None = None,
        equity: str | None = None,
        reason: str | None = None,
    ) -> None:
        """写入一条风控事件（`event_id` 相同则忽略，天然幂等）。"""
        self._execute(
            """
            INSERT OR IGNORE INTO risk_events
                (event_id, ts, strategy_id, inst_id, signal_id, decision,
                 rule, limit_name, limit_value, actual_value,
                 qty_before, qty_after, equity, reason)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                event_id,
                ts,
                strategy_id,
                inst_id,
                signal_id,
                decision,
                rule,
                limit_name,
                limit_value,
                actual_value,
                qty_before,
                qty_after,
                equity,
                reason,
            ),
        )

    def list_risk_events(
        self,
        limit: int = 100,
        offset: int = 0,
        *,
        rule: str | None = None,
        decision: str | None = None,
        strategy_id: str | None = None,
        since: int | None = None,
        until: int | None = None,
    ) -> list[dict]:
        """按时间倒序列出风控事件。"""
        where, params = _risk_event_filters(
            rule=rule,
            decision=decision,
            strategy_id=strategy_id,
            since=since,
            until=until,
        )
        rows = self._query_all(
            f"SELECT * FROM risk_events{where} "
            "ORDER BY ts DESC, event_id LIMIT ? OFFSET ?",
            (*params, limit, offset),
        )
        return [dict(r) for r in rows]

    def count_risk_events(
        self,
        *,
        rule: str | None = None,
        decision: str | None = None,
        strategy_id: str | None = None,
        since: int | None = None,
        until: int | None = None,
    ) -> int:
        where, params = _risk_event_filters(
            rule=rule,
            decision=decision,
            strategy_id=strategy_id,
            since=since,
            until=until,
        )
        row = self._query_one(
            f"SELECT COUNT(*) AS n FROM risk_events{where}", params
        )
        return int(row["n"]) if row else 0

    def get_effective_overrides(
        self, strategy_id: str | None = None
    ) -> dict[str, str]:
        """返回未撤销的在线覆盖：``{"{strategy}:{param}": value}``。

        键格式与 Redis hash ``quant:risk:overrides`` 一致，调用方可以直接
        喂给 `quant.risk.limits.parse_override_hash`。

        同一个 `(strategy_id, param)` 可能有多条历史行，取最新一条；
        没有任何未撤销行的字段视为「无覆盖」，回落 ②③ 取严结果。
        """
        where = "WHERE o.revoked_at IS NULL"
        params: list[object] = []
        if strategy_id is not None:
            where += " AND o.strategy_id = ?"
            params.append(strategy_id)
        rows = self._query_all(
            f"""
            SELECT o.strategy_id, o.param, o.new_value
              FROM risk_overrides o
              JOIN (
                    SELECT strategy_id, param, MAX(id) AS max_id
                      FROM risk_overrides
                     WHERE revoked_at IS NULL
                     GROUP BY strategy_id, param
              ) t ON o.id = t.max_id
            {where}
            """,
            tuple(params),
        )
        return {
            f"{r['strategy_id']}:{r['param']}": str(r["new_value"])
            for r in rows
        }

    # ------------------------------------------------------------------
    # equity snapshots
    # ------------------------------------------------------------------

    def save_equity_snapshot(
        self,
        ts: int,
        total_equity: Decimal,
        avail_usdt: Decimal,
        frozen_usdt: Decimal,
        unrealized_pnl: Decimal,
        margin_ratio: Decimal | None,
    ) -> None:
        """写入一条权益快照（同 ts 覆盖）。"""
        self._execute(
            """
            INSERT INTO equity_snapshots
                (ts, total_equity, avail_usdt, frozen_usdt,
                 unrealized_pnl, margin_ratio)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(ts) DO UPDATE SET
                total_equity=excluded.total_equity,
                avail_usdt=excluded.avail_usdt,
                frozen_usdt=excluded.frozen_usdt,
                unrealized_pnl=excluded.unrealized_pnl,
                margin_ratio=excluded.margin_ratio
            """,
            (
                ts,
                str(total_equity),
                str(avail_usdt),
                str(frozen_usdt),
                str(unrealized_pnl),
                str(margin_ratio) if margin_ratio is not None else None,
            ),
        )

    def get_equity_history(
        self, since_ms: int, limit: int = 2000
    ) -> list[dict[str, object]]:
        """按时间升序返回权益快照（取最近 limit 条）。"""
        rows = self._query_all(
            """
            SELECT * FROM equity_snapshots WHERE ts >= ?
            ORDER BY ts DESC LIMIT ?
            """,
            (since_ms, limit),
        )
        return [
            {
                "ts": int(r["ts"]),
                "total_equity": r["total_equity"],
                "avail_usdt": r["avail_usdt"],
                "frozen_usdt": r["frozen_usdt"],
                "unrealized_pnl": r["unrealized_pnl"],
                "margin_ratio": r["margin_ratio"],
            }
            for r in reversed(rows)
        ]

    def get_peak_equity(self) -> Decimal | None:
        """历史最高权益；无记录返回 None。

        以 REAL 排序仅用于挑出最大值，返回值仍是原始 Decimal 字符串。
        """
        row = self._query_one(
            """
            SELECT total_equity FROM equity_snapshots
            ORDER BY CAST(total_equity AS REAL) DESC LIMIT 1
            """,
            (),
        )
        return Decimal(row["total_equity"]) if row else None

    def purge_before(self, cutoff_ms: int) -> dict[str, int]:
        """删除早于 cutoff_ms 的历史记录，返回各表删除行数。

        权益快照保留历史峰值那一行：`get_peak_equity` 依赖它恢复回撤峰值，
        若一并删掉，monitor 重启后回撤会被低估。
        """
        statements = (
            ("fills", "DELETE FROM fills WHERE ts < ?", (cutoff_ms,)),
            ("orders", "DELETE FROM orders WHERE created_ts < ?", (cutoff_ms,)),
            ("signals", "DELETE FROM signals WHERE ts < ?", (cutoff_ms,)),
            (
                "equity_snapshots",
                """
                DELETE FROM equity_snapshots WHERE ts < ? AND ts <> (
                    SELECT ts FROM equity_snapshots
                    ORDER BY CAST(total_equity AS REAL) DESC LIMIT 1
                )
                """,
                (cutoff_ms,),
            ),
        )
        deleted: dict[str, int] = {}
        with self._lock:
            try:
                for table, sql, params in statements:
                    deleted[table] = self._conn.execute(sql, params).rowcount
                self._conn.commit()
            except sqlite3.Error as e:
                self._conn.rollback()
                raise StateError(f"sqlite purge failed: {e}") from e
        return deleted

    # ------------------------------------------------------------------
    # internal
    # ------------------------------------------------------------------

    def _fills_since(self, since_ms: int) -> list[Fill]:
        rows = self._query_all(
            "SELECT * FROM fills WHERE ts >= ? ORDER BY ts", (since_ms,)
        )
        return [
            Fill(
                order_id=r["order_id"],
                inst_id=r["inst_id"],
                side=r["side"],
                qty=Decimal(r["qty"]),
                price=Decimal(r["price"]),
                fee=Decimal(r["fee"]),
                ts=int(r["ts"]),
            )
            for r in rows
        ]

    def _execute(self, sql: str, params: tuple) -> None:
        with self._lock:
            try:
                self._conn.execute(sql, params)
                self._conn.commit()
            except sqlite3.Error as e:
                raise StateError(f"sqlite execute failed: {e}") from e

    def _query_one(self, sql: str, params: tuple) -> sqlite3.Row | None:
        with self._lock:
            try:
                return self._conn.execute(sql, params).fetchone()
            except sqlite3.Error as e:
                raise StateError(f"sqlite query failed: {e}") from e

    def _query_all(self, sql: str, params: tuple = ()) -> list[sqlite3.Row]:
        with self._lock:
            try:
                return self._conn.execute(sql, params).fetchall()
            except sqlite3.Error as e:
                raise StateError(f"sqlite query failed: {e}") from e


def _risk_event_filters(
    *,
    rule: str | None,
    decision: str | None,
    strategy_id: str | None,
    since: int | None,
    until: int | None,
) -> tuple[str, tuple]:
    """拼 risk_events 的 WHERE 子句（全为等值/区间条件，无需转义）。"""
    clauses: list[str] = []
    params: list[object] = []
    for column, value in (
        ("rule", rule),
        ("decision", decision),
        ("strategy_id", strategy_id),
    ):
        if value is not None:
            clauses.append(f"{column} = ?")
            params.append(value)
    if since is not None:
        clauses.append("ts >= ?")
        params.append(since)
    if until is not None:
        clauses.append("ts <= ?")
        params.append(until)
    where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
    return where, tuple(params)


def _realized_pnl(fills: list[Fill]) -> Decimal:
    """FIFO 回合已实现盈亏（未平仓部分不计）。"""
    pos_qty = Decimal("0")
    avg_price = Decimal("0")
    lots: list[list[Decimal]] = []
    total = Decimal("0")

    for fill in fills:
        signed = fill.qty if fill.side == "buy" else -fill.qty
        fee_per_unit = fill.fee / fill.qty

        if pos_qty == 0:
            pos_qty, avg_price = signed, fill.price
            lots.append([signed, fee_per_unit])
            continue

        if (pos_qty > 0) == (signed > 0):
            avg_price = (
                pos_qty * avg_price + signed * fill.price
            ) / (pos_qty + signed)
            pos_qty += signed
            lots.append([signed, fee_per_unit])
            continue

        closing = min(abs(signed), abs(pos_qty))
        if pos_qty > 0:
            pnl = (fill.price - avg_price) * closing
        else:
            pnl = (avg_price - fill.price) * closing

        consume = closing
        entry_fee = Decimal("0")
        while consume > 0:
            lot = lots[0]
            take = min(consume, abs(lot[0]))
            entry_fee += lot[1] * take
            if take == abs(lot[0]):
                lots.pop(0)
            else:
                lot[0] -= take if lot[0] > 0 else -take
            consume -= take

        total += pnl - entry_fee - fee_per_unit * closing

        old_qty = pos_qty
        pos_qty += signed
        if pos_qty == 0:
            avg_price = Decimal("0")
            lots.clear()
        elif abs(signed) > abs(old_qty):
            avg_price = fill.price
            lots = [[pos_qty, fee_per_unit]]

    return total


__all__ = ["SQLiteStore"]
