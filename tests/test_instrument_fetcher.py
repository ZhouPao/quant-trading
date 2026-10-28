"""InstrumentFetcher 单元测试（spec §5.6）。

测试策略：
- 用 MagicMock 替代 OKXClient，避免联网
- 构造真实 Instrument 对象，验证 fetch/load 全流程
- 覆盖边界：未知 inst_type、空列表、非 live 过滤、全量覆盖
"""
from __future__ import annotations

from decimal import Decimal
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from quant.fetcher.instrument_fetcher import InstrumentFetcher
from quant.models.instrument import Instrument

# =====================================================================
# 工具：构造测试 Instrument
# =====================================================================


def _make_spot(
    inst_id: str = "BTC-USDT",
    state: str = "live",
    list_time: int = 1_600_000_000_000,
) -> Instrument:
    """构造一个合法的 SPOT 产品。"""
    return Instrument(
        inst_id=inst_id,
        inst_type="SPOT",
        base_ccy=inst_id.split("-")[0],
        quote_ccy=inst_id.split("-")[1] if "-" in inst_id else "USDT",
        settle_ccy="",
        tick_sz=Decimal("0.01"),
        lot_sz=Decimal("0.0001"),
        min_sz=Decimal("0.0001"),
        ct_val=Decimal("1"),
        state=state,
        list_time=list_time,
    )


def _make_swap(
    inst_id: str = "BTC-USDT-SWAP",
    state: str = "live",
) -> Instrument:
    """构造一个合法的 SWAP 产品。"""
    return Instrument(
        inst_id=inst_id,
        inst_type="SWAP",
        base_ccy="BTC",
        quote_ccy="USDT",
        settle_ccy="USDT",
        tick_sz=Decimal("0.1"),
        lot_sz=Decimal("0.001"),
        min_sz=Decimal("0.001"),
        ct_val=Decimal("1"),
        state=state,
        list_time=1_600_000_000_000,
    )


def _make_client(returning: list[Instrument]) -> MagicMock:
    """构造 mock client，get_instruments 返回固定列表。"""
    client = MagicMock()
    client.get_instruments.return_value = returning
    return client


# =====================================================================
# InstrumentFetcher 主流程测试
# =====================================================================


