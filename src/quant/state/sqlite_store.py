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

import hashlib
import json
import sqlite3
import threading
from collections.abc import Callable
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
    # v4 -> v5：策略参数两层合并与回测结果落库（MS4-C §3.2 / §10）
    #
    # 详设 §10 把本切片标为 v6（MS4-B 预留了 v5），但 MS4-B 实际只用到 v4，
    # 迁移版本又必须连续，所以这里按真实序列用 v5。
    # strategy_bindings（§4.1）与 optimize_jobs（§6）留到各自切片再加迁移。
    """
    CREATE TABLE IF NOT EXISTS strategy_params (
        strategy_id TEXT NOT NULL,
        param       TEXT NOT NULL,
        value       TEXT NOT NULL,
        updated_at  INTEGER NOT NULL,
        PRIMARY KEY (strategy_id, param)
    );

    CREATE TABLE IF NOT EXISTS strategy_param_changes (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        strategy_id TEXT NOT NULL,
        param       TEXT NOT NULL,
        old_value   TEXT,
        new_value   TEXT NOT NULL,
        reason      TEXT,
        changed_by  TEXT NOT NULL,
        changed_at  INTEGER NOT NULL,
        git_commit  TEXT
    );
    CREATE INDEX IF NOT EXISTS idx_param_changes
        ON strategy_param_changes(strategy_id, changed_at DESC);

    CREATE TABLE IF NOT EXISTS backtest_runs (
        run_id      TEXT PRIMARY KEY,
        strategy_id TEXT NOT NULL,
        inst_id     TEXT NOT NULL,
        bar         TEXT NOT NULL,
        start_ts    INTEGER NOT NULL,
        end_ts      INTEGER NOT NULL,
        warmup_bars INTEGER NOT NULL DEFAULT 200,
        params      TEXT NOT NULL,
        kind        TEXT NOT NULL,
        status      TEXT NOT NULL,
        created_at  INTEGER NOT NULL,
        started_at  INTEGER,
        finished_at INTEGER,
        error       TEXT,
        git_commit  TEXT
    );
    CREATE INDEX IF NOT EXISTS idx_runs_created
        ON backtest_runs(created_at DESC);
    CREATE INDEX IF NOT EXISTS idx_runs_filter
        ON backtest_runs(strategy_id, inst_id, bar, created_at DESC);
    CREATE INDEX IF NOT EXISTS idx_runs_status
        ON backtest_runs(status, created_at DESC);

    CREATE TABLE IF NOT EXISTS backtest_metrics (
        run_id TEXT PRIMARY KEY REFERENCES backtest_runs(run_id),
        total_return TEXT, annual_return TEXT, sharpe TEXT,
        max_drawdown TEXT, win_rate TEXT, profit_factor TEXT,
        trade_count INTEGER, avg_holding_bars TEXT,
        calmar TEXT, sortino TEXT,
        max_consecutive_losses INTEGER,
        max_consecutive_loss_amount TEXT,
        exposure_ratio TEXT,
        monthly_returns TEXT,
        compounded_return TEXT, profitable_segments INTEGER,
        avg_oos_sharpe TEXT, total_oos_trades INTEGER, modal_params TEXT,
        pine_total_return TEXT, pine_sharpe TEXT,
        pine_max_drawdown TEXT, pine_deviation TEXT
    );

    CREATE TABLE IF NOT EXISTS backtest_artifacts (
        run_id     TEXT NOT NULL,
        kind       TEXT NOT NULL,
        path       TEXT NOT NULL,
        size       INTEGER,
        created_at INTEGER NOT NULL,
        PRIMARY KEY (run_id, kind)
    );
    """,
    # v5 -> v6：策略启停矩阵（MS4-C §4.1）
    #
    # 主键只有 (strategy_id, inst_id)，bar 不在主键里：同一策略同一品种
    # 只允许一个周期，否则两个周期会对同一持仓重复下单。
    """
    CREATE TABLE IF NOT EXISTS strategy_bindings (
        strategy_id TEXT NOT NULL,
        inst_id     TEXT NOT NULL,
        bar         TEXT NOT NULL,
        enabled     INTEGER NOT NULL DEFAULT 1,
        updated_at  INTEGER NOT NULL,
        PRIMARY KEY (strategy_id, inst_id)
    );
    """,
    # v6 -> v7：死信落库（MS4-B §10.1、§11.3）
    #
    # Redis LIST 继续保留给 monitor 计数告警，SQLite 作为可查询的权威记录。
    # stage 收敛为 parse / submit / reconcile；风控拒绝不进死信（那属于
    # risk_events 的正常业务结果）。
    """
    CREATE TABLE IF NOT EXISTS dead_letters (
        dead_id       TEXT PRIMARY KEY,
        signal_id     TEXT,
        stage         TEXT NOT NULL,
        reason        TEXT NOT NULL,
        payload       TEXT NOT NULL,
        created_at    INTEGER NOT NULL,
        replayed_at   INTEGER,
        replay_result TEXT
    );
    CREATE INDEX IF NOT EXISTS idx_dead_created
        ON dead_letters(created_at DESC);
    CREATE INDEX IF NOT EXISTS idx_dead_stage
        ON dead_letters(stage, created_at DESC);
    """,
    # v7 -> v8：三项对账差异（M8-18）
    #
    # severity 区分「可自动修复」（auto，本地已被校正）与「需人工确认」
    # （manual）。持续存在的差异不每轮插新行：同 (kind,key,severity) 的
    # 未处理行原地更新数值与时间，处理后再出现才新增一条。
    """
    CREATE TABLE IF NOT EXISTS reconciliations (
        rec_id       TEXT PRIMARY KEY,
        ts           INTEGER NOT NULL,
        kind         TEXT NOT NULL,
        key          TEXT NOT NULL,
        local_value  TEXT,
        remote_value TEXT,
        diff         TEXT,
        severity     TEXT NOT NULL,
        resolved     INTEGER NOT NULL DEFAULT 0,
        resolved_at  INTEGER,
        resolved_by  TEXT,
        note         TEXT
    );
    CREATE INDEX IF NOT EXISTS idx_rec_created
        ON reconciliations(ts DESC);
    CREATE INDEX IF NOT EXISTS idx_rec_kind
        ON reconciliations(kind, ts DESC);
    CREATE INDEX IF NOT EXISTS idx_rec_open
        ON reconciliations(resolved, ts DESC);
    """,
    # v8 -> v9：回测指标补资金费率成本（M4-15 收尾）
    #
    # 这一列原先只在 `BacktestResult.metrics` 里存在（回测结束即丢），
    # 历史列表与详情刷新后都看不到「这笔回测到底付了多少资金费」。
    # 改走增量迁移而不是改 v5 的建表语句：v5 早已在用户库里执行过，
    # 直接改它等于让存量库永远缺列；`user_version` 保证本迁移只跑一次。
    """
    ALTER TABLE backtest_metrics ADD COLUMN funding_cost TEXT;
    """,
    # v9 -> v10：参数寻优任务落库（MS4-C §6.2 / §10.4，AGENTS.md 已知问题 #1）
    #
    # 详设 §10.4 给出完整 DDL，但迁移列表里长期只有「留到各自切片再加」的占位
    # 注释，optimize_jobs 表从未真正创建。当前 optimize 命令同步跑完即返，进度只
    # 活在 Redis（TTL 300s），dashboard 重启后查不到「跑到哪了」。
    # 本段只补齐这张表：建表用 IF NOT EXISTS，天然幂等，不参与任何运行逻辑；
    # 真正的进度上报（M4-13，写 optimize_jobs.completed + Redis 1s 轮询）属独立
    # 功能，留待后续切片。SCHEMA_VERSION 会随列表长度自动变为 10。
    """
    CREATE TABLE IF NOT EXISTS optimize_jobs (
        job_id      TEXT PRIMARY KEY,
        run_id      TEXT REFERENCES backtest_runs(run_id),
        grid        TEXT NOT NULL,          -- JSON
        metric      TEXT NOT NULL,
        n_jobs      INTEGER NOT NULL,
        total       INTEGER NOT NULL,
        completed   INTEGER NOT NULL DEFAULT 0,
        status      TEXT NOT NULL,
        created_at  INTEGER NOT NULL,
        finished_at INTEGER
    );
    """,
    # v10 -> v11：一次性初始化标记的持久化位置。
    # 「strategy_bindings 表为空」既可能是从未初始化，也可能是用户主动解绑了
    # 全部组合；没有持久标记就无法区分，ensure_seed 会把用户解绑的结果复活
    # （P0）。用通用 kv 表而不是给 bindings 表加哨兵行，避免污染矩阵数据。
    """
    CREATE TABLE IF NOT EXISTS app_meta (
        key   TEXT PRIMARY KEY,
        value TEXT NOT NULL
    );
    """,
]

