"""BarStorage + InstrumentStorage 单元测试（spec §6 + §10.2）。

覆盖：
- Bar save/load roundtrip（含 Decimal 精度）
- 同 ts 去重保留最新
- 跨年分文件
- inst_id → inst_type 推断（SPOT/SWAP/FUTURES/OPTION）
- 时间区间过滤（跨年查询）
- 空输入边界
- Instrument JSON 读写 + Decimal 还原
"""
from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest

from quant.models.bar import Bar
from quant.models.instrument import Instrument
from quant.storage.aligner import DataAligner
from quant.storage.bar_storage import BarStorage
from quant.storage.instrument_storage import InstrumentStorage
from quant.storage.validator import DataValidator

# 2024-01-01 00:00 UTC ms
TS_2024_START = 1704067200000
TS_2024_END = 1735689599000  # 2024-12-31 23:59:59 UTC
TS_2025_START = 1735689600000  # 2025-01-01 00:00 UTC


def _make_bar(ts: int, close: str = "100.0", inst_id: str = "BTC-USDT",
             bar_size: str = "1H", confirm: int = 1) -> Bar:
    return Bar(
        ts=ts,
        open=Decimal("100.0"),
        high=Decimal("1000.0"),
        low=Decimal("1.0"),
        close=Decimal(close),
        vol=Decimal("10.0"),
        vol_quote=Decimal("1000.0"),
        confirm=confirm,
        inst_id=inst_id,
        bar_size=bar_size,
    )


@pytest.fixture
def storage(tmp_path: Path) -> BarStorage:
    return BarStorage(tmp_path)


class TestBarStorageRoundtrip:
    def test_save_then_load_single_bar(self, storage: BarStorage) -> None:
        bar = _make_bar(TS_2024_START)
        written = storage.save_bars([bar])
        assert written == 1

        loaded = storage.load_bars("BTC-USDT", "1H", TS_2024_START, TS_2024_START)
        assert len(loaded) == 1
        assert loaded[0] == bar

    def test_save_then_load_multiple_bars(self, storage: BarStorage) -> None:
        bars = [_make_bar(TS_2024_START + i * 3_600_000) for i in range(10)]
        storage.save_bars(bars)

        loaded = storage.load_bars(
            "BTC-USDT", "1H", TS_2024_START, TS_2024_START + 9 * 3_600_000
        )
        assert len(loaded) == 10
        # 时间升序
        assert loaded[0].ts == TS_2024_START
        assert loaded[-1].ts == TS_2024_START + 9 * 3_600_000

    def test_decimal_precision_preserved(self, storage: BarStorage) -> None:
        # 用 18 位精度的数验证 parquet string 列保精度
        bar = Bar(
            ts=TS_2024_START,
            open=Decimal("0.123456789012345678"),
            high=Decimal("0.200000000000000000"),
            low=Decimal("0.100000000000000000"),
            close=Decimal("0.150000000000000000"),
            vol=Decimal("1.234567890123456789"),
            vol_quote=Decimal("0.185185185185185185"),
            confirm=1,
            inst_id="BTC-USDT",
            bar_size="1H",
        )
        storage.save_bars([bar])
        loaded = storage.load_bars("BTC-USDT", "1H", TS_2024_START, TS_2024_START)
        assert loaded[0] == bar
        # 验证精度完全一致，无 float 误差
        assert loaded[0].close.as_tuple() == bar.close.as_tuple()