class TestInstrumentFetcherFetch:
    """fetch 主流程。"""

    def test_fetch_spot_returns_count(self, tmp_path: Path) -> None:
        """fetch SPOT 应返回 live 产品数。"""
        spots = [
            _make_spot("BTC-USDT"),
            _make_spot("ETH-USDT"),
            _make_spot("SOL-USDT"),
        ]
        client = _make_client(spots)
        fetcher = InstrumentFetcher(client, tmp_path)
        count = fetcher.fetch("SPOT")
        assert count == 3

    def test_fetch_filters_non_live(self, tmp_path: Path) -> None:
        """非 live 状态的产品应被过滤掉。"""
        spots = [
            _make_spot("BTC-USDT", state="live"),
            _make_spot("OLD-USDT", state="canceled"),
            _make_spot("EXP-USDT", state="expired"),
            _make_spot("ETH-USDT", state="live"),
        ]
        client = _make_client(spots)
        fetcher = InstrumentFetcher(client, tmp_path)
        count = fetcher.fetch("SPOT")
        assert count == 2  # 只有 BTC/ETH 是 live

    def test_fetch_empty_returns_zero(self, tmp_path: Path) -> None:
        """空列表应返回 0。"""
        client = _make_client([])
        fetcher = InstrumentFetcher(client, tmp_path)
        count = fetcher.fetch("SPOT")
        assert count == 0

    def test_fetch_persists_to_disk(self, tmp_path: Path) -> None:
        """fetch 后应落盘到 data/instruments/spot.json。"""
        spots = [_make_spot("BTC-USDT"), _make_spot("ETH-USDT")]
        client = _make_client(spots)
        fetcher = InstrumentFetcher(client, tmp_path)
        fetcher.fetch("SPOT")
        path = tmp_path / "instruments" / "spot.json"
        assert path.exists()
        # 落盘内容可读
        loaded = fetcher.load("SPOT")
        assert len(loaded) == 2
        assert loaded[0].inst_id == "BTC-USDT"
        assert loaded[1].inst_id == "ETH-USDT"

    def test_fetch_overwrites_previous(self, tmp_path: Path) -> None:
        """重复 fetch 应全量覆盖，不留旧数据。"""
        fetcher = InstrumentFetcher(_make_client([]), tmp_path)
        # 第一次写 3 个
        fetcher._client = _make_client(
            [_make_spot("A-USDT"), _make_spot("B-USDT"), _make_spot("C-USDT")]
        )
        assert fetcher.fetch("SPOT") == 3
        # 第二次写 2 个，应覆盖（A 不在了）
        fetcher._client = _make_client(
            [_make_spot("B-USDT"), _make_spot("D-USDT")]
        )
        assert fetcher.fetch("SPOT") == 2
        loaded = fetcher.load("SPOT")
        ids = [i.inst_id for i in loaded]
        assert ids == ["B-USDT", "D-USDT"]

    def test_fetch_swap(self, tmp_path: Path) -> None:
        """fetch SWAP 应正常落盘到 swap.json。"""
        swaps = [_make_swap("BTC-USDT-SWAP"), _make_swap("ETH-USDT-SWAP")]
        client = _make_client(swaps)
        fetcher = InstrumentFetcher(client, tmp_path)
        count = fetcher.fetch("SWAP")
        assert count == 2
        assert (tmp_path / "instruments" / "swap.json").exists()

    def test_fetch_sorts_by_inst_id(self, tmp_path: Path) -> None:
        """fetch 后落盘的产品应按 inst_id 排序。"""
        # 故意乱序
        spots = [
            _make_spot("Z-USDT"),
            _make_spot("A-USDT"),
            _make_spot("M-USDT"),
        ]
        client = _make_client(spots)
        fetcher = InstrumentFetcher(client, tmp_path)
        fetcher.fetch("SPOT")
        loaded = fetcher.load("SPOT")
        ids = [i.inst_id for i in loaded]
        assert ids == ["A-USDT", "M-USDT", "Z-USDT"]


# =====================================================================
# 边界场景
# =====================================================================


class TestInstrumentFetcherEdgeCases:
    """边界场景。"""

    def test_unknown_inst_type_raises(self, tmp_path: Path) -> None:
        """未知 inst_type 应抛 ValueError。"""
        fetcher = InstrumentFetcher(_make_client([]), tmp_path)
        with pytest.raises(ValueError, match="unknown inst_type"):
            fetcher.fetch("STOCKS")

    def test_load_missing_file_returns_empty(self, tmp_path: Path) -> None:
        """load 未拉取过的 inst_type 应返回空列表。"""
        fetcher = InstrumentFetcher(_make_client([]), tmp_path)
        assert fetcher.load("OPTION") == []

    def test_load_returns_real_objects(self, tmp_path: Path) -> None:
        """load 出来应是 Instrument 实例（非 dict）。"""
        spots = [_make_spot("BTC-USDT")]
        fetcher = InstrumentFetcher(_make_client(spots), tmp_path)
        fetcher.fetch("SPOT")
        loaded = fetcher.load("SPOT")
        assert len(loaded) == 1
        assert isinstance(loaded[0], Instrument)
        # Decimal 字段正确反序列化
        assert loaded[0].tick_sz == Decimal("0.01")
        assert loaded[0].ct_val == Decimal("1")

    def test_different_inst_types_separate_files(self, tmp_path: Path) -> None:
        """不同 inst_type 应落到不同文件。"""
        fetcher = InstrumentFetcher(_make_client([]), tmp_path)
        fetcher._client = _make_client([_make_spot("BTC-USDT")])
        fetcher.fetch("SPOT")
        fetcher._client = _make_client([_make_swap("BTC-USDT-SWAP")])
        fetcher.fetch("SWAP")
        assert (tmp_path / "instruments" / "spot.json").exists()
        assert (tmp_path / "instruments" / "swap.json").exists()
        # 互不干扰
        assert len(fetcher.load("SPOT")) == 1
        assert len(fetcher.load("SWAP")) == 1
