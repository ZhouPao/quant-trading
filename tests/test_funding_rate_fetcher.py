"""FundingRateFetcher 单元测试（spec §5.7）。

测试策略：
- 用 MagicMock 替代 OKXClient，避免联网
- 构造真实 FundingRate 对象，验证 fetch/load 全流程
- 覆盖边界：空响应、去重追加、跨实例持久化
"""
from __future__ import annotations

from decimal import Decimal
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from quant.fetcher.funding_rate_fetcher import FundingRateFetcher
from quant.models.funding_rate import FundingRate

# =====================================================================
# 工具：构造测试 FundingRate
# =====================================================================


def _make_rate(
    inst_id: str = "BTC-USDT-SWAP",
    ts: int = 1_700_000_000_000,
    rate: str = "0.0001",
    next_funding_ts: int = 1_700_028_800_000,
) -> FundingRate:
    return FundingRate(
        inst_id=inst_id,
        ts=ts,
        rate=Decimal(rate),
        next_funding_ts=next_funding_ts,
    )


def _make_client(returning: FundingRate | None) -> MagicMock:
    """构造 mock client，get_funding_rate 返回固定值（或 None）。"""
    client = MagicMock()
    client.get_funding_rate.return_value = returning
    return client


# =====================================================================
# FundingRateFetcher 主流程测试
# =====================================================================


class TestFundingRateFetcherFetch:
    """fetch 主流程。"""

    def test_fetch_returns_one_for_new(self, tmp_path: Path) -> None:
        """新记录应返回 1。"""
        rate = _make_rate()
        client = _make_client(rate)
        fetcher = FundingRateFetcher(client, tmp_path)
        count = fetcher.fetch("BTC-USDT-SWAP")
        assert count == 1

    def test_fetch_persists_to_disk(self, tmp_path: Path) -> None:
        """fetch 后应落盘到 data/funding_rates/{inst_id}.json。"""
        rate = _make_rate()
        client = _make_client(rate)
        fetcher = FundingRateFetcher(client, tmp_path)
        fetcher.fetch("BTC-USDT-SWAP")
        path = tmp_path / "funding_rates" / "BTC-USDT-SWAP.json"
        assert path.exists()
        loaded = fetcher.load("BTC-USDT-SWAP")
        assert len(loaded) == 1
        assert loaded[0].ts == rate.ts
        assert loaded[0].rate == rate.rate

    def test_fetch_returns_zero_for_empty(self, tmp_path: Path) -> None:
        """OKX 返回 None 应返回 0。"""
        client = _make_client(None)
        fetcher = FundingRateFetcher(client, tmp_path)
        count = fetcher.fetch("BTC-USDT-SWAP")
        assert count == 0
        # 不应创建文件
        path = tmp_path / "funding_rates" / "BTC-USDT-SWAP.json"
        assert not path.exists()

    def test_fetch_dedup_same_ts(self, tmp_path: Path) -> None:
        """ts 相同的记录应去重，不重复追加。"""
        rate = _make_rate(ts=1_700_000_000_000)
        fetcher = FundingRateFetcher(_make_client(rate), tmp_path)
        # 第一次
        assert fetcher.fetch("BTC-USDT-SWAP") == 1
        # 第二次（同 ts）：应跳过
        assert fetcher.fetch("BTC-USDT-SWAP") == 0
        loaded = fetcher.load("BTC-USDT-SWAP")
        assert len(loaded) == 1

    def test_fetch_appends_different_ts(self, tmp_path: Path) -> None:
        """不同 ts 的记录应追加，按 ts 排序。"""
        # 用同一 client 模拟两次拉不同记录
        rate1 = _make_rate(ts=1_700_000_000_000, rate="0.0001")
        rate2 = _make_rate(ts=1_700_028_800_000, rate="-0.0002")
        fetcher = FundingRateFetcher(_make_client(rate1), tmp_path)
        assert fetcher.fetch("BTC-USDT-SWAP") == 1
        # 换 client 返回新的 rate
        fetcher._client = _make_client(rate2)
        assert fetcher.fetch("BTC-USDT-SWAP") == 1
        loaded = fetcher.load("BTC-USDT-SWAP")
        assert len(loaded) == 2
        # 按 ts 升序
        assert loaded[0].ts == 1_700_000_000_000
        assert loaded[1].ts == 1_700_028_800_000
        # 负费率正确反序列化
        assert loaded[1].rate == Decimal("-0.0002")

    def test_fetch_preserves_negative_rate(self, tmp_path: Path) -> None:
        """负费率应正确落盘 + 反序列化。"""
        rate = _make_rate(rate="-0.005")
        client = _make_client(rate)
        fetcher = FundingRateFetcher(client, tmp_path)
        fetcher.fetch("BTC-USDT-SWAP")
        loaded = fetcher.load("BTC-USDT-SWAP")
        assert loaded[0].rate == Decimal("-0.005")