class TestBarStorageDedup:
    def test_same_ts_in_one_call_keeps_last(self, storage: BarStorage) -> None:
        b1 = _make_bar(TS_2024_START, close="100.0")
        b2 = _make_bar(TS_2024_START, close="200.0")
        b3 = _make_bar(TS_2024_START, close="300.0")
        storage.save_bars([b1, b2, b3])

        loaded = storage.load_bars("BTC-USDT", "1H", TS_2024_START, TS_2024_START)
        assert len(loaded) == 1
        assert loaded[0].close == Decimal("300.0")

    def test_save_twice_same_ts_second_overwrites(self, storage: BarStorage) -> None:
        b1 = _make_bar(TS_2024_START, close="100.0")
        storage.save_bars([b1])
        assert storage.save_bars([b1]) == 0  # 同 ts 净增 0

        b2 = _make_bar(TS_2024_START, close="999.0")
        assert storage.save_bars([b2]) == 0  # 覆盖，条数不变

        loaded = storage.load_bars("BTC-USDT", "1H", TS_2024_START, TS_2024_START)
        assert len(loaded) == 1
        assert loaded[0].close == Decimal("999.0")

    def test_partial_overlap_merges_correctly(self, storage: BarStorage) -> None:
        bars1 = [_make_bar(TS_2024_START + i * 3_600_000) for i in range(5)]
        bars2 = [_make_bar(TS_2024_START + i * 3_600_000, close="999.0") for i in range(3, 8)]
        storage.save_bars(bars1)
        written = storage.save_bars(bars2)
        # 5 旧 + 5 新 - 3 重叠 = 7 总；净增 = 7 - 5 = 2 -> 实际净增 = 8 - 5 = 3
        # (3 ts 仅旧 + 5 ts 新值，其中 ts 3/4 旧值被覆盖, ts 5/6/7 是新增)
        assert written == 3

        loaded = storage.load_bars(
            "BTC-USDT", "1H", TS_2024_START, TS_2024_START + 7 * 3_600_000
        )
        assert len(loaded) == 8
        # ts 0/1/2 是仅旧的（close=100），ts 3/4/5/6/7 是新值（close=999）
        for i in range(3):
            assert loaded[i].close == Decimal("100.0"), f"index {i} should be 100.0"
        for i in range(3, 8):
            assert loaded[i].close == Decimal("999.0"), f"index {i} should be 999.0"


class TestBarStorageYearSplit:
    def test_cross_year_writes_two_files(self, storage: BarStorage) -> None:
        bars = [
            _make_bar(TS_2024_END - 3_600_000, close="100.0"),  # 2024-12-31 23:00
            _make_bar(TS_2025_START, close="200.0"),  # 2025-01-01 00:00
        ]
        storage.save_bars(bars)
        assert storage.list_years("BTC-USDT", "1H") == [2024, 2025]

    def test_load_cross_year_returns_both(self, storage: BarStorage) -> None:
        bars = [
            _make_bar(TS_2024_END - 3_600_000, close="100.0"),
            _make_bar(TS_2025_START, close="200.0"),
        ]
        storage.save_bars(bars)

        loaded = storage.load_bars(
            "BTC-USDT", "1H", TS_2024_END - 3_600_000, TS_2025_START
        )
        assert len(loaded) == 2
        assert loaded[0].ts == TS_2024_END - 3_600_000
        assert loaded[1].ts == TS_2025_START

    def test_load_filter_excludes_out_of_range(self, storage: BarStorage) -> None:
        bars = [
            _make_bar(TS_2024_START, close="100.0"),
            _make_bar(TS_2024_START + 3_600_000, close="200.0"),
            _make_bar(TS_2024_START + 7_200_000, close="300.0"),
        ]
        storage.save_bars(bars)

        # 区间 [start+1h, start+2h] 只含中间一根
        loaded = storage.load_bars(
            "BTC-USDT", "1H",
            TS_2024_START + 3_600_000, TS_2024_START + 3_600_000,
        )
        assert len(loaded) == 1
        assert loaded[0].close == Decimal("200.0")


