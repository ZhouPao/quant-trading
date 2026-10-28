"""数据校验器（spec §6.5）。

校验 K线列表的：
- 重复时间戳（同 ts 出现多次）
- 连续性缺失（仅校验已收盘 K线，confirm=1）
- OHLC 合法性（low <= open/close <= high 且 low <= high）

注意：Bar.__post_init__ 已对单根 Bar 做 OHLC 校验，
此处的 OHLC 校验作为防御性二次检查（例如反序列化绕过构造时）。
"""
from __future__ import annotations

from dataclasses import dataclass, field

from quant.models.bar import Bar


@dataclass
class DuplicateRecord:
    ts: int
    count: int  # 出现次数（含第一次）


@dataclass
class MissingRecord:
    prev_ts: int
    next_ts: int
    gap_ms: int  # 实际间隔
    expected_ms: int  # 期望间隔
    missing_count: int  # 中间缺失的 K线根数


@dataclass
class InvalidOhlcRecord:
    ts: int
    reason: str


@dataclass
class ValidationReport:
    errors: list[str] = field(default_factory=list)
    duplicates: list[DuplicateRecord] = field(default_factory=list)
    missing: list[MissingRecord] = field(default_factory=list)
    invalid_ohlc: list[InvalidOhlcRecord] = field(default_factory=list)

    def add_error(self, msg: str) -> None:
        self.errors.append(msg)

    def add_duplicate(self, ts: int, count: int) -> None:
        self.duplicates.append(DuplicateRecord(ts=ts, count=count))

    def add_missing(
        self, prev_ts: int, next_ts: int, gap_ms: int, expected_ms: int
    ) -> None:
        missing_count = gap_ms // expected_ms - 1
        self.missing.append(
            MissingRecord(
                prev_ts=prev_ts, next_ts=next_ts,
                gap_ms=gap_ms, expected_ms=expected_ms,
                missing_count=missing_count,
            )
        )

    def add_invalid_ohlc(self, ts: int, reason: str) -> None:
        self.invalid_ohlc.append(InvalidOhlcRecord(ts=ts, reason=reason))

    @property
    def is_ok(self) -> bool:
        """是否完全通过校验（无任何错误/重复/缺失/非法）。"""
        return not (
            self.errors or self.duplicates or self.missing or self.invalid_ohlc
        )

    def summary(self) -> str:
        return (
            f"ValidationReport(errors={len(self.errors)}, "
            f"duplicates={len(self.duplicates)}, "
            f"missing={len(self.missing)}, "
            f"invalid_ohlc={len(self.invalid_ohlc)})"
        )


class DataValidator:
    """K线列表数据校验器。"""

    def validate(self, bars: list[Bar]) -> ValidationReport:
        """校验 K线列表。返回详细报告。"""
        report = ValidationReport()
        if not bars:
            report.add_error("empty bars")
            return report

        # 1. 重复时间戳检测
        counts: dict[int, int] = {}
        for b in bars:
            counts[b.ts] = counts.get(b.ts, 0) + 1
        for ts, c in counts.items():
            if c > 1:
                report.add_duplicate(ts, c)

        # 2. 连续性校验（仅校验已收盘 K线，先按 ts 去重避免同 ts 影响 gap）
        seen_ts: set[int] = set()
        closed: list[Bar] = []
        for b in sorted([b for b in bars if b.is_closed()], key=lambda b: b.ts):
            if b.ts not in seen_ts:
                closed.append(b)
                seen_ts.add(b.ts)
        if len(closed) >= 2:
            bar_size_ms = self._bar_size_ms(closed[0].bar_size)
            for i in range(1, len(closed)):
                gap = closed[i].ts - closed[i - 1].ts
                if gap != bar_size_ms:
                    report.add_missing(
                        closed[i - 1].ts, closed[i].ts, gap, bar_size_ms
                    )

        # 3. OHLC 合法性（防御性二次校验，理论上 Bar 构造时已拦截）
        for b in bars:
            if b.high < b.low:
                report.add_invalid_ohlc(b.ts, f"high<low ({b.high}<{b.low})")
            if not (b.low <= b.open <= b.high):
                report.add_invalid_ohlc(
                    b.ts, f"open {b.open} not in [{b.low}, {b.high}]"
                )
            if not (b.low <= b.close <= b.high):
                report.add_invalid_ohlc(
                    b.ts, f"close {b.close} not in [{b.low}, {b.high}]"
                )

        return report

    @staticmethod
    def _bar_size_ms(bar_size: str) -> int:
        """OKX bar_size 转毫秒。支持 1m/5m/15m/30m/1H/4H/1D/1W。"""
        unit = bar_size[-1]
        n = int(bar_size[:-1])
        if unit == "m":
            return n * 60_000
        if unit == "H":
            return n * 3_600_000
        if unit == "D":
            return n * 86_400_000
        if unit == "W":
            return n * 604_800_000
        raise ValueError(f"unknown bar_size: {bar_size}")


__all__ = [
    "DataValidator",
    "ValidationReport",
    "DuplicateRecord",
    "MissingRecord",
    "InvalidOhlcRecord",
]
