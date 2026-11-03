"""CandleFetcher 单元测试（spec §5.5）。

测试策略：
- 用 MagicMock 替代 OKXClient，避免联网
- 构造真实 Bar 对象（不通过 from_okx_raw），直接传给 mock 的返回值
- 覆盖边界：断点续传、翻页到边界、空区间、补拉循环终止
"""
from __future__ import annotations

from decimal import Decimal
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from quant.fetcher.candle_fetcher import CandleFetcher
from quant.models.bar import Bar

# =====================================================================
# 工具：构造测试 Bar
# =====================================================================


def _make_bar(ts: int, inst_id: str = "BTC-USDT", bar_size: str = "1H") -> Bar:
    """构造一根合法的 Bar（OHLC 单调）。"""
    return Bar(
        ts=ts,
        open=Decimal("100"),
        high=Decimal("110"),
        low=Decimal("90"),
        close=Decimal("105"),
        vol=Decimal("10"),
        vol_quote=Decimal("1050"),
        confirm=1,
        inst_id=inst_id,
        bar_size=bar_size,
    )


def _make_bar_sequence(
    start_ts: int, count: int, bar_ms: int = 3_600_000
) -> list[Bar]:
    """构造 count 根连续的 Bar，从 start_ts 开始，每 bar_ms 一根。"""
    return [_make_bar(start_ts + i * bar_ms) for i in range(count)]


def _make_client(returning_pages: list[list[Bar]]) -> MagicMock:
    """构造 mock client，每次调 get_history_candles 返回 pages 里的下一页。"""
    client = MagicMock()
    pages_iter = iter(returning_pages + [])  # 复制迭代器

    def _get_history_candles(**kwargs):
        try:
            return next(pages_iter)
        except StopIteration:
            return []

    client.get_history_candles.side_effect = _get_history_candles
    return client


# =====================================================================
# CandleFetcher 主流程测试
# =====================================================================


class TestCandleFetcherFreshStart:
    """首次拉取（无断点）。"""

    def test_single_page_fetch(self, tmp_path: Path) -> None:
        """单页就能拉完，无翻页无补拉。"""
        # 5 根连续的 Bar，ts 从 0 开始
        bars = _make_bar_sequence(0, 5)
        client = _make_client([bars])
        fetcher = CandleFetcher(client, tmp_path, page_size=100)
        # 范围 [0, 4h]
        total = fetcher.fetch(
            "BTC-USDT", "1H", start_ts=0, end_ts=4 * 3_600_000
        )
        assert total == 5
        # 断点已写入，last_ts = 4h
        ckpt = fetcher._load_checkpoint("BTC-USDT", "1H")
        assert ckpt.last_ts == 4 * 3_600_000

    def test_empty_range_returns_zero(self, tmp_path: Path) -> None:
        """区间为空（start == end）应该返回 0。"""
        client = _make_client([[]])
        fetcher = CandleFetcher(client, tmp_path)
        total = fetcher.fetch("BTC-USDT", "1H", start_ts=0, end_ts=0)
        assert total == 0

    def test_okx_returns_empty_stops_loop(self, tmp_path: Path) -> None:
        """OKX 第一次就返回空，循环立即终止。"""
        client = _make_client([[]])
        fetcher = CandleFetcher(client, tmp_path)
        total = fetcher.fetch("BTC-USDT", "1H", 0, 10 * 3_600_000)
        assert total == 0


