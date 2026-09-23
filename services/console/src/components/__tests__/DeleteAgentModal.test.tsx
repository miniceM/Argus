import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { DeleteAgentModal } from "../../features/agents/DeleteAgentModal";
import { api } from "../../api/client";

vi.mock("../../api/client", () => ({
  api: {
    DELETE: vi.fn(),
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

  const renderModal = (agent: { id: string; name: string; version_count?: number } | null, isOpen = true) => {
    return render(
      <QueryClientProvider client={queryClient}>
        <DeleteAgentModal agent={agent} isOpen={isOpen} onClose={vi.fn()} onSuccess={vi.fn()} />
      </QueryClientProvider>
    );
  };

  it("renders simple deletion prompt for agent with 0 versions", async () => {
    (api.DELETE as any).mockResolvedValue({ data: { id: "test-agent", deleted: true } });

    renderModal({
      id: "test-agent",
      name: "测试 Agent",
      version_count: 0,
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

  it("requires exact full name match to enable force delete button for agent with versions", async () => {
    (api.DELETE as any).mockResolvedValue({ data: { id: "banking-agent", deleted: true, launches_deleted: 2 } });

    renderModal({
      id: "banking-agent",
      name: "银行客服 Agent",
      version_count: 2,
    });

    expect(screen.getByText("高危：强制删除 Agent")).toBeInTheDocument();
    expect(screen.getByText(/Langfuse 中的 Dataset \/ Trace 记录不会被删除/)).toBeInTheDocument();

    const submitBtn = screen.getByRole("button", { name: "确认强制删除" });
    // Button must be disabled before exact name match
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

    // Click submit triggers force delete
    fireEvent.click(submitBtn);

    await waitFor(() => {
      expect(api.DELETE).toHaveBeenCalledWith("/api/v1/agents", {
        params: {
          query: {
            id: "banking-agent",
            force: true,
          },
        },
      });
    });
  });

  it("switches to force deletion mode if normal deletion fails with 409 conflict", async () => {
    (api.DELETE as any).mockResolvedValueOnce({
      error: {
        detail: "无法删除 Agent 'demo-agent'：存在 1 条关联的评测记录 (Experiment Launches)。为防止误删历史评测数据，如确认清理，请开启强制删除并确认 Agent 全称。",
      },
    });

    renderModal({
      id: "demo-agent",
      name: "演示 Agent",
      version_count: 0,
    });

    const deleteBtn = screen.getByRole("button", { name: "确认删除" });
    fireEvent.click(deleteBtn);

    // After 409 error, modal must switch to force confirmation requiring full name
    await waitFor(() => {
      expect(screen.getByText("高危：强制删除 Agent")).toBeInTheDocument();
      expect(screen.getByPlaceholderText("请输入 演示 Agent")).toBeInTheDocument();
    });

    const forceBtn = screen.getByRole("button", { name: "确认强制删除" });
    expect(forceBtn).toBeDisabled();
  });
});
