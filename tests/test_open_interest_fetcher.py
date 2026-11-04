"""OpenInterestFetcher 单元测试（M2-14）。

测试策略：
- 用 MagicMock 替代 OKXClient，避免联网
- 构造真实 OpenInterest 对象，验证 fetch/load 全流程
- 覆盖边界：空响应、去重追加、跨实例持久化
"""
from __future__ import annotations

from decimal import Decimal
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from quant.fetcher.open_interest_fetcher import OpenInterestFetcher
from quant.models.open_interest import OpenInterest

# =====================================================================
# 工具：构造测试 OpenInterest
# =====================================================================


def _make_record(
    inst_id: str = "BTC-USDT-SWAP",
    ts: int = 1_700_000_000_000,
    oi: str = "12345.67",
    oi_value: str = "500000000",
) -> OpenInterest:
    return OpenInterest(
        inst_id=inst_id,
        ts=ts,
        oi=Decimal(oi),
        oi_value=Decimal(oi_value),
    )


def _make_client(returning: dict | None) -> MagicMock:
    """构造 mock client，get_open_interest 返回固定值（或 None）。"""
    client = MagicMock()
    client.get_open_interest.return_value = returning
    return client


# =====================================================================
# OpenInterestFetcher 主流程测试
# =====================================================================


class TestOpenInterestFetcherFetch:
    """fetch 主流程。"""

    def test_fetch_returns_one_for_new(self, tmp_path: Path) -> None:
        """新记录应返回 1。"""
        record = _make_record()
        client = _make_client({
            "instId": record.inst_id,
            "ts": str(record.ts),
            "oi": str(record.oi),
            "oiValue": str(record.oi_value),
        })
        fetcher = OpenInterestFetcher(client, tmp_path)
        count = fetcher.fetch("BTC-USDT-SWAP")
        assert count == 1

    def test_fetch_persists_to_disk(self, tmp_path: Path) -> None:
        """fetch 后应落盘到 data/open_interest/{inst_id}.json。"""
        record = _make_record()
        client = _make_client({
            "instId": record.inst_id,
            "ts": str(record.ts),
            "oi": str(record.oi),
            "oiValue": str(record.oi_value),
        })
        fetcher = OpenInterestFetcher(client, tmp_path)
        fetcher.fetch("BTC-USDT-SWAP")
        path = tmp_path / "open_interest" / "BTC-USDT-SWAP.json"
        assert path.exists()
        loaded = fetcher.load("BTC-USDT-SWAP")
        assert len(loaded) == 1
        assert loaded[0].ts == record.ts
        assert loaded[0].oi == record.oi
        assert loaded[0].oi_value == record.oi_value

    def test_fetch_returns_zero_for_empty(self, tmp_path: Path) -> None:
        """OKX 返回 None 应返回 0。"""
        client = _make_client(None)
        fetcher = OpenInterestFetcher(client, tmp_path)
        count = fetcher.fetch("BTC-USDT-SWAP")
        assert count == 0
        # 不应创建文件
        path = tmp_path / "open_interest" / "BTC-USDT-SWAP.json"
        assert not path.exists()

    def test_fetch_dedup_same_ts(self, tmp_path: Path) -> None:
        """ts 相同的记录应去重，不重复追加。"""
        record = _make_record(ts=1_700_000_000_000)
        client = _make_client({
            "instId": record.inst_id,
            "ts": str(record.ts),
            "oi": str(record.oi),
            "oiValue": str(record.oi_value),
        })
        fetcher = OpenInterestFetcher(client, tmp_path)
        # 第一次
        assert fetcher.fetch("BTC-USDT-SWAP") == 1
        # 第二次（同 ts）：应跳过
        assert fetcher.fetch("BTC-USDT-SWAP") == 0
        loaded = fetcher.load("BTC-USDT-SWAP")
        assert len(loaded) == 1

    def test_fetch_appends_different_ts(self, tmp_path: Path) -> None:
        """不同 ts 的记录应追加，按 ts 排序。"""
        # 用同一 client 模拟两次拉不同记录
        record1 = _make_record(ts=1_700_000_000_000, oi="10000")
        record2 = _make_record(ts=1_700_028_800_000, oi="11000")

        client1 = _make_client({
            "instId": record1.inst_id,
            "ts": str(record1.ts),
            "oi": str(record1.oi),
            "oiValue": str(record1.oi_value),
        })
        fetcher = OpenInterestFetcher(client1, tmp_path)
        assert fetcher.fetch("BTC-USDT-SWAP") == 1

        # 换 client 返回新的 record
        client2 = _make_client({
            "instId": record2.inst_id,
            "ts": str(record2.ts),
            "oi": str(record2.oi),
            "oiValue": str(record2.oi_value),
        })
        fetcher._client = client2
        assert fetcher.fetch("BTC-USDT-SWAP") == 1

        loaded = fetcher.load("BTC-USDT-SWAP")
        assert len(loaded) == 2
        # 按 ts 升序
        assert loaded[0].ts == 1_700_000_000_000
        assert loaded[1].ts == 1_700_028_800_000
        # oi 值正确
        assert loaded[0].oi == Decimal("10000")
        assert loaded[1].oi == Decimal("11000")


