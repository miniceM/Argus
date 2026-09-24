import { describe, it, expect, vi } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter, Routes, Route } from "react-router-dom";
import { LaunchDetail } from "../../features/launches/LaunchDetail";
import { api } from "../../api/client";

vi.mock("../../api/client", () => ({
  api: {
    GET: vi.fn(),
    POST: vi.fn(),
  },
}));

describe("LaunchDetail Frozen Manifest Structured Audit View", () => {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });

  const mockLaunch = {
    id: "launch-freeze-001",
    name: "Audit Verification Launch",
    status: "SUCCEEDED",
    quality_conclusion: "pass",
    dataset_name: "banking-regression",
    dataset_version: "2026-09-20T00:00:00Z",
    agent_id: "banking-agent",
    agent_version: "v2",
    langfuse_sync_status: "SYNCED",
    langfuse_experiment_url: "http://localhost:3000/runs/123",
    created_at: "2026-09-20T00:00:00Z",
    started_at: "2026-09-20T00:00:01Z",
    completed_at: "2026-09-20T00:00:05Z",
    manifest: {
      schema_version: "1.0",
      dataset: {
        dataset_name: "banking-regression",
        dataset_version: "2026-09-20T00:00:00Z",
        snapshot_digest: "sha256:abcd1234efgh5678",
        items_count: 6,
      },
      agent: {
        id: "banking-agent",
        version: "v2",
        endpoint: "http://127.0.0.1:18082/invoke",
        spec_digest: "sha256:spec9876543210",
      },
      evaluators: [
        { id: "intent_match", version: "1.0.0", scope: "item" },
        { id: "pii_safe", version: "1.0.0", scope: "item" },
      ],
      execution_policy: {
        max_concurrency: 4,
        timeout_seconds: 10,
        max_retries: 2,
      },
      runner: {
        runner_version: "0.1.0",
        mapping_engine_version: "sha256-mapping-engine-v1",
      },
    },
  };

  it("renders all critical frozen snapshot audit fields structured in UI", async () => {
    (api.GET as any).mockImplementation((path: string) => {
      if (path === "/api/v1/experiment-launches/{launch_id}") {
        return Promise.resolve({ data: mockLaunch });
      }
      if (path === "/api/v1/experiment-launches/{launch_id}/items") {
        return Promise.resolve({ data: [] });
      }
      return Promise.resolve({ data: null });
    });

    render(
      <QueryClientProvider client={queryClient}>
        <MemoryRouter initialEntries={["/launches/launch-freeze-001"]}>
          <Routes>
            <Route path="/launches/:launchId" element={<LaunchDetail />} />
          </Routes>
        </MemoryRouter>
      </QueryClientProvider>
    );

    // 1. Schema Version badge
    await waitFor(() => {
      expect(screen.getByTestId("manifest-schema-version")).toHaveTextContent("Schema v1.0");
    });

    // 2. Langfuse Sync Status badge in primary banner
    expect(screen.getByTestId("langfuse-sync-badge")).toHaveTextContent("SYNCED");

    // 3. Dataset Snapshot Digest
    expect(screen.getByTestId("dataset-snapshot-digest")).toHaveTextContent("sha256:abcd1...");

    // 4. Runner Version & Engine
    expect(screen.getByTestId("runner-version")).toHaveTextContent("0.1.0");
    expect(screen.getByText("sha256-mapping-engine-v1")).toBeInTheDocument();

    // 5. Evaluators
    expect(screen.getByText("intent_match")).toBeInTheDocument();
    expect(screen.getByText("pii_safe")).toBeInTheDocument();
  });

  it("renders S2 progress board and action buttons according to allowed_actions", async () => {
    const s2MockLaunch = {
      ...mockLaunch,
      id: "launch-s2-002",
      status: "RUNNING",
      allowed_actions: ["cancel"],
      progress: {
        total: 10,
        pending: 0,
        queued: 2,
        running: 3,
        retry_wait: 1,
        succeeded: 3,
        failed: 1,
        timed_out: 0,
        cancelled: 0,
        completed: 4,
        percentage: 40.0,
        attempts: 5,
        retries: 1,
        allowed_actions: ["cancel"],
      },
    };

    (api.GET as any).mockImplementation((path: string) => {
      if (path === "/api/v1/experiment-launches/{launch_id}" || path === "/api/v1/experiment-launches") {
        return Promise.resolve({ data: s2MockLaunch });
      }
      if (path === "/api/v1/experiment-launches/{launch_id}/items" || path === "/api/v1/experiment-launch-items") {
        return Promise.resolve({ data: [] });
      }
      return Promise.resolve({ data: null });
    });

    render(
      <QueryClientProvider client={queryClient}>
        <MemoryRouter initialEntries={["/launches/launch-s2-002"]}>
          <Routes>
            <Route path="/launches/:launchId" element={<LaunchDetail />} />
          </Routes>
        </MemoryRouter>
      </QueryClientProvider>
    );

    // Verify progress board
    await waitFor(() => {
      expect(screen.getByText("实时执行进度看板")).toBeInTheDocument();
      expect(screen.getByText("(40%)")).toBeInTheDocument();
      expect(screen.getByText("总调用: 5 次")).toBeInTheDocument();
      expect(screen.getByText("重试: 1 次")).toBeInTheDocument();
    });

    // Verify Cancel button is rendered
    expect(screen.getByText("取消评测 (Cancel)")).toBeInTheDocument();
    // Verify Run button is NOT rendered
    expect(screen.queryByText("启动评测 (Run)")).not.toBeInTheDocument();
  });

  it("opens retry confirmation modal with force replay checkbox when retry_failed is allowed", async () => {
    const s2FailedLaunch = {
      ...mockLaunch,
      id: "launch-s2-003",
      status: "PARTIAL_FAILED",
      allowed_actions: ["retry_failed"],
      progress: {
        total: 10,
        pending: 0,
        queued: 0,
        running: 0,
        retry_wait: 0,
        succeeded: 8,
        failed: 2,
        timed_out: 0,
        cancelled: 0,
        completed: 10,
        percentage: 100.0,
        attempts: 12,
        retries: 2,
        allowed_actions: ["retry_failed"],
      },
    };

    (api.GET as any).mockImplementation((path: string) => {
      if (path === "/api/v1/experiment-launches/{launch_id}" || path === "/api/v1/experiment-launches") {
        return Promise.resolve({ data: s2FailedLaunch });
      }
      if (path === "/api/v1/experiment-launches/{launch_id}/items" || path === "/api/v1/experiment-launch-items") {
        return Promise.resolve({ data: [] });
      }
      return Promise.resolve({ data: null });
    });

    const { fireEvent } = await import("@testing-library/react");

    render(
      <QueryClientProvider client={queryClient}>
        <MemoryRouter initialEntries={["/launches/launch-s2-003"]}>
          <Routes>
            <Route path="/launches/:launchId" element={<LaunchDetail />} />
          </Routes>
        </MemoryRouter>
      </QueryClientProvider>
    );

    await waitFor(() => {
      expect(screen.getByText("重试失败用例 (Retry Failed)")).toBeInTheDocument();
    });

    fireEvent.click(screen.getByText("重试失败用例 (Retry Failed)"));

    expect(screen.getByText("强制重试非幂等可能已发送用例 (Force Replay)")).toBeInTheDocument();
    expect(screen.getByText("确认重新调度")).toBeInTheDocument();
  });

  it("never renders a different Launch when the detail response ID does not match the route", async () => {
    const requestedId = "launch-requested-001";
    const wrongLaunch = { ...mockLaunch, id: "launch-wrong-001", name: "Wrong Launch" };

    (api.GET as any).mockImplementation((path: string) => {
      if (path === "/api/v1/experiment-launches" || path === "/api/v1/experiment-launches/{launch_id}") {
        return Promise.resolve({ data: [wrongLaunch] });
      }
      return Promise.resolve({ data: [] });
    });

    render(
      <QueryClientProvider client={queryClient}>
        <MemoryRouter initialEntries={[`/launches/${requestedId}`]}>
          <Routes>
            <Route path="/launches/:launchId" element={<LaunchDetail />} />
          </Routes>
        </MemoryRouter>
      </QueryClientProvider>
    );

    expect(await screen.findByTestId("error-state")).toBeInTheDocument();
    expect(screen.queryByRole("heading", { name: wrongLaunch.id })).not.toBeInTheDocument();
    expect(screen.queryByText("Wrong Launch")).not.toBeInTheDocument();
  });

  it("shows Items API failures instead of presenting them as an empty result", async () => {
    const requestedId = "launch-items-error-001";
    const launch = { ...mockLaunch, id: requestedId };

    (api.GET as any).mockImplementation((path: string) => {
      if (path === "/api/v1/experiment-launches" || path === "/api/v1/experiment-launches/{launch_id}") {
        return Promise.resolve({ data: launch });
      }
      if (
        path === "/api/v1/experiment-launch-items" ||
        path === "/api/v1/experiment-launches/{launch_id}/items"
      ) {
        return Promise.resolve({
          error: { detail: "Items service unavailable" },
          response: { status: 503 },
        });
      }
      return Promise.resolve({ data: null });
    });

    render(
      <QueryClientProvider client={queryClient}>
        <MemoryRouter initialEntries={[`/launches/${requestedId}`]}>
          <Routes>
            <Route path="/launches/:launchId" element={<LaunchDetail />} />
          </Routes>
        </MemoryRouter>
      </QueryClientProvider>
    );

    expect(await screen.findByText("Items service unavailable")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "重新加载" })).toBeInTheDocument();
    expect(screen.getByText("暂不可用")).toBeInTheDocument();
  });

  it("does not treat a Launch permission error as a missing record", async () => {
    const requestedId = "launch-forbidden-001";
    (api.GET as any).mockResolvedValue({
      error: { detail: "You do not have permission to view this Launch" },
      response: { status: 403 },
    });

    render(
      <QueryClientProvider client={queryClient}>
        <MemoryRouter initialEntries={[`/launches/${requestedId}`]}>
          <Routes>
            <Route path="/launches/:launchId" element={<LaunchDetail />} />
          </Routes>
        </MemoryRouter>
      </QueryClientProvider>
    );

    expect(await screen.findByText("You do not have permission to view this Launch")).toBeInTheDocument();
    expect(screen.queryByText("不存在或已删除")).not.toBeInTheDocument();
    expect(screen.getByRole("link", { name: "返回评测列表" })).toBeInTheDocument();
  });

  it("rejects Items that belong to another Launch", async () => {
    const requestedId = "launch-items-mismatch-001";
    const launch = { ...mockLaunch, id: requestedId };

    (api.GET as any).mockImplementation((path: string) => {
      if (path === "/api/v1/experiment-launches/{launch_id}") {
        return Promise.resolve({ data: launch });
      }
      if (path === "/api/v1/experiment-launches/{launch_id}/items") {
        return Promise.resolve({ data: [{ id: "item-from-other-launch", launch_id: "another-launch" }] });
      }
      return Promise.resolve({ data: null });
    });

    render(
      <QueryClientProvider client={queryClient}>
        <MemoryRouter initialEntries={[`/launches/${requestedId}`]}>
          <Routes>
            <Route path="/launches/:launchId" element={<LaunchDetail />} />
          </Routes>
        </MemoryRouter>
      </QueryClientProvider>
    );

    expect(await screen.findByText("用例明细归属的 Launch 与当前页面不一致，请重新加载")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "重新加载" })).toBeInTheDocument();
  });
});