class TestBarStorageInferType:
    @pytest.mark.parametrize(
        "inst_id, expected_type",
        [
            ("BTC-USDT", "spot"),
            ("ETH-USDT", "spot"),
            ("BTC-USDT-SWAP", "swap"),
            ("ETH-USDT-SWAP", "swap"),
            ("BTC-USDT-240928", "futures"),
            ("ETH-USDT-241227", "futures"),
            ("BTC-USD-240928-50000-C", "option"),
            ("BTC-USD-240928-50000-P", "option"),
        ],
    )
    def test_infer_type(self, inst_id: str, expected_type: str) -> None:
        assert BarStorage._infer_type(inst_id) == expected_type

    def test_path_uses_correct_inst_type_dir(
        self, storage: BarStorage, tmp_path: Path
    ) -> None:
        # SWAP 落到 data/candles/swap/...
        storage.save_bars([_make_bar(TS_2024_START, inst_id="BTC-USDT-SWAP")])
        assert (tmp_path / "candles" / "swap" / "BTC-USDT-SWAP" / "1H" / "2024.parquet").exists()

        # SPOT 落到 data/candles/spot/...
        storage.save_bars([_make_bar(TS_2024_START, inst_id="ETH-USDT")])
        assert (tmp_path / "candles" / "spot" / "ETH-USDT" / "1H" / "2024.parquet").exists()


class TestBarStorageEdgeCases:
    def test_save_empty_returns_zero(self, storage: BarStorage) -> None:
        assert storage.save_bars([]) == 0

    def test_load_nonexistent_returns_empty(self, storage: BarStorage) -> None:
        loaded = storage.load_bars("NOPE-USDT", "1H", 0, 2_000_000_000_000)
        assert loaded == []

    def test_list_years_empty(self, storage: BarStorage) -> None:
        assert storage.list_years("NOPE-USDT", "1H") == []

    def test_list_bar_sizes(self, storage: BarStorage) -> None:
        storage.save_bars([_make_bar(TS_2024_START)])
        storage.save_bars([_make_bar(TS_2024_START, bar_size="4H")])

        assert storage.list_bar_sizes("BTC-USDT") == ["1H", "4H"]
        assert storage.list_bar_sizes("NOPE-USDT") == []

    def test_confirm_field_preserved(self, storage: BarStorage) -> None:
        # 同一 ts 写 confirm=0 后写 confirm=1，应保留 confirm=1
        storage.save_bars([_make_bar(TS_2024_START, confirm=0)])
        storage.save_bars([_make_bar(TS_2024_START, confirm=1)])
        loaded = storage.load_bars("BTC-USDT", "1H", TS_2024_START, TS_2024_START)
        assert loaded[0].confirm == 1

    def test_file_size_within_budget(self, storage: BarStorage, tmp_path: Path) -> None:
        """F1.5: 1年1H parquet < 200KB。8784 根 bar 应远小于 200KB。"""
        bars = [_make_bar(TS_2024_START + i * 3_600_000) for i in range(8784)]
        storage.save_bars(bars)
        path = tmp_path / "candles" / "spot" / "BTC-USDT" / "1H" / "2024.parquet"
        size_kb = path.stat().st_size / 1024
        assert size_kb < 200, f"parquet file too large: {size_kb:.1f}KB"


# ============================================================
# InstrumentStorage 测试
# ============================================================

def _make_instrument(
    inst_id: str = "BTC-USDT",
    inst_type: str = "SPOT",
    tick_sz: str = "0.1",
    lot_sz: str = "0.00000001",
    min_sz: str = "0.00000001",
    ct_val: str = "1",
    settle_ccy: str = "",
    state: str = "live",
    list_time: int = 1577836800000,
) -> Instrument:
    """构造测试用 Instrument。SPOT 默认 settle='' ct_val=1。"""
    return Instrument(
        inst_id=inst_id,
        inst_type=inst_type,
        base_ccy="BTC" if inst_id.startswith("BTC") else "ETH",
        quote_ccy="USDT" if "USDT" in inst_id else "USD",
        settle_ccy=settle_ccy,
        tick_sz=Decimal(tick_sz),
        lot_sz=Decimal(lot_sz),
        min_sz=Decimal(min_sz),
        ct_val=Decimal(ct_val),
        state=state,
        list_time=list_time,
    )