#: 当前期望的 schema 版本，供启动自检与测试断言。
SCHEMA_VERSION = 1 + len(_MIGRATIONS)


def _apply_migrations(conn: sqlite3.Connection) -> None:
    """建基线表并依次应用未执行的迁移。

    每段迁移都必须可「重放」：存量库可能由更早的代码版本创建（例如某列原本
    写在基线 ``_SCHEMA`` 里，后被抽成一段增量迁移）。此时 ``user_version`` 仍判定
    该段「未应用」，再次执行 ``ALTER ... ADD COLUMN`` 会抛 ``duplicate column``。
    这类「列已存在」属于幂等安全的已应用态，跳过即可，绝不该阻断进程启动——
    这与「迁移必须幂等、崩溃同版本可重放」的硬规则一致。
    """
    conn.executescript(_SCHEMA)
    row = conn.execute("PRAGMA user_version").fetchone()
    version = int(row[0]) if row else 0
    for target, script in enumerate(_MIGRATIONS, start=2):
        if version >= target:
            continue
        try:
            conn.executescript(script)
        except sqlite3.OperationalError as exc:
            # 幂等重放：列/索引已存在等可安全忽略；其余错误照常上抛。
            if "duplicate column" not in str(exc).lower():
                raise
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
                # qty 可为空（服务端算量前的信号）；落库统一用 "0"，
                # 避免把 None 写成字符串 "None" 污染看板展示
                str(signal.qty) if signal.qty is not None else "0",
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
        """写入订单；已存在时按状态机推进，禁止回退。

        同一 `order_id` 可能被重复落库（重放、崩溃恢复、对账后重申）。
        直接覆盖状态会把 `filled` 打回 `pending`，成交量与持仓随之失真，
        因此走后与 `update_order_status` 相同的迁移校验。
        """
        self._execute(
            """
            INSERT OR IGNORE INTO orders (order_id, signal_id, status, created_ts)
            VALUES (?, ?, ?, ?)
            """,
            (
                order.order_id,
                order.signal.signal_id,
                order.status,
                order.created_ts,
            ),
        )
        self.update_order_status(order.order_id, order.status)

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

    def get_order(self, order_id: str) -> dict | None:
        """单笔订单（含信号上下文）；手动撤单/改单与幂等判断用。"""
        row = self._query_one(
            """
            SELECT o.order_id, o.status, o.created_ts, o.filled_ts,
                   o.filled_price, o.filled_qty, o.fee,
                   s.signal_id, s.strategy_id, s.inst_id, s.side,
                   s.order_type, s.qty, s.price
            FROM orders o
            LEFT JOIN signals s ON o.signal_id = s.signal_id
            WHERE o.order_id = ?
            """,
            (order_id,),
        )
        return dict(row) if row else None

    def update_signal_order(
        self, signal_id: str, qty: Decimal, price: Decimal | None
    ) -> bool:
        """改单后回写信号的量与价（M8-17）。

        `orders` 只存成交信息，看板的品种/方向/数量/价格全部来自 JOIN
        `signals`；不更新这里，改单成功后页面仍显示旧值。
        """
        changed = self._execute(
            "UPDATE signals SET qty = ?, price = ? WHERE signal_id = ?",
            (str(qty), str(price) if price is not None else None, signal_id),
        )
        return changed > 0

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

    def get_today_pnl_by_inst(self, day_start_ms: int) -> dict[str, dict]:
        """M1-8 今日盈亏按品种拆分。

        返回 {inst_id: {realized, fee, turnover}}，口径与 get_today_realized_pnl 一致。
        """
        fills = self._fills_since(day_start_ms)
        by_inst: dict[str, list[Fill]] = {}
        for fill in fills:
            by_inst.setdefault(fill.inst_id, []).append(fill)

        result = {}
        for inst_id, inst_fills in by_inst.items():
            realized = _realized_pnl(inst_fills)
            fee = sum((f.fee for f in inst_fills), Decimal("0"))
            turnover = sum((f.qty * f.price for f in inst_fills), Decimal("0"))
            result[inst_id] = {
                "realized": realized,
                "fee": fee,
                "turnover": turnover,
            }
        return result

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

    def risk_rule_stats(self, since: int | None = None) -> dict[str, dict]:
        """按规则聚合触发次数与最近一次详情（M7-9）。

        看板风控页要回答「这条规则今天触发了几次、最后一次是什么」。
        逐规则各查一次会放大查询次数，这里一次聚合出计数，再按
        `(rule, max_ts)` 取最近一条——`rule` 上已有 `(rule, ts DESC)` 索引。

        Args:
            since: 时间下界（UTC ms）；看板传当日 UTC+8 零点。None 表示不限

        Returns:
            `{rule: {"count": n, "last": {...风险事件行...}}}`。
            `rule` 为空的事件不计入（无法归属到具体规则）。
        """
        where = " WHERE ts >= ?" if since is not None else ""
        params: tuple = (since,) if since is not None else ()
        rows = self._query_all(
            "SELECT rule, COUNT(*) AS n, MAX(ts) AS last_ts FROM risk_events"
            f"{where} GROUP BY rule",
            params,
        )
        stats: dict[str, dict] = {}
        for row in rows:
            rule = row["rule"]
            if not rule:
                continue
            latest = self._query_one(
                "SELECT * FROM risk_events WHERE rule = ? AND ts = ? LIMIT 1",
                (rule, row["last_ts"]),
            )
            stats[str(rule)] = {
                "count": int(row["n"]),
                "last": dict(latest) if latest else None,
            }
        return stats

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

    def insert_risk_override(
        self,
        strategy_id: str,
        param: str,
        new_value: str,
        *,
        changed_at: int,
        changed_by: str,
        reason: str | None = None,
        old_value: str | None = None,
        source: str = "dashboard",
    ) -> int:
        """写一条阈值变更审计（M7-14），返回自增 id。

        只追加不更新：同一字段的多次调整是独立的变更记录，
        不能靠覆盖旧行来「保持整洁」——那就没有审计了。
        """
        row = self._query_one(
            """
            INSERT INTO risk_overrides (
                strategy_id, param, old_value, new_value, reason, source,
                changed_by, changed_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            RETURNING id
            """,
            (
                strategy_id,
                param,
                old_value,
                new_value,
                reason,
                source,
                changed_by,
                changed_at,
            ),
        )
        return int(row["id"]) if row else 0

    def get_active_risk_override(
        self, strategy_id: str, param: str
    ) -> dict | None:
        """某字段当前生效的覆盖（`revoked_at IS NULL` 的最新一条）。"""
        row = self._query_one(
            """
            SELECT id, strategy_id, param, old_value, new_value, reason,
                   source, changed_by, changed_at
              FROM risk_overrides
             WHERE strategy_id = ? AND param = ? AND revoked_at IS NULL
             ORDER BY id DESC
             LIMIT 1
            """,
            (strategy_id, param),
        )
        return dict(row) if row else None

    def revoke_risk_overrides(
        self,
        strategy_id: str,
        param: str,
        *,
        revoked_at: int,
        revoked_by: str,
    ) -> int:
        """撤销某字段的全部生效覆盖，返回撤销条数。

        一次撤销所有未撤销行（而不是只撤最新一条）：历史遗留的多条
        生效行会让「清除覆盖」看起来没生效。
        """
        return self._execute(
            """
            UPDATE risk_overrides
               SET revoked_at = ?, revoked_by = ?
             WHERE strategy_id = ? AND param = ? AND revoked_at IS NULL
            """,
            (revoked_at, revoked_by, strategy_id, param),
        )

    def list_risk_overrides(
        self,
        limit: int = 50,
        offset: int = 0,
        *,
        strategy_id: str | None = None,
        include_revoked: bool = True,
    ) -> tuple[list[dict], int]:
        """阈值变更审计记录，返回 (行, 命中总数)，按时间倒序。"""
        clauses: list[str] = []
        params: list[object] = []
        if strategy_id is not None:
            clauses.append("strategy_id = ?")
            params.append(strategy_id)
        if not include_revoked:
            clauses.append("revoked_at IS NULL")
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        total_row = self._query_one(
            f"SELECT COUNT(*) AS n FROM risk_overrides {where}", tuple(params)
        )
        rows = self._query_all(
            f"""
            SELECT id, strategy_id, param, old_value, new_value, reason,
                   source, changed_by, changed_at, revoked_at, revoked_by
              FROM risk_overrides {where}
             ORDER BY changed_at DESC, id DESC
             LIMIT ? OFFSET ?
            """,
            (*params, limit, offset),
        )
        return [dict(r) for r in rows], int(total_row["n"]) if total_row else 0

    # ------------------------------------------------------------------
    # 今日事件流（M1-6，MS4-A §8）
    # ------------------------------------------------------------------

    #: 事件流支持的四种类型，顺序即 UNION 分支顺序（也用作展示排序偏好）
    TIMELINE_KINDS: tuple[str, ...] = ("alert", "signal", "order", "fill")

    def daily_timeline(
        self,
        since_ms: int,
        limit: int = 50,
        offset: int = 0,
        kinds: list[str] | None = None,
    ) -> list[dict]:
        """当日信号/下单/成交/告警四表合并，按事件时间倒序。

        用单条 SQL 而不是分头查再在 Python 里合并：只有 `ORDER BY + LIMIT`
        才能正确分页，否则跨类型时每路各取 N 条会错位。

        时间一律取「事件发生时间」（`signals.ts` / `orders.created_ts` /
        `fills.ts` / `alerts.created_at`）。用写入时间会让补评估的历史 bar
        被排到今天，时间轴失真。

        `kinds` 为空表示全部；非法值忽略并回落为全部，不报错——前端筛选
        组件拼错参数不该白屏。
        """
        wanted = [k for k in (kinds or []) if k in self.TIMELINE_KINDS]
        # 每个分支都必须显式起列别名：只筛一类时 UNION 只剩一个分支，
        # 外层 ORDER BY 就找不到 kind/ref_id 这些名字
        branches = {
            "alert": """
                SELECT created_at AS ts, 'alert' AS kind, level AS severity,
                       rule AS title, message AS detail, alert_id AS ref_id
                  FROM alerts WHERE created_at >= ?
            """,
            "signal": """
                SELECT ts AS ts, 'signal' AS kind, NULL AS severity,
                       side || ' ' || inst_id AS title,
                       strategy_id || ' qty=' || COALESCE(qty, '0') AS detail,
                       signal_id AS ref_id
                  FROM signals WHERE ts >= ?
            """,
            "order": """
                SELECT created_ts AS ts, 'order' AS kind, status AS severity,
                       'order ' || order_id AS title,
                       'filled=' || COALESCE(filled_qty, '0') AS detail,
                       order_id AS ref_id
                  FROM orders WHERE created_ts >= ?
            """,
            "fill": """
                SELECT ts AS ts, 'fill' AS kind, side AS severity,
                       side || ' ' || inst_id AS title,
                       'qty=' || qty || ' @' || price AS detail,
                       order_id AS ref_id
                  FROM fills WHERE ts >= ?
            """,
        }
        chosen = [k for k in self.TIMELINE_KINDS if not wanted or k in wanted]
        sql = " UNION ALL ".join(branches[k] for k in chosen)
        rows = self._query_all(
            f"""
            SELECT * FROM ({sql})
             ORDER BY ts DESC, kind ASC, ref_id ASC
             LIMIT ? OFFSET ?
            """,
            (*(since_ms for _ in chosen), limit, offset),
        )
        return [dict(row) for row in rows]

    # ------------------------------------------------------------------
    # alerts / silences
    # ------------------------------------------------------------------

    def insert_alert(
        self,
        alert_id: str,
        rule: str,
        level: str,
        message: str,
        fingerprint: str,
        *,
        created_at: int,
        inst_id: str | None = None,
        strategy_id: str | None = None,
        suppressed: bool = False,
    ) -> bool:
        """写入一条告警。`alert_id` 由事件内容派生，重复发布视为幂等。

        被静音压制的告警直接落 `state='silenced'`（M9-7）：它仍在库里、
        仍可被标记已读与被恢复关闭，只是不发邮件，看板据此显示「静音中」。

        Returns:
            是否真的插入了新行（重复 ID 返回 False）。
        """
        return (
            self._execute(
                """
                INSERT OR IGNORE INTO alerts (
                    alert_id, rule, level, message, fingerprint, inst_id,
                    strategy_id, state, suppressed, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    alert_id,
                    rule,
                    level,
                    message,
                    fingerprint,
                    inst_id,
                    strategy_id,
                    "silenced" if suppressed else "open",
                    1 if suppressed else 0,
                    created_at,
                ),
            )
            > 0
        )

    def list_alerts(
        self,
        limit: int = 50,
        offset: int = 0,
        *,
        level: str | None = None,
        rule: str | None = None,
        state: str | None = None,
        since_ms: int | None = None,
        until_ms: int | None = None,
    ) -> tuple[list[dict], int]:
        """按条件分页查询告警，返回 (行, 命中总数)。

        总数与分页用同一组 WHERE 条件：分别拼两次会因漏条件让 `total`
        与实际翻页结果对不上。
        """
        where, params = _alert_filters(
            level=level,
            rule=rule,
            state=state,
            since_ms=since_ms,
            until_ms=until_ms,
        )
        total_row = self._query_one(
            f"SELECT COUNT(*) AS n FROM alerts {where}", tuple(params)
        )
        total = int(total_row["n"]) if total_row else 0
        rows = self._query_all(
            f"""
            SELECT alert_id, rule, level, message, fingerprint, inst_id,
                   strategy_id, state, suppressed, created_at, acked_at,
                   acked_by, resolved_at
              FROM alerts {where}
             ORDER BY created_at DESC, alert_id DESC
             LIMIT ? OFFSET ?
            """,
            (*params, limit, offset),
        )
        return [dict(r) for r in rows], total

    def alert_summary_counts(self) -> dict:
        """全部告警的未读汇总（M1-9 口径：open/silenced 算未读）。

        不带时间窗：未读是「现在还欠处理」的动作项，历史遗留的 open 也要
        计入。供看板告警摘要消费，替代「前端从最近 200 条里自己数」的口径。
        """
        row = self._query_one(
            """
            SELECT COALESCE(SUM(state IN ('open', 'silenced')), 0) AS unread,
                   COALESCE(SUM(
                       state IN ('open', 'silenced') AND level = 'ERROR'
                   ), 0) AS errors,
                   COALESCE(SUM(
                       state IN ('open', 'silenced') AND level = 'WARNING'
                   ), 0) AS warnings,
                   COALESCE(SUM(
                       state IN ('open', 'silenced') AND level = 'INFO'
                   ), 0) AS infos,
                   COUNT(*) AS total_all
              FROM alerts
            """,
            (),
        )
        if row is None:
            return {
                "unread": 0,
                "errors": 0,
                "warnings": 0,
                "infos": 0,
                "total_all": 0,
            }
        return {
            "unread": int(row["unread"]),
            "errors": int(row["errors"]),
            "warnings": int(row["warnings"]),
            "infos": int(row["infos"]),
            "total_all": int(row["total_all"]),
        }

    def alert_rule_counts(self, since_ms: int) -> list[dict]:
        """按规则统计告警条数与各数量（M9-14 告警趋势），按总数降序。"""
        rows = self._query_all(
            """
            SELECT rule,
                   COUNT(*) AS total,
                   SUM(CASE WHEN level = 'ERROR' THEN 1 ELSE 0 END) AS errors,
                   SUM(CASE WHEN level = 'WARNING' THEN 1 ELSE 0 END) AS warnings,
                   SUM(CASE WHEN level = 'INFO' THEN 1 ELSE 0 END) AS infos
              FROM alerts
             WHERE created_at >= ?
             GROUP BY rule
             ORDER BY total DESC, rule ASC
            """,
            (since_ms,),
        )
        return [
            {
                "rule": row["rule"],
                "total": int(row["total"]),
                "errors": int(row["errors"] or 0),
                "warnings": int(row["warnings"] or 0),
                "infos": int(row["infos"] or 0),
            }
            for row in rows
        ]

    def ack_alert(self, alert_id: str, by: str, now_ms: int) -> bool:
        """标记已读。幂等：已确认的再调不覆盖首次确认时间与人。

        Returns:
            记录是否存在。
        """
        row = self._query_one(
            "SELECT state, acked_at, acked_by FROM alerts WHERE alert_id = ?",
            (alert_id,),
        )
        if row is None:
            return False
        if row["state"] not in ("open", "silenced"):
            # 已 acked / resolved：保持首次确认信息，不重复写
            return True
        self._execute(
            """
            UPDATE alerts SET state = 'acked', acked_at = ?, acked_by = ?
             WHERE alert_id = ? AND state IN ('open', 'silenced')
            """,
            (now_ms, by, alert_id),
        )
        return True

    def resolve_alerts(
        self, rule: str, *, inst_id: str | None, now_ms: int
    ) -> int:
        """把该规则（+品种）下所有未关闭记录置为 resolved，返回影响行数。"""
        if inst_id is None:
            where = "rule = ? AND inst_id IS NULL"
            params: tuple = (rule,)
        else:
            where = "rule = ? AND inst_id = ?"
            params = (rule, inst_id)
        return self._execute(
            f"""
            UPDATE alerts SET state = 'resolved', resolved_at = ?
             WHERE {where} AND state IN ('open', 'acked', 'silenced')
            """,
            (now_ms, *params),
        )

    def insert_silence(
        self,
        silence_id: str,
        rule: str,
        *,
        inst_id: str | None,
        until_ts: int,
        reason: str,
        created_by: str,
        created_at: int,
    ) -> None:
        """创建静音记录。`inst_id=None` 表示该规则全部品种。"""
        self._execute(
            """
            INSERT INTO alert_silences (
                silence_id, rule, inst_id, until_ts, reason,
                created_by, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (silence_id, rule, inst_id, until_ts, reason, created_by, created_at),
        )

    def revoke_silence(self, silence_id: str, now_ms: int) -> bool:
        """提前解除静音；重复解除返回 False（已撤销）。"""
        return (
            self._execute(
                """
                UPDATE alert_silences SET revoked_at = ?
                 WHERE silence_id = ? AND revoked_at IS NULL
                """,
                (now_ms, silence_id),
            )
            > 0
        )

    def list_active_silences(self, now_ms: int) -> list[dict]:
        """当前生效的静音（未撤销且未到期），按到期时间升序。"""
        rows = self._query_all(
            """
            SELECT silence_id, rule, inst_id, until_ts, reason,
                   created_by, created_at, revoked_at
              FROM alert_silences
             WHERE revoked_at IS NULL AND until_ts > ?
             ORDER BY until_ts
            """,
            (now_ms,),
        )
        return [dict(r) for r in rows]

    # ------------------------------------------------------------------
    # strategy params（MS4-C §3.2 / §10.1 / §10.2）
    # ------------------------------------------------------------------

    def get_strategy_params(self, strategy_id: str) -> list[dict]:
        """该策略的在线覆盖值，按参数名排序。"""
        rows = self._query_all(
            """
            SELECT param, value, updated_at
              FROM strategy_params
             WHERE strategy_id = ?
             ORDER BY param
            """,
            (strategy_id,),
        )
        return [dict(r) for r in rows]

    def set_strategy_param(
        self,
        strategy_id: str,
        param: str,
        value: str,
        now_ms: int,
        *,
        reason: str = "",
        changed_by: str = "owner",
        git_commit: str | None = None,
    ) -> bool:
        """写入覆盖值并记一条变更历史。

        返回 False 表示新旧值相同（不写历史，避免审计表被无意义行淹没）。
        """
        with self._lock:
            # 读旧值必须与写入同锁：否则两个并发 PUT 都读到同一个 old_value，
            # 审计链出现重复记录（读-比-写不原子）（P2）。
            # 注意：_query_one 自身也会取这把非重入锁，嵌套调用会自锁，
            # 因此这里在已持锁的前提下直接用连接查询。
            row = self._conn.execute(
                "SELECT value FROM strategy_params WHERE strategy_id = ? AND param = ?",
                (strategy_id, param),
            ).fetchone()
            old_value = row["value"] if row else None
            if old_value == value:
                return False
            self._conn.execute(
                """
                INSERT INTO strategy_params (strategy_id, param, value, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(strategy_id, param) DO UPDATE SET
                    value=excluded.value, updated_at=excluded.updated_at
                """,
                (strategy_id, param, value, now_ms),
            )
            self._conn.execute(
                """
                INSERT INTO strategy_param_changes
                    (strategy_id, param, old_value, new_value, reason,
                     changed_by, changed_at, git_commit)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    strategy_id, param, old_value, value, reason,
                    changed_by, now_ms, git_commit,
                ),
            )
            self._conn.commit()
        return True

    def list_param_changes(
        self, strategy_id: str, limit: int = 50, offset: int = 0
    ) -> tuple[list[dict], int]:
        """变更历史（新→旧）与总数。"""
        total = self._query_one(
            "SELECT COUNT(*) AS n FROM strategy_param_changes WHERE strategy_id = ?",
            (strategy_id,),
        )
        rows = self._query_all(
            """
            SELECT id, strategy_id, param, old_value, new_value, reason,
                   changed_by, changed_at, git_commit
              FROM strategy_param_changes
             WHERE strategy_id = ?
             ORDER BY changed_at DESC, id DESC
             LIMIT ? OFFSET ?
            """,
            (strategy_id, limit, offset),
        )
        return [dict(r) for r in rows], int(total["n"]) if total else 0

    def rollback_param(
        self,
        strategy_id: str,
        change_id: int,
        now_ms: int,
        *,
        reason: str = "",
        changed_by: str = "owner",
        git_commit: str | None = None,
        validate: Callable[[str, str], None] | None = None,
    ) -> tuple[bool, str]:
        """从历史记录恢复参数值（M3-14）。

        Args:
            validate: 可选的 (param, value) 校验回调，抛异常即拒绝回滚且不写库。
                由服务层按当前 PARAM_SPEC 校验，避免 store 反向依赖策略模块。

        Returns:
            (success, message): success=False 表示找不到记录或值相同
        """
        row = self._query_one(
            """
            SELECT param, old_value
              FROM strategy_param_changes
             WHERE id = ? AND strategy_id = ?
            """,
            (change_id, strategy_id),
        )
        if not row:
            return False, "change record not found"

        param = row["param"]
        target_value = row["old_value"]
        # 首条变更的 old_value 为空：变更前用的是 toml 默认值，没有可恢复的
        # 数据库覆盖值；删除覆盖不在本接口范围内，明确拒绝而不是写坏数据。
        if target_value is None:
            return False, "no previous override to restore (config default)"

        # PARAM_SPEC 可能已收紧：恢复出非法值会被 resolve 静默忽略，
        # 宁可拒绝回滚也不写一条「显示 db 实际用 toml」的脏覆盖（P2）
        if validate is not None:
            validate(param, target_value)

        # 调用 set_strategy_param 写入新变更（不删历史）
        changed = self.set_strategy_param(
            strategy_id,
            param,
            target_value,
            now_ms,
            reason=reason or f"rollback to change #{change_id}",
            changed_by=changed_by,
            git_commit=git_commit,
        )
        if not changed:
            return False, "value unchanged (already at target)"
        return True, param

    # ------------------------------------------------------------------
    # dead letters（MS4-B §10）
    # ------------------------------------------------------------------

    def insert_dead_letter(
        self,
        dead_id: str,
        stage: str,
        reason: str,
        payload: str,
        *,
        created_at: int,
        signal_id: str | None = None,
    ) -> bool:
        """写入一条死信。`dead_id` 由调用方生成，重复写入幂等。"""
        return (
            self._execute(
                """
                INSERT OR IGNORE INTO dead_letters (
                    dead_id, signal_id, stage, reason, payload, created_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (dead_id, signal_id, stage, reason, payload, created_at),
            )
            > 0
        )

    def get_dead_letter(self, dead_id: str) -> dict | None:
        row = self._query_one(
            "SELECT * FROM dead_letters WHERE dead_id = ?", (dead_id,)
        )
        return dict(row) if row else None

    def list_dead_letters(
        self,
        limit: int = 50,
        offset: int = 0,
        *,
        stage: str | None = None,
        since: int | None = None,
        only_pending: bool = False,
    ) -> tuple[list[dict], int]:
        """按时间倒序分页；总数与分页共用同一组 WHERE。"""
        where, params = _dead_letter_filters(
            stage=stage, since=since, only_pending=only_pending
        )
        total_row = self._query_one(
            f"SELECT COUNT(*) AS n FROM dead_letters {where}", tuple(params)
        )
        total = int(total_row["n"]) if total_row else 0
        rows = self._query_all(
            f"""
            SELECT dead_id, signal_id, stage, reason, payload, created_at,
                   replayed_at, replay_result
              FROM dead_letters {where}
             ORDER BY created_at DESC, dead_id DESC
             LIMIT ? OFFSET ?
            """,
            (*params, limit, offset),
        )
        return [dict(r) for r in rows], total

    def claim_dead_letter(self, dead_id: str, now_ms: int) -> bool:
        """原子占用一条死信用于重放（防双击重复入队）。

        `replayed_at IS NULL` 是条件：并发调用只有一个能拿到 True。
        重放入队失败时调用方需 `release_dead_letter` 归还，否则这条
        死信会永远显示「已重放」而实际没回队列。
        """
        return (
            self._execute(
                """
                UPDATE dead_letters
                   SET replayed_at = ?, replay_result = 'replaying'
                 WHERE dead_id = ? AND replayed_at IS NULL
                """,
                (now_ms, dead_id),
            )
            > 0
        )

    def finish_dead_letter(self, dead_id: str, result: str) -> None:
        """写入重放结果（requeued / already_processed / discarded）。"""
        self._execute(
            "UPDATE dead_letters SET replay_result = ? WHERE dead_id = ?",
            (result, dead_id),
        )

    def release_dead_letter(self, dead_id: str) -> None:
        """撤销占用：重放入队失败时恢复为「未处理」，允许重试。"""
        self._execute(
            """
            UPDATE dead_letters
               SET replayed_at = NULL, replay_result = NULL
             WHERE dead_id = ? AND replay_result = 'replaying'
            """,
            (dead_id,),
        )

    # ------------------------------------------------------------------
    # reconciliations（M8-18）
    # ------------------------------------------------------------------

    #: 对账差异的两档处置：auto=本地已被校正，manual=必须人工确认
    RECONCILE_AUTO = "auto"
    RECONCILE_MANUAL = "manual"

    def record_reconciliation(
        self,
        *,
        kind: str,
        key: str,
        local_value: str | None,
        remote_value: str | None,
        diff: str | None,
        severity: str,
        note: str | None,
        now_ms: int,
        resolved: bool = False,
    ) -> str:
        """写入一条对账差异并返回 rec_id。

        用于「已自动修复」的一次性记录（`resolved=True`），或需要人工
        处理但不需要原地更新的场景。持续存在的差异请用
        `upsert_open_reconciliation`，避免每轮插新行。
        """
        rec_id = hashlib.sha1(
            f"{kind}|{key}|{severity}|{now_ms}".encode()
        ).hexdigest()[:16]
        self._execute(
            """
            INSERT INTO reconciliations
                (rec_id, ts, kind, key, local_value, remote_value, diff,
                 severity, resolved, resolved_at, resolved_by, note)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                rec_id,
                now_ms,
                kind,
                key,
                local_value,
                remote_value,
                diff,
                severity,
                1 if resolved else 0,
                now_ms if resolved else None,
                "auto" if resolved else None,
                note,
            ),
        )
        return rec_id

    def upsert_open_reconciliation(
        self,
        *,
        kind: str,
        key: str,
        local_value: str | None,
        remote_value: str | None,
        diff: str | None,
        severity: str,
        note: str | None,
        now_ms: int,
    ) -> str:
        """写入/刷新一条未处理差异，同 (kind,key,severity) 只保留一行。

        对账每分钟跑一次，持仓/余额漂移如果每轮都插一行，表会迅速被同一
        条问题刷满，页面也无法判断「这是新问题还是老问题」。这里原地更新
        数值与时间；用户标记处理后再出现才会新增一条。
        """
        row = self._query_one(
            """
            SELECT rec_id FROM reconciliations
             WHERE kind = ? AND key = ? AND severity = ? AND resolved = 0
             ORDER BY ts DESC LIMIT 1
            """,
            (kind, key, severity),
        )
        if row is not None:
            rec_id = row["rec_id"]
            self._execute(
                """
                UPDATE reconciliations
                   SET ts = ?, local_value = ?, remote_value = ?,
                       diff = ?, note = ?
                 WHERE rec_id = ?
                """,
                (now_ms, local_value, remote_value, diff, note, rec_id),
            )
            return rec_id
        return self.record_reconciliation(
            kind=kind,
            key=key,
            local_value=local_value,
            remote_value=remote_value,
            diff=diff,
            severity=severity,
            note=note,
            now_ms=now_ms,
        )

    def list_reconciliations(
        self,
        limit: int = 50,
        offset: int = 0,
        *,
        kind: str | None = None,
        only_open: bool = False,
    ) -> tuple[list[dict], int]:
        """按时间倒序分页；总数与分页共用同一组 WHERE。"""
        where, params = _reconciliation_filters(kind=kind, only_open=only_open)
        total_row = self._query_one(
            f"SELECT COUNT(*) AS n FROM reconciliations {where}", tuple(params)
        )
        total = int(total_row["n"]) if total_row else 0
        rows = self._query_all(
            f"""
            SELECT rec_id, ts, kind, key, local_value, remote_value, diff,
                   severity, resolved, resolved_at, resolved_by, note
              FROM reconciliations {where}
             ORDER BY ts DESC, rec_id DESC
             LIMIT ? OFFSET ?
            """,
            (*params, limit, offset),
        )
        return [dict(r) for r in rows], total

    def resolve_reconciliation(
        self, rec_id: str, *, by: str, now_ms: int
    ) -> bool:
        """标记差异已处理；已处理过的返回 False（幂等，不覆盖首次时间）。"""
        return (
            self._execute(
                """
                UPDATE reconciliations
                   SET resolved = 1, resolved_at = ?, resolved_by = ?
                 WHERE rec_id = ? AND resolved = 0
                """,
                (now_ms, by, rec_id),
            )
            > 0
        )

    def get_latest_equity_snapshot(self) -> dict | None:
        """最近一条权益快照；余额对账用它的 `ts` 判断本地视图是否过时。"""
        row = self._query_one(
            "SELECT * FROM equity_snapshots ORDER BY ts DESC LIMIT 1", ()
        )
        return dict(row) if row else None

    # ------------------------------------------------------------------
    # strategy bindings（MS4-C §4.1 / §10.3）
    # ------------------------------------------------------------------

    def get_meta(self, key: str) -> str | None:
        """读取一次性初始化标记；不存在返回 None。"""
        row = self._query_one(
            "SELECT value FROM app_meta WHERE key = ?", (key,)
        )
        return str(row["value"]) if row else None

    def set_meta(self, key: str, value: str) -> None:
        """写入一次性初始化标记（幂等覆盖）。"""
        self._execute(
            """
            INSERT INTO app_meta (key, value) VALUES (?, ?)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value
            """,
            (key, value),
        )

    def list_bindings(self, strategy_id: str | None = None) -> list[dict]:
        """启停矩阵；传 strategy_id 只看该策略，否则全量。"""
        sql = (
            "SELECT strategy_id, inst_id, bar, enabled, updated_at"
            "  FROM strategy_bindings"
        )
        params: tuple = ()
        if strategy_id is not None:
            sql += " WHERE strategy_id = ?"
            params = (strategy_id,)
        sql += " ORDER BY strategy_id, inst_id"
        return [dict(r) for r in self._query_all(sql, params)]

    def get_binding(self, strategy_id: str, inst_id: str) -> dict | None:
        row = self._query_one(
            """
            SELECT strategy_id, inst_id, bar, enabled, updated_at
              FROM strategy_bindings
             WHERE strategy_id = ? AND inst_id = ?
            """,
            (strategy_id, inst_id),
        )
        return dict(row) if row else None

    def upsert_binding(
        self,
        strategy_id: str,
        inst_id: str,
        bar: str,
        enabled: bool,
        now_ms: int,
    ) -> None:
        """新增或更新一条绑定。

        周期冲突（已有不同 bar）由调用方先校验并给出可读提示；这里只做写入，
        因为「先读后写」在同一进程内已足够，跨进程改动由引擎下一轮轮询兜住。
        """
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO strategy_bindings
                    (strategy_id, inst_id, bar, enabled, updated_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(strategy_id, inst_id) DO UPDATE SET
                    bar=excluded.bar,
                    enabled=excluded.enabled,
                    updated_at=excluded.updated_at
                """,
                (strategy_id, inst_id, bar, 1 if enabled else 0, now_ms),
            )
            self._conn.commit()

    def delete_binding(self, strategy_id: str, inst_id: str) -> bool:
        with self._lock:
            cursor = self._conn.execute(
                "DELETE FROM strategy_bindings WHERE strategy_id = ? AND inst_id = ?",
                (strategy_id, inst_id),
            )
            self._conn.commit()
        return cursor.rowcount > 0

    # ------------------------------------------------------------------
    # backtest runs（MS4-C §10.4）
    # ------------------------------------------------------------------

    def create_backtest_run(
        self,
        run_id: str,
        *,
        strategy_id: str,
        inst_id: str,
        bar: str,
        start_ts: int,
        end_ts: int,
        params: str,
        kind: str,
        status: str,
        created_at: int,
        warmup_bars: int = 200,
        git_commit: str | None = None,
    ) -> None:
        self._execute(
            """
            INSERT INTO backtest_runs
                (run_id, strategy_id, inst_id, bar, start_ts, end_ts,
                 warmup_bars, params, kind, status, created_at, git_commit)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                run_id, strategy_id, inst_id, bar, start_ts, end_ts,
                warmup_bars, params, kind, status, created_at, git_commit,
            ),
        )

    def update_run_status(
        self,
        run_id: str,
        status: str,
        *,
        started_at: int | None = None,
        finished_at: int | None = None,
        error: str | None = None,
    ) -> bool:
        """更新任务状态；不存在返回 False。"""
        return (
            self._execute(
                """
                UPDATE backtest_runs
                   SET status = ?,
                       started_at = COALESCE(?, started_at),
                       finished_at = COALESCE(?, finished_at),
                       error = COALESCE(?, error)
                 WHERE run_id = ?
                """,
                (status, started_at, finished_at, error, run_id),
            )
            > 0
        )

    def get_backtest_run(self, run_id: str) -> dict | None:
        row = self._query_one(
            "SELECT * FROM backtest_runs WHERE run_id = ?", (run_id,)
        )
        return dict(row) if row else None

    def fail_interrupted_runs(self, now_ms: int) -> int:
        """把上次进程留下的 running 任务标为 failed。

        不自动重跑：重跑会生成第二份产物，静默覆盖用户对上一次结果的预期。
        """
        return self._execute(
            """
            UPDATE backtest_runs
               SET status = 'failed', finished_at = ?, error = 'interrupted'
             WHERE status IN ('pending', 'running')
            """,
            (now_ms,),
        )

    def count_runs_with_prefix(self, prefix: str) -> int:
        """统计 run_id 前缀命中数，用于生成当天的递增序号。"""
        row = self._query_one(
            "SELECT COUNT(*) AS n FROM backtest_runs WHERE run_id LIKE ?",
            (f"{prefix}%",),
        )
        return int(row["n"]) if row else 0

    def list_backtest_runs(
        self,
        limit: int = 50,
        offset: int = 0,
        *,
        strategy_id: str | None = None,
        inst_id: str | None = None,
        bar: str | None = None,
        status: str | None = None,
        kind: str | None = None,
        since_ms: int | None = None,
        until_ms: int | None = None,
    ) -> tuple[list[dict], int]:
        where, params = _run_filters(
            strategy_id=strategy_id,
            inst_id=inst_id,
            bar=bar,
            status=status,
            kind=kind,
            since_ms=since_ms,
            until_ms=until_ms,
        )
        total = self._query_one(
            f"SELECT COUNT(*) AS n FROM backtest_runs {where}", params
        )
        rows = self._query_all(
            f"""
            SELECT * FROM backtest_runs {where}
             ORDER BY created_at DESC, run_id DESC
             LIMIT ? OFFSET ?
            """,
            (*params, limit, offset),
        )
        return [dict(r) for r in rows], int(total["n"]) if total else 0

    def save_backtest_metrics(self, run_id: str, metrics: dict) -> None:
        """写入指标行；只认表里存在的列，多余键忽略（扩展指标由各切片填）。"""
        columns = {k: v for k, v in metrics.items() if k in _METRIC_COLUMNS}
        if not columns:
            return
        names = ", ".join(columns)
        marks = ", ".join("?" for _ in columns)
        self._execute(
            f"""
            INSERT INTO backtest_metrics (run_id, {names})
            VALUES (?, {marks})
            ON CONFLICT(run_id) DO UPDATE SET
                {", ".join(f"{k}=excluded.{k}" for k in columns)}
            """,
            (run_id, *columns.values()),
        )

    def get_backtest_metrics(self, run_id: str) -> dict | None:
        row = self._query_one(
            "SELECT * FROM backtest_metrics WHERE run_id = ?", (run_id,)
        )
        return dict(row) if row else None

    # ------------------------------------------------------------------
    # optimize jobs（MS4-C §10.4 / M4-13 寻优进度落库；表由 A1 的 v10 迁移建立）
    # ------------------------------------------------------------------

    _OPTIMIZE_JOB_STATUSES = frozenset({"running", "done", "failed", "canceled"})

    def save_optimize_job(
        self,
        job_id: str,
        *,
        run_id: str,
        grid: dict,
        metric: str,
        n_jobs: int,
        total: int,
        created_at: int,
        status: str = "running",
    ) -> None:
        """登记一个寻优任务（M4-13 进度上报的落库基础）。

        `grid` 以 JSON 落库，与 spec §10.4 的 TEXT 列一致；`created_at` 为 epoch
        毫秒，由调用方传入（与 create_backtest_run 约定一致）。`job_id` 必须唯一，
        重复登记按主键唯一约束抛 StateError。
        """
        if status not in self._OPTIMIZE_JOB_STATUSES:
            raise ValueError(f"invalid optimize job status: {status!r}")
        self._execute(
            """
            INSERT INTO optimize_jobs
                (job_id, run_id, grid, metric, n_jobs, total,
                 completed, status, created_at, finished_at)
            VALUES (?, ?, ?, ?, ?, ?, 0, ?, ?, NULL)
            """,
            (
                job_id, run_id, json.dumps(grid, ensure_ascii=False), metric,
                n_jobs, total, status, created_at,
            ),
        )

    def update_optimize_progress(self, job_id: str, completed: int) -> bool:
        """累加已完成组合数；单调更新（仅当比已记录更大才写），任务不存在返回 False。"""
        return (
            self._execute(
                """
                UPDATE optimize_jobs
                   SET completed = ?
                 WHERE job_id = ? AND completed < ?
                """,
                (completed, job_id, completed),
            )
            > 0
        )

    def finish_optimize_job(
        self, job_id: str, status: str, *, finished_at: int
    ) -> bool:
        """结束任务：置终态（done/failed/canceled）与结束时间；不存在返回 False。"""
        if status not in self._OPTIMIZE_JOB_STATUSES:
            raise ValueError(f"invalid optimize job status: {status!r}")
        return (
            self._execute(
                """
                UPDATE optimize_jobs
                   SET status = ?, finished_at = ?
                 WHERE job_id = ?
                """,
                (status, finished_at, job_id),
            )
            > 0
        )

    def get_optimize_job(self, job_id: str) -> dict | None:
        """查询单个寻优任务（供 P3 的 GET /api/optimize/{job_id} 复用）。"""
        row = self._query_one(
            "SELECT * FROM optimize_jobs WHERE job_id = ?", (job_id,)
        )
        return dict(row) if row else None

    def worst_pine_deviation(
        self,
        strategy_id: str,
        params_text: str,
        inst_id: str | None = None,
        bar: str | None = None,
    ) -> dict | None:
        """同一策略 + 同一参数集已登记的最大 Pine 偏离度（M4-16）。

        参数以 `backtest_runs.params` 的规范化 JSON 文本比对——同一份参数
        只有一种序列化结果（提交时统一 `sort_keys=True`），无需额外哈希列。
        只看 `done` 且已导入过 Pine 基线的回测。

        Args:
            inst_id / bar: 传入时按「同品种同周期」过滤。不同品种/周期行情
                不同，偏离度不可比；一次 BTC 1H 的坏导入不应阻止 ETH 4H
                启用（P1）。不传则取该策略最坏记录（展示用）。

        Returns:
            `{run_id, deviation}`；没有导入记录返回 None。
        """
        sql = """
            SELECT r.run_id AS run_id, m.pine_deviation AS deviation
              FROM backtest_runs r
              JOIN backtest_metrics m ON m.run_id = r.run_id
             WHERE r.strategy_id = ?
               AND r.params = ?
               AND r.status = 'done'
               AND m.pine_deviation IS NOT NULL
        """
        params: list[object] = [strategy_id, params_text]
        if inst_id is not None:
            sql += " AND r.inst_id = ?"
            params.append(inst_id)
        if bar is not None:
            sql += " AND r.bar = ?"
            params.append(bar)
        sql += " ORDER BY CAST(m.pine_deviation AS REAL) DESC LIMIT 1"
        row = self._query_one(sql, tuple(params))
        return dict(row) if row else None

    def add_backtest_artifact(
        self, run_id: str, kind: str, path: str, size: int | None, now_ms: int
    ) -> None:
        self._execute(
            """
            INSERT INTO backtest_artifacts (run_id, kind, path, size, created_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(run_id, kind) DO UPDATE SET
                path=excluded.path, size=excluded.size,
                created_at=excluded.created_at
            """,
            (run_id, kind, path, size, now_ms),
        )

    def list_backtest_artifacts(self, run_id: str) -> list[dict]:
        rows = self._query_all(
            """
            SELECT kind, path, size, created_at
              FROM backtest_artifacts
             WHERE run_id = ?
             ORDER BY kind
            """,
            (run_id,),
        )
        return [dict(r) for r in rows]

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
            # 只清已处理的对账差异：未处理的行是待办事项，不能随历史过期
            (
                "reconciliations",
                "DELETE FROM reconciliations WHERE ts < ? AND resolved = 1",
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

    def purge_alerts(
        self,
        now_ms: int,
        *,
        error_days: int = 90,
        warning_days: int = 30,
        info_days: int = 7,
    ) -> dict[str, int]:
        """按级别删除过期告警（M9-12），返回各表删除行数。

        级别天数 <= 0 表示该级别不自动清理；未知级别沿用 WARNING 的天数，
        避免新增级别时被无声清掉。
        """
        day_ms = 86_400_000
        plan = [
            (
                "ERROR",
                now_ms - error_days * day_ms if error_days > 0 else None,
                "level = 'ERROR'",
            ),
            # 未知级别与 WARNING 同档，避免新增规则时被无声清掉
            (
                "WARNING",
                now_ms - warning_days * day_ms if warning_days > 0 else None,
                "level NOT IN ('ERROR', 'INFO')",
            ),
            (
                "INFO",
                now_ms - info_days * day_ms if info_days > 0 else None,
                "level = 'INFO'",
            ),
        ]
        deleted: dict[str, int] = {}
        with self._lock:
            try:
                for level, cutoff, condition in plan:
                    if cutoff is None:
                        deleted[level] = 0
                        continue
                    deleted[level] = self._conn.execute(
                        f"DELETE FROM alerts WHERE created_at < ? AND {condition}",
                        (cutoff,),
                    ).rowcount
                self._conn.commit()
            except sqlite3.Error as e:
                self._conn.rollback()
                raise StateError(f"sqlite alert purge failed: {e}") from e
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

    def _execute(self, sql: str, params: tuple) -> int:
        """执行写语句，返回受影响行数（调用方不关心时可忽略）。"""
        with self._lock:
            try:
                cursor = self._conn.execute(sql, params)
                self._conn.commit()
                return cursor.rowcount
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


def _alert_filters(
    *,
    level: str | None,
    rule: str | None,
    state: str | None,
    since_ms: int | None,
    until_ms: int | None,
) -> tuple[str, tuple]:
    """拼 alerts 的 WHERE 子句（全为等值/区间条件，无需转义）。"""
    clauses: list[str] = []
    params: list[object] = []
    for column, value in (
        ("level", level),
        ("rule", rule),
        ("state", state),
    ):
        if value is not None:
            clauses.append(f"{column} = ?")
            params.append(value)
    if since_ms is not None:
        clauses.append("created_at >= ?")
        params.append(since_ms)
    if until_ms is not None:
        clauses.append("created_at <= ?")
        params.append(until_ms)
    where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
    return where, tuple(params)


def _dead_letter_filters(
    *,
    stage: str | None,
    since: int | None,
    only_pending: bool,
) -> tuple[str, tuple]:
    """拼 dead_letters 的 WHERE 子句。"""
    clauses: list[str] = []
    params: list[object] = []
    if stage is not None:
        clauses.append("stage = ?")
        params.append(stage)
    if since is not None:
        clauses.append("created_at >= ?")
        params.append(since)
    if only_pending:
        clauses.append("replayed_at IS NULL")
    where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
    return where, tuple(params)


def _reconciliation_filters(
    *,
    kind: str | None,
    only_open: bool,
) -> tuple[str, tuple]:
    """拼 reconciliations 的 WHERE 子句。"""
    clauses: list[str] = []
    params: list[object] = []
    if kind is not None:
        clauses.append("kind = ?")
        params.append(kind)
    if only_open:
        clauses.append("resolved = 0")
    where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
    return where, tuple(params)


def _run_filters(
    *,
    strategy_id: str | None,
    inst_id: str | None,
    bar: str | None,
    status: str | None,
    kind: str | None,
    since_ms: int | None,
    until_ms: int | None,
) -> tuple[str, tuple]:
    """拼 backtest_runs 的 WHERE 子句（全为等值/区间条件，无需转义）。"""
    clauses: list[str] = []
    params: list[object] = []
    for column, value in (
        ("strategy_id", strategy_id),
        ("inst_id", inst_id),
        ("bar", bar),
        ("status", status),
        ("kind", kind),
    ):
        if value is not None:
            clauses.append(f"{column} = ?")
            params.append(value)
    if since_ms is not None:
        clauses.append("created_at >= ?")
        params.append(since_ms)
    if until_ms is not None:
        clauses.append("created_at <= ?")
        params.append(until_ms)
    where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
    return where, tuple(params)


#: `backtest_metrics` 的可写列（与迁移 v5 建表 + v9 补列保持一致）。
_METRIC_COLUMNS = frozenset(
    {
        "total_return",
        "annual_return",
        "sharpe",
        "max_drawdown",
        "win_rate",
        "profit_factor",
        "trade_count",
        "avg_holding_bars",
        "calmar",
        "sortino",
        "max_consecutive_losses",
        "max_consecutive_loss_amount",
        "exposure_ratio",
        "monthly_returns",
        "compounded_return",
        "profitable_segments",
        "avg_oos_sharpe",
        "total_oos_trades",
        "modal_params",
        "funding_cost",
        "pine_total_return",
        "pine_sharpe",
        "pine_max_drawdown",
        "pine_deviation",
    }
)


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
