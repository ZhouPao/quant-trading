import { useEffect, useRef, useState } from "react";
import { parsePeriods, type ChartSettings } from "./types";

type Props = { settings: ChartSettings; onChange: (next: ChartSettings) => void };

const CHIPS: { key: keyof ChartSettings; label: string }[] = [
  { key: "ma", label: "MA" },
  { key: "boll", label: "BOLL" },
  { key: "supertrend", label: "SuperTrend" },
  { key: "sr", label: "撑压线" },
  { key: "vol", label: "VOL" },
  { key: "macd", label: "MACD" },
  { key: "kdj", label: "KDJ" },
];

export function IndicatorBar({ settings, onChange }: Props) {
  const toggle = (key: keyof ChartSettings) => {
    const current = settings[key];
    onChange({ ...settings, [key]: { ...current, enabled: !current.enabled } });
  };

  return (
    <div className="ind-bar">
      <div className="ind-chips">
        {CHIPS.map((chip) => (
          <button
            key={chip.key}
            type="button"
            className={settings[chip.key].enabled ? "ind-chip ind-on" : "ind-chip"}
            onClick={() => toggle(chip.key)}
            aria-pressed={settings[chip.key].enabled}
          >
            {chip.label}
          </button>
        ))}
      </div>

      <div className="ind-params">
        {settings.ma.enabled && (
          <ListInput
            label="MA 周期"
            value={settings.ma.periods}
            onCommit={(periods) => onChange({ ...settings, ma: { ...settings.ma, periods } })}
          />
        )}

        {settings.boll.enabled && (
          <>
            <NumberInput
              label="BOLL 周期"
              value={settings.boll.period}
              min={2}
              max={500}
              onCommit={(period) => onChange({ ...settings, boll: { ...settings.boll, period } })}
            />
            <NumberInput
              label="倍数"
              value={settings.boll.mult}
              min={0.5}
              max={10}
              step={0.1}
              onCommit={(mult) => onChange({ ...settings, boll: { ...settings.boll, mult } })}
            />
          </>
        )}

        {settings.supertrend.enabled && (
          <>
            <NumberInput
              label="ST 周期"
              value={settings.supertrend.period}
              min={2}
              max={500}
              onCommit={(period) =>
                onChange({ ...settings, supertrend: { ...settings.supertrend, period } })
              }
            />
            <NumberInput
              label="倍数"
              value={settings.supertrend.mult}
              min={0.5}
              max={10}
              step={0.1}
              onCommit={(mult) =>
                onChange({ ...settings, supertrend: { ...settings.supertrend, mult } })
              }
            />
          </>
        )}

        {settings.sr.enabled && (
          <>
            <NumberInput
              label="左右根数"
              value={settings.sr.leftRight}
              min={1}
              max={20}
              onCommit={(leftRight) =>
                onChange({ ...settings, sr: { ...settings.sr, leftRight } })
              }
            />
            <NumberInput
              label="聚类 %"
              value={settings.sr.tolerancePct}
              min={0.01}
              max={20}
              step={0.1}
              onCommit={(tolerancePct) =>
                onChange({ ...settings, sr: { ...settings.sr, tolerancePct } })
              }
            />
            <NumberInput
              label="最多条数"
              value={settings.sr.maxLevels}
              min={1}
              max={10}
              onCommit={(maxLevels) => onChange({ ...settings, sr: { ...settings.sr, maxLevels } })}
            />
          </>
        )}

        {settings.vol.enabled && (
          <ListInput
            label="VOL 均线"
            value={settings.vol.maPeriods}
            onCommit={(maPeriods) => onChange({ ...settings, vol: { ...settings.vol, maPeriods } })}
          />
        )}

        {settings.macd.enabled && (
          <>
            <NumberInput
              label="快线"
              value={settings.macd.fast}
              min={2}
              max={200}
              onCommit={(fast) => onChange({ ...settings, macd: { ...settings.macd, fast } })}
            />
            <NumberInput
              label="慢线"
              value={settings.macd.slow}
              min={3}
              max={300}
              onCommit={(slow) => onChange({ ...settings, macd: { ...settings.macd, slow } })}
            />
            <NumberInput
              label="信号"
              value={settings.macd.signal}
              min={2}
              max={100}
              onCommit={(signal) => onChange({ ...settings, macd: { ...settings.macd, signal } })}
            />
          </>
        )}

        {settings.kdj.enabled && (
          <>
            <NumberInput
              label="N"
              value={settings.kdj.n}
              min={2}
              max={200}
              onCommit={(n) => onChange({ ...settings, kdj: { ...settings.kdj, n } })}
            />
            <NumberInput
              label="K 平滑"
              value={settings.kdj.k}
              min={2}
              max={10}
              onCommit={(k) => onChange({ ...settings, kdj: { ...settings.kdj, k } })}
            />
            <NumberInput
              label="D 平滑"
              value={settings.kdj.d}
              min={2}
              max={10}
              onCommit={(d) => onChange({ ...settings, kdj: { ...settings.kdj, d } })}
            />
          </>
        )}
      </div>
    </div>
  );
}