@pytest.fixture
def inst_storage(tmp_path: Path) -> InstrumentStorage:
    return InstrumentStorage(tmp_path)


class TestInstrumentStorageRoundtrip:
    def test_save_and_load_spot(self, inst_storage: InstrumentStorage) -> None:
        instruments = [
            _make_instrument("BTC-USDT", inst_type="SPOT", tick_sz="0.1"),
            _make_instrument("ETH-USDT", inst_type="SPOT", tick_sz="0.01"),
        ]
        written = inst_storage.save_instruments("SPOT", instruments)
        assert written == 2

        loaded = inst_storage.load_instruments("SPOT")
        assert len(loaded) == 2
        assert loaded[0] == instruments[0]
        assert loaded[1] == instruments[1]

    def test_save_and_load_swap(self, inst_storage: InstrumentStorage) -> None:
        instruments = [
            _make_instrument(
                "BTC-USDT-SWAP", inst_type="SWAP",
                settle_ccy="USDT", ct_val="0.01",
            ),
        ]
        inst_storage.save_instruments("SWAP", instruments)
        loaded = inst_storage.load_instruments("SWAP")
        assert len(loaded) == 1
        assert loaded[0] == instruments[0]
        # SPOT 校验规则不应用于 SWAP
        assert loaded[0].settle_ccy == "USDT"
        assert loaded[0].ct_val == Decimal("0.01")

    def test_decimal_precision_preserved(self, inst_storage: InstrumentStorage) -> None:
        instruments = [
            _make_instrument(
                "BTC-USDT", tick_sz="0.00000001", lot_sz="0.000000000001",
            ),
        ]
        inst_storage.save_instruments("SPOT", instruments)
        loaded = inst_storage.load_instruments("SPOT")
        assert loaded[0].tick_sz == Decimal("0.00000001")
        assert loaded[0].tick_sz.as_tuple() == Decimal("0.00000001").as_tuple()
        assert loaded[0].lot_sz == Decimal("0.000000000001")

    def test_save_twice_overwrites_completely(self, inst_storage: InstrumentStorage) -> None:
        # 第一次写 3 个产品
        first = [
            _make_instrument("BTC-USDT"),
            _make_instrument("ETH-USDT"),
            _make_instrument("SOL-USDT"),
        ]
        inst_storage.save_instruments("SPOT", first)
        # 第二次写 1 个（全量替换）
        second = [_make_instrument("BTC-USDT")]
        inst_storage.save_instruments("SPOT", second)
        loaded = inst_storage.load_instruments("SPOT")
        assert len(loaded) == 1
        assert loaded[0].inst_id == "BTC-USDT"

    def test_load_nonexistent_returns_empty(self, inst_storage: InstrumentStorage) -> None:
        loaded = inst_storage.load_instruments("OPTION")
        assert loaded == []

    def test_load_one_finds_by_inst_id(self, inst_storage: InstrumentStorage) -> None:
        instruments = [
            _make_instrument("BTC-USDT"),
            _make_instrument("ETH-USDT"),
        ]
        inst_storage.save_instruments("SPOT", instruments)
        found = inst_storage.load_one("ETH-USDT", "SPOT")
        assert found is not None
        assert found.inst_id == "ETH-USDT"

    def test_load_one_returns_none_when_not_found(
        self, inst_storage: InstrumentStorage
    ) -> None:
        inst_storage.save_instruments("SPOT", [_make_instrument("BTC-USDT")])
        assert inst_storage.load_one("DOGE-USDT", "SPOT") is None

    def test_path_uses_lower_case_inst_type(
        self, inst_storage: InstrumentStorage, tmp_path: Path
    ) -> None:
        inst_storage.save_instruments("SPOT", [_make_instrument()])
        assert (tmp_path / "instruments" / "spot.json").exists()
        # 大小写不敏感：传 "spot" / "Spot" / "SPOT" 都读同一个文件
        inst_storage.save_instruments("spot", [_make_instrument()])
        assert (tmp_path / "instruments" / "spot.json").exists()

    def test_json_format_is_human_readable(
        self, inst_storage: InstrumentStorage, tmp_path: Path
    ) -> None:
        """JSON 文件应该是缩进格式 + UTF-8 中文可读。"""
        inst = Instrument(
            inst_id="ETH-USDT",
            inst_type="SPOT",
            base_ccy="ETH",
            quote_ccy="USDT",
            settle_ccy="",
            tick_sz=Decimal("0.01"),
            lot_sz=Decimal("0.0001"),
            min_sz=Decimal("0.0001"),
            ct_val=Decimal("1"),
            state="live",
            list_time=1577836800000,
        )
        inst_storage.save_instruments("SPOT", [inst])
        content = (tmp_path / "instruments" / "spot.json").read_text(encoding="utf-8")
        # 缩进
        assert "  \"inst_id\":" in content
        # 字段顺序稳定
        assert content.index("inst_id") < content.index("tick_sz")
        assert content.index("tick_sz") < content.index("state")


