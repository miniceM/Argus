import { describe, it, expect, vi } from "vitest";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { BrowserRouter } from "react-router-dom";
import { CreateLaunch } from "../../features/launches/CreateLaunch";
import { api } from "../../api/client";

vi.mock("../../api/client", () => ({
  api: {
    GET: vi.fn(),
    POST: vi.fn(),
  },
}));

describe("CreateLaunch Evaluator Scope Invariant", () => {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });

  it("auto-selects only item-scope evaluators and prevents selecting run-scope evaluators", async () => {
    // Mock GET /api/v1/agents
    (api.GET as any).mockImplementation((path: string) => {
      if (path === "/api/v1/agents") {
        return Promise.resolve({
          data: [{ id: "agent-1", name: "Agent One", version_count: 1, latest_version: "v1" }],
        });
      }
      if (path === "/api/v1/agent-versions") {
        return Promise.resolve({
          data: [{ id: "ver-1", agent_id: "agent-1", version: "v1", is_active: true }],
        });
      }
      if (path === "/api/v1/evaluators") {
        return Promise.resolve({
          data: [
            { id: "intent_match", version: "1.0.0", scope: "item", threshold: 1.0, description: "意图匹配" },
            { id: "pii_safe", version: "1.0.0", scope: "item", threshold: 1.0, description: "PII检测" },
            { id: "run_pass_rate", version: "1.0.0", scope: "run", threshold: 1.0, description: "聚合通过率" },
          ],
        });
      }
      return Promise.resolve({ data: [] });
    });

    render(
      <QueryClientProvider client={queryClient}>
        <BrowserRouter>
          <CreateLaunch />
        </BrowserRouter>
      </QueryClientProvider>
    );

    // Wait for evaluators to load
    await waitFor(() => {
      expect(screen.getByText("intent_match")).toBeInTheDocument();
    });

    // Should display selected count as 2 (only the 2 item-scope evaluators, NOT run_pass_rate)
    expect(screen.getByText("已选 2 项")).toBeInTheDocument();

    // Verify run_pass_rate displays the disabled warning text
    expect(screen.getByText("(聚合指标，暂不支持在单次 Launch 中直接运行)")).toBeInTheDocument();

    // Try clicking run_pass_rate
    const runScopeCard = screen.getByText("run_pass_rate").closest("div");
    if (runScopeCard) {
      fireEvent.click(runScopeCard);
    }

    // Selected count should still be 2, run-scope MUST NOT be added
    expect(screen.getByText("已选 2 项")).toBeInTheDocument();
  });
});
