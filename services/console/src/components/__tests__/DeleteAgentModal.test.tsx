import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { DeleteAgentModal } from "../../features/agents/DeleteAgentModal";
import { api } from "../../api/client";

vi.mock("../../api/client", () => ({
  api: {
    DELETE: vi.fn(),
    POST: vi.fn(),
  },
}));

describe("DeleteAgentModal UX and Strong Name Verification Flow", () => {
  let queryClient: QueryClient;

  beforeEach(() => {
    queryClient = new QueryClient({
      defaultOptions: { queries: { retry: false } },
    });
    vi.clearAllMocks();
  });

  const renderModal = (
    agent: {
      id: string;
      name: string;
      version_count?: number;
      launch_count?: number;
      active_launch_count?: number;
    } | null,
    onClose = vi.fn(),
    isOpen = true
  ) => {
    return render(
      <QueryClientProvider client={queryClient}>
        <DeleteAgentModal agent={agent} isOpen={isOpen} onClose={onClose} onSuccess={vi.fn()} />
      </QueryClientProvider>
    );
  };

  it("renders simple deletion prompt for agent with versions but 0 launches", async () => {
    (api.DELETE as any).mockResolvedValue({ data: { id: "test-agent", deleted: true } });

    renderModal({
      id: "test-agent",
      name: "测试 Agent",
      version_count: 2,
      launch_count: 0,
      active_launch_count: 0,
    });

    expect(screen.getByText("删除 Agent")).toBeInTheDocument();
    expect(screen.getByText(/确认删除 Agent/)).toBeInTheDocument();

    const deleteBtn = screen.getByRole("button", { name: "确认删除" });
    expect(deleteBtn).toBeEnabled();

    fireEvent.click(deleteBtn);

    await waitFor(() => {
      expect(api.DELETE).toHaveBeenCalledWith("/api/v1/agents", {
        params: {
          query: {
            id: "test-agent",
            force: false,
          },
        },
      });
    });
  });

  it("requires exact full name match and calls purge endpoint for agent with launches", async () => {
    (api.POST as any).mockResolvedValue({ data: { id: "banking-agent", deleted: true, launches_deleted: 2 } });

    renderModal({
      id: "banking-agent",
      name: "银行客服 Agent",
      version_count: 2,
      launch_count: 2,
      active_launch_count: 0,
    });

    expect(screen.getByText("高危：强制清理 Agent 及评测记录")).toBeInTheDocument();
    expect(screen.getByText(/Langfuse 中的 Dataset \/ Trace 记录不会被删除/)).toBeInTheDocument();

    const submitBtn = screen.getByRole("button", { name: "确认强制清理" });
    expect(submitBtn).toBeDisabled();

    const input = screen.getByPlaceholderText("请输入 银行客服 Agent");

    // Partial or mismatched input keeps button disabled
    fireEvent.change(input, { target: { value: "银行客服" } });
    expect(submitBtn).toBeDisabled();

    fireEvent.change(input, { target: { value: "银行客服 agent" } });
    expect(submitBtn).toBeDisabled();

    // Exact match activates button
    fireEvent.change(input, { target: { value: "银行客服 Agent" } });
    expect(submitBtn).toBeEnabled();

    // Click submit triggers purge API
    fireEvent.click(submitBtn);

    await waitFor(() => {
      expect(api.POST).toHaveBeenCalledWith("/api/v1/agents/purge", {
        body: {
          agent_id: "banking-agent",
          confirm_name: "银行客服 Agent",
        },
      });
    });
  });

  it("blocks deletion when active launches exist", async () => {
    renderModal({
      id: "active-agent",
      name: "活跃任务 Agent",
      version_count: 1,
      launch_count: 3,
      active_launch_count: 1,
    });

    expect(screen.getByText("禁止删除：存在活跃评测任务")).toBeInTheDocument();
    expect(
      screen.getByText((_, element) =>
        element?.tagName === "P" && element.textContent?.includes("当前有 1 条尚未结束的评测记录") === true
      )
    ).toBeInTheDocument();
    expect(screen.getByText(/待执行、排队中、运行中、取消中等尚未结束状态/)).toBeInTheDocument();
    const submitBtn = screen.getByRole("button", { name: "确认强制清理" });
    expect(submitBtn).toBeDisabled();

    // Even if name matches, submit remains disabled while active launches exist
    const input = screen.getByPlaceholderText("请输入 活跃任务 Agent");
    fireEvent.change(input, { target: { value: "活跃任务 Agent" } });
    expect(submitBtn).toBeDisabled();
  });

  it("switches to force purge mode if normal deletion fails with AGENT_HAS_LAUNCHES", async () => {
    (api.DELETE as any).mockResolvedValueOnce({
      error: {
        code: "AGENT_HAS_LAUNCHES",
        detail: "无法删除 Agent 'demo-agent'：存在 1 条关联的评测记录",
      },
    });

    renderModal({
      id: "demo-agent",
      name: "演示 Agent",
      version_count: 0,
      launch_count: 0,
      active_launch_count: 0,
    });

    const deleteBtn = screen.getByRole("button", { name: "确认删除" });
    fireEvent.click(deleteBtn);

    await waitFor(() => {
      expect(screen.getByText("高危：强制清理 Agent 及评测记录")).toBeInTheDocument();
      expect(screen.getByPlaceholderText("请输入 演示 Agent")).toBeInTheDocument();
    });

    const forceBtn = screen.getByRole("button", { name: "确认强制清理" });
    expect(forceBtn).toBeDisabled();
  });

  it("supports ESC key closing and includes accessible dialog attributes", async () => {
    const handleClose = vi.fn();
    renderModal(
      {
        id: "esc-agent",
        name: "ESC Agent",
      },
      handleClose
    );

    const dialog = screen.getByRole("dialog");
    expect(dialog).toHaveAttribute("aria-modal", "true");
    expect(dialog).toHaveAttribute("aria-labelledby", "delete-agent-title");

    fireEvent.keyDown(window, { key: "Escape" });
    expect(handleClose).toHaveBeenCalledTimes(1);
  });

  it("does not close on ESC key when deletion mutation is pending", async () => {
    let resolveDelete: (val: any) => void;
    const deletePromise = new Promise((resolve) => {
      resolveDelete = resolve;
    });
    (api.DELETE as any).mockReturnValue(deletePromise);

    const handleClose = vi.fn();
    renderModal(
      {
        id: "pending-agent",
        name: "Pending Agent",
        launch_count: 0,
      },
      handleClose
    );

    // Trigger delete
    const deleteBtn = screen.getByRole("button", { name: "确认删除" });
    fireEvent.click(deleteBtn);

    // Wait until mutation is pending (button says 正在删除...)
    await waitFor(() => {
      expect(screen.getByText("正在删除...")).toBeInTheDocument();
    });

    // ESC pressed during pending must be ignored
    fireEvent.keyDown(window, { key: "Escape" });
    expect(handleClose).not.toHaveBeenCalled();

    // Resolve mutation
    resolveDelete!({ data: { id: "pending-agent", deleted: true } });
    await waitFor(() => {
      expect(handleClose).toHaveBeenCalledTimes(1);
    });
  });

  it("updates launch counts and blocks active tasks when 409 error contains counts", async () => {
    (api.DELETE as any).mockResolvedValueOnce({
      error: {
        code: "AGENT_HAS_LAUNCHES",
        detail: "无法删除 Agent：存在历史任务与活跃任务",
        launch_count: 5,
        active_launch_count: 2,
      },
    });

    renderModal({
      id: "stale-agent",
      name: "Stale Agent",
      launch_count: 0,
      active_launch_count: 0,
    });

    // Initially says no associated launches
    expect(screen.getByText(/当前无关联评测记录/)).toBeInTheDocument();

    const deleteBtn = screen.getByRole("button", { name: "确认删除" });
    fireEvent.click(deleteBtn);

    // After 409 with counts, UI must update to show 5 launches and active warning for 2 tasks
    await waitFor(() => {
      expect(screen.getByText("高危：强制清理 Agent 及评测记录")).toBeInTheDocument();
      expect(screen.getByText("注意：该 Agent 包含关联评测记录")).toBeInTheDocument();
      expect(screen.getByText("5")).toBeInTheDocument();
      expect(screen.getByText("禁止删除：存在活跃评测任务")).toBeInTheDocument();
      expect(screen.getByText("2")).toBeInTheDocument();
    });

    // Submit button should be disabled because active_launch_count is 2
    const forceBtn = screen.getByRole("button", { name: "确认强制清理" });
    expect(forceBtn).toBeDisabled();
  });
});