# ============================================================
# DataValidator 测试
# ============================================================

def _make_bar_for_validator(
    ts: int, close: str = "100.0", bar_size: str = "1H", confirm: int = 1
) -> Bar:
    """构造校验器测试用的 Bar。"""
    return Bar(
        ts=ts,
        open=Decimal("100.0"),
        high=Decimal("1000.0"),
        low=Decimal("1.0"),
        close=Decimal(close),
        vol=Decimal("10.0"),
        vol_quote=Decimal("1000.0"),
        confirm=confirm,
        inst_id="BTC-USDT",
        bar_size=bar_size,
    )


@pytest.fixture
def validator() -> DataValidator:
    return DataValidator()


class TestDataValidatorEmpty:
    def test_empty_bars_adds_error(self, validator: DataValidator) -> None:
        report = validator.validate([])
        assert not report.is_ok
        assert report.errors == ["empty bars"]
        assert report.duplicates == []
        assert report.missing == []
        assert report.invalid_ohlc == []

    def test_single_bar_is_ok(self, validator: DataValidator) -> None:
        report = validator.validate([_make_bar_for_validator(TS_2024_START)])
        assert report.is_ok
        assert report.summary() == (
            "ValidationReport(errors=0, duplicates=0, missing=0, invalid_ohlc=0)"
        )


class TestDataValidatorDuplicates:
    def test_detects_duplicate(self, validator: DataValidator) -> None:
        bars = [
            _make_bar_for_validator(TS_2024_START),
            _make_bar_for_validator(TS_2024_START, close="200.0"),
            _make_bar_for_validator(TS_2024_START, close="300.0"),
        ]
        report = validator.validate(bars)
        assert not report.is_ok
        assert len(report.duplicates) == 1
        assert report.duplicates[0].ts == TS_2024_START
        assert report.duplicates[0].count == 3

    def test_no_duplicate_when_ts_unique(self, validator: DataValidator) -> None:
        bars = [
            _make_bar_for_validator(TS_2024_START),
            _make_bar_for_validator(TS_2024_START + 3_600_000),
        ]
        report = validator.validate(bars)
        assert report.duplicates == []


