import { describe, it, expect, vi, afterEach } from "vitest";
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


  it("shows the Langfuse link or the concrete reason even without allowed_actions", async () => {
    const freshClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    (api.GET as any).mockImplementation((path: string) => {
      if (path === "/api/v1/experiment-launches/{launch_id}") {
        return Promise.resolve({
          data: {
            ...mockLaunch,
            status: "COMPLETED",
            allowed_actions: [],
            langfuse_sync_status: "SYNCED",
            langfuse_experiment_id: "r1",
            langfuse_experiment_url: "https://cloud.example.com/project/p1/datasets/d1/runs/r1",
          },
        });
      }
      if (path === "/api/v1/experiment-launches/{launch_id}/items") {
        return Promise.resolve({ data: [] });
      }
      return Promise.resolve({ data: null });
    });

    render(
      <QueryClientProvider client={freshClient}>
        <MemoryRouter initialEntries={["/launches/launch-freeze-001"]}>
          <Routes>
            <Route path="/launches/:launchId" element={<LaunchDetail />} />
          </Routes>
        </MemoryRouter>
      </QueryClientProvider>
    );

    // A terminal launch with no business actions still exposes the external link.
    const link = await screen.findByRole("link", { name: "在 Langfuse 中查看" });
    expect(link).toHaveAttribute(
      "href",
      "https://cloud.example.com/project/p1/datasets/d1/runs/r1"
    );
    expect(link).toHaveAttribute("target", "_blank");
    expect(link).toHaveAttribute("rel", "noopener noreferrer");
    expect(screen.getByRole("button", { name: "刷新" })).toBeInTheDocument();
  });

  it.each([
    [
      "a synced launch whose link is not backfilled yet",
      {
        status: "COMPLETED",
        allowed_actions: [],
        langfuse_sync_status: "SYNCED",
        langfuse_experiment_id: "r1",
        langfuse_experiment_url: null,
      },
      "已同步，链接暂不可用",
    ],
    [
      "a dangerous stored URL",
      {
        status: "COMPLETED",
        allowed_actions: [],
        langfuse_sync_status: "SYNCED",
        langfuse_experiment_id: "r1",
        langfuse_experiment_url: "javascript:alert(1)",
      },
      "Langfuse 地址无效",
    ],
    [
      "a launch that has not started",
      {
        status: "PENDING",
        allowed_actions: [],
        langfuse_sync_status: "PENDING",
        langfuse_experiment_url: null,
      },
      "尚未创建 Langfuse Run",
    ],
    [
      "a seed dataset without a remote run",
      {
        status: "COMPLETED",
        allowed_actions: [],
        langfuse_sync_status: "SYNCED",
        langfuse_experiment_url: null,
        manifest: { dataset: { source: "seed" } },
      },
      "未创建 Langfuse Run",
    ],
  ])("degrades gracefully for %s", async (_name, launch, expected) => {
    const freshClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    (api.GET as any).mockImplementation((path: string) => {
      if (path === "/api/v1/experiment-launches/{launch_id}") {
        return Promise.resolve({ data: { ...mockLaunch, ...(launch as Record<string, unknown>) } });
      }
      if (path === "/api/v1/experiment-launches/{launch_id}/items") {
        return Promise.resolve({ data: [] });
      }
      return Promise.resolve({ data: null });
    });

    render(
      <QueryClientProvider client={freshClient}>
        <MemoryRouter initialEntries={["/launches/launch-freeze-001"]}>
          <Routes>
            <Route path="/launches/:launchId" element={<LaunchDetail />} />
          </Routes>
        </MemoryRouter>
      </QueryClientProvider>
    );

    expect(await screen.findByTestId("langfuse-link-reason")).toHaveTextContent(expected);
    expect(screen.queryByRole("link", { name: "在 Langfuse 中查看" })).not.toBeInTheDocument();
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

  it("accounts for every item in the progress meter, including queued ones", async () => {
    // The meter previously drew only pass/fail/timeout/running/retry/cancelled,
    // so a launch whose items were all queued rendered an empty bar next to a
    // grid reading "queued: 6". The bar must cover the full item set.
    const queuedLaunch = {
      ...mockLaunch,
      id: "launch-bar-004",
      status: "PENDING",
      progress: {
        total: 6,
        pending: 6,
        queued: 0,
        running: 0,
        retry_wait: 0,
        succeeded: 0,
        failed: 0,
        timed_out: 0,
        cancelled: 0,
        completed: 0,
        percentage: 0,
        attempts: 0,
        retries: 0,
        allowed_actions: ["run"],
      },
    };

    (api.GET as any).mockImplementation((path: string) => {
      if (path.includes("items")) return Promise.resolve({ data: [] });
      if (path.includes("/api/v1/experiment-launches")) {
        return Promise.resolve({ data: queuedLaunch });
      }
      return Promise.resolve({ data: null });
    });

    render(
      <QueryClientProvider client={queryClient}>
        <MemoryRouter initialEntries={["/launches/launch-bar-004"]}>
          <Routes>
            <Route path="/launches/:launchId" element={<LaunchDetail />} />
          </Routes>
        </MemoryRouter>
      </QueryClientProvider>
    );

    await waitFor(() => {
      expect(screen.getByText("实时执行进度看板")).toBeInTheDocument();
    });

    const meter = document.querySelector('[data-testid="progress-meter"]');
    expect(meter, "progress meter must be exposed for verification").not.toBeNull();

    const total = queuedLaunch.progress.total;
    const segments = Array.from(meter!.querySelectorAll("[data-segment]"));
    const covered = segments.reduce((sum, el) => {
      const share = Number(el.getAttribute("data-share"));
      return sum + (Number.isFinite(share) ? share : 0);
    }, 0);

    expect(covered).toBe(total);
    expect(segments.some((el) => el.getAttribute("data-segment") === "queued")).toBe(true);
  });
});

