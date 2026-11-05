import { useCallback, useState, type ReactNode } from "react";

/**
 * M3 策略研究页的分节容器。
 *
 * 原先 6 个区块全用 `.param-panel` + `.card-note` 平铺在一张卡片里，
 * 没有标题层级、全部同时展开。这里把「标题 + 折叠 + 摘要 + 动作」抽成
 * 独立组件，折叠状态按节持久化；内容仍由调用方原样渲染。
 */
const STORAGE_PREFIX = "quant.engine.section.";

export const ENGINE_SECTIONS = {
  params: "参数调整",
  history: "参数变更历史",
  bindings: "启停矩阵",
  evaluations: "最近评估",
  runs: "最近回测",
  compare: "参数版本对比",
} as const;

export type SectionId = keyof typeof ENGINE_SECTIONS;

/** 默认只展开「参数 / 启停」这两个日常操作区，结果类默认收起。 */
export const DEFAULT_OPEN: Record<SectionId, boolean> = {
  params: true,
  history: false,
  bindings: true,
  evaluations: false,
  runs: false,
  compare: false,
};

function readOpen(id: SectionId): boolean {
  try {
    const raw = window.localStorage.getItem(STORAGE_PREFIX + id);
    return raw === null ? DEFAULT_OPEN[id] : raw === "1";
  } catch {
    // 隐私模式下 localStorage 不可读，按默认展开策略处理
    return DEFAULT_OPEN[id];
  }
}

function initialState(): Record<SectionId, boolean> {
  return {
    params: readOpen("params"),
    history: readOpen("history"),
    bindings: readOpen("bindings"),
    evaluations: readOpen("evaluations"),
    runs: readOpen("runs"),
    compare: readOpen("compare"),
  };
}

export function useEngineSections() {
  const [open, setOpen] = useState<Record<SectionId, boolean>>(initialState);

  const toggle = useCallback((id: SectionId) => {
    setOpen((prev) => {
      const next = { ...prev, [id]: !prev[id] };
      try {
        window.localStorage.setItem(STORAGE_PREFIX + id, next[id] ? "1" : "0");
      } catch {
        // 隐私模式下不持久化，本次折叠仍然生效
      }
      return next;
    });
  }, []);

  return { open, toggle };
}

type HeadProps = {
  id: SectionId;
  open: boolean;
  onToggle: (id: SectionId) => void;
  summary?: ReactNode;
  actions?: ReactNode;
};

export function EngineSectionHead({
  id,
  open,
  onToggle,
  summary,
  actions,
}: HeadProps) {
  return (
    <div className="engine-section-head">
      <h3 className="engine-section-title">
        <button
          type="button"
          className="engine-section-toggle"
          aria-expanded={open}
          aria-controls={`engine-section-${id}`}
          onClick={() => onToggle(id)}
        >
          <span className="engine-caret" aria-hidden="true">
            {open ? "−" : "+"}
          </span>
          {ENGINE_SECTIONS[id]}
        </button>
      </h3>
      {summary !== undefined && (
        <span className="engine-section-summary">{summary}</span>
      )}
      {actions !== undefined && (
        <div className="engine-section-actions">{actions}</div>
      )}
    </div>
  );
}