class TestDataValidatorMissing:
    def test_detects_missing_one_bar(self, validator: DataValidator) -> None:
        # 3 根连续 1H，但中间缺一根
        bars = [
            _make_bar_for_validator(TS_2024_START),
            _make_bar_for_validator(TS_2024_START + 2 * 3_600_000),
        ]
        report = validator.validate(bars)
        assert len(report.missing) == 1
        m = report.missing[0]
        assert m.prev_ts == TS_2024_START
        assert m.next_ts == TS_2024_START + 2 * 3_600_000
        assert m.gap_ms == 2 * 3_600_000
        assert m.expected_ms == 3_600_000
        assert m.missing_count == 1

    def test_detects_missing_multiple_bars(self, validator: DataValidator) -> None:
        # 缺 5 根（gap = 6 根）
        bars = [
            _make_bar_for_validator(TS_2024_START),
            _make_bar_for_validator(TS_2024_START + 6 * 3_600_000),
        ]
        report = validator.validate(bars)
        assert len(report.missing) == 1
        assert report.missing[0].missing_count == 5

    def test_no_missing_when_continuous(self, validator: DataValidator) -> None:
        bars = [
            _make_bar_for_validator(TS_2024_START + i * 3_600_000) for i in range(10)
        ]
        report = validator.validate(bars)
        assert report.missing == []

    def test_unconfirmed_bars_skip_continuity_check(
        self, validator: DataValidator
    ) -> None:
        # 都是 confirm=0 的，跳过连续性校验，不报 missing
        bars = [
            _make_bar_for_validator(TS_2024_START, confirm=0),
            _make_bar_for_validator(TS_2024_START + 2 * 3_600_000, confirm=0),
        ]
        report = validator.validate(bars)
        assert report.missing == []


class TestDataValidatorInvalidOhlc:
    def test_high_less_than_low_detected(self, validator: DataValidator) -> None:
        # Bar.__post_init__ 会拦 high<low，需要用 object.__setattr__ 绕过
        bar = _make_bar_for_validator(TS_2024_START)
        object.__setattr__(bar, "high", Decimal("0.5"))
        object.__setattr__(bar, "low", Decimal("1.0"))
        report = validator.validate([bar])
        assert len(report.invalid_ohlc) >= 1
        reasons = " ".join(r.reason for r in report.invalid_ohlc)
        assert "high<low" in reasons

    def test_open_out_of_range_detected(self, validator: DataValidator) -> None:
        bar = _make_bar_for_validator(TS_2024_START)
        object.__setattr__(bar, "open", Decimal("2000.0"))  # > high=1000
        report = validator.validate([bar])
        assert len(report.invalid_ohlc) == 1
        assert "open" in report.invalid_ohlc[0].reason

    def test_close_out_of_range_detected(self, validator: DataValidator) -> None:
        bar = _make_bar_for_validator(TS_2024_START)
        object.__setattr__(bar, "close", Decimal("0.5"))  # < low=1.0
        report = validator.validate([bar])
        assert len(report.invalid_ohlc) == 1
        assert "close" in report.invalid_ohlc[0].reason


class TestDataValidatorBarSizeMs:
    @pytest.mark.parametrize(
        "bar_size, expected_ms",
        [
            ("1m", 60_000),
            ("5m", 300_000),
            ("15m", 900_000),
            ("30m", 1_800_000),
            ("1H", 3_600_000),
            ("4H", 14_400_000),
            ("1D", 86_400_000),
            ("1W", 604_800_000),
            ("2H", 7_200_000),
            ("3D", 259_200_000),
        ],
    )
    def test_bar_size_ms(self, bar_size: str, expected_ms: int) -> None:
        assert DataValidator._bar_size_ms(bar_size) == expected_ms

    def test_unknown_bar_size_raises(self) -> None:
        with pytest.raises(ValueError, match="unknown bar_size"):
            DataValidator._bar_size_ms("2S")


class TestDataValidatorMixedReport:
    def test_mixed_report_contains_all_categories(
        self, validator: DataValidator
    ) -> None:
        # 1 个 dup + 1 个 missing + 1 个 valid
        bars = [
            _make_bar_for_validator(TS_2024_START),
            _make_bar_for_validator(TS_2024_START, close="200.0"),  # dup
            _make_bar_for_validator(TS_2024_START + 2 * 3_600_000),  # gap
        ]
        report = validator.validate(bars)
        assert not report.is_ok
        assert len(report.duplicates) == 1
        assert len(report.missing) == 1
        assert report.invalid_ohlc == []  # OHLC 合法

    def test_report_summary_format(self, validator: DataValidator) -> None:
        report = validator.validate([_make_bar_for_validator(TS_2024_START)])
        assert "errors=0" in report.summary()
        assert "duplicates=0" in report.summary()
        assert "missing=0" in report.summary()
        assert "invalid_ohlc=0" in report.summary()