class TestCandleFetcherPagination:
    """翻页拉取。"""

    def test_multi_page_pagination(self, tmp_path: Path) -> None:
        """数据超过 page_size，需要翻页拉完。

        mock 顺序对应 OKX 翻页方向（从新到旧）：
        page1 最新 100 根 → page2 中间 100 根 → page3 最旧 50 根
        """
        # 250 根数据，ts 0..249h
        all_bars = _make_bar_sequence(0, 250)
        # 翻页顺序：最新 → 最旧
        page1 = all_bars[150:]  # ts 150..249h（最新 100 根）
        page2 = all_bars[50:150]  # ts 50..149h（中间 100 根）
        page3 = all_bars[:50]  # ts 0..49h（最旧 50 根）
        client = _make_client([page1, page2, page3])
        fetcher = CandleFetcher(client, tmp_path, page_size=100)
        total = fetcher.fetch(
            "BTC-USDT", "1H", 0, 249 * 3_600_000
        )
        assert total == 250

    def test_multi_page_checkpoint_ends_at_newest(self, tmp_path: Path) -> None:
        """翻页结束后断点应停在最新一根，而不是最后一页（最旧）的最新根。

        否则下一轮 sync 会以旧断点为 cursor，把整段历史重新拉一遍。
        """
        all_bars = _make_bar_sequence(0, 250)
        client = _make_client([all_bars[150:], all_bars[50:150], all_bars[:50]])
        fetcher = CandleFetcher(client, tmp_path, page_size=100)

        fetcher.fetch("BTC-USDT", "1H", 0, 249 * 3_600_000)

        ckpt = fetcher._load_checkpoint("BTC-USDT", "1H")
        assert ckpt.last_ts == 249 * 3_600_000

    def test_partial_page_stops_loop(self, tmp_path: Path) -> None:
        """某页不足 page_size，循环应停止。

        mock 顺序：page1 最新满页，page2 不满页（触发 stop）
        """
        # 130 根数据，ts 0..129h
        all_bars = _make_bar_sequence(0, 130)
        # 翻页：page1 最新 100 根（满），page2 更旧 30 根（不满，触发 stop）
        page1 = all_bars[30:]  # ts 30..129h（最新 100 根，满）
        page2 = all_bars[:30]  # ts 0..29h（更旧 30 根，不满）
        client = _make_client([page1, page2, []])  # 第 3 页不应被调
        fetcher = CandleFetcher(client, tmp_path, page_size=100)
        total = fetcher.fetch(
            "BTC-USDT", "1H", 0, 200 * 3_600_000
        )
        assert total == 130
        # 应该只调了 2 次 API
        assert client.get_history_candles.call_count == 2

    def test_out_of_range_filtered(self, tmp_path: Path) -> None:
        """OKX 返回超出 end_ts 的数据应被过滤。

        mock 返回 ts=100..199h（最新 100 根，超过 end_ts=50h），
        主循环应只保留 ts <= 50h 的 51 根（ts 100..199 都超 end_ts=50h，全部过滤），
        然后翻下一页拿更旧数据。
        """
        # 简化：返回单页数据全部超出 end_ts，应被过滤
        bars_out_of_range = _make_bar_sequence(
            100 * 3_600_000, 100  # ts 100..199h，全部 > end_ts=50h
        )
        # 第二页空，停止
        client = _make_client([bars_out_of_range, []])
        fetcher = CandleFetcher(client, tmp_path, page_size=100)
        total = fetcher.fetch(
            "BTC-USDT", "1H", 0, 50 * 3_600_000
        )
        # 应该只落盘 0 根（全部超 end_ts）
        assert total == 0


class TestCandleFetcherResume:
    """断点续传。"""

    def test_resume_from_checkpoint(self, tmp_path: Path) -> None:
        """有断点时应从断点 + bar_size_ms 续拉。"""
        # 先写一个断点：last_ts = 10h
        ckpt_dir = tmp_path / ".checkpoints"
        ckpt_dir.mkdir()
        from quant.fetcher.checkpoint import Checkpoint
        Checkpoint(last_ts=10 * 3_600_000).save(
            ckpt_dir / "BTC-USDT_1H.json"
        )
        # mock 返回 11h..20h 的 10 根（最新一页，按 ts 倒序返回但 mock 内部 sorted）
        bars = _make_bar_sequence(11 * 3_600_000, 10)
        client = _make_client([bars])
        fetcher = CandleFetcher(client, tmp_path, page_size=100)
        total = fetcher.fetch(
            "BTC-USDT", "1H", 0, 20 * 3_600_000
        )
        assert total == 10
        # 断点更新到 20h
        ckpt = fetcher._load_checkpoint("BTC-USDT", "1H")
        assert ckpt.last_ts == 20 * 3_600_000

    def test_resume_skip_main_loop_but_backfill_older(self, tmp_path: Path) -> None:
        """断点 >= end_ts 时跳过主循环，但请求区间早于断点的部分仍要回补。

        （P1 修复：原先这种情况静默返回 0，早于断点的历史永远补不上。）
        """
        ckpt_dir = tmp_path / ".checkpoints"
        ckpt_dir.mkdir()
        from quant.fetcher.checkpoint import Checkpoint
        # 断点 100h，但请求 [0, 50h]，断点已超 end_ts
        Checkpoint(last_ts=100 * 3_600_000).save(
            ckpt_dir / "BTC-USDT_1H.json"
        )
        client = _make_client([_make_bar_sequence(0, 10)])
        fetcher = CandleFetcher(client, tmp_path)
        total = fetcher.fetch(
            "BTC-USDT", "1H", 0, 50 * 3_600_000
        )
        # 回补 [0, 50h]：mock 返回的 10 根落盘（原先这里是静默 0）
        assert total == 10
        # 只调了回补这一次（主循环被跳过）：after = 50h + 1
        assert client.get_history_candles.call_count == 1
        assert (
            client.get_history_candles.call_args.kwargs["after"]
            == 50 * 3_600_000 + 1
        )


