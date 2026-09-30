import { describe, expect, it } from "vitest";
import { render, screen, within } from "@testing-library/react";
import { LangfuseSyncPanel } from "../langfuseSyncStatus";

const scope = (over: Record<string, unknown> = {}) => ({
  status: "SYNCED",
  reason: null,
  task_count: 3,
  failed_count: 0,
  pending_count: 0,
  ...over,
});

const sync = (over: Record<string, any> = {}) => ({
  overall: "SYNCED",
  item_trace: scope(),
  run_score: scope(),
  ...over,
});

describe("LangfuseSyncPanel (Issue #87)", () => {
  it("shows both sync scopes when everything is synced", () => {
    render(<LangfuseSyncPanel sync={sync()} />);
    expect(screen.getByTestId("langfuse-sync-overall")).toHaveTextContent("全部已同步");
    expect(screen.getByTestId("langfuse-sync-item-trace")).toHaveTextContent("已同步");
    expect(screen.getByTestId("langfuse-sync-run-score")).toHaveTextContent("已同步");
  });

  it("never claims everything is synced when the run score scope failed", () => {
    render(
      <LangfuseSyncPanel
        sync={sync({
          overall: "FAILED",
          item_trace: scope(),
          run_score: scope({ status: "FAILED", reason: "Run score publish failed", failed_count: 1 }),
        })}
      />,
    );
    const panel = screen.getByTestId("langfuse-sync-panel");
    expect(within(panel).getByTestId("langfuse-sync-overall")).toHaveTextContent("同步失败");
    expect(within(panel).getByTestId("langfuse-sync-overall")).not.toHaveTextContent("全部已同步");
    // The scope that is fine still reads as fine, so the user knows where to look.
    expect(within(panel).getByTestId("langfuse-sync-item-trace")).toHaveTextContent("已同步");
    expect(within(panel).getByTestId("langfuse-sync-run-score")).toHaveTextContent("同步失败");
    expect(within(panel).getByTestId("langfuse-sync-run-score")).toHaveTextContent("Run score publish failed");
  });

  it("distinguishes an exhausted retry budget from a transient failure", () => {
    render(
      <LangfuseSyncPanel
        sync={sync({
          overall: "RETRY_EXHAUSTED",
          item_trace: scope({ status: "RETRY_EXHAUSTED", reason: "Langfuse timeout", failed_count: 2 }),
          run_score: scope({ status: "RETRY_EXHAUSTED", reason: "Langfuse timeout", failed_count: 1 }),
        })}
      />,
    );
    expect(screen.getByTestId("langfuse-sync-overall")).toHaveTextContent("同步重试已耗尽");
    expect(screen.getByTestId("langfuse-sync-item-trace")).toHaveTextContent("重试已耗尽");
  });

  it("explains a not-applicable scope instead of waiting forever", () => {
    render(
      <LangfuseSyncPanel
        sync={sync({
          overall: "SYNCED",
          run_score: scope({ status: "NOT_APPLICABLE", reason: "没有需要同步的任务。", task_count: 0 }),
        })}
      />,
    );
    expect(screen.getByTestId("langfuse-sync-overall")).toHaveTextContent("全部已同步");
    expect(screen.getByTestId("langfuse-sync-run-score")).toHaveTextContent("不适用");
    expect(screen.getByTestId("langfuse-sync-run-score")).toHaveTextContent("没有需要同步的任务。");
  });

  it("states that sync never changes the Argus quality conclusion", () => {
    render(<LangfuseSyncPanel sync={sync()} />);
    expect(screen.getByTestId("langfuse-sync-disclaimer")).toHaveTextContent("不会改变");
    expect(screen.getByTestId("langfuse-sync-disclaimer")).toHaveTextContent("单向");
  });

  it("renders nothing when the sync state is unavailable", () => {
    const { container } = render(<LangfuseSyncPanel sync={null} />);
    expect(container).toBeEmptyDOMElement();
  });
});
