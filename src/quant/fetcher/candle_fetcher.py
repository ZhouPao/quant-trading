"""CandleFetcher: K线下载编排（spec §5.5）。

从 OKX 拉历史 K线，落盘到 parquet，校验后自动补拉缺失段。

核心能力：
- 断点续传：每个 (inst_id, bar_size) 在 `.checkpoints/{key}.json` 记 last_ts
- 自动翻页：用 history-candles 的 after 参数翻页拉旧数据
- 缺失补拉：完成后调 DataValidator，对每个 missing_range 翻页补全（最多 max_attempts 次）

OKX history-candles 参数语义（与 spec §4.3 一致）：
- after:  返回此时间戳**之前**的 K线（更旧的数据）
- before: 返回此时间戳**之后**的 K线（更新的数据）
- 返回顺序：时间从新到旧（最新在前）
"""
from __future__ import annotations

import time
from pathlib import Path

from quant.fetcher.checkpoint import Checkpoint
from quant.foundation.logger import get_logger
from quant.models.bar import Bar
from quant.okx.client import OKXClient
from quant.storage.bar_storage import BarStorage
from quant.storage.validator import DataValidator

_log = get_logger("fetcher.candle")


class CandleFetcher:
    """从 OKX 拉历史 K线并落盘。

    Args:
        client: OKXClient 实例
        data_dir: 数据根目录（parquet 落 data/candles/...，断点落 .checkpoints/）
        page_size: 单次 API 调用返回上限（OKX 最大 100）
        max_attempts: 缺失补拉最大尝试次数（默认 3，避免无限循环）
    """

    PAGE_SIZE = 100

    def __init__(
        self,
        client: OKXClient,
        data_dir: Path,
        page_size: int = PAGE_SIZE,
        max_attempts: int = 3,
    ) -> None:
        self._client = client
        self._storage = BarStorage(data_dir)
        self._validator = DataValidator()
        self._ckpt_dir = data_dir / ".checkpoints"
        self._ckpt_dir.mkdir(parents=True, exist_ok=True)
        self._page_size = page_size
        self._max_attempts = max_attempts

    # ------------------------------------------------------------------
    # 公开 API
    # ------------------------------------------------------------------

    def fetch(
        self,
        inst_id: str,
        bar_size: str,
        start_ts: int,
        end_ts: int,
    ) -> int:
        """拉取 [start_ts, end_ts] 区间 K线，返回净增根数。

        流程：
        1. 读断点 ckpt，主循环只拉 cursor 之后的新增段
        2. 若请求起点早于断点（向更早历史回补），先显式补拉更早段
           （原先这段会被静默丢弃，返回「净增 0」）
        3. 校验已落盘数据（含前导缺口），对 missing_ranges 翻页补拉
           （最多 max_attempts 次）
        """
        if start_ts < 0 or end_ts < 0:
            raise ValueError(
                f"start_ts and end_ts must be non-negative, "
                f"got start={start_ts} end={end_ts}"
            )
        if start_ts > end_ts:
            raise ValueError(
                f"start_ts ({start_ts}) must be <= end_ts ({end_ts})"
            )

        bar_size_ms = self._bar_size_ms(bar_size)
        _log.info(
            f"fetch start inst={inst_id} bar={bar_size} "
            f"range=[{start_ts}, {end_ts}] bar_ms={bar_size_ms}"
        )

        ckpt = self._load_checkpoint(inst_id, bar_size)
        total = 0

        # ① 主拉取循环：只拉断点之后的新增段
        # 断点续：从 last_ts+1 个 bar 开始；首次拉：从 start_ts 开始
        cursor = (
            max(start_ts, ckpt.last_ts + bar_size_ms)
            if ckpt.last_ts > 0 else start_ts
        )
        _log.info(
            f"checkpoint last_ts={ckpt.last_ts} → cursor={cursor} "
            f"({'resume' if ckpt.last_ts > 0 else 'fresh'})"
        )

        max_saved_ts = 0
        if cursor > end_ts:
            # 断点已超过 end_ts，跳过主循环（仅回补 + 校验）
            _log.info(
                f"cursor={cursor} > end_ts={end_ts}, skip main loop, "
                f"only validate"
            )
        else:
            # 主拉取循环：用 after 推进向旧方向翻页
            # OKX history-candles: after=N 返回 ts < N（更旧），返回 ts 倒序
            # 策略：第一页 after=end_ts+1（拿 <= end_ts 的最新 page_size 根）
            # 后续页 after=oldest-1（拿比上次更旧的）
            # 停止：返回空，或 oldest <= cursor（已覆盖到 start）
            next_after = end_ts + 1  # 第一页拿 ts <= end_ts
            prev_oldest: int | None = None
            while True:
                batch = self._client.get_history_candles(
                    inst_id=inst_id,
                    bar=bar_size,
                    after=next_after,
                    limit=self._page_size,
                )
                if not batch:
                    _log.info(f"page empty, stop main loop at total={total}")
                    break
                # OKX 返回按 ts 倒序（新→旧），转正序
                bars = sorted(batch, key=lambda b: b.ts)
                oldest = bars[0].ts
                # 停滞保护：OKX 异常返回同一页时 oldest 不前进，主循环会
                # 无限打转（资金费率侧有 stall 检测，K 线侧原先没有，P2）
                if prev_oldest is not None and oldest >= prev_oldest:
                    _log.warning(
                        f"candle page stalled: oldest={oldest} not older than "
                        f"previous={prev_oldest}, stop main loop"
                    )
                    break
                prev_oldest = oldest
                # 过滤：只保留 [cursor, end_ts] 区间
                in_range = [b for b in bars if cursor <= b.ts <= end_ts]
                newest = bars[-1].ts
                if in_range:
                    saved = self._storage.save_bars(in_range)
                    total += saved
                    max_saved_ts = max(max_saved_ts, newest)
                    # 断点：更新到已落盘的最新 ts
                    self._save_checkpoint(inst_id, bar_size, newest)
                    _log.info(
                        f"page saved={saved} oldest={oldest} newest={newest} "
                        f"ckpt={newest}"
                    )
                # 推进：下一页拿比 oldest 更旧的（ts < oldest）
                next_after = oldest
                # oldest 已 <= cursor，说明已覆盖到起点
                if oldest <= cursor:
                    break
                # 不足 page_size，说明没有更旧数据
                if len(bars) < self._page_size:
                    break

            # 翻页是从新到旧，循环结束时断点会停在最旧那一页；
            # 这里统一推回已落盘的最新 ts，否则下一轮会把整段历史重拉一遍。
            if max_saved_ts:
                self._save_checkpoint(inst_id, bar_size, max_saved_ts)

            _log.info(f"main loop done, total={total}, start validation+fill")

        # ② 向早回补：断点只记录「已落盘的最新 ts」。请求起点早于断点（想补
        # 更早历史）时，原先 cursor = ckpt.last_ts + bar_ms 会把早于断点的
        # bar 全部过滤、validator 又只查相邻缺口 → 静默返回「净增 0」（必须
        # 手删 checkpoint 才能回补）。这里显式补拉更早段；与已有数据重叠的
        # 部分由 save_bars 按 ts 去重，幂等。
        if ckpt.last_ts > 0 and start_ts < ckpt.last_ts:
            older_end = min(ckpt.last_ts - bar_size_ms, end_ts)
            if older_end >= start_ts:
                _log.info(
                    f"backfill before checkpoint: [{start_ts}, {older_end}] "
                    f"(ckpt={ckpt.last_ts})"
                )
                older = self._fetch_page_range(
                    inst_id, bar_size, start_ts, older_end
                )
                if older:
                    total += self._storage.save_bars(older)

        # ③ 校验（含前导缺口）+ 补拉缺失
        self._validate_and_fill(inst_id, bar_size, start_ts, end_ts, bar_size_ms)

        _log.info(f"fetch done inst={inst_id} bar={bar_size} total={total}")
        return total

    def sync(
        self,
        inst_id: str,
        bar_size: str,
        now_ts: int | None = None,
        lookback_bars: int = 3,
    ) -> int:
        """增量同步到最近一根已收盘 K线，返回净增根数。

        与 `fetch` 的区别：终点自动取「最近已收盘的 bar」，不需要人工传日期，
        适合定时轮询。未收盘的最后一根不拉，它的 close 还会变，落盘会污染
        回测与日报。

        每轮都会把断点回退 `lookback_bars + 1` 根重拉：上一轮落盘的最后一根
        当时可能还没收盘，只有重拉才能覆盖成收盘后的完整 OHLC。顺带也补上
        断点附近可能出现的洞（如上次进程在落盘前被杀）。
        """
        bar_ms = self._bar_size_ms(bar_size)
        now = int(time.time() * 1000) if now_ts is None else now_ts
        last_closed = ((now - bar_ms) // bar_ms) * bar_ms

        ckpt = self._load_checkpoint(inst_id, bar_size)
        if ckpt.last_ts > last_closed:
            _log.warning(
                f"sync rewind inst={inst_id} bar={bar_size} "
                f"ckpt={ckpt.last_ts} ahead of last_closed={last_closed}"
            )

        if ckpt.last_ts > 0:
            anchor = min(ckpt.last_ts, last_closed)
            start_ts = max(0, anchor - (lookback_bars + 1) * bar_ms)
            # 把断点退到 start_ts 前一根，让 fetch 的 cursor 正好落在 start_ts
            self._save_checkpoint(inst_id, bar_size, start_ts - bar_ms)
        else:
            start_ts = max(0, last_closed - lookback_bars * bar_ms)

        if start_ts > last_closed:
            _log.info(
                f"sync skip inst={inst_id} bar={bar_size} "
                f"(nothing closed yet, last_closed={last_closed})"
            )
            return 0

        _log.info(
            f"sync start inst={inst_id} bar={bar_size} "
            f"range=[{start_ts}, {last_closed}] ckpt={ckpt.last_ts}"
        )
        return self.fetch(inst_id, bar_size, start_ts, last_closed)

    # ------------------------------------------------------------------
    # 内部：补拉段
    # ------------------------------------------------------------------

    def _fetch_page_range(
        self,
        inst_id: str,
        bar_size: str,
        range_start: int,
        range_end: int,
    ) -> list[Bar]:
        """补拉缺失段：拉 [range_start, range_end] 范围的 K线。

        多次翻页直到把整个缺失段覆盖。
        """
        all_bars: list[Bar] = []
        # 从 range_end 开始向旧方向翻页
        next_after = range_end + 1
        prev_oldest: int | None = None
        while True:
            data = self._client.get_history_candles(
                inst_id=inst_id,
                bar=bar_size,
                after=next_after,
                limit=self._page_size,
            )
            if not data:
                break
            bars = sorted(data, key=lambda b: b.ts)
            oldest = bars[0].ts
            # 停滞保护：与主循环同口径，防止补拉循环无限打转（P2）
            if prev_oldest is not None and oldest >= prev_oldest:
                _log.warning(
                    f"fill page stalled: oldest={oldest} not older than "
                    f"previous={prev_oldest}, stop filling"
                )
                break
            prev_oldest = oldest
            # 只保留 [range_start, range_end] 内的
            in_range = [
                b for b in bars
                if range_start <= b.ts <= range_end
            ]
            all_bars.extend(in_range)
            # 推进到本页最旧一根的 -1
            next_after = oldest
            # oldest <= range_start 说明已覆盖到起点
            if oldest <= range_start:
                break
            if len(bars) < self._page_size:
                break
        # 去重（可能多次翻页有重叠）
        seen: set[int] = set()
        unique: list[Bar] = []
        for b in sorted(all_bars, key=lambda x: x.ts):
            if b.ts not in seen:
                unique.append(b)
                seen.add(b.ts)
        return unique

    # ------------------------------------------------------------------
    # 内部：校验 + 补拉
    # ------------------------------------------------------------------

    def _validate_and_fill(
        self,
        inst_id: str,
        bar_size: str,
        start_ts: int,
        end_ts: int,
        bar_size_ms: int,
    ) -> None:
        """校验已落盘数据，对每个 missing_range 翻页补拉，最多 max_attempts 次。"""
        for attempt in range(self._max_attempts):
            bars = self._storage.load_bars(inst_id, bar_size, start_ts, end_ts)
            report = self._validator.validate(bars, expected_start_ts=start_ts)
            _log.info(
                f"validate attempt={attempt + 1}/{self._max_attempts} "
                f"{report.summary()}"
            )
            # 校验器层错误（如整段为空）必须显式暴露：原先只藏在 info 级
            # summary 里，失败被伪装成「净增 0」无人察觉（P1）。提到 WARNING
            # 后会进 monitor 的日志告警体系。
            for err in report.errors:
                _log.warning(f"validate error: {err}")
            if not report.missing:
                return
            # 对每个缺失段补拉
            total_filled = 0
            for m in report.missing:
                range_start = m.prev_ts + bar_size_ms
                range_end = m.next_ts - bar_size_ms
                if range_start > range_end:
                    continue
                _log.info(
                    f"fill missing range=[{range_start}, {range_end}] "
                    f"missing_count={m.missing_count}"
                )
                filled = self._fetch_page_range(
                    inst_id, bar_size, range_start, range_end
                )
                if filled:
                    saved = self._storage.save_bars(filled)
                    total_filled += saved
                    _log.info(f"filled {saved} bars in gap")
            if total_filled == 0:
                # 没补到任何数据，OKX 可能本身就没这些 K线（如交易所维护期间）
                _log.warning(
                    f"no bars filled in attempt {attempt + 1}, "
                    f"stop filling (data may be genuinely missing on OKX)"
                )
                return

    # ------------------------------------------------------------------
    # 内部：断点
    # ------------------------------------------------------------------

    def _ckpt_path(self, inst_id: str, bar_size: str) -> Path:
        # inst_id 含 "-"，bar_size 是字符串，组合作为文件名
        return self._ckpt_dir / f"{inst_id}_{bar_size}.json"

    def _load_checkpoint(self, inst_id: str, bar_size: str) -> Checkpoint:
        return Checkpoint.load_or_default(self._ckpt_path(inst_id, bar_size))

    def _save_checkpoint(
        self, inst_id: str, bar_size: str, last_ts: int
    ) -> None:
        Checkpoint(last_ts=last_ts).save(self._ckpt_path(inst_id, bar_size))

    # ------------------------------------------------------------------
    # 内部：工具
    # ------------------------------------------------------------------

    @staticmethod
    def _bar_size_ms(bar_size: str) -> int:
        """把 bar_size 字符串转成毫秒（与 DataValidator._bar_size_ms 一致）。"""
        # 复用 validator 的实现，保持单一真实源
        from quant.storage.validator import DataValidator
        # DataValidator._bar_size_ms 是 staticmethod
        return DataValidator._bar_size_ms(bar_size)  # type: ignore[attr-defined]