# =====================================================================
# CandleFetcher 补拉测试
# =====================================================================


class TestCandleFetcherFillMissing:
    """缺失补拉。"""

    def test_validate_and_fill_no_missing(self, tmp_path: Path) -> None:
        """已落盘数据无缺失时，不调补拉。"""
        bars = _make_bar_sequence(0, 10)
        client = _make_client([bars, []])  # 第二次给空，模拟补拉返回空
        fetcher = CandleFetcher(client, tmp_path, page_size=100)
        fetcher.fetch("BTC-USDT", "1H", 0, 9 * 3_600_000)
        # 应该校验通过，不触发补拉
        # 实际调用次数：1 次主拉，补拉 0 次
        assert client.get_history_candles.call_count == 1

    def test_fill_missing_recovers_gap(self, tmp_path: Path) -> None:
        """主拉只拉到一半，缺失段应被补拉。"""
        # 主拉只返回 0-2h 和 5-7h（缺 3-4h）
        main_bars = (
            _make_bar_sequence(0, 3) + _make_bar_sequence(5 * 3_600_000, 3)
        )
        # 补拉时返回 3-4h 的两根
        fill_bars = _make_bar_sequence(3 * 3_600_000, 2)
        client = _make_client([main_bars, fill_bars])
        fetcher = CandleFetcher(client, tmp_path, page_size=100, max_attempts=3)
        fetcher.fetch("BTC-USDT", "1H", 0, 7 * 3_600_000)
        # 校验：所有 ts 0-7 都已落盘
        loaded = fetcher._storage.load_bars(
            "BTC-USDT", "1H", 0, 7 * 3_600_000
        )
        ts_set = {b.ts for b in loaded}
        for i in range(8):
            assert i * 3_600_000 in ts_set, f"missing ts={i * 3_600_000}"

    def test_max_attempts_stops_infinite_loop(self, tmp_path: Path) -> None:
        """补拉永远补不到时，max_attempts 应阻止无限循环。"""
        # 主拉只拉到 0-1h（缺 2h-9h），补拉永远返回空
        main_bars = _make_bar_sequence(0, 2)
        client = _make_client([main_bars, []])  # 后续都返回空
        fetcher = CandleFetcher(client, tmp_path, page_size=100, max_attempts=2)
        # 不应死循环
        fetcher.fetch("BTC-USDT", "1H", 0, 9 * 3_600_000)
        # 主拉 1 次 + 补拉 2 次（max_attempts=2）
        assert client.get_history_candles.call_count <= 3

    def test_genuine_gap_on_okx_stops_filling(self, tmp_path: Path) -> None:
        """OKX 本身就没这些 K线（交易所维护期），补拉停止。"""
        # 主拉只拉到 0-1h
        main_bars = _make_bar_sequence(0, 2)
        # 补拉也返回空（OKX 真的没数据）
        client = _make_client([main_bars, []])
        fetcher = CandleFetcher(client, tmp_path, max_attempts=3)
        fetcher.fetch("BTC-USDT", "1H", 0, 9 * 3_600_000)
        # 应该在 1 次 attempt 后停止（total_filled=0）
        # 主拉 1 次 + 校验 attempt 1 次（无补到数据，立即停止）
        # max_attempts=3 但不会用满
        assert client.get_history_candles.call_count <= 2


