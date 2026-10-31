"""数据质量报告（MS4-C §9）。

`ValidationReport` 是**一次校验**的内存结果，缺口没有「是否已修复」的
概念。本模块把它与上次的 `latest.json` 合并，跨次累计缺口状态：

- 本次仍缺 → `open`
- 上次 open、本次窗口内不再缺 → `repaired` + `repaired_at`
- 上次 open、但落在本次校验窗口外 → 保持 open（**不能**因为「没看它」
  就当成修好了）

文件路径 `data/quality/{inst_id}/{bar_size}/latest.json`：按目录分层，
避免 `inst_id` 里的 `-` 与文件名解析冲突。
"""
from __future__ import annotations

import json
import time
from pathlib import Path

from quant.foundation.logger import get_logger
from quant.storage.bar_storage import BarStorage
from quant.storage.validator import DataValidator, ValidationReport

log = get_logger("storage.quality")

RULES_ALERT = "data_quality"


def report_path(data_dir: Path, inst_id: str, bar_size: str) -> Path:
    """质量报告落盘路径。"""
    return data_dir / "quality" / inst_id / bar_size / "latest.json"


def load_report(data_dir: Path, inst_id: str, bar_size: str) -> dict | None:
    """读上次报告；不存在或损坏时返回 None（按首次运行处理）。"""
    path = report_path(data_dir, inst_id, bar_size)
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        log.warning(f"quality report unreadable inst={inst_id} bar={bar_size}: {e}")
        return None


def save_report(data_dir: Path, report: dict) -> Path:
    """原子写：先写临时文件再替换，避免半截 JSON 被 monitor 读到。"""
    path = report_path(data_dir, str(report["inst_id"]), str(report["bar_size"]))
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    tmp.replace(path)
    return path


def merge_report(
    prev: dict | None,
    report: ValidationReport,
    *,
    inst_id: str,
    bar_size: str,
    checked_range: tuple[int, int],
    now: int,
    present: int,
) -> dict:
    """把本次校验并入上次报告。

    Args:
        prev: 上次的 latest.json 内容（首次运行为 None）
        report: 本次 `DataValidator.validate` 的结果
        checked_range: 本次校验覆盖的 [start, end]
        now: 本次生成时间
        present: 本次窗口内实际存在的已收盘 K 线根数
    """
    start, end = checked_range
    prev_gaps = {g["from"]: g for g in (prev or {}).get("gaps", [])}
    current = {
        m.prev_ts + m.expected_ms: {
            "from": m.prev_ts + m.expected_ms,
            "to": m.next_ts - m.expected_ms,
            "bars": m.missing_count,
        }
        for m in report.missing
    }

    gaps: list[dict] = []
    for from_ts, gap in prev_gaps.items():
        if from_ts in current:
            # 仍缺：用本次的区间/根数覆盖（缺口可能被补了一部分）
            gaps.append({**gap, **current[from_ts], "status": "open"})
        elif gap.get("status") == "open" and start <= from_ts <= end:
            gaps.append({**gap, "status": "repaired", "repaired_at": now})
        else:
            gaps.append(gap)
    for from_ts, gap in current.items():
        if from_ts not in prev_gaps:
            gaps.append({**gap, "status": "open"})
    gaps.sort(key=lambda g: g["from"])

    open_gaps = [g for g in gaps if g.get("status") == "open"]
    repaired = [g for g in gaps if g.get("status") == "repaired"]
    missing_in_range = sum(
        gap["bars"] for gap in gaps
        if gap.get("status") == "open" and start <= gap["from"] <= end
    )

    return {
        "inst_id": inst_id,
        "bar_size": bar_size,
        "generated_at": now,
        "checked_range": [start, end],
        "summary": {
            "expected": present + missing_in_range,
            "present": present,
            "gaps_open": len(open_gaps),
            "gaps_repaired": len(repaired),
        },
        "gaps": gaps,
        "invalid_ohlc": [
            {"ts": item.ts, "reason": item.reason} for item in report.invalid_ohlc
        ],
    }


class QualityReporter:
    """生成/读取数据质量报告。"""

    def __init__(
        self,
        data_dir: Path,
        storage: BarStorage | None = None,
        validator: DataValidator | None = None,
    ) -> None:
        self._data_dir = data_dir
        self._storage = storage or BarStorage(data_dir)
        self._validator = validator or DataValidator()

    def check(
        self,
        inst_id: str,
        bar_size: str,
        *,
        now: int | None = None,
        start_ts: int | None = None,
        end_ts: int | None = None,
        window_bars: int = 5000,
    ) -> dict | None:
        """校验一段区间并合并落盘；本地没有数据时返回 None。

        不传 `start_ts`/`end_ts` 时校验最近 `window_bars` 根已收盘 K 线。
        """
        now = int(time.time() * 1000) if now is None else now
        bar_ms = DataValidator._bar_size_ms(bar_size)
        if end_ts is None:
            end_ts = ((now - bar_ms) // bar_ms) * bar_ms
        if start_ts is None:
            start_ts = end_ts - max(window_bars - 1, 0) * bar_ms

        bars = self._storage.load_bars(inst_id, bar_size, start_ts, end_ts)
        if not bars:
            log.warning(
                f"quality skip inst={inst_id} bar={bar_size} "
                f"(no local bars in [{start_ts}, {end_ts}])"
            )
            return None

        closed = {b.ts for b in bars if b.is_closed()}
        report = self._validator.validate(bars)
        prev = load_report(self._data_dir, inst_id, bar_size)
        merged = merge_report(
            prev,
            report,
            inst_id=inst_id,
            bar_size=bar_size,
            checked_range=(start_ts, end_ts),
            now=now,
            present=len(closed),
        )
        save_report(self._data_dir, merged)
        return merged

    def load(self, inst_id: str, bar_size: str) -> dict | None:
        return load_report(self._data_dir, inst_id, bar_size)

    def summaries(self) -> list[dict]:
        """列出全部 latest.json 的摘要（供看板与 monitor 使用）。"""
        root = self._data_dir / "quality"
        if not root.exists():
            return []
        out: list[dict] = []
        for path in sorted(root.glob("*/*/latest.json")):
            inst_id, bar_size = path.parent.parent.name, path.parent.name
            report = load_report(self._data_dir, inst_id, bar_size)
            if report is None:
                continue
            out.append(
                {
                    "inst_id": report.get("inst_id", inst_id),
                    "bar_size": report.get("bar_size", bar_size),
                    "generated_at": report.get("generated_at"),
                    "checked_range": report.get("checked_range", []),
                    "summary": report.get("summary", {}),
                }
            )
        return out


__all__ = [
    "RULES_ALERT",
    "QualityReporter",
    "load_report",
    "merge_report",
    "report_path",
    "save_report",
]