function NumberInput({
  label,
  value,
  min,
  max,
  step,
  onCommit,
}: {
  label: string;
  value: number;
  min: number;
  max: number;
  step?: number;
  onCommit: (value: number) => void;
}) {
  const [text, setText] = useState(String(value));
  const timer = useRef<number | null>(null);

  // 外部修改（如读取本地设置或重置）时同步输入框
  useEffect(() => {
    if (Number(text) !== value) setText(String(value));
  }, [value]);

  // 卸载时丢弃未提交的防抖（极少发生：编辑中途切页）
  useEffect(
    () => () => {
      if (timer.current !== null) window.clearTimeout(timer.current);
    },
    [],
  );

  const commitSoon = (parsed: number) => {
    if (timer.current !== null) window.clearTimeout(timer.current);
    timer.current = window.setTimeout(() => {
      timer.current = null;
      onCommit(parsed);
    }, 300);
  };

  return (
    <label className="ind-field">
      <span>{label}</span>
      <input
        value={text}
        inputMode="decimal"
        step={step}
        onChange={(event) => {
          const next = event.target.value;
          setText(next);
          const parsed = Number(next);
          if (next.trim() !== "" && Number.isFinite(parsed) && parsed >= min && parsed <= max) {
            commitSoon(parsed);
          }
        }}
        onBlur={() => {
          if (timer.current !== null) {
            window.clearTimeout(timer.current);
            timer.current = null;
          }
          const parsed = Number(text);
          if (
            text.trim() !== "" &&
            Number.isFinite(parsed) &&
            parsed >= min &&
            parsed <= max
          ) {
            if (parsed !== value) onCommit(parsed);
          } else {
            setText(String(value)); // 非法输入回弹到当前生效值
          }
        }}
      />
    </label>
  );
}

function ListInput({
  label,
  value,
  onCommit,
}: {
  label: string;
  value: number[];
  onCommit: (value: number[]) => void;
}) {
  const join = (items: number[]) => items.join(",");
  const [text, setText] = useState(join(value));
  const timer = useRef<number | null>(null);

  useEffect(() => {
    if (join(parsePeriods(text)) !== join(value)) setText(join(value));
  }, [value]);

  useEffect(
    () => () => {
      if (timer.current !== null) window.clearTimeout(timer.current);
    },
    [],
  );

  const commitSoon = (parsed: number[]) => {
    if (timer.current !== null) window.clearTimeout(timer.current);
    timer.current = window.setTimeout(() => {
      timer.current = null;
      onCommit(parsed);
    }, 300);
  };

  return (
    <label className="ind-field ind-field-wide">
      <span>{label}</span>
      <input
        value={text}
        placeholder="如 5,10,20"
        onChange={(event) => {
          const next = event.target.value;
          setText(next);
          const parsed = parsePeriods(next);
          if (parsed.length > 0) commitSoon(parsed);
        }}
        onBlur={() => {
          if (timer.current !== null) {
            window.clearTimeout(timer.current);
            timer.current = null;
          }
          const parsed = parsePeriods(text);
          if (parsed.length > 0) {
            if (join(parsed) !== join(value)) onCommit(parsed);
          } else {
            setText(join(value)); // 非法输入回弹到当前生效值
          }
        }}
      />
    </label>
  );
}