# =====================================================================
# CandleFetcher 边界场景
# =====================================================================


class TestCandleFetcherEdgeCases:
    """边界场景。"""

    def test_unknown_bar_size_raises(self, tmp_path: Path) -> None:
        """不支持的 bar_size 应抛 ValueError。"""
        client = _make_client([[]])
        fetcher = CandleFetcher(client, tmp_path)
        with pytest.raises(ValueError):
            fetcher.fetch("BTC-USDT", "2S", 0, 1000)

    def test_negative_start_raises(self, tmp_path: Path) -> None:
        """start_ts 为负应抛 ValueError（Bar 构造时就会抛）。"""
        client = _make_client([])
        fetcher = CandleFetcher(client, tmp_path)
        with pytest.raises(ValueError):
            fetcher.fetch("BTC-USDT", "1H", -1, 1000)

    def test_checkpoint_persistence_across_instances(self, tmp_path: Path) -> None:
        """断点应在 CandleFetcher 实例间持久化。"""
        bars = _make_bar_sequence(0, 5)
        client1 = _make_client([bars])
        fetcher1 = CandleFetcher(client1, tmp_path, page_size=100)
        fetcher1.fetch("BTC-USDT", "1H", 0, 4 * 3_600_000)

        # 用新实例继续，断点应被读到
        client2 = _make_client([])
        fetcher2 = CandleFetcher(client2, tmp_path, page_size=100)
        ckpt = fetcher2._load_checkpoint("BTC-USDT", "1H")
        assert ckpt.last_ts == 4 * 3_600_000

    def test_different_inst_separate_checkpoints(self, tmp_path: Path) -> None:
        """不同 inst_id 应有独立断点。"""
        btc_bars = _make_bar_sequence(0, 3, bar_ms=3_600_000)
        eth_bars = [_make_bar(0, "ETH-USDT")]
        client = _make_client([btc_bars, eth_bars])
        fetcher = CandleFetcher(client, tmp_path, page_size=100)
        fetcher.fetch("BTC-USDT", "1H", 0, 2 * 3_600_000)
        fetcher.fetch("ETH-USDT", "1H", 0, 0)
        btc_ckpt = fetcher._load_checkpoint("BTC-USDT", "1H")
        eth_ckpt = fetcher._load_checkpoint("ETH-USDT", "1H")
        assert btc_ckpt.last_ts == 2 * 3_600_000
        assert eth_ckpt.last_ts == 0

    def test_different_bar_size_separate_checkpoints(self, tmp_path: Path) -> None:
        """同 inst 不同 bar_size 应有独立断点。"""
        bars_1h = _make_bar_sequence(0, 3, bar_ms=3_600_000)
        bars_4h = _make_bar_sequence(0, 2, bar_ms=4 * 3_600_000)
        client = _make_client([bars_1h, bars_4h])
        fetcher = CandleFetcher(client, tmp_path, page_size=100)
        fetcher.fetch("BTC-USDT", "1H", 0, 2 * 3_600_000)
        fetcher.fetch("BTC-USDT", "4H", 0, 4 * 3_600_000)
        ckpt_1h = fetcher._load_checkpoint("BTC-USDT", "1H")
        ckpt_4h = fetcher._load_checkpoint("BTC-USDT", "4H")
        assert ckpt_1h.last_ts == 2 * 3_600_000
        assert ckpt_4h.last_ts == 4 * 3_600_000


