"""存储层加固测试：路径穿越防护 + parquet 原子写（M2 批次四 P2）。"""
from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest

from quant.models.bar import Bar
from quant.storage.bar_storage import BarStorage
from quant.storage.quality import report_path


def _bar(ts: int) -> Bar:
    return Bar(
        ts=ts,
        open=Decimal("100"),
        high=Decimal("110"),
        low=Decimal("90"),
        close=Decimal("105"),
        vol=Decimal("10"),
        vol_quote=Decimal("1050"),
        confirm=1,
        inst_id="BTC-USDT",
        bar_size="1H",
    )


class TestPathTraversalGuard:
    def test_rejects_inst_id_with_slashes(self, tmp_path: Path) -> None:
        storage = BarStorage(tmp_path)
        with pytest.raises(ValueError, match="invalid inst_id"):
            storage.load_bars("../../etc", "1H", 0, 1)

    def test_rejects_inst_id_with_encoded_chars(self, tmp_path: Path) -> None:
        storage = BarStorage(tmp_path)
        with pytest.raises(ValueError, match="invalid inst_id"):
            storage.list_bar_sizes("..%2F..%2Fetc")

    def test_rejects_bad_bar_size(self, tmp_path: Path) -> None:
        storage = BarStorage(tmp_path)
        with pytest.raises(ValueError, match="invalid bar_size"):
            storage.load_bars("BTC-USDT", "../x", 0, 1)

    def test_quality_report_path_rejects_traversal(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError, match="invalid"):
            report_path(tmp_path, "../../etc", "1H")

    def test_normal_identifiers_still_work(self, tmp_path: Path) -> None:
        storage = BarStorage(tmp_path)
        assert storage.save_bars([_bar(0)]) == 1
        assert storage.load_bars("BTC-USDT", "1H", 0, 1)
        assert "1H" in storage.list_bar_sizes("BTC-USDT")


class TestAtomicParquetWrite:
    def test_no_tmp_files_left_after_save(self, tmp_path: Path) -> None:
        """写入走 tmp+rename：save 后目录里不残留 .tmp，数据完整（P2）。"""
        storage = BarStorage(tmp_path)
        storage.save_bars([_bar(0), _bar(3_600_000)])
        storage.save_bars([_bar(2 * 3_600_000)])  # 触发 merge 路径

        assert list(tmp_path.rglob("*.tmp")) == []
        loaded = storage.load_bars("BTC-USDT", "1H", 0, 2 * 3_600_000)
        assert len(loaded) == 3
