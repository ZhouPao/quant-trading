"""Trade 模型与 TradeStorage 单测（M2-14 批1）。

不依赖真实 OKX：用内存临时目录验证 save/load/去重/分区/回补起点。
"""
from __future__ import annotations

import tempfile
from decimal import Decimal
from pathlib import Path

import pytest

from quant.models.trade import Trade
from quant.storage.trade_storage import TradeStorage, validate_inst_id


def _trade(ts: int, trade_id: str, px: str = "100", sz: str = "1", side: str = "buy") -> Trade:
    return Trade(ts=ts, inst_id="BTC-USDT-SWAP", trade_id=trade_id,
                 px=Decimal(px), sz=Decimal(sz), side=side)


def test_trade_validation() -> None:
    # side 非法
    with pytest.raises(ValueError):
        Trade(ts=1, inst_id="X", trade_id="t", px=Decimal("1"), sz=Decimal("1"), side="x")
    # 负价
    with pytest.raises(ValueError):
        Trade(ts=1, inst_id="X", trade_id="t", px=Decimal("-1"), sz=Decimal("1"), side="buy")


def test_save_and_load_roundtrip() -> None:
    with tempfile.TemporaryDirectory() as d:
        s = TradeStorage(Path(d))
        s.save_trades([_trade(1_000, "a"), _trade(2_000, "b")])
        rows = s.load_trades("BTC-USDT-SWAP", 0, 10_000)
        assert len(rows) == 2
        ids = {r.trade_id for r in rows}
        assert ids == {"a", "b"}


def test_dedup_by_trade_id() -> None:
    with tempfile.TemporaryDirectory() as d:
        s = TradeStorage(Path(d))
        s.save_trades([_trade(1_000, "a", px="100"), _trade(1_000, "a", px="200")])
        rows = s.load_trades("BTC-USDT-SWAP", 0, 10_000)
        assert len(rows) == 1
        assert rows[0].px == Decimal("200")  # 后写覆盖


def test_daily_partition() -> None:
    with tempfile.TemporaryDirectory() as d:
        s = TradeStorage(Path(d))
        # 跨两天
        s.save_trades([_trade(1_700_000_000_000, "a"), _trade(1_700_086_400_000, "b")])
        path1 = d + "/trades/swap/BTC-USDT-SWAP/2023-11-14.parquet"
        path2 = d + "/trades/swap/BTC-USDT-SWAP/2023-11-15.parquet"
        import os
        assert os.path.exists(path1) and os.path.exists(path2)


def test_latest_trade_id() -> None:
    with tempfile.TemporaryDirectory() as d:
        s = TradeStorage(Path(d))
        s.save_trades([_trade(1_000, "old"), _trade(9_000, "new")])
        assert s.latest_trade_id("BTC-USDT-SWAP") == "new"
        assert s.latest_trade_id("NOPE-USDT-SWAP") is None


def test_validate_inst_id_rejects_traversal() -> None:
    with pytest.raises(ValueError):
        validate_inst_id("../etc")