class TestSync:
    """增量同步：终点自动取最近已收盘 bar。"""

    HOUR = 3_600_000
    # 某根 1H bar 的开盘时刻 + 30 分钟，即该 bar 尚未收盘
    NOW = 100 * HOUR + 30 * 60_000

    def test_excludes_unclosed_last_bar(self, tmp_path: Path) -> None:
        """最近一根未收盘的 bar 不应落盘。"""
        # 第 100 根（ts=100h）尚未收盘，第 99 根已收盘
        bars = _make_bar_sequence(97 * self.HOUR, 3)
        client = _make_client([list(reversed(bars))])
        fetcher = CandleFetcher(client, tmp_path, page_size=100)

        fetcher.sync("BTC-USDT", "1H", now_ts=self.NOW, lookback_bars=3)

        ckpt = fetcher._load_checkpoint("BTC-USDT", "1H")
        assert ckpt.last_ts == 99 * self.HOUR

    def test_rewinds_from_checkpoint_and_refetches(self, tmp_path: Path) -> None:
        """已有断点时回退几根重拉，覆盖上一轮可能是未收盘落盘的数据。"""
        fetcher = CandleFetcher(_make_client([]), tmp_path)
        fetcher._save_checkpoint("BTC-USDT", "1H", 98 * self.HOUR)

        client = _make_client([[
            _make_bar(99 * self.HOUR),
            _make_bar(98 * self.HOUR),
            _make_bar(97 * self.HOUR),
        ]])
        fetcher = CandleFetcher(client, tmp_path, page_size=100)
        total = fetcher.sync("BTC-USDT", "1H", now_ts=self.NOW, lookback_bars=0)

        # lookback=0 时回退 1 根：从 97h 拉到 99h
        assert total == 3
        kwargs = client.get_history_candles.call_args.kwargs
        assert kwargs["after"] == 99 * self.HOUR + 1
        assert fetcher._load_checkpoint("BTC-USDT", "1H").last_ts == 99 * self.HOUR

    def test_fresh_start_uses_lookback(self, tmp_path: Path) -> None:
        """无断点时回看 lookback_bars 根，不会从 0 开始拉全历史。"""
        client = _make_client([[]])
        fetcher = CandleFetcher(client, tmp_path)
        fetcher.sync("BTC-USDT", "1H", now_ts=self.NOW, lookback_bars=3)

        kwargs = client.get_history_candles.call_args.kwargs
        # 最近已收盘是 99h；回看 3 根 -> 起于 96h；首页 after=99h+1
        assert kwargs["after"] == 99 * self.HOUR + 1

    def test_no_new_bars_returns_zero(self, tmp_path: Path) -> None:
        """最近几根已存在时净增为 0（重拉是覆盖，不新增）。"""
        fetcher = CandleFetcher(_make_client([]), tmp_path)
        fetcher._save_checkpoint("BTC-USDT", "1H", 99 * self.HOUR)

        client = _make_client([[]])
        fetcher = CandleFetcher(client, tmp_path)
        assert fetcher.sync("BTC-USDT", "1H", now_ts=self.NOW) == 0

    def test_unknown_bar_size_raises(self, tmp_path: Path) -> None:
        fetcher = CandleFetcher(_make_client([]), tmp_path)
        with pytest.raises(ValueError):
            fetcher.sync("BTC-USDT", "2S", now_ts=self.NOW)

    def test_rewinds_when_checkpoint_ahead(self, tmp_path: Path) -> None:
        """断点指向未收盘 bar 时回退并重拉，避免那根永不被修正。"""
        fetcher = CandleFetcher(_make_client([]), tmp_path)
        # 100h 那根尚未收盘，但断点已指向它（旧的 fetch-candles 传了未来日期）
        fetcher._save_checkpoint("BTC-USDT", "1H", 100 * self.HOUR)

        client = _make_client([[_make_bar(99 * self.HOUR)]])
        fetcher = CandleFetcher(client, tmp_path, page_size=100)
        fetcher.sync("BTC-USDT", "1H", now_ts=self.NOW, lookback_bars=3)

        # 前导缺口补拉会多一次 API 调用，这里只断言主循环的第一页
        kwargs = client.get_history_candles.call_args_list[0].kwargs
        assert kwargs["after"] == 99 * self.HOUR + 1
        # 断点回退到最后一个已收盘 bar，之后每次都能继续前进
        assert fetcher._load_checkpoint("BTC-USDT", "1H").last_ts == 99 * self.HOUR


# =====================================================================
# P1 回归：断点只向前导致向早回补静默失效
# =====================================================================