# ============================================================
# DataAligner 测试
# ============================================================

def _make_bar_for_aligner(
    ts: int, close: float = 100.0, inst_id: str = "BTC-USDT",
    bar_size: str = "1H",
) -> Bar:
    """构造对齐器测试用的 Bar。close 用 float 表示便于断言。"""
    return Bar(
        ts=ts,
        open=Decimal("100.0"),
        high=Decimal("1000.0"),
        low=Decimal("1.0"),
        close=Decimal(str(close)),
        vol=Decimal("10.0"),
        vol_quote=Decimal("1000.0"),
        confirm=1,
        inst_id=inst_id,
        bar_size=bar_size,
    )


@pytest.fixture
def aligner() -> DataAligner:
    return DataAligner()


class TestDataAlignerBasic:
    def test_empty_input_returns_empty_df(self, aligner: DataAligner) -> None:
        df = aligner.align({})
        assert df.empty

    def test_single_instrument_returns_its_columns(
        self, aligner: DataAligner
    ) -> None:
        bars = [_make_bar_for_aligner(TS_2024_START + i * 3_600_000) for i in range(5)]
        df = aligner.align({"BTC-USDT": bars})
        assert len(df) == 5
        # 列名为 inst_id_field
        assert "BTC-USDT_close" in df.columns
        assert "BTC-USDT_open" in df.columns
        # 时间索引升序
        assert df.index[0] == TS_2024_START
        assert df.index[-1] == TS_2024_START + 4 * 3_600_000

    def test_two_instruments_same_range(self, aligner: DataAligner) -> None:
        # BTC 和 ETH 都有 5 根同样 ts 的 K线
        btc = [
            _make_bar_for_aligner(TS_2024_START + i * 3_600_000, close=100.0 + i)
            for i in range(5)
        ]
        eth = [_make_bar_for_aligner(
            TS_2024_START + i * 3_600_000, close=50.0 + i, inst_id="ETH-USDT"
        ) for i in range(5)]
        df = aligner.align({"BTC-USDT": btc, "ETH-USDT": eth})
        assert len(df) == 5
        assert "BTC-USDT_close" in df.columns
        assert "ETH-USDT_close" in df.columns
        # 第一行
        assert df["BTC-USDT_close"].iloc[0] == 100.0
        assert df["ETH-USDT_close"].iloc[0] == 50.0


class TestDataAlignerDifferentLengths:
    def test_btc_longer_eth_shorter(self, aligner: DataAligner) -> None:
        # BTC 有 5 根，ETH 只有 3 根（中间缺 ts_1, ts_2）
        btc = [_make_bar_for_aligner(TS_2024_START + i * 3_600_000) for i in range(5)]
        eth = [
            _make_bar_for_aligner(TS_2024_START, close=50.0, inst_id="ETH-USDT"),
            _make_bar_for_aligner(TS_2024_START + 3_600_000, close=51.0, inst_id="ETH-USDT"),
            _make_bar_for_aligner(TS_2024_START + 4 * 3_600_000, close=54.0, inst_id="ETH-USDT"),
        ]
        df = aligner.align({"BTC-USDT": btc, "ETH-USDT": eth}, fill="ffill")
        assert len(df) == 5  # 取并集
        # ETH 在 ts_2h、ts_3h 应该用 ffill
        assert df["ETH-USDT_close"].iloc[0] == 50.0
        assert df["ETH-USDT_close"].iloc[1] == 51.0
        assert df["ETH-USDT_close"].iloc[2] == 51.0  # ffill
        assert df["ETH-USDT_close"].iloc[3] == 51.0  # ffill
        assert df["ETH-USDT_close"].iloc[4] == 54.0

    def test_eth_no_value_at_first_row_ffill_keeps_nan(
        self, aligner: DataAligner
    ) -> None:
        # ETH 在 ts_0 没数据，BTC 有；ffill 不能补第一行的 NaN
        btc = [_make_bar_for_aligner(TS_2024_START + i * 3_600_000) for i in range(3)]
        eth = [_make_bar_for_aligner(
            TS_2024_START + 3_600_000, close=50.0, inst_id="ETH-USDT"
        )]
        df = aligner.align({"BTC-USDT": btc, "ETH-USDT": eth}, fill="ffill")
        assert len(df) == 3
        # ETH 在 ts_0 是 NaN（ffill 不能向后填）
        import math
        assert math.isnan(df["ETH-USDT_close"].iloc[0])
        # ts_1 起 ETH 有值
        assert df["ETH-USDT_close"].iloc[1] == 50.0
        assert df["ETH-USDT_close"].iloc[2] == 50.0  # ffill