# =====================================================================
# 边界场景
# =====================================================================


class TestOpenInterestFetcherEdgeCases:
    """边界场景。"""

    def test_empty_inst_id_raises(self, tmp_path: Path) -> None:
        """空 inst_id 应抛 ValueError。"""
        fetcher = OpenInterestFetcher(_make_client(None), tmp_path)
        with pytest.raises(ValueError, match="inst_id must not be empty"):
            fetcher.fetch("")

    def test_load_missing_returns_empty(self, tmp_path: Path) -> None:
        """load 不存在的 inst_id 应返回空列表。"""
        fetcher = OpenInterestFetcher(_make_client(None), tmp_path)
        assert fetcher.load("DOGE-USDT-SWAP") == []

    def test_different_inst_separate_files(self, tmp_path: Path) -> None:
        """不同 inst_id 应落到不同文件。"""
        fetcher = OpenInterestFetcher(_make_client(None), tmp_path)

        record1 = _make_record("ETH-USDT-SWAP", ts=1_700_000_000_000)
        client1 = _make_client({
            "instId": record1.inst_id,
            "ts": str(record1.ts),
            "oi": str(record1.oi),
            "oiValue": str(record1.oi_value),
        })
        fetcher._client = client1
        fetcher.fetch("ETH-USDT-SWAP")

        record2 = _make_record("BTC-USDT-SWAP", ts=1_700_000_000_000)
        client2 = _make_client({
            "instId": record2.inst_id,
            "ts": str(record2.ts),
            "oi": str(record2.oi),
            "oiValue": str(record2.oi_value),
        })
        fetcher._client = client2
        fetcher.fetch("BTC-USDT-SWAP")

        # 两个文件都存在
        assert (tmp_path / "open_interest" / "ETH-USDT-SWAP.json").exists()
        assert (tmp_path / "open_interest" / "BTC-USDT-SWAP.json").exists()
        # 互不干扰
        assert len(fetcher.load("ETH-USDT-SWAP")) == 1
        assert len(fetcher.load("BTC-USDT-SWAP")) == 1

    def test_persistence_across_instances(self, tmp_path: Path) -> None:
        """新 fetcher 实例应能读旧 fetcher 写的数据。"""
        record = _make_record()
        client = _make_client({
            "instId": record.inst_id,
            "ts": str(record.ts),
            "oi": str(record.oi),
            "oiValue": str(record.oi_value),
        })
        fetcher1 = OpenInterestFetcher(client, tmp_path)
        fetcher1.fetch("BTC-USDT-SWAP")
        # 新实例
        fetcher2 = OpenInterestFetcher(_make_client(None), tmp_path)
        loaded = fetcher2.load("BTC-USDT-SWAP")
        assert len(loaded) == 1
        assert loaded[0].ts == record.ts

    def test_load_returns_real_objects(self, tmp_path: Path) -> None:
        """load 出来应是 OpenInterest 实例（非 dict）。"""
        record = _make_record()
        client = _make_client({
            "instId": record.inst_id,
            "ts": str(record.ts),
            "oi": str(record.oi),
            "oiValue": str(record.oi_value),
        })
        fetcher = OpenInterestFetcher(client, tmp_path)
        fetcher.fetch("BTC-USDT-SWAP")
        loaded = fetcher.load("BTC-USDT-SWAP")
        assert len(loaded) == 1
        assert isinstance(loaded[0], OpenInterest)
        # Decimal 正确反序列化
        assert isinstance(loaded[0].oi, Decimal)
        assert isinstance(loaded[0].oi_value, Decimal)
