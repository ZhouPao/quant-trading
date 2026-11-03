import { Component, type ReactNode } from "react";

type Props = { children: ReactNode };
type State = { error: Error | null };

/**
 * 全局错误边界：渲染期异常不再卸载整棵 React 树（白屏），
 * 而是落到一个不依赖应用样式的可读兜底页。
 */
export class ErrorBoundary extends Component<Props, State> {
  state: State = { error: null };

  static getDerivedStateFromError(error: Error): State {
    return { error };
  }

  componentDidCatch(error: Error): void {
    // 控制台留痕便于排查；看板暂无日志上报通道，不额外发送
    console.error("UI 渲染异常:", error);
  }

  render(): ReactNode {
    if (!this.state.error) return this.props.children;
    return (
      <div
        style={{
          padding: 40,
          fontFamily: "monospace",
          fontSize: 14,
          color: "#e06c60",
          background: "#0d0f0e",
          minHeight: "100vh",
        }}
      >
        <h1 style={{ fontSize: 18 }}>界面出错了</h1>
        <pre style={{ whiteSpace: "pre-wrap", margin: "16px 0" }}>
          {String(this.state.error.message ?? this.state.error)}
        </pre>
        <button type="button" onClick={() => window.location.reload()}>
          刷新页面
        </button>
      </div>
    );
  }
}