class TestDataAlignerFillStrategies:
    @pytest.fixture
    def mixed_bars(self) -> dict[str, list[Bar]]:
        btc = [_make_bar_for_aligner(TS_2024_START + i * 3_600_000, close=100.0 + i)
                for i in range(5)]
        eth = [_make_bar_for_aligner(TS_2024_START + i * 3_600_000, close=50.0 + i,
                                       inst_id="ETH-USDT") for i in range(5)]
        # ETH 删掉中间两根
        eth = [eth[0], eth[3], eth[4]]
        return {"BTC-USDT": btc, "ETH-USDT": eth}

    def test_ffill_fills_with_previous(self, aligner: DataAligner, mixed_bars) -> None:
        df = aligner.align(mixed_bars, fill="ffill")
        # ETH 在 ts_1h, ts_2h 应该都是 50.0（ffill from ts_0）
        assert df["ETH-USDT_close"].iloc[1] == 50.0
        assert df["ETH-USDT_close"].iloc[2] == 50.0

    def test_nan_leaves_gaps(self, aligner: DataAligner, mixed_bars) -> None:
        df = aligner.align(mixed_bars, fill="nan")
        import math
        # ETH 在 ts_1h, ts_2h 是 NaN
        assert math.isnan(df["ETH-USDT_close"].iloc[1])
        assert math.isnan(df["ETH-USDT_close"].iloc[2])
        # 在 ts_0, ts_3, ts_4 有值
        assert df["ETH-USDT_close"].iloc[0] == 50.0
        assert df["ETH-USDT_close"].iloc[3] == 53.0

    def test_interpolate_linear_fills(self, aligner: DataAligner, mixed_bars) -> None:
        # ETH ts_0=50.0, ts_3h=53.0，interpolate 后 ts_1h, ts_2h 应分别是 51.0, 52.0
        df = aligner.align(mixed_bars, fill="interpolate")
        assert df["ETH-USDT_close"].iloc[1] == 51.0
        assert df["ETH-USDT_close"].iloc[2] == 52.0


class TestDataAlignerDedup:
    def test_same_ts_deduped_keeps_last(self, aligner: DataAligner) -> None:
        bars = [
            _make_bar_for_aligner(TS_2024_START, close=100.0),
            _make_bar_for_aligner(TS_2024_START, close=999.0),  # 同 ts 后写
        ]
        df = aligner.align({"BTC-USDT": bars})
        assert len(df) == 1
        assert df["BTC-USDT_close"].iloc[0] == 999.0


class TestDataAlignerColumnOrder:
    def test_columns_are_inst_field_prefixed(self, aligner: DataAligner) -> None:
        bars = [_make_bar_for_aligner(TS_2024_START)]
        df = aligner.align({"BTC-USDT": bars})
        expected = {
            "BTC-USDT_open", "BTC-USDT_high", "BTC-USDT_low",
            "BTC-USDT_close", "BTC-USDT_vol",
        }
        assert set(df.columns) == expected