describe("Issue #45 quality pass rate wording", () => {
  const buildLaunch = (id: string, overrides: Record<string, unknown> = {}) => ({
    id,
    name: "Quality Wording Launch",
    status: "COMPLETED",
    quality_conclusion: "fail",
    dataset_name: "banking-regression",
    dataset_version: "2026-09-20T00:00:00Z",
    agent_id: "banking-agent",
    agent_version: "v2",
    langfuse_sync_status: "SYNCED",
    created_at: "2026-09-20T00:00:00Z",
    started_at: "2026-09-20T00:00:01Z",
    completed_at: "2026-09-20T00:00:05Z",
    manifest: { schema_version: "1.0", dataset: { items_count: 6 } },
    progress: {
      total: 6,
      pending: 0,
      queued: 0,
      running: 0,
      retry_wait: 0,
      succeeded: 6,
      failed: 0,
      timed_out: 0,
      cancelled: 0,
      completed: 6,
      percentage: 100,
      attempts: 6,
      retries: 0,
      allowed_actions: [],
    },
    ...overrides,
  });

  const buildItems = (launchId: string, specs: Array<Record<string, unknown>>) =>
    specs.map((spec, index) => ({
      id: `item-${index}`,
      launch_id: launchId,
      dataset_item_id: `case-${index}`,
      execution_status: "SUCCEEDED",
      eval_status: "SUCCEEDED",
      quality_conclusion: "UNKNOWN",
      scores: {},
      attempt_count: 1,
      started_at: "2026-09-20T00:00:01Z",
      ...spec,
    }));

  const renderDetail = (launch: Record<string, unknown>, items: Array<Record<string, unknown>>) => {
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    (api.GET as any).mockImplementation((path: string) => {
      if (path.includes("/items")) return Promise.resolve({ data: items });
      // The results endpoints share the launches prefix; answer them first so
      // the comparison panel never receives a Launch payload.
      if (path.includes("/summary") || path.includes("/comparison") || path.includes("/baselines")) {
        return Promise.resolve({ data: null });
      }
      if (path.includes("/api/v1/experiment-launches")) return Promise.resolve({ data: launch });
      return Promise.resolve({ data: null });
    });

    return render(
      <QueryClientProvider client={client}>
        <MemoryRouter initialEntries={[`/launches/${launch.id}`]}>
          <Routes>
            <Route path="/launches/:launchId" element={<LaunchDetail />} />
          </Routes>
        </MemoryRouter>
      </QueryClientProvider>
    );
  };

  afterEach(() => {
    vi.clearAllMocks();
  });

  it("labels the top metric as quality pass rate and keeps it separate from execution results", async () => {
    const launch = buildLaunch("launch-quality-001");
    const items = buildItems(launch.id, [
      { quality_conclusion: "pass" },
      { quality_conclusion: "pass" },
      { quality_conclusion: "fail" },
      { quality_conclusion: "fail" },
      { quality_conclusion: "fail" },
      { quality_conclusion: "fail" },
    ]);

    renderDetail(launch, items);

    const metric = await screen.findByTestId("quality-pass-rate");
    expect(metric).toHaveTextContent("质量通过率 (Quality Pass Rate)");
    expect(metric).toHaveTextContent("统计范围：全部用例");
    expect(metric).toHaveTextContent("2");
    expect(metric).toHaveTextContent("6");
    expect(metric).toHaveTextContent("(33.3%)");

    // Execution and quality conclusions stay separate, never derived from the ratio.
    // The launch header renders above the per-item table, so the first badge of
    // each kind is the launch-level conclusion rather than an item's.
    expect(screen.getAllByTestId("status-badge")[0]).toHaveTextContent("COMPLETED");
    expect(screen.getAllByTestId("quality-badge")[0]).toHaveTextContent("FAIL");

    // Execution progress still reports 6/6 successes.
    await waitFor(() => {
      expect(screen.getByText("实时执行进度看板")).toBeInTheDocument();
    });
    const successCard = document.querySelector('[data-card="pass"]');
    expect(successCard).not.toBeNull();
    expect(successCard).toHaveTextContent("6");

    // The ambiguous legacy label is gone.
    expect(screen.queryByText("用例通过率 (Pass Rate)")).not.toBeInTheDocument();

    // The denominator rules are stated in always-visible help text.
    const help = screen.getByTestId("quality-pass-rate-help");
    expect(help).toHaveTextContent("分母为当前返回的全部用例数");
    expect(help).toHaveTextContent("仍计入分母，但不计入分子");
    expect(help).toHaveTextContent("不代表质量结论为 FAIL");
    expect(help).toHaveTextContent("不是执行成功率");
  });

  it("keeps execution failures, evaluator errors and UNKNOWN items in the top denominator", async () => {
    const launch = buildLaunch("launch-quality-002", {
      progress: {
        total: 4,
        pending: 0,
        queued: 0,
        running: 0,
        retry_wait: 0,
        // Must mirror the four items below: three executed successfully
        // (one of which then failed in the evaluator) and one failed outright.
        succeeded: 3,
        failed: 1,
        timed_out: 0,
        cancelled: 0,
        completed: 4,
        percentage: 100,
        attempts: 4,
        retries: 0,
        allowed_actions: [],
      },
    });
    const items = buildItems(launch.id, [
      { quality_conclusion: "pass" },
      { quality_conclusion: "fail" },
      { execution_status: "FAILED", eval_status: "SKIPPED", quality_conclusion: "unknown" },
      { execution_status: "SUCCEEDED", eval_status: "FAILED", quality_conclusion: "unknown" },
    ]);

    renderDetail(launch, items);

    const metric = await screen.findByTestId("quality-pass-rate");
    // 1 of 4: the two UNKNOWN items stay in the denominator, which is what the
    // top-of-page metric has always meant. The comparable-cohort pass_rate in
    // the comparison report is a different, narrower denominator.
    expect(metric).toHaveTextContent("(25.0%)");
    expect(metric).toHaveTextContent("统计范围：全部用例");
  });

  it("renders 0/N and keeps the quality conclusion UNKNOWN when every item is UNKNOWN", async () => {
    // A zero numerator is not a FAIL verdict. The badge must keep reporting the
    // Launch-level conclusion, and the ratio must stay a real 0/N.
    const launch = buildLaunch("launch-quality-005", {
      quality_conclusion: "unknown",
      progress: {
        total: 3,
        pending: 0,
        queued: 0,
        running: 0,
        retry_wait: 0,
        succeeded: 0,
        failed: 1,
        timed_out: 1,
        cancelled: 1,
        completed: 3,
        percentage: 0,
        attempts: 3,
        retries: 0,
        allowed_actions: [],
      },
    });
    const items = buildItems(launch.id, [
      { execution_status: "FAILED", eval_status: "SKIPPED", quality_conclusion: "unknown" },
      { execution_status: "TIMED_OUT", eval_status: "SKIPPED", quality_conclusion: "unknown" },
      { execution_status: "CANCELLED", eval_status: "SKIPPED", quality_conclusion: "unknown" },
    ]);

    renderDetail(launch, items);

    const metric = await screen.findByTestId("quality-pass-rate");
    expect(metric).toHaveTextContent("(0.0%)");
    expect(metric).toHaveTextContent("统计范围：全部用例");
    // The badge is not rewritten into FAIL just because the numerator is zero.
    expect(screen.getAllByTestId("quality-badge")[0]).toHaveTextContent("UNKNOWN");
  });

  it("counts mixed-case quality conclusions without double counting or dropping any", async () => {
    // The production comparison lowercases quality_conclusion. Uppercase API
    // values must therefore land in the numerator exactly once each.
    const launch = buildLaunch("launch-quality-006", {
      progress: {
        total: 4,
        pending: 0,
        queued: 0,
        running: 0,
        retry_wait: 0,
        succeeded: 4,
        failed: 0,
        timed_out: 0,
        cancelled: 0,
        completed: 4,
        percentage: 100,
        attempts: 4,
        retries: 0,
        allowed_actions: [],
      },
    });
    const items = buildItems(launch.id, [
      { quality_conclusion: "PASS" },
      { quality_conclusion: "pass" },
      { quality_conclusion: "PASS" },
      { quality_conclusion: "Fail" },
    ]);

    renderDetail(launch, items);

    const metric = await screen.findByTestId("quality-pass-rate");
    // Three of the four items are PASS once case-folded: 3/4 = 75.0%.
    expect(metric).toHaveTextContent("(75.0%)");
  });

  it("keeps cancelled, timed-out and skipped items in the denominator", async () => {
    // Each terminal execution state that never reached a comparable quality
    // verdict must still consume denominator space.
    const launch = buildLaunch("launch-quality-007", {
      progress: {
        total: 4,
        pending: 0,
        queued: 0,
        running: 0,
        retry_wait: 0,
        succeeded: 1,
        failed: 1,
        timed_out: 1,
        cancelled: 1,
        completed: 4,
        percentage: 100,
        attempts: 4,
        retries: 0,
        allowed_actions: [],
      },
    });
    const items = buildItems(launch.id, [
      { quality_conclusion: "pass" },
      { quality_conclusion: "fail" },
      { execution_status: "CANCELLED", eval_status: "SKIPPED", quality_conclusion: "unknown" },
      { execution_status: "TIMED_OUT", eval_status: "SKIPPED", quality_conclusion: "unknown" },
    ]);

    renderDetail(launch, items);

    const metric = await screen.findByTestId("quality-pass-rate");
    expect(metric).toHaveTextContent("(25.0%)");
    expect(metric).toHaveTextContent("统计范围：全部用例");
  });

  it("renders an explicit empty state instead of a fabricated ratio when no items exist", async () => {
    const launch = buildLaunch("launch-quality-003", {
      status: "PENDING",
      quality_conclusion: "unknown",
      progress: {
        total: 0,
        pending: 0,
        queued: 0,
        running: 0,
        retry_wait: 0,
        succeeded: 0,
        failed: 0,
        timed_out: 0,
        cancelled: 0,
        completed: 0,
        percentage: 0,
        attempts: 0,
        retries: 0,
        allowed_actions: ["run"],
      },
    });

    renderDetail(launch, []);

    const metric = await screen.findByTestId("quality-pass-rate");
    expect(metric).toHaveTextContent("尚未统计");
    expect(metric).not.toHaveTextContent("NaN");
    expect(metric).not.toHaveTextContent("Infinity");
  });

  it("keeps the unavailable state when the Items request fails", async () => {
    const launch = buildLaunch("launch-quality-004");
    (api.GET as any).mockImplementation((path: string) => {
      if (path.includes("/items")) {
        return Promise.resolve({ error: { message: "boom" } });
      }
      if (path.includes("/summary") || path.includes("/comparison") || path.includes("/baselines")) {
        return Promise.resolve({ data: null });
      }
      if (path.includes("/api/v1/experiment-launches")) return Promise.resolve({ data: launch });
      return Promise.resolve({ data: null });
    });

    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    render(
      <QueryClientProvider client={client}>
        <MemoryRouter initialEntries={[`/launches/${launch.id}`]}>
          <Routes>
            <Route path="/launches/:launchId" element={<LaunchDetail />} />
          </Routes>
        </MemoryRouter>
      </QueryClientProvider>
    );

    const metric = await screen.findByTestId("quality-pass-rate");
    await waitFor(() => {
      expect(metric).toHaveTextContent("暂不可用");
    });
    expect(metric).not.toHaveTextContent("(0.0%)");
  });
});