class TestBackfillBeforeCheckpoint:
    def test_backfills_history_before_checkpoint(self, tmp_path: Path) -> None:
        """已有断点后再拉更早区间，必须真的补到数据而不是静默返回 0。"""
        bar = 3_600_000
        # 第一次拉 [10h, 14h]，断点停在 14h
        fetcher = CandleFetcher(
            _make_client([_make_bar_sequence(10 * bar, 5)]), tmp_path
        )
        assert fetcher.fetch("BTC-USDT", "1H", 10 * bar, 14 * bar) == 5

        # 第二次拉 [0h, 14h]：0h~9h 早于断点，原先会被静默丢弃
        fetcher2 = CandleFetcher(
            _make_client([list(reversed(_make_bar_sequence(0, 10)))]), tmp_path
        )
        total = fetcher2.fetch("BTC-USDT", "1H", 0, 14 * bar)

        assert total == 10
        # 断点保持最新 ts，不被回补拉低
        assert fetcher2._load_checkpoint("BTC-USDT", "1H").last_ts == 14 * bar

    def test_backfill_respects_end_ts(self, tmp_path: Path) -> None:
        """请求区间整体早于断点时，回补不得越过 end_ts。"""
        bar = 3_600_000
        fetcher = CandleFetcher(
            _make_client([_make_bar_sequence(20 * bar, 3)]), tmp_path
        )
        fetcher.fetch("BTC-USDT", "1H", 20 * bar, 22 * bar)

        # 再拉 [0h, 5h]（整体早于断点 22h）：older_end 必须 clamp 到 5h
        fetcher2 = CandleFetcher(
            _make_client([list(reversed(_make_bar_sequence(0, 6)))]), tmp_path
        )
        total = fetcher2.fetch("BTC-USDT", "1H", 0, 5 * bar)

        assert total == 6
        assert fetcher2._load_checkpoint("BTC-USDT", "1H").last_ts == 22 * bar


class TestValidatorLeadingGap:
    def test_detects_leading_gap(self) -> None:
        """expected_start_ts 传入时，首根之前的前导缺口计入 missing。"""
        from quant.storage.validator import DataValidator

        bars = _make_bar_sequence(10 * 3_600_000, 3)  # [10h, 12h]
        report = DataValidator().validate(bars, expected_start_ts=0)
        assert len(report.missing) == 1
        m = report.missing[0]
        # 缺口从期望起点开始（prev_ts 是起点前一根虚拟 bar）
        assert m.prev_ts + 3_600_000 == 0
        assert m.next_ts == 10 * 3_600_000
        assert m.missing_count == 10

    def test_no_leading_gap_when_aligned(self) -> None:
        from quant.storage.validator import DataValidator

        report = DataValidator().validate(
            _make_bar_sequence(0, 3), expected_start_ts=0
        )
        assert report.missing == []

    def test_errors_surface_as_warning(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """整段拉空时错误必须以 WARNING 显式暴露（原先失败伪装成净增 0）。"""
        warnings_logged: list[str] = []

        class _FakeLog:
            def __getattr__(self, name: str):
                def _record(msg: str, *args, **kwargs) -> None:
                    if name == "warning":
                        warnings_logged.append(msg)
                return _record

        monkeypatch.setattr("quant.fetcher.candle_fetcher._log", _FakeLog())

        fetcher = CandleFetcher(_make_client([[]]), tmp_path)
        assert fetcher.fetch("BTC-USDT", "1H", 0, 10 * 3_600_000) == 0
        assert any("empty bars" in m for m in warnings_logged)


class TestPaginationStallGuard:
    def test_stalled_page_terminates(self, tmp_path: Path) -> None:
        """OKX 异常重复返回同一页时必须停止，不能无限打转（P2）。"""
        bar = 3_600_000
        page = _make_bar_sequence(201 * bar, 100)  # [201h..300h] 满页，够不到 start=0

        client = MagicMock()
        client.get_history_candles.side_effect = lambda **kwargs: list(page)

        fetcher = CandleFetcher(client, tmp_path, page_size=100)
        total = fetcher.fetch("BTC-USDT", "1H", 0, 300 * bar)

        # 100 根只落盘一次；循环靠停滞保护终止（旧实现会无限打转）
        assert total == 100

