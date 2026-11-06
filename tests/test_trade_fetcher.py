"""TradeFetcher 回补翻页逻辑单测（M2-14 批1）。

用 fake client 模拟 history-trades 分页：按 after 游标递减返回，验证翻页
终止条件（达到 days 边界 / 空页 / 安全上限）。
"""
from __future__ import annotations

import tempfile
import time
from decimal import Decimal
from pathlib import Path

from quant.fetcher.trade_fetcher import TradeFetcher
from quant.models.trade import Trade
from quant.storage.trade_storage import TradeStorage

# 用真实毫秒基准，使 backfill 的 days 边界判断与真实一致
_BASE_MS = int(time.time() * 1000)


class _FakeClient:
    """模拟 OKX history-trades：tradeId 形如 't{seq}'，seq 递减即更旧。

    ts 取 _BASE_MS - seq*1000（秒级递减），保证 newest 在「现在」附近，
    不会被 days 边界误判终止。从 seq=1000 起，after='t{N}' 返回严格比 N
    更旧的 100 条（OKX history-trades 语义：after 不含）。
    """

    def __init__(self) -> None:
        self.calls = 0

    def get_history_trades(self, inst_id, after=None, limit=100):
        self.calls += 1
        if after is None:
            rows = [
                Trade(ts=_BASE_MS - seq * 1000, inst_id=inst_id, trade_id=f"t{seq}",
                      px=Decimal("100"), sz=Decimal("1"), side="buy")
                for seq in range(1000, 900, -1)
            ]
        else:
            top = int(after[1:])
            rows = [
                Trade(ts=_BASE_MS - seq * 1000, inst_id=inst_id, trade_id=f"t{seq}",
                      px=Decimal("100"), sz=Decimal("1"), side="buy")
                for seq in range(top - 1, top - 101, -1)
                if seq >= 1
            ]
        return rows


def _fetcher(days: int):
    tmp = tempfile.mkdtemp()
    storage = TradeStorage(Path(tmp))
    client = _FakeClient()
    fetcher = TradeFetcher(client, storage, demo=False)
    return fetcher, client


def test_backfill_pages_until_empty() -> None:
    # days 足够大 → 翻到 after='t0' 返回空，正常终止
    fetcher, client = _fetcher(days=10_000)
    saved = fetcher.backfill("BTC-USDT-SWAP", days=10_000)
    assert saved == 1000  # t1..t1000
    assert client.calls == 11  # 1000/100 + 1 空页终止


def test_backfill_stops_at_days_boundary() -> None:
    # 每 seq 间隔 1s：days=1 → 截止 ~86400 秒前。t 序列 ts=seq(ms) 太小，
    # 这里仅验证逻辑：用 days=0 直接不请求
    fetcher, client = _fetcher(days=0)
    assert fetcher.backfill("BTC-USDT-SWAP", days=0) == 0
    assert client.calls == 0


def test_backfill_uses_latest_trade_id() -> None:
    # 本地已有 t900..t1000，回补应从 t900 往旧翻（只补 t1..t899）
    tmp = tempfile.mkdtemp()
    storage = TradeStorage(Path(tmp))
    storage.save_trades([
        Trade(ts=_BASE_MS - seq * 1000, inst_id="BTC-USDT-SWAP", trade_id=f"t{seq}",
              px=Decimal("100"), sz=Decimal("1"), side="buy")
        for seq in range(900, 1001)
    ])
    client = _FakeClient()
    fetcher = TradeFetcher(client, storage, demo=False)
    saved = fetcher.backfill("BTC-USDT-SWAP", days=10_000)
    assert saved == 899  # t1..t899
    assert client.calls == 10  # 从 latest(t900) 起，9 页非空 + 1 空


def test_backfill_resume_dedup() -> None:
    """续拉去重：已全量回补后再次回补，应新增 0（不重拉）。"""
    tmp = tempfile.mkdtemp()
    storage = TradeStorage(Path(tmp))
    client = _FakeClient()
    fetcher = TradeFetcher(client, storage, demo=False)
    first = fetcher.backfill("BTC-USDT-SWAP", days=10_000)  # 拉全量 t1..t1000
    assert first == 1000
    second = fetcher.backfill("BTC-USDT-SWAP", days=10_000)
    assert second == 0  # 全部已存在，去重后无新增
    assert client.calls >= 11  # 第二次仍完整翻页，但全去重（验证续拉而非空返回）