# =====================================================================
# 边界场景
# =====================================================================


class TestFundingRateFetcherEdgeCases:
    """边界场景。"""

    def test_empty_inst_id_raises(self, tmp_path: Path) -> None:
        """空 inst_id 应抛 ValueError。"""
        fetcher = FundingRateFetcher(_make_client(None), tmp_path)
        with pytest.raises(ValueError, match="inst_id must not be empty"):
            fetcher.fetch("")

    def test_load_missing_returns_empty(self, tmp_path: Path) -> None:
        """load 不存在的 inst_id 应返回空列表。"""
        fetcher = FundingRateFetcher(_make_client(None), tmp_path)
        assert fetcher.load("DOGE-USDT-SWAP") == []

    def test_different_inst_separate_files(self, tmp_path: Path) -> None:
        """不同 inst_id 应落到不同文件。"""
        fetcher = FundingRateFetcher(_make_client(None), tmp_path)
        rate1 = _make_rate("ETH-USDT-SWAP", ts=1_700_000_000_000)
        rate2 = _make_rate("BTC-USDT-SWAP", ts=1_700_000_000_000)
        fetcher._client = _make_client(rate1)
        fetcher.fetch("ETH-USDT-SWAP")
        fetcher._client = _make_client(rate2)
        fetcher.fetch("BTC-USDT-SWAP")
        # 两个文件都存在
        assert (tmp_path / "funding_rates" / "ETH-USDT-SWAP.json").exists()
        assert (tmp_path / "funding_rates" / "BTC-USDT-SWAP.json").exists()
        # 互不干扰
        assert len(fetcher.load("ETH-USDT-SWAP")) == 1
        assert len(fetcher.load("BTC-USDT-SWAP")) == 1

    def test_persistence_across_instances(self, tmp_path: Path) -> None:
        """新 fetcher 实例应能读旧 fetcher 写的数据。"""
        rate = _make_rate()
        fetcher1 = FundingRateFetcher(_make_client(rate), tmp_path)
        fetcher1.fetch("BTC-USDT-SWAP")
        # 新实例
        fetcher2 = FundingRateFetcher(_make_client(None), tmp_path)
        loaded = fetcher2.load("BTC-USDT-SWAP")
        assert len(loaded) == 1
        assert loaded[0].ts == rate.ts

    def test_load_returns_real_objects(self, tmp_path: Path) -> None:
        """load 出来应是 FundingRate 实例（非 dict）。"""
        rate = _make_rate()
        fetcher = FundingRateFetcher(_make_client(rate), tmp_path)
        fetcher.fetch("BTC-USDT-SWAP")
        loaded = fetcher.load("BTC-USDT-SWAP")
        assert len(loaded) == 1
        assert isinstance(loaded[0], FundingRate)
        # Decimal 正确反序列化
        assert isinstance(loaded[0].rate, Decimal)
