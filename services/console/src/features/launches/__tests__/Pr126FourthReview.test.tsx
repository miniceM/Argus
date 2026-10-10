import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, waitFor, fireEvent, within, act } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter, Routes, Route, useLocation } from "react-router-dom";
import { LaunchDetail } from "../LaunchDetail";
import { validateSnapshotDetail } from "../launchReportView";
import { ManifestAuditTab } from "../tabs/ManifestAuditTab";
import { ExecutionProgressPanel } from "../ExecutionProgressPanel";
import { SetBaselineModal } from "../SetBaselineModal";
import { queryKeys } from "../../../api/query-keys";
import { api } from "../../../api/client";
import { snapshotListKey } from "../launchSnapshotQueries";

vi.mock("../../../api/client", () => ({
  api: {
    GET: vi.fn(),
    POST: vi.fn(),
  },
}));

const LocationWatcher = () => {
  const loc = useLocation();
  return <div data-testid="current-location">{loc.pathname}{loc.search}</div>;
};

describe("Launch Detail Refactoring (#119, #120-#124)", () => {
  let queryClient: QueryClient;

  const mockLaunch = {
    id: "ff04d66b-3d00-4d67-8cf4-d36772fff708",
    name: "banking-agent-v2",
    status: "COMPLETED",
    quality_conclusion: "pass",
    dataset_name: "banking-agent-regression",
    dataset_version: "2026-10-08T16:08:13Z",
    agent_id: "banking-agent",
    agent_version: "v2",
    langfuse_sync_status: "SYNCED",
    langfuse_experiment_url: "https://cloud.langfuse.com/project/p1/datasets/d1/runs/r1",
    created_at: "2026-10-08T16:08:13Z",
    started_at: "2026-10-08T16:08:14Z",
    completed_at: "2026-10-08T16:08:18.82Z",
    allowed_actions: ["retry_evaluation"],
    manifest: {
      schema_version: "1.0",
      comparison: {
        environment: "production",
        baseline_snapshot_id: "base-snap-001",
      },
      dataset: {
        dataset_name: "banking-agent-regression",
        dataset_version: "2026-10-08T16:08:13Z",
        snapshot_digest: "sha256:19e7bbcbd7c6",
        items_count: 6,
      },
      agent: {
        id: "banking-agent",
        version: "v2",
        endpoint: "http://127.0.0.1:18082/invoke",
        spec_digest: "sha256:banking-v2-digest",
      },
      evaluators: [
        { id: "pii_safe", version: "1.0.0", required: true },
        { id: "tool_match", version: "1.0.0", required: true },
        { id: "escalation_match", version: "1.0.0", required: true },
      ],
      execution_policy: {
        max_concurrency: 4,
        timeout_seconds: 30,
        max_retries: 2,
      },
      runner: {
        runner_version: "v0.2.0",
        mapping_engine_version: "engine-v1",
      },
      quality_policy: {
        policy_id: "banking-policy",
        version: "1.0",
        rules: [
          { evaluator_id: "pii_safe", operator: "==", expected_value: 1, required: true },
          { evaluator_id: "tool_match", operator: ">=", threshold: 0.9, required: true },
        ],
      },
    },
  };

  const mockItems = [
    {
      id: "item-exec-1",
      launch_id: "ff04d66b-3d00-4d67-8cf4-d36772fff708",
      dataset_item_id: "item-001",
      execution_status: "succeeded",
      eval_status: "succeeded",
      quality_conclusion: "pass",
      langfuse_trace_url: "https://cloud.langfuse.com/trace/t1",
      attempt_count: 1,
      final_attempt_http_status: 200,
      final_attempt_latency_ms: 142,
      scores: { pii_safe: 1, tool_match: 1 },
      quality_evaluation: {
        conclusion: "pass",
        rules: [
          { evaluator_id: "pii_safe", conclusion: "pass" },
          { evaluator_id: "tool_match", conclusion: "pass" },
        ],
      },
    },
    {
      id: "item-exec-2",
      launch_id: "ff04d66b-3d00-4d67-8cf4-d36772fff708",
      dataset_item_id: "item-002",
      execution_status: "succeeded",
      eval_status: "succeeded",
      quality_conclusion: "pass",
      langfuse_trace_url: "https://cloud.langfuse.com/trace/t2",
      attempt_count: 1,
      final_attempt_http_status: 200,
      final_attempt_latency_ms: 165,
      scores: { pii_safe: 1, tool_match: 1 },
      quality_evaluation: {
        conclusion: "pass",
        rules: [
          { evaluator_id: "pii_safe", conclusion: "pass" },
          { evaluator_id: "tool_match", conclusion: "pass" },
        ],
      },
    },
  ];

  const mockSnapshotList = {
    latest_snapshot_id: "snap-v2-001",
    latest_revision: 1,
    revisions: [
      {
        snapshot_id: "snap-v2-001",
        revision: 1,
        is_latest: true,
        created_at: "2026-10-08T16:08:18Z",
        evidence_state: "COMPLETE",
        quality_pass_count: 2,
        quality_fail_count: 0,
        quality_unknown_count: 0,
        source_result_digest: "sha256:snap-digest-v2",
      },
    ],
  };

  const mockSummary = {
    launch_id: "ff04d66b-3d00-4d67-8cf4-d36772fff708",
    snapshot_id: "snap-v2-001",
    revision: 1,
    evidence_state: "COMPLETE",
    summary: {
      pass_rate: 1.0,
      total_cases: 2,
      evaluated_cases: 2,
      evaluation_coverage: 1.0,
      p95_latency_ms: 148,
      cost_per_case: 0.0019,
      cost_currency: "USD",
      cost_coverage: 1.0,
      cost_case_count: 2,
    },
    versions: {
      agent: { id: "banking-agent", version: "v2" },
      dataset: { name: "banking-agent-regression", version: "2026-10-08T16:08:13Z" },
      evaluators: [{ id: "pii_safe", version: "1.0.0" }],
      runner: { runner_version: "v0.2.0" },
    },
  };

  const mockComparison = {
    candidate_snapshot_id: "snap-v2-001",
    baseline_snapshot_id: "base-snap-001",
    classification_counts: {
      ALL: 2,
      REGRESSION: 0,
      IMPROVEMENT: 1,
      UNCHANGED: 1,
      NOT_COMPARABLE: 0,
    },
    summary: {
      comparable_case_count: 2,
      candidate: {
        pass_rate: 1.0,
        p95_latency_ms: 148,
        cost_per_case: 0.0019,
        cost_currency: "USD",
        cost_coverage: 1.0,
      },
      baseline: {
        pass_rate: 0.333,
        p95_latency_ms: 160,
        cost_per_case: 0.0019,
        cost_currency: "USD",
        cost_coverage: 1.0,
      },
      comparable_cohort: {
        candidate: {
          pass_rate: 1.0,
          p95_latency_ms: 148,
          cost_per_case: 0.0019,
          cost_currency: "USD",
          cost_coverage: 1.0,
          score_means: { pii_safe: 1.0 },
        },
        baseline: {
          pass_rate: 0.333,
          p95_latency_ms: 160,
          cost_per_case: 0.0019,
          cost_currency: "USD",
          cost_coverage: 1.0,
          score_means: { pii_safe: 0.667 },
        },
      },
      cost_comparison: {
        status: "COMPARABLE",
        delta: 0,
        currency: "USD",
      },
    },
    items: [],
    comparability: {
      comparable: true,
      reason_codes: [],
      provenance: "VERIFIED",
      dimensions: [],
      suggestions: [],
    },
    formal: {
      available: true,
      verdict: "IMPROVEMENT",
      required_cases: 2,
    },
    versions: {
      candidate: mockSummary.versions,
      baseline: {
        agent: { id: "banking-agent", version: "v1" },
        dataset: mockSummary.versions.dataset,
        evaluators: mockSummary.versions.evaluators,
        runner: mockSummary.versions.runner,
      },
    },
  };

  beforeEach(() => {
    queryClient = new QueryClient({
      defaultOptions: { queries: { retry: false } },
    });
    vi.clearAllMocks();

    (api.GET as any).mockImplementation((path: string, options?: any) => {
      if (path === "/api/v1/experiment-launches/{launch_id}") {
        return Promise.resolve({ data: mockLaunch });
      }
      if (path === "/api/v1/experiment-launches/{launch_id}/items") {
        return Promise.resolve({ data: mockItems });
      }
      if (path === "/api/v1/experiment-launches/{launch_id}/result-snapshots") {
        return Promise.resolve({ data: mockSnapshotList });
      }
      if (path.includes("/result-snapshots/")) {
        // Echo the requested identity the way the real endpoint does, so a historical
        // revision is a payload for *that* revision instead of a foreign one.
        const requested = options?.params?.path?.snapshot_id ?? "snap-v2-001";
        return Promise.resolve({
          data: {
            ...mockSnapshotList.revisions[0],
            launch_id: mockLaunch.id,
            snapshot_id: requested,
            releasable: true,
            manifest: mockLaunch.manifest,
            items: mockItems,
          },
        });
      }
      if (path === "/api/v1/experiment-launches/{launch_id}/summary") {
        return Promise.resolve({ data: mockSummary });
      }
      if (path === "/api/v1/experiment-launches/{launch_id}/comparison") {
        return Promise.resolve({ data: mockComparison });
      }
      if (path === "/api/v1/agents/{agent_id}/baselines") {
        return Promise.resolve({
          data: {
            agent_id: "banking-agent",
            environment: "production",
            revision: 1,
            result_snapshot_id: "base-snap-001",
            result_revision: 1,
            result_evidence_state: "COMPLETE",
          },
        });
      }
      if (path === "/api/v1/experiment-launches/{launch_id}/comparison/case") {
        return Promise.resolve({
          data: {
            launch_id: mockLaunch.id,
            candidate_snapshot_id: "snap-v2-001",
            dataset_item_id: options?.params?.query?.dataset_item_id || "item-001",
            classification: "IMPROVEMENT",
            baseline: {
              output_status: "AVAILABLE",
              output: "v1 output text",
              scores: { pii_safe: 0 },
            },
            candidate: {
              output_status: "AVAILABLE",
              output: "v2 output masked text",
              scores: { pii_safe: 1 },
              trace_url: "https://cloud.langfuse.com/trace/t1",
            },
          },
        });
      }
      return Promise.resolve({ data: null });
    });
  });

  const renderComponent = (initialUrl = "/launches/ff04d66b-3d00-4d67-8cf4-d36772fff708") => {
    return render(
      <QueryClientProvider client={queryClient}>
        <MemoryRouter initialEntries={[initialUrl]}>
          <LocationWatcher />
          <Routes>
            <Route path="/launches/:launchId" element={<LaunchDetail />} />
          </Routes>
        </MemoryRouter>
      </QueryClientProvider>
    );
  };

  it("AC1 & #120: Renders business Header with human-readable name, exact version badge, compact UUID copy, and 4 KPI cards", async () => {
    renderComponent();

    // 1. Header contains agent name / id and exact version badge
    await waitFor(() => {
      expect(screen.getByRole("heading", { name: "banking-agent" })).toBeInTheDocument();
      expect(screen.getByText("v2 (Candidate)")).toBeInTheDocument();
    });

    // 2. Gate conclusion badge & evidence status badge
    expect(screen.getByText("门禁准入通过 (PASS)")).toBeInTheDocument();
    expect(screen.getAllByText("证据完整").length).toBeGreaterThanOrEqual(1);

    // 3. Compact UUID display with copy button
    expect(screen.getByText(/ID: ff04d66b…f708/)).toBeInTheDocument();

    // 4. Four Core KPI Cards
    expect(screen.getByText("综合质量通过率")).toBeInTheDocument();
    expect(screen.getByText("正式回归用例数")).toBeInTheDocument();
    expect(screen.getByText("P95 响应时延")).toBeInTheDocument();
    expect(screen.getByText("单用例平均成本")).toBeInTheDocument();

    // 5. Default tab is Gate & Comparison, with audit/manifest panel not displayed in Tab 1
    expect(screen.getByRole("tab", { name: /门禁与版本对比/ })).toHaveAttribute("aria-selected", "true");
    expect(screen.queryByTestId("manifest-4d-grid")).not.toBeInTheDocument();
  });

  it("AC2 & #121: Tab switching synchronizes URL query param (?tab=cases, ?tab=audit)", async () => {
    renderComponent();

    await waitFor(() => {
      expect(screen.getByRole("tab", { name: /用例排查与 Trace/ })).toBeInTheDocument();
    });

    // Click Cases Tab
    const casesTab = screen.getByRole("tab", { name: /用例排查与 Trace/ });
    fireEvent.click(casesTab);

    await waitFor(() => {
      expect(screen.getByTestId("current-location")).toHaveTextContent("tab=cases");
    });

    // Click Audit Tab
    const auditTab = screen.getByRole("tab", { name: /不可变快照与审计/ });
    fireEvent.click(auditTab);

    await waitFor(() => {
      expect(screen.getByTestId("current-location")).toHaveTextContent("tab=audit");
      expect(screen.getByTestId("manifest-4d-grid")).toBeInTheDocument();
    });
  });

  it("AC3 & AC4 & #122: Tab 1 presents real Evaluator metric comparison and changes summary", async () => {
    renderComponent("/launches/ff04d66b-3d00-4d67-8cf4-d36772fff708?tab=compare");

    await waitFor(() => {
      expect(screen.getByText("回归评测指标对比 (Regression Metrics)")).toBeInTheDocument();
    });

    // Metrics table checks
    expect(screen.getAllByText("pii_safe").length).toBeGreaterThanOrEqual(1);
    // Delta should reflect percentage points change
    expect(screen.getByText("+33.3 pp")).toBeInTheDocument();

    // Summary cards exist based on real data
    expect(screen.getByTestId("capabilities-delta-summary")).toBeInTheDocument();
  });

  it("AC5 & AC6 & #123: Tab 2 supports filter counts and inline accordion case expansion with lazy loaded outputs", async () => {
    renderComponent("/launches/ff04d66b-3d00-4d67-8cf4-d36772fff708?tab=cases");

    await waitFor(() => {
      expect(screen.getByRole("button", { name: /全部用例 \(2\)/ })).toBeInTheDocument();
      expect(screen.getByRole("button", { name: /已通过 \(2\)/ })).toBeInTheDocument();
    });

    // Expand first case
    const expandBtn = screen.getByTestId("case-row-expand-item-001");
    fireEvent.click(expandBtn);

    // Lazy load candidate output
    await waitFor(() => {
      expect(screen.getByText("v2 output masked text")).toBeInTheDocument();
      const traceLinks = screen.getAllByRole("link", { name: /Trace/ });
      expect(traceLinks.length).toBeGreaterThanOrEqual(1);
      expect(traceLinks[0]).toHaveAttribute("href", "https://cloud.langfuse.com/trace/t1");
    });
  });

  it("AC7 & AC9 & #124: Tab 3 offers 4D manifest, JSON copy/download, and baseline set modal", async () => {
    renderComponent("/launches/ff04d66b-3d00-4d67-8cf4-d36772fff708?tab=audit");

    await waitFor(() => {
      expect(screen.getByTestId("manifest-4d-grid")).toBeInTheDocument();
      expect(screen.getByText("1. Agent 规格快照")).toBeInTheDocument();
      expect(screen.getByText("2. 数据集快照")).toBeInTheDocument();
      expect(screen.getByText("3. 门禁规则集")).toBeInTheDocument();
      expect(screen.getByText("4. Runner 执行引擎")).toBeInTheDocument();
    });

    // Download JSON button exists and is clickable
    const downloadBtn = screen.getByRole("button", { name: /下载原始 JSON/ });
    expect(downloadBtn).toBeInTheDocument();

    // Copy JSON button exists
    const copyBtn = screen.getByRole("button", { name: /复制 JSON/ });
    expect(copyBtn).toBeInTheDocument();
  });

  const overrideGet = (overrides: Record<string, any>) => {
    const original = (api.GET as any).getMockImplementation();
    (api.GET as any).mockImplementation((path: string, options?: any) => {
      if (path in overrides) return Promise.resolve(overrides[path]);
      return original(path, options);
    });
  };

  it("REVIEW: historical S1 KPI must read frozen counts rather than live S2 Items", async () => {
    const old = { ...mockSnapshotList.revisions[0], snapshot_id: "S1", revision: 1,
      quality_pass_count: 0, quality_fail_count: 2, is_latest: false };
    overrideGet({
      "/api/v1/experiment-launches/{launch_id}/result-snapshots": { data: {
        latest_snapshot_id: "S2", latest_revision: 2,
        revisions: [{ ...mockSnapshotList.revisions[0], snapshot_id: "S2", revision: 2 }, old] } },
      "/api/v1/experiment-launches/{launch_id}/summary": { data: {
        ...mockSummary, snapshot_id: "S1", summary: { ...mockSummary.summary,
          pass_rate: 0, quality_pass_count: 0, quality_fail_count: 2, quality_unknown_count: 0 } } },
      "/api/v1/experiment-launches/{launch_id}/comparison": { data: {
        ...mockComparison, candidate_snapshot_id: "S1" } },
    });
    renderComponent(`/launches/${mockLaunch.id}?snapshot_id=S1&tab=cases`);
    await screen.findByRole("button", { name: "全部用例 (2)" });
    expect(within(screen.getByTestId("quality-pass-rate")).getByText("0.0%", { exact: true })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "未通过 (2)" })).toBeInTheDocument();
  });

  it("REVIEW: invalid Snapshot must not expose a Baseline action for latest", async () => {
    overrideGet({
      "/api/v1/experiment-launches/{launch_id}/summary": { error: { detail: "Snapshot WRONG not found" }, response: { status: 404 } },
      "/api/v1/experiment-launches/{launch_id}/comparison": { error: { detail: "Snapshot WRONG not found" }, response: { status: 404 } },
    });
    renderComponent(`/launches/${mockLaunch.id}?snapshot_id=WRONG`);
    await screen.findByText("Revision 1");
    expect(screen.queryByRole("button", { name: "设为新 Baseline" })).not.toBeInTheDocument();
    expect(screen.getByRole("alert")).toHaveTextContent("Snapshot WRONG not found");
  });

  it("REVIEW: a cached directory that confirms absence stays authoritative if refresh fails", async () => {
    queryClient.setQueryData(snapshotListKey(mockLaunch.id), {
      launch_id: mockLaunch.id,
      latest_snapshot_id: "snap-v2-001",
      latest_revision: 1,
      revisions: mockSnapshotList.revisions,
    });
    overrideGet({
      "/api/v1/experiment-launches/{launch_id}/result-snapshots": {
        error: { detail: "snapshot history unavailable" },
        response: { status: 503 },
      },
      "/api/v1/experiment-launches/{launch_id}/summary": {
        error: { detail: "Snapshot WRONG not found" },
        response: { status: 404 },
      },
      "/api/v1/experiment-launches/{launch_id}/comparison": {
        error: { detail: "Snapshot WRONG not found" },
        response: { status: 404 },
      },
    });

    renderComponent(`/launches/${mockLaunch.id}?snapshot_id=WRONG`);

    await waitFor(() => {
      expect(api.GET).toHaveBeenCalledWith(
        "/api/v1/experiment-launches/{launch_id}/result-snapshots",
        expect.anything(),
      );
    });
    expect(screen.queryByRole("button", { name: "设为新 Baseline" })).not.toBeInTheDocument();
    expect(
      (api.GET as any).mock.calls.some(([path, options]: [string, any]) =>
        path.includes("/result-snapshots/") && options?.params?.path?.snapshot_id === "WRONG",
      ),
    ).toBe(false);
  });

  it("REVIEW: a verified explicit snapshot can be set as Baseline when history is unavailable", async () => {
    overrideGet({
      "/api/v1/experiment-launches/{launch_id}/result-snapshots": {
        error: { detail: "snapshot history unavailable" },
        response: { status: 503 },
      },
    });

    renderComponent(`/launches/${mockLaunch.id}?snapshot_id=snap-v2-001`);

    expect(await screen.findByText("证据完整")).toBeInTheDocument();
    fireEvent.click(await screen.findByRole("button", { name: "设为新 Baseline" }));

    expect(await screen.findByRole("button", { name: "确认设为 Baseline" })).toBeEnabled();
    expect(screen.getByText(/Revision 1 \(snap-v2-001/)).toBeInTheDocument();
  });

  it("REVIEW: a mismatched direct snapshot detail cannot authorize a Baseline", async () => {
    overrideGet({
      "/api/v1/experiment-launches/{launch_id}/result-snapshots": {
        error: { detail: "snapshot history unavailable" },
        response: { status: 503 },
      },
      "/api/v1/experiment-launches/{launch_id}/result-snapshots/{snapshot_id}": {
        data: {
          ...mockSnapshotList.revisions[0],
          launch_id: mockLaunch.id,
          snapshot_id: "different-snapshot",
          releasable: true,
          items: mockItems,
        },
      },
    });

    renderComponent(`/launches/${mockLaunch.id}?snapshot_id=snap-v2-001`);

    expect(await screen.findByText("诊断快照")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "设为新 Baseline" })).not.toBeInTheDocument();
  });

  it("REVIEW: comparable regression must not be labelled a pass-rate improvement", async () => {
    const c = structuredClone(mockComparison);
    c.summary.comparable_cohort.baseline.pass_rate = 1;
    c.summary.comparable_cohort.candidate.pass_rate = 0.5;
    c.formal.verdict = "REGRESSION";
    c.classification_counts.REGRESSION = 1;
    overrideGet({ "/api/v1/experiment-launches/{launch_id}/comparison": { data: c } });
    renderComponent();
    await screen.findByText(/共同可比用例通过率从 100.0%/);
    expect(screen.getByText(/共同可比用例通过率从 100.0%/)).not.toHaveTextContent("提升至");
    expect(screen.queryByText("质量通过率提升")).not.toBeInTheDocument();
  });

  it("REVIEW: no frozen 90 percent rule means no fabricated overall gate", async () => {
    const c = structuredClone(mockComparison);
    c.summary.comparable_cohort.baseline.pass_rate = 0.7;
    c.summary.comparable_cohort.candidate.pass_rate = 0.8;
    overrideGet({ "/api/v1/experiment-launches/{launch_id}/comparison": { data: c } });
    renderComponent();
    await screen.findByText("综合质量通过率 (Overall Pass)");
    const row = screen.getByText("综合质量通过率 (Overall Pass)").closest("tr")!;
    // This row states a nonexistent rule and even declares 80% as meeting 90%.
    expect(within(row).queryByText("达标")).not.toBeInTheDocument();
  });

  it("REVIEW: comparison API error must remain an error with a retry action", async () => {
    overrideGet({ "/api/v1/experiment-launches/{launch_id}/comparison": {
      error: { detail: "comparison unavailable" }, response: { status: 503 } } });
    renderComponent();
    await screen.findByText(/暂无可比指标数据/);
    expect(screen.getByRole("alert")).toHaveTextContent("comparison unavailable");
  });

  it("REVIEW: Cases filter survives same-snapshot tab navigation", async () => {
    renderComponent(`/launches/${mockLaunch.id}?tab=cases`);
    fireEvent.click(await screen.findByRole("button", { name: "未通过 (0)" }));
    expect(screen.getByRole("button", { name: "未通过 (0)" })).toHaveAttribute("aria-pressed", "true");
    fireEvent.click(screen.getByRole("tab", { name: /门禁与版本对比/ }));
    fireEvent.click(screen.getByRole("tab", { name: /用例排查与 Trace/ }));
    expect(await screen.findByRole("button", { name: "未通过 (0)" })).toHaveAttribute("aria-pressed", "true");
  });

  it("REVIEW: invalid tab has a safe compare fallback", async () => {
    renderComponent(`/launches/${mockLaunch.id}?tab=typo`);
    await screen.findByTestId("quality-pass-rate");
    expect(screen.getByRole("tab", { name: /门禁与版本对比/ })).toHaveAttribute("aria-selected", "true");
    expect(screen.getByRole("tabpanel")).toBeInTheDocument();
  });

  it("REVIEW: baseline conflict refreshes binding before renewed confirmation", async () => {
    // F03 migration: the 409 recovery is a single fresh GET whose verified result lands
    // in the shared cache and in the dialog display. The old assertion observed the
    // invalidate + manual GET double path, which is exactly the defect this unit removes.
    (api.POST as any).mockResolvedValue({ error: { detail: "conflict" }, response: { status: 409 } });
    (api.GET as any).mockImplementation((path: string) =>
      path === "/api/v1/agents/{agent_id}/baselines"
        ? Promise.resolve({
            data: {
              agent_id: "banking-agent",
              environment: "production",
              revision: 5,
              result_snapshot_id: "base-snap-005",
              updated_by: "tester",
              updated_at: "2026-10-08T16:08:13Z",
              launch_id: "ff04d66b-3d00-4d67-8cf4-d36772fff708",
              agent_version: "v2",
              dataset_name: "banking-agent-regression",
              dataset_version: "2026-10-08T16:08:13Z",
              summary: {},
            },
          })
        : Promise.resolve({ data: {} }),
    );
    render(<QueryClientProvider client={queryClient}>
      <SetBaselineModal open onClose={() => {}} agentId="banking-agent" environment="production"
        activeSnapshot={mockSnapshotList.revisions[0] as any}
        activeBaseline={{ revision: 1, result_snapshot_id: "base-snap-001" } as any} />
    </QueryClientProvider>);
    fireEvent.click(screen.getByRole("button", { name: "确认设为 Baseline" }));
    await screen.findByText(/HTTP 409/);
    await waitFor(() =>
      expect(screen.getByText(/Revision 5 \(Snapshot: base-sna/)).toBeInTheDocument(),
    );
    const baselineGets = (api.GET as any).mock.calls.filter(
      (c: any[]) => c[0] === "/api/v1/agents/{agent_id}/baselines",
    );
    expect(baselineGets).toHaveLength(1);
    expect(baselineGets[0][1].params.query).toEqual({ environment: "production" });
    expect(
      queryClient.getQueryData<any>(queryKeys.baselines.detail("banking-agent", "production"))?.revision,
    ).toBe(5);
  });

  it("REVIEW: progress board collapses on RUNNING to COMPLETED without remount", () => {
    const progress = { total: 2, percentage: 50, running: 1, succeeded: 1 } as any;
    const view = render(<ExecutionProgressPanel status="RUNNING" progress={progress} />);
    const grid = document.querySelector('[data-card="running"]')?.parentElement;
    expect(grid).not.toHaveClass("hidden");
    view.rerender(<ExecutionProgressPanel status="COMPLETED" progress={{ ...progress, percentage: 100 }} />);
    expect(grid).toHaveClass("hidden");
  });

  it("REVIEW: numeric cost threshold retains its raw unit", async () => {
    const launch = structuredClone(mockLaunch) as any;
    launch.manifest.quality_policy.rules = [{ evaluator_id: "call_cost", operator: "<=", threshold: 0.2, required: true }];
    const c = structuredClone(mockComparison) as any;
    c.summary.comparable_cohort.baseline.score_means = { call_cost: 0.3 };
    c.summary.comparable_cohort.candidate.score_means = { call_cost: 0.1 };
    overrideGet({
      "/api/v1/experiment-launches/{launch_id}": { data: launch },
      "/api/v1/experiment-launches/{launch_id}/comparison": { data: c },
    });
    renderComponent();
    const rule = await screen.findByText("<= 0.2 (类型未声明 · 必要)");
    expect(rule).toHaveTextContent("<= 0.2");
  });

  it("REVIEW: legacy numeric thresholds without a declared type retain their raw value", async () => {
    const launch = structuredClone(mockLaunch) as any;
    launch.manifest.quality_policy.rules = [
      { evaluator_id: "correctness", operator: ">=", threshold: 0.8, required: true },
    ];
    launch.manifest.evaluators = [];
    const comparison = structuredClone(mockComparison) as any;
    comparison.summary.comparable_cohort.baseline.score_means = { correctness: 0.7 };
    comparison.summary.comparable_cohort.candidate.score_means = { correctness: 0.9 };
    overrideGet({
      "/api/v1/experiment-launches/{launch_id}": { data: launch },
      "/api/v1/experiment-launches/{launch_id}/comparison": { data: comparison },
    });

    renderComponent();

    const metric = await screen.findByText("correctness", { selector: "td" });
    const row = metric.closest("tr")!;
    expect(row).toHaveTextContent(">= 0.8");
    expect(row).toHaveTextContent("类型未声明");
    expect(row).not.toHaveTextContent("80.0%");
  });

  it("REVIEW: snapshot read failure must not download Manifest as selected result JSON", async () => {
    const original = (api.GET as any).getMockImplementation();
    (api.GET as any).mockImplementation((path: string, options?: any) => {
      if (path.includes('/result-snapshots/')) return Promise.resolve({ error: { detail: "snapshot store unavailable" }, response: { status: 503 } });
      return original(path, options);
    });
    renderComponent(`/launches/${mockLaunch.id}?snapshot_id=snap-v2-001&tab=audit`);
    const btn = await screen.findByRole("button", { name: "下载原始 JSON" });
    await waitFor(() => expect(btn).toBeDisabled());
  });
  const seedHistoricalFailures = () => {
    const old = { ...mockSnapshotList.revisions[0], snapshot_id: "S1", revision: 1,
      quality_pass_count: 0, quality_fail_count: 2, quality_unknown_count: 0, is_latest: false };
    overrideGet({
      "/api/v1/experiment-launches/{launch_id}/result-snapshots": { data: {
        latest_snapshot_id: "S2", latest_revision: 2,
        revisions: [{ ...mockSnapshotList.revisions[0], snapshot_id: "S2", revision: 2 }, old] } },
      "/api/v1/experiment-launches/{launch_id}/summary": { data: {
        ...mockSummary, snapshot_id: "S1", summary: { ...mockSummary.summary,
          pass_rate: 0, quality_pass_count: 0, quality_fail_count: 2, quality_unknown_count: 0 } } },
      "/api/v1/experiment-launches/{launch_id}/comparison": { data: { ...mockComparison, candidate_snapshot_id: "S1" } },
      "/api/v1/experiment-launches/{launch_id}/result-snapshots/{snapshot_id}": { data: {
        launch_id: mockLaunch.id, ...old, releasable: true, versions: mockSummary.versions,
        summary: { quality_pass_count: 0, quality_fail_count: 2, quality_unknown_count: 0, total_cases: 2 },
        items: mockItems.map(i=>({...i, quality_conclusion: "fail", scores: {pii_safe:0},
          quality_evaluation: {conclusion:"fail",rules:[]}})) } },
    });
  };

  it("R126: S1 fail filter must show the actual frozen failed Case IDs", async () => {
    seedHistoricalFailures();
    renderComponent(`/launches/${mockLaunch.id}?snapshot_id=S1&tab=cases`);
    const btn = await screen.findByRole("button", { name: "未通过 (2)" });
    fireEvent.click(btn);
    expect(await screen.findByTestId("case-row-expand-item-001")).toBeInTheDocument();
    expect(await screen.findByTestId("case-row-expand-item-002")).toBeInTheDocument();
  });

  it("R126: frozen PASS plus UNKNOWN must not become Header gate PASS", async () => {
    overrideGet({
      "/api/v1/experiment-launches/{launch_id}/result-snapshots": {data:{...mockSnapshotList,revisions:[{
        ...mockSnapshotList.revisions[0], evidence_state:"DIAGNOSTIC", quality_pass_count:1,quality_fail_count:0,quality_unknown_count:1 }]}},
      "/api/v1/experiment-launches/{launch_id}/summary": {data:{...mockSummary,evidence_state:"DIAGNOSTIC",summary:{...mockSummary.summary,
        quality_pass_count:1,quality_fail_count:0,quality_unknown_count:1,pass_rate:1}}},
    });
    renderComponent();
    await screen.findByRole("tab",{name:/用例排查与 Trace/});
    expect(screen.queryByText("门禁准入通过 (PASS)")).not.toBeInTheDocument();
  });

  it("R126: no overall policy must show no fabricated 90 percent requirement", async () => {
    renderComponent();
    await screen.findByText("综合质量通过率 (Overall Pass)");
    expect(screen.queryByText("≥ 90.0%")).not.toBeInTheDocument();
  });

  it("R126: frozen all-PASS counts stay UNKNOWN unless exact detail confirms releasable", async () => {
    overrideGet({
      "/api/v1/experiment-launches/{launch_id}/result-snapshots/{snapshot_id}": {
        data: {
          ...mockSnapshotList.revisions[0],
          launch_id: mockLaunch.id,
          snapshot_id: "snap-v2-001",
          evidence_state: "COMPLETE",
          releasable: false,
          items: mockItems,
        },
      },
    });

    renderComponent();

    expect(await screen.findByText("门禁状态未知 (UNKNOWN)")).toBeInTheDocument();
    expect(screen.queryByText("门禁准入通过 (PASS)")).not.toBeInTheDocument();
  });

  it("R126: no latency policy must show no fabricated 500ms requirement", async () => {
    renderComponent();
    await screen.findByText("综合质量通过率 (Overall Pass)");
    expect(screen.queryByText("< 500 ms")).not.toBeInTheDocument();
  });

  it("REVIEW: an item duration rule cannot verdict run-level P95 latency", async () => {
    const launch = structuredClone(mockLaunch) as any;
    launch.manifest.quality_policy.rules = [
      { evaluator_id: "duration_ms", result_type: "numeric", operator: "<=", threshold: 500, required: true },
    ];
    launch.manifest.evaluators = [
      { id: "duration_ms", version: "1", result_type: "numeric", scope: "item" },
    ];
    const c = structuredClone(mockComparison) as any;
    c.summary.comparable_cohort.baseline.p95_latency_ms = 200;
    c.summary.comparable_cohort.candidate.p95_latency_ms = 800;
    overrideGet({
      "/api/v1/experiment-launches/{launch_id}": { data: launch },
      "/api/v1/experiment-launches/{launch_id}/comparison": { data: c },
    });

    renderComponent();

    const latency = await screen.findByText("P95 响应时延 (Latency)", { selector: "td" });
    const row = latency.closest("tr")!;
    expect(within(row).getByText("较基线上升")).toBeInTheDocument();
    expect(row).not.toHaveTextContent("<= 500 ms");
    expect(row).not.toHaveTextContent("未达标");
  });

  it("REVIEW: an explicitly declared run-scope P95 rule may verdict run-level latency", async () => {
    const launch = structuredClone(mockLaunch) as any;
    launch.manifest.quality_policy.rules = [
      { evaluator_id: "p95_latency_ms", result_type: "numeric", operator: "<=", threshold: 500, required: true },
    ];
    launch.manifest.evaluators = [
      { id: "p95_latency_ms", version: "1", result_type: "numeric", scope: "run" },
    ];
    const c = structuredClone(mockComparison) as any;
    c.summary.comparable_cohort.baseline.p95_latency_ms = 600;
    c.summary.comparable_cohort.candidate.p95_latency_ms = 400;
    overrideGet({
      "/api/v1/experiment-launches/{launch_id}": { data: launch },
      "/api/v1/experiment-launches/{launch_id}/comparison": { data: c },
    });

    renderComponent();

    const latency = await screen.findByText("P95 响应时延 (Latency)", { selector: "td" });
    const row = latency.closest("tr")!;
    expect(row).toHaveTextContent("<= 500 ms");
    expect(within(row).getByText("达标")).toBeInTheDocument();
  });

  it("R126: non-cost numeric thresholds and score means retain their units", async () => {
    const launch = structuredClone(mockLaunch) as any;
    launch.manifest.quality_policy.rules=[{evaluator_id:"duration_ms",result_type:"numeric",operator:"<=",threshold:500,required:true}];
    launch.manifest.evaluators=[{id:"duration_ms",version:"1",direction:"lower_is_better"}];
    const c=structuredClone(mockComparison) as any;
    c.summary.comparable_cohort.baseline.score_means={duration_ms:400};
    c.summary.comparable_cohort.candidate.score_means={duration_ms:200};
    overrideGet({"/api/v1/experiment-launches/{launch_id}":{data:launch},
      "/api/v1/experiment-launches/{launch_id}/comparison":{data:c}});
    renderComponent();
    const cell=await screen.findByText("duration_ms",{selector:"td"});
    const row=cell.closest("tr")!;
    expect(row).not.toHaveTextContent("50000.0%");
    expect(row).not.toHaveTextContent("20000.0%");
  });

  it("R126: cost decrease with lower-is-better is not a failed improvement", async () => {
    const launch=structuredClone(mockLaunch) as any;
    launch.manifest.quality_policy.rules=[{evaluator_id:"call_cost",result_type:"numeric",operator:"<=",threshold:0.2,required:true}];
    launch.manifest.evaluators=[{id:"call_cost",version:"1",direction:"lower_is_better"}];
    const c=structuredClone(mockComparison) as any;
    c.summary.comparable_cohort.baseline.score_means={call_cost:0.3};
    c.summary.comparable_cohort.candidate.score_means={call_cost:0.1};
    overrideGet({"/api/v1/experiment-launches/{launch_id}":{data:launch},
      "/api/v1/experiment-launches/{launch_id}/comparison":{data:c}});
    renderComponent();
    const cell=await screen.findByText("call_cost",{selector:"td"});
    expect(within(cell.closest("tr")!).queryByText("下降")).not.toBeInTheDocument();
  });

  it("REVIEW: formal regression does not invert an increasing comparable pass rate", async () => {
    const c = structuredClone(mockComparison) as any;
    c.summary.comparable_cohort.baseline.pass_rate = 0.5;
    c.summary.comparable_cohort.candidate.pass_rate = 0.75;
    c.formal.verdict = "REGRESSION";
    c.classification_counts.REGRESSION = 1;
    overrideGet({ "/api/v1/experiment-launches/{launch_id}/comparison": { data: c } });

    renderComponent();

    const summary = await screen.findByTestId("capabilities-delta-summary");
    expect(summary).toHaveTextContent("综合质量通过率提升");
    expect(summary).toHaveTextContent("共同可比用例通过率从 50.0% 提升至 75.0%");
    expect(summary).toHaveTextContent("评测门禁未达标 (存在退化)");
  });

  it("REVIEW: candidate policy status follows the frozen operator and threshold", async () => {
    const launch = structuredClone(mockLaunch) as any;
    launch.manifest.quality_policy.rules = [
      { evaluator_id: "call_cost", result_type: "numeric", operator: "<=", threshold: 0.2, required: true },
      { evaluator_id: "overall_pass_rate", result_type: "numeric", operator: "<=", threshold: 0.8, required: true },
    ];
    launch.manifest.evaluators = [{ id: "call_cost", version: "1", result_type: "numeric", direction: "lower_is_better" }];
    const comparison = structuredClone(mockComparison) as any;
    comparison.summary.comparable_cohort.baseline.score_means = { call_cost: 0.3 };
    comparison.summary.comparable_cohort.candidate.score_means = { call_cost: 0.25 };
    overrideGet({
      "/api/v1/experiment-launches/{launch_id}": { data: launch },
      "/api/v1/experiment-launches/{launch_id}/comparison": { data: comparison },
    });

    renderComponent();

    const metric = await screen.findByText("call_cost", { selector: "td" });
    const row = metric.closest("tr")!;
    expect(row).toHaveTextContent("<= 0.2");
    expect(within(row).getByText("聚合均值仅作趋势")).toBeInTheDocument();
    expect(within(row).queryByText("未达标")).not.toBeInTheDocument();
    expect(within(row).queryByText("达标")).not.toBeInTheDocument();
    const overallMetric = screen.getByText("综合质量通过率 (Overall Pass)", { selector: "td" });
    expect(within(overallMetric.closest("tr")!).getByText("未达标")).toBeInTheDocument();
  });

  it("REVIEW: configured overall and evaluator rules report missing evidence, not no gate or trend PASS", async () => {
    const launch = structuredClone(mockLaunch) as any;
    launch.manifest.quality_policy.rules = [
      { evaluator_id: "overall_pass_rate", operator: ">=", threshold: 0.9, required: true },
      { evaluator_id: "call_cost", operator: "<=", threshold: 0.2, required: true },
    ];
    launch.manifest.evaluators = [
      { id: "call_cost", version: "1", result_type: "numeric", direction: "lower_is_better" },
    ];
    const comparison = structuredClone(mockComparison) as any;
    comparison.summary.comparable_cohort.baseline.pass_rate = 0.8;
    comparison.summary.comparable_cohort.candidate.pass_rate = null;
    comparison.summary.comparable_cohort.baseline.score_means = { call_cost: 0.3 };
    comparison.summary.comparable_cohort.candidate.score_means = {};
    overrideGet({
      "/api/v1/experiment-launches/{launch_id}": { data: launch },
      "/api/v1/experiment-launches/{launch_id}/comparison": { data: comparison },
    });

    renderComponent();

    const overall = await screen.findByText("综合质量通过率 (Overall Pass)", { selector: "td" });
    const overallRow = overall.closest("tr")!;
    expect(overallRow).toHaveTextContent(">= 90.0%");
    expect(within(overallRow).getByText("证据不足")).toBeInTheDocument();
    expect(within(overallRow).queryByText("无冻结门槛")).not.toBeInTheDocument();

    const cost = await screen.findByText("call_cost", { selector: "td" });
    const costRow = cost.closest("tr")!;
    expect(costRow).toHaveTextContent("<= 0.2");
    expect(within(costRow).getByText("证据不足")).toBeInTheDocument();
    expect(within(costRow).queryByText("较基线下降")).not.toBeInTheDocument();
  });

  it("REVIEW: evaluator rules are not applied to aggregate score means", async () => {
    const launch = structuredClone(mockLaunch) as any;
    launch.manifest.quality_policy.rules = [
      { evaluator_id: "correctness", result_type: "numeric", operator: ">=", threshold: 0.8, required: true },
    ];
    launch.manifest.evaluators = [
      { id: "correctness", version: "1", result_type: "numeric", direction: "higher_is_better" },
    ];
    const comparison = structuredClone(mockComparison) as any;
    comparison.summary.comparable_cohort.baseline.score_means = { correctness: 0.7 };
    comparison.summary.comparable_cohort.candidate.score_means = { correctness: 0.85 };
    comparison.items = [
      { dataset_item_id: "case-pass", classification: "IMPROVEMENT", candidate_scores: { correctness: 1.0 } },
      { dataset_item_id: "case-fail", classification: "REGRESSION", candidate_scores: { correctness: 0.7 } },
    ];
    overrideGet({
      "/api/v1/experiment-launches/{launch_id}": { data: launch },
      "/api/v1/experiment-launches/{launch_id}/comparison": { data: comparison },
    });

    renderComponent();

    const metric = await screen.findByText("correctness", { selector: "td" });
    const row = metric.closest("tr")!;
    expect(row).toHaveTextContent(">= 0.8");
    expect(within(row).getByText("聚合均值仅作趋势")).toBeInTheDocument();
    expect(within(row).queryByText("达标")).not.toBeInTheDocument();
    expect(within(row).queryByText("未达标")).not.toBeInTheDocument();
    expect(screen.getByText(/score_means 仅为可比 Case 的聚合趋势/)).toBeInTheDocument();
  });

  it("REVIEW: frozen policy renders all strict and inclusive numeric operators", () => {
    const launch = structuredClone(mockLaunch) as any;
    launch.manifest.quality_policy.rules = [
      { evaluator_id: "latency_ms", result_type: "numeric", operator: "<", threshold: 100, required: true },
      { evaluator_id: "score", result_type: "numeric", operator: ">", threshold: 0.8, required: true },
    ];
    render(
      <QueryClientProvider client={queryClient}>
        <ManifestAuditTab launch={launch} activeSnapshot={null} />
      </QueryClientProvider>,
    );

    expect(screen.getByText("latency_ms", { selector: "span" }).closest("li")).toHaveTextContent("< 100");
    expect(screen.getByText("score", { selector: "span" }).closest("li")).toHaveTextContent("> 0.8");
  });

  it("REVIEW: required and critical policy flags remain independent", async () => {
    const launch = structuredClone(mockLaunch) as any;
    launch.manifest.quality_policy.rules = [
      { evaluator_id: "tool_match", result_type: "numeric", operator: ">=", threshold: 0.9, required: true, critical: false },
      { evaluator_id: "pii_safe", result_type: "numeric", operator: ">=", threshold: 0.8, required: false, critical: true },
    ];
    const comparison = structuredClone(mockComparison) as any;
    comparison.summary.comparable_cohort.baseline.score_means.tool_match = 0.5;
    comparison.summary.comparable_cohort.candidate.score_means.tool_match = 0.9;
    overrideGet({ "/api/v1/experiment-launches/{launch_id}": { data: launch } });
    overrideGet({ "/api/v1/experiment-launches/{launch_id}/comparison": { data: comparison } });

    renderComponent();

    const requiredMetric = await screen.findByText("tool_match", { selector: "td" });
    const criticalMetric = await screen.findByText("pii_safe", { selector: "td" });
    expect(requiredMetric.closest("tr")).toHaveTextContent("必要");
    expect(requiredMetric.closest("tr")).not.toHaveTextContent("关键");
    expect(criticalMetric.closest("tr")).toHaveTextContent("关键");
    expect(criticalMetric.closest("tr")).not.toHaveTextContent("必要");
  });

  it("REVIEW: terminal Launch waits for unresolved snapshot history before showing live Cases", async () => {
    let resolveHistory!: (value: any) => void;
    const pendingHistory = new Promise((resolve) => {
      resolveHistory = resolve;
    });
    overrideGet({ "/api/v1/experiment-launches/{launch_id}/result-snapshots": pendingHistory });

    renderComponent(`/launches/${mockLaunch.id}?tab=cases`);

    await waitFor(() => expect(api.GET).toHaveBeenCalledWith(
      "/api/v1/experiment-launches/{launch_id}/result-snapshots",
      expect.anything(),
    ));
    await screen.findByText("正在加载用例明细与得分...");
    expect(screen.queryByTestId("case-row-expand-item-001")).not.toBeInTheDocument();
    expect(screen.getByTestId("quality-pass-rate")).toHaveTextContent("—");
    expect(screen.getByRole("tab", { name: /用例排查与 Trace/ })).toHaveTextContent("(—)");
    expect(screen.getByText("门禁状态未知 (UNKNOWN)")).toBeInTheDocument();

    await act(async () => resolveHistory({ data: mockSnapshotList }));
    expect(await screen.findByTestId("case-row-expand-item-001")).toBeInTheDocument();
  });

  it("REVIEW: historical snapshot Attempts never use a matching live execution ID", async () => {
    renderComponent(`/launches/${mockLaunch.id}?snapshot_id=snap-v2-001&tab=cases`);

    const attempts = await screen.findAllByRole("button", { name: /次尝试/ });
    expect(attempts.length).toBeGreaterThan(0);
    for (const attempt of attempts) {
      expect(attempt).toBeDisabled();
      expect(attempt).toHaveAttribute("title", "历史快照无实时 Attempt 执行记录");
    }
  });

  it("REVIEW: expanded historical Cases never open a live Attempt timeline", async () => {
    renderComponent(`/launches/${mockLaunch.id}?snapshot_id=snap-v2-001&tab=cases`);
    fireEvent.click(await screen.findByTestId("case-row-expand-item-001"));

    expect(await screen.findByText(/历史快照不包含实时 Attempt 时间线/)).toBeInTheDocument();
    expect(screen.queryByText(/Item Execution ID:/)).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /查看调用历史/ })).not.toBeInTheDocument();
    expect((api.GET as any).mock.calls.filter((call: any[]) => call[0].includes("/attempts"))).toHaveLength(0);
  });

  it.each([
    ["empty string", ""],
    ["false", false],
    ["zero", 0],
  ])("REVIEW: frozen input preserves the %s value", async (_label, input) => {
    const launch = structuredClone(mockLaunch) as any;
    launch.manifest.dataset.items = [{ id: "item-001", input }];
    overrideGet({ "/api/v1/experiment-launches/{launch_id}": { data: launch } });

    renderComponent(`/launches/${mockLaunch.id}?snapshot_id=snap-v2-001&tab=cases`);
    fireEvent.click(await screen.findByTestId("case-row-expand-item-001"));

    const frozenInput = await screen.findByTestId("frozen-input-value");
    expect(frozenInput).toHaveTextContent(input === "" ? '""' : String(input));
    expect(screen.queryByText(/未在快照中嵌入输入/)).not.toBeInTheDocument();
  });

  it("R126: snapshot detail error must disable result download", async () => {
    overrideGet({"/api/v1/experiment-launches/{launch_id}/result-snapshots/{snapshot_id}":{
      error:{detail:"snapshot store unavailable"},response:{status:503}}});
    renderComponent(`/launches/${mockLaunch.id}?snapshot_id=snap-v2-001&tab=audit`);
    const btn=await screen.findByRole("button",{name:"下载原始 JSON"});
    await waitFor(()=>expect(api.GET).toHaveBeenCalledWith(
      "/api/v1/experiment-launches/{launch_id}/result-snapshots/{snapshot_id}",expect.anything()));
    expect(btn).toBeDisabled();
    expect(await screen.findByRole("alert")).toHaveTextContent("snapshot store unavailable");
  });

  it("R126: raw snapshot view exposes detail errors and never falls back to the Launch manifest", async () => {
    overrideGet({"/api/v1/experiment-launches/{launch_id}/result-snapshots/{snapshot_id}":{
      error:{detail:"snapshot store unavailable"},response:{status:503}}});
    renderComponent(`/launches/${mockLaunch.id}?snapshot_id=snap-v2-001&tab=audit`);
    fireEvent.click(await screen.findByRole("button", { name: "查看完整 Manifest JSON" }));

    expect(await screen.findByTestId("snapshot-detail-error")).toHaveTextContent("snapshot store unavailable");
    expect(screen.queryByText("Immutable Manifest JSON")).not.toBeInTheDocument();
  });

  it("R126: invalid audit snapshot must not read latest snapshot detail", async () => {
    overrideGet({"/api/v1/experiment-launches/{launch_id}/summary":{
      error:{detail:"Snapshot WRONG not found"},response:{status:404}}});
    renderComponent(`/launches/${mockLaunch.id}?snapshot_id=WRONG&tab=audit`);
    await screen.findByRole("button",{name:"下载原始 JSON"});
    const detailCalls = (api.GET as any).mock.calls.filter((call: any[]) =>
      call[0] === "/api/v1/experiment-launches/{launch_id}/result-snapshots/{snapshot_id}",
    );
    expect(detailCalls).toHaveLength(0);
  });

  it("R126: clipboard must not report success before its promise resolves", async () => {
    Object.defineProperty(navigator,"clipboard",{configurable:true,value:{writeText:vi.fn(()=>new Promise(()=>{}))}});
    renderComponent(`/launches/${mockLaunch.id}?snapshot_id=snap-v2-001&tab=audit`);
    fireEvent.click(await screen.findByRole("button",{name:"复制 JSON"}));
    expect(screen.queryByRole("button",{name:"已复制 JSON"})).not.toBeInTheDocument();
  });

  it("R126: cancel must require confirmation before issuing the mutation", async () => {
    overrideGet({"/api/v1/experiment-launches/{launch_id}":{data:{...mockLaunch,status:"RUNNING",allowed_actions:["cancel"]}}});
    (api.POST as any).mockResolvedValue({data:{}});
    renderComponent();
    const cancelBtn=await screen.findByRole("button",{name:"取消评测 (Cancel)"});
    await act(async()=>{fireEvent.click(cancelBtn);});
    await waitFor(()=>expect(api.POST).not.toHaveBeenCalled());
    expect(screen.getByRole("dialog")).toBeInTheDocument();
  });

  it("R126: same-snapshot tab roundtrip must preserve expanded Case", async () => {
    renderComponent(`/launches/${mockLaunch.id}?snapshot_id=snap-v2-001&tab=cases`);
    fireEvent.click(await screen.findByTestId("case-row-expand-item-001"));
    await screen.findByText("v2 output masked text");
    fireEvent.click(screen.getByRole("tab",{name:/门禁与版本对比/}));
    fireEvent.click(screen.getByRole("tab",{name:/用例排查与 Trace/}));
    expect(screen.queryByText("v2 output masked text")).toBeInTheDocument();
  });

  it("R126: expanded Case must expose both frozen Baseline and Candidate output", async () => {
    renderComponent(`/launches/${mockLaunch.id}?snapshot_id=snap-v2-001&tab=cases`);
    fireEvent.click(await screen.findByTestId("case-row-expand-item-001"));
    await screen.findByText("v2 output masked text");
    expect(screen.getByText("v1 output text")).toBeInTheDocument();
  });

  it("R126: refresh pending after CAS409 must block resubmission with stale revision", async () => {
    const original=(api.GET as any).getMockImplementation();
    let blocked=false;
    (api.GET as any).mockImplementation((path:string,options:any)=>{
      if(blocked && path==="/api/v1/agents/{agent_id}/baselines")return new Promise(()=>{});
      return original(path,options);
    });
    (api.POST as any).mockImplementation(()=>{blocked=true;return Promise.resolve({error:{detail:"conflict"},response:{status:409}});});
    renderComponent();
    fireEvent.click(await screen.findByRole("button",{name:"设为新 Baseline"}));
    fireEvent.click(screen.getByRole("button",{name:"确认设为 Baseline"}));
    await screen.findByText(/HTTP 409/);
    expect(screen.getByRole("button",{name:"确认设为 Baseline"})).toBeDisabled();
  });

  it("R126: active evaluation recovery must not be hidden as an idle terminal run", () => {
    render(<ExecutionProgressPanel status="COMPLETED" progress={{total:2,percentage:100,pending:0,queued:0,running:0,retry_wait:0,succeeded:2,failed:0,timed_out:0,cancelled:0}} isEvaluating />);
    const grid=document.querySelector('[data-card="running"]')?.parentElement;
    expect(grid).not.toHaveClass("hidden");
  });

  const realFrozenItem = {"dataset_item_id": "item-001", "case_digest": "6728a5061f0dbe8cd75ea2e8d5c2237072a8fc7ea39dec7568bec35e3f0d9908", "execution_status": "succeeded", "eval_status": "succeeded", "quality_conclusion": "fail", "scores": {"correctness": 0.2}, "evaluation_results": [], "quality_evaluation": {"conclusion": "fail", "policy_id": "default", "policy_version": "1.0", "policy_digest": "sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa", "decided_by": "quality_policy", "releasable": false, "unknown_reasons": [], "rules": [{"evaluator_id": "correctness", "result_type": "numeric", "required": true, "critical": true, "operator": ">=", "expected": 0.8, "observed_value": 0.2, "observed_status": "known", "conclusion": "fail", "reason_code": "REQUIRED_RULE_VIOLATED", "explanation": "correctness >= 0.8"}]}, "latency_ms": 123, "usage": {"input_tokens": null, "output_tokens": null, "total_tokens": null}, "cost": {"amount": null, "currency": null, "source": null, "scope": "launch_case_total", "policy_version": "case-cost-v1", "measurement_scope": null, "complete": false, "unavailable_reason": "COST_NOT_RECORDED"}, "cost_evidence": {"policy_version": "case-cost-v1", "attempt_count": 0, "recorded_attempt_count": 0, "dispatch_generations": [], "attempts": []}, "final_attempt_id": null, "dispatch_generation": 1, "trace_id": "trace-71ad74ff-36e3-42e2-be9c-28b27938bba6", "trace_url": "https://cloud.langfuse.com/trace/frozen-s1", "observation_id": "obs-71ad74ff-36e3-42e2-be9c-28b27938bba6", "output_ref": {"trace_id": "trace-71ad74ff-36e3-42e2-be9c-28b27938bba6", "observation_id": "obs-71ad74ff-36e3-42e2-be9c-28b27938bba6"}} as any;

  const seedRealDetail = () => {
    overrideGet({"/api/v1/experiment-launches/{launch_id}/result-snapshots/{snapshot_id}":{data:{
      launch_id:mockLaunch.id,snapshot_id:"snap-v2-001",revision:1,created_at:"2026-10-08T16:08:18Z",
      evidence_state:"COMPLETE",evidence_reasons:[],releasable:true,
      versions:mockSummary.versions,summary:mockSummary.summary,items:[realFrozenItem],
    }}});
  };

  it("RECHECK: explicit invalid Snapshot must not render live Case rows",async()=>{
    overrideGet({"/api/v1/experiment-launches/{launch_id}/summary":{
      error:{detail:"Snapshot WRONG not found"},response:{status:404}},
      "/api/v1/experiment-launches/{launch_id}/comparison":{
      error:{detail:"Snapshot WRONG not found"},response:{status:404}}});
    renderComponent(`/launches/${mockLaunch.id}?snapshot_id=WRONG&tab=cases`);
    await screen.findByText("Snapshot WRONG not found");
    await waitFor(()=>expect(queryClient.getQueryState(queryKeys.launches.items(mockLaunch.id))?.status).toBe("success"));
    expect(screen.queryByTestId("case-row-expand-item-001")).not.toBeInTheDocument();
  });

  it("RECHECK: valid frozen report is independent of live Items API failure",async()=>{
    seedRealDetail();
    overrideGet({"/api/v1/experiment-launches/{launch_id}/items":{
      error:{detail:"live items unavailable"},response:{status:503}}});
    renderComponent(`/launches/${mockLaunch.id}?snapshot_id=snap-v2-001&tab=cases`);
    await waitFor(()=>expect(queryClient.getQueryState([...queryKeys.launches.all,"result-snapshot",mockLaunch.id,"snap-v2-001"])?.status).toBe("success"));
    expect(screen.getByTestId("case-row-expand-item-001")).toBeInTheDocument();
  });

  it("RECHECK: actual frozen trace_url is exposed as a Trace link",async()=>{
    seedRealDetail();
    renderComponent(`/launches/${mockLaunch.id}?snapshot_id=snap-v2-001&tab=cases`);
    const row=await screen.findByTestId("case-row-expand-item-001");
    expect(within(row).getByRole("link",{name:"Trace"})).toHaveAttribute("href",realFrozenItem.trace_url);
  });

  it("RECHECK: Case retry reloads failed Snapshot detail rather than live Items",async()=>{
    const original=(api.GET as any).getMockImplementation();let fail=true;let detailCalls=0;
    (api.GET as any).mockImplementation((path:string,options:any)=>{
      if(path==="/api/v1/experiment-launches/{launch_id}/result-snapshots/{snapshot_id}"){
        detailCalls++;
        return Promise.resolve(fail?{error:{detail:"snapshot detail unavailable"},response:{status:503}}:
          {data:{items:[realFrozenItem],launch_id:mockLaunch.id,snapshot_id:"snap-v2-001"}});
      }
      return original(path,options);
    });
    renderComponent(`/launches/${mockLaunch.id}?snapshot_id=snap-v2-001&tab=cases`);
    await screen.findAllByText("snapshot detail unavailable");fail=false;
    fireEvent.click(screen.getAllByRole("button",{name:"重新加载"})[0]);
    await waitFor(()=>expect(detailCalls).toBeGreaterThan(1));
  });

  it("RECHECK: higher_is_better direction must outrank a latency name heuristic",async()=>{
    const launch=structuredClone(mockLaunch) as any;
    // With no frozen policy rule this assertion isolates direction-based trend semantics.
    launch.manifest.quality_policy.rules=[];
    launch.manifest.evaluators=[{id:"latency_score",version:"1",result_type:"numeric",direction:"higher_is_better"}];
    const c=structuredClone(mockComparison) as any;
    c.summary.comparable_cohort.baseline.score_means={latency_score:0.5};
    c.summary.comparable_cohort.candidate.score_means={latency_score:0.9};
    overrideGet({"/api/v1/experiment-launches/{launch_id}":{data:launch},
      "/api/v1/experiment-launches/{launch_id}/comparison":{data:c}});
    renderComponent();
    const cell=await screen.findByText("latency_score",{selector:"td"});
    expect(within(cell.closest("tr")!).getByText("提升")).toBeInTheDocument();
  });

  it("RECHECK: Comparison classification filter survives same Snapshot tab roundtrip",async()=>{
    renderComponent(`/launches/${mockLaunch.id}?snapshot_id=snap-v2-001&tab=compare`);
    await waitFor(()=>expect(queryClient.getQueryState([...queryKeys.launches.all,"result-snapshot",mockLaunch.id,"snap-v2-001"])?.status).toBe("success"));
    const filter=await screen.findByRole("button",{name:/^REGRESSION /});fireEvent.click(filter);
    await waitFor(()=>expect(screen.getByRole("button",{name:/^REGRESSION /})).toHaveAttribute("aria-pressed","true"));
    fireEvent.click(screen.getByRole("tab",{name:/用例排查与 Trace/}));
    fireEvent.click(screen.getByRole("tab",{name:/门禁与版本对比/}));
    expect(await screen.findByRole("button",{name:/^REGRESSION /})).toHaveAttribute("aria-pressed","true");
  });

  it("RECHECK: Clipboard rejection must show an actionable failure",async()=>{
    const write=vi.fn().mockRejectedValue(new Error("clipboard denied"));
    Object.defineProperty(navigator,"clipboard",{configurable:true,value:{writeText:write}});
    renderComponent(`/launches/${mockLaunch.id}?snapshot_id=snap-v2-001&tab=audit`);
    const btn=await screen.findByRole("button",{name:"复制 JSON"});await waitFor(()=>expect(btn).toBeEnabled());
    await act(async()=>{fireEvent.click(btn);});
    expect(write).toHaveBeenCalledOnce();
    expect(screen.getByRole("button",{name:"复制失败"})).toHaveAttribute("title","剪贴板写入失败，请检查浏览器权限");
  });

  it("RECHECK: failed Baseline refresh after409 must allow aborting the modal",async()=>{
    const original=(api.GET as any).getMockImplementation();let refreshFail=false;
    (api.GET as any).mockImplementation((path:string,options:any)=>{
      if(refreshFail&&path==="/api/v1/agents/{agent_id}/baselines")return Promise.resolve({error:{detail:"baseline unavailable"},response:{status:503}});
      return original(path,options);
    });
    (api.POST as any).mockImplementation(()=>{refreshFail=true;return Promise.resolve({error:{detail:"conflict"},response:{status:409}});});
    renderComponent();
    fireEvent.click(await screen.findByRole("button",{name:"设为新 Baseline"}));
    const confirm=screen.getByRole("button",{name:"确认设为 Baseline"});await waitFor(()=>expect(confirm).toBeEnabled());
    fireEvent.click(confirm);await screen.findByText(/HTTP 409/);
    await waitFor(()=>expect(queryClient.getQueryState(queryKeys.baselines.detail("banking-agent","production"))?.status).toBe("error"));
    expect(within(screen.getByRole("dialog")).getByRole("button",{name:"取消"})).toBeEnabled();
  });

  it("RECHECK: retryable Baseline output failure has its own retry entry",async()=>{
    const original=(api.GET as any).getMockImplementation();
    (api.GET as any).mockImplementation((path:string,options:any)=>{
      if(path==="/api/v1/experiment-launches/{launch_id}/comparison/case")return Promise.resolve({data:{
        launch_id:mockLaunch.id,candidate_snapshot_id:"snap-v2-001",dataset_item_id:"item-001",classification:"UNKNOWN",
        baseline:{output_status:"FETCH_FAILED",retryable:true,reason:"baseline fetch unavailable"},
        candidate:{output_status:"AVAILABLE",output:"candidate works",retryable:false},
      }});
      return original(path,options);
    });
    renderComponent(`/launches/${mockLaunch.id}?snapshot_id=snap-v2-001&tab=cases`);
    fireEvent.click(await screen.findByTestId("case-row-expand-item-001"));await screen.findByText("candidate works");
    expect(screen.getByRole("button",{name:/重试.*输出|重试读取/})).toBeInTheDocument();
  });

  it("RECHECK: UUID copy must not display success before Clipboard completes",async()=>{
    const write=vi.fn().mockImplementation(()=>new Promise<void>(()=>{}));
    Object.defineProperty(navigator,"clipboard",{configurable:true,value:{writeText:write}});
    renderComponent();await screen.findByText("banking-agent-v2");
    const button=screen.getByTitle(`点击复制完整 ID: ${mockLaunch.id}`);
    fireEvent.click(button);expect(write).toHaveBeenCalledWith(mockLaunch.id);
    expect(button.querySelector(".lucide-check")).toBeNull();
  });


  it("EXTRA: explicit null frozen evidence never borrows live Trace or latency",async()=>{
    const original=(api.GET as any).getMockImplementation();
    (api.GET as any).mockImplementation(async(path:string,options:any)=>{
      if(path==="/api/v1/experiment-launches/{launch_id}/result-snapshots/{snapshot_id}")return {data:{launch_id:mockLaunch.id,snapshot_id:"snap-v2-001",items:[{...realFrozenItem,trace_url:null,trace_id:null,latency_ms:null,final_attempt_id:null}]}};
      const res=await original(path,options);
      if(path==="/api/v1/experiment-launches/{launch_id}/items")return {data:res.data.map((i:any)=>({...i,langfuse_trace_url:"https://cloud.langfuse.com/trace/LIVE-S2",final_attempt_latency_ms:777,final_attempt_id:"LIVE-S2-final"}))};
      return res;
    });
    renderComponent(`/launches/${mockLaunch.id}?snapshot_id=snap-v2-001&tab=cases`);
    await waitFor(()=>expect(queryClient.getQueryState(queryKeys.launches.items(mockLaunch.id))?.status).toBe("success"));
    const row=await screen.findByTestId("case-row-expand-item-001");
    expect(within(row).queryByRole("link",{name:"Trace"})).not.toBeInTheDocument();
    expect(row).not.toHaveTextContent("777");
  });

  it("EXTRA: CAS conflict refresh retains non-production environment query",async()=>{
    renderComponent();await screen.findByText("banking-agent-v2");
    (api.GET as any).mockClear();(api.POST as any).mockResolvedValue({error:{detail:"conflict"},response:{status:409}});
    render(<QueryClientProvider client={queryClient}><SetBaselineModal open onClose={vi.fn()} agentId="banking-agent" environment="staging" activeSnapshot={{snapshot_id:"snap-v2-001",revision:1,evidence_state:"COMPLETE"} as any} activeBaseline={{revision:4,result_snapshot_id:"staging-base"} as any}/></QueryClientProvider>);
    fireEvent.click(screen.getByRole("button",{name:"确认设为 Baseline"}));
    await waitFor(()=>expect((api.GET as any).mock.calls.some((c:any)=>c[0]==="/api/v1/agents/{agent_id}/baselines")).toBe(true));
    const refresh=(api.GET as any).mock.calls.filter((c:any)=>c[0]==="/api/v1/agents/{agent_id}/baselines").at(-1);
    expect(refresh[1].params.query).toEqual({environment:"staging"});
  });

  it("EXTRA: transient non409 Baseline POST error allows an explicit retry",async()=>{
    (api.POST as any).mockResolvedValue({error:{detail:"baseline temporarily unavailable"},response:{status:503}});
    renderComponent();fireEvent.click(await screen.findByRole("button",{name:"设为新 Baseline"}));
    const confirm=screen.getByRole("button",{name:"确认设为 Baseline"});await waitFor(()=>expect(confirm).toBeEnabled());fireEvent.click(confirm);
    await screen.findByText("baseline temporarily unavailable");
    expect(confirm).toBeEnabled();
  });

  it("EXTRA: missing direction on call_cost is neutral instead of invented improvement",async()=>{
    const launch=structuredClone(mockLaunch) as any;
    launch.manifest.evaluators=[{id:"call_cost",version:"1",result_type:"numeric"}];launch.manifest.quality_policy.rules=[];
    const c=structuredClone(mockComparison) as any;
    c.summary.comparable_cohort.baseline.score_means={call_cost:0.5};
    c.summary.comparable_cohort.candidate.score_means={call_cost:0.2};
    overrideGet({"/api/v1/experiment-launches/{launch_id}":{data:launch},"/api/v1/experiment-launches/{launch_id}/comparison":{data:c}});
    renderComponent(`/launches/${mockLaunch.id}?snapshot_id=snap-v2-001&tab=compare`);
    const name=await screen.findByText("call_cost");
    expect(name.closest("tr")).toHaveTextContent("变化");
    expect(name.closest("tr")).not.toHaveTextContent("提升");
  });

  it("EXTRA: snapshot history503 is an actionable error, never a no-snapshots empty state",async()=>{
    overrideGet({"/api/v1/experiment-launches/{launch_id}/result-snapshots":{error:{detail:"history temporarily unavailable"},response:{status:503}}});
    renderComponent(`/launches/${mockLaunch.id}?snapshot_id=snap-v2-001&tab=cases`);
    await waitFor(()=>expect(queryClient.getQueryState([...queryKeys.launches.all,"result-snapshots",mockLaunch.id])?.status).toBe("error"));
    expect(screen.getByText("history temporarily unavailable")).toBeInTheDocument();
    expect(screen.queryByTestId("result-snapshot-empty")).not.toBeInTheDocument();
  });
  it("EXTRA: successful copy followed by rejection must clear old success feedback",async()=>{
    const write=vi.fn().mockResolvedValueOnce(undefined).mockRejectedValueOnce(new Error("clipboard denied"));
    Object.defineProperty(navigator,"clipboard",{configurable:true,value:{writeText:write}});
    renderComponent(`/launches/${mockLaunch.id}?snapshot_id=snap-v2-001&tab=audit`);
    const first=await screen.findByRole("button",{name:"复制 JSON"});await waitFor(()=>expect(first).toBeEnabled());
    await act(async()=>{fireEvent.click(first);});const copied=screen.getByRole("button",{name:"已复制 JSON"});
    await act(async()=>{fireEvent.click(copied);});expect(write).toHaveBeenCalledTimes(2);
    expect(screen.queryByRole("button",{name:"已复制 JSON"})).not.toBeInTheDocument();
    expect(screen.getByRole("button",{name:"复制失败"})).toBeInTheDocument();
  });

  it("FOURTH: wrong Snapshot identity is an actionable error rather than an empty Cases list",async()=>{
    overrideGet({"/api/v1/experiment-launches/{launch_id}/result-snapshots/{snapshot_id}":{data:{launch_id:mockLaunch.id,snapshot_id:"OTHER-SNAPSHOT",items:[realFrozenItem]}}});
    renderComponent(`/launches/${mockLaunch.id}?snapshot_id=snap-v2-001&tab=cases`);
    await waitFor(()=>expect(queryClient.getQueryState([...queryKeys.launches.all,"result-snapshot",mockLaunch.id,"snap-v2-001"])?.status).toBe("error"));
    expect(screen.getByText(/快照 ID 不一致/)).toBeInTheDocument();
  });

  it("FOURTH: missing items on Snapshot detail is an actionable contract error",async()=>{
    overrideGet({"/api/v1/experiment-launches/{launch_id}/result-snapshots/{snapshot_id}":{data:{launch_id:mockLaunch.id,snapshot_id:"snap-v2-001"}}});
    renderComponent(`/launches/${mockLaunch.id}?snapshot_id=snap-v2-001&tab=cases`);
    await waitFor(()=>expect(queryClient.getQueryState([...queryKeys.launches.all,"result-snapshot",mockLaunch.id,"snap-v2-001"])?.status).toBe("error"));
    expect(screen.getByText(/items 字段缺失或非数组/)).toBeInTheDocument();
  });

  it("FOURTH: invalid Snapshot identity must disable raw export in Audit",async()=>{
    overrideGet({"/api/v1/experiment-launches/{launch_id}/result-snapshots/{snapshot_id}":{data:{launch_id:mockLaunch.id,snapshot_id:"OTHER-SNAPSHOT",items:[realFrozenItem]}}});
    renderComponent(`/launches/${mockLaunch.id}?snapshot_id=snap-v2-001&tab=audit`);
    await waitFor(()=>expect(queryClient.getQueryState([...queryKeys.launches.all,"result-snapshot",mockLaunch.id,"snap-v2-001"])?.status).toBe("error"));
    expect(screen.getByRole("button",{name:"下载原始 JSON"})).toBeDisabled();
  });

  it("FOURTH: Snapshot identity fields are mandatory rather than optional checks",()=>{
    expect(validateSnapshotDetail({items:[]},mockLaunch.id,"snap-v2-001").isValid).toBe(false);
  });

  it("FOURTH: running-to-terminal automatically discovers newly frozen Snapshot history",async()=>{
    const original=(api.GET as any).getMockImplementation();let terminal=false,historyCalls=0;
    (api.GET as any).mockImplementation((path:string,options:any)=>{
      if(path==="/api/v1/experiment-launches/{launch_id}")return Promise.resolve({data:{...mockLaunch,status:terminal?"COMPLETED":"RUNNING"}});
      if(path==="/api/v1/experiment-launches/{launch_id}/result-snapshots"){
        historyCalls++;return Promise.resolve(terminal?{data:mockSnapshotList}:{data:{revisions:[],latest_snapshot_id:null,latest_revision:0}});
      }
      return original(path,options);
    });
    renderComponent();await screen.findByTestId("result-snapshot-empty");
    const before=historyCalls;terminal=true;await act(async()=>{await queryClient.refetchQueries({queryKey:queryKeys.launches.detail(mockLaunch.id),exact:true});});
    await waitFor(()=>expect(queryClient.getQueryData<any>(queryKeys.launches.detail(mockLaunch.id))?.status).toBe("COMPLETED"));
    await waitFor(()=>expect(historyCalls).toBeGreaterThan(before));
    await waitFor(()=>expect(screen.queryByTestId("result-snapshot-empty")).not.toBeInTheDocument());
  });

  it("FOURTH: late Baseline refresh failure from a closed dialog cannot poison a reopened session",async()=>{
    let resolveRefresh:any;
    (api.GET as any).mockImplementation(()=>new Promise(r=>{resolveRefresh=r;}));
    (api.POST as any).mockResolvedValue({error:{detail:"conflict"},response:{status:409}});
    const onClose=vi.fn();const props={onClose,agentId:"banking-agent",environment:"production",activeSnapshot:{snapshot_id:"snap-v2-001",revision:1,evidence_state:"COMPLETE"} as any,activeBaseline:{agent_id:"banking-agent",environment:"production",revision:4,result_snapshot_id:"base"} as any};
    const ui=(open:boolean)=><QueryClientProvider client={queryClient}><SetBaselineModal open={open} {...props}/></QueryClientProvider>;
    const view=render(ui(true));fireEvent.click(screen.getByRole("button",{name:"确认设为 Baseline"}));await screen.findByText(/HTTP 409/);
    expect(resolveRefresh).toBeTypeOf("function");view.rerender(ui(false));view.rerender(ui(true));
    await waitFor(()=>expect(screen.getByRole("button",{name:"确认设为 Baseline"})).toBeEnabled());
    await act(async()=>{resolveRefresh({error:{detail:"OLD-SESSION-refresh unavailable"},response:{status:503}});});
    expect(screen.getByRole("button",{name:"确认设为 Baseline"})).toBeEnabled();
    expect(screen.queryByText(/OLD-SESSION-refresh/)).not.toBeInTheDocument();
  });

  it("FOURTH: Baseline refresh rejects a different Agent even when environment matches",async()=>{
    (api.GET as any).mockResolvedValue({data:{agent_id:"OTHER-AGENT",environment:"production",revision:99,result_snapshot_id:"foreign-base"}});
    (api.POST as any).mockResolvedValue({error:{detail:"conflict"},response:{status:409}});
    render(<QueryClientProvider client={queryClient}><SetBaselineModal open onClose={vi.fn()} agentId="banking-agent" environment="production" activeSnapshot={{snapshot_id:"snap-v2-001",revision:1,evidence_state:"COMPLETE"} as any} activeBaseline={{agent_id:"banking-agent",environment:"production",revision:4,result_snapshot_id:"base"} as any}/></QueryClientProvider>);
    fireEvent.click(screen.getByRole("button",{name:"确认设为 Baseline"}));await screen.findByText(/HTTP 409/);
    await waitFor(()=>expect((api.GET as any)).toHaveBeenCalled());
    expect(queryClient.getQueryData<any>(queryKeys.baselines.detail("banking-agent","production"))?.agent_id).not.toBe("OTHER-AGENT");
  });

  it("FOURTH: late Clipboard success for S1 must not be displayed as S2 copy success",async()=>{
    const original=(api.GET as any).getMockImplementation();
    (api.GET as any).mockImplementation((path:string,options:any)=>{
      if(path==="/api/v1/experiment-launches/{launch_id}/result-snapshots/{snapshot_id}")return Promise.resolve({data:{launch_id:mockLaunch.id,snapshot_id:options.params.path.snapshot_id,items:[realFrozenItem]}});
      return original(path,options);
    });
    let finish:any;Object.defineProperty(navigator,"clipboard",{configurable:true,value:{writeText:vi.fn(()=>new Promise<void>(r=>{finish=r;}))}});
    const ui=(sid:string)=><QueryClientProvider client={queryClient}><ManifestAuditTab launch={mockLaunch as any} activeSnapshot={{...mockSnapshotList.revisions[0],snapshot_id:sid} as any}/></QueryClientProvider>;
    const view=render(ui("snap-v2-001"));const copy=await screen.findByRole("button",{name:"复制 JSON"});await waitFor(()=>expect(copy).toBeEnabled());fireEvent.click(copy);
    view.rerender(ui("S2"));await waitFor(()=>expect(queryClient.getQueryState([...queryKeys.launches.all,"result-snapshot",mockLaunch.id,"S2"])?.status).toBe("success"));
    await act(async()=>{finish();});
    expect(screen.queryByRole("button",{name:"已复制 JSON"})).not.toBeInTheDocument();
  });


  it("REVIEW: loads, copies, and downloads an explicit snapshot when history is unavailable", async () => {
    const snapshotId = "pinned-without-history";
    const detail = {
      launch_id: mockLaunch.id,
      snapshot_id: snapshotId,
      manifest: mockLaunch.manifest,
      summary: mockSummary.summary,
      items: [realFrozenItem],
    };
    overrideGet({
      "/api/v1/experiment-launches/{launch_id}/result-snapshots": {
        error: { detail: "snapshot history unavailable" },
        response: { status: 503 },
      },
      "/api/v1/experiment-launches/{launch_id}/result-snapshots/{snapshot_id}": { data: detail },
    });
    const writeText = vi.fn().mockResolvedValue(undefined);
    Object.defineProperty(navigator, "clipboard", {
      configurable: true,
      value: { writeText },
    });
    const clipboardDescriptor = Object.getOwnPropertyDescriptor(navigator, "clipboard");
    const createUrlDescriptor = Object.getOwnPropertyDescriptor(URL, "createObjectURL");
    const revokeUrlDescriptor = Object.getOwnPropertyDescriptor(URL, "revokeObjectURL");
    const createObjectURL = vi.fn(() => "blob:pinned-snapshot");
    const revokeObjectURL = vi.fn();
    Object.defineProperty(URL, "createObjectURL", { configurable: true, value: createObjectURL });
    Object.defineProperty(URL, "revokeObjectURL", { configurable: true, value: revokeObjectURL });
    let downloadedName = "";
    const anchorClick = vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(function (this: HTMLAnchorElement) {
      downloadedName = this.download;
    });

    try {
      renderComponent(`/launches/${mockLaunch.id}?snapshot_id=${snapshotId}&tab=audit`);
      fireEvent.click(await screen.findByRole("tab", { name: /不可变快照与审计/ }));
      const auditPanel = await screen.findByRole("tabpanel", { name: /不可变快照与审计/ });
      const copy = await within(auditPanel).findByRole("button", { name: "复制 JSON" });
      await waitFor(() => expect(copy).toBeEnabled());
      fireEvent.click(copy);
      await screen.findByRole("button", { name: "已复制 JSON" });
      expect(writeText).toHaveBeenCalledWith(JSON.stringify(detail, null, 2));

      fireEvent.click(within(auditPanel).getByRole("button", { name: "下载原始 JSON" }));
      expect(createObjectURL).toHaveBeenCalledTimes(1);
      expect(anchorClick).toHaveBeenCalledTimes(1);
      expect(downloadedName).toBe(`snapshot-${mockLaunch.id}-${snapshotId}.json`);
      expect(revokeObjectURL).toHaveBeenCalledWith("blob:pinned-snapshot");

      fireEvent.click(within(auditPanel).getByRole("button", { name: "查看完整 Manifest JSON" }));
      expect(await within(auditPanel).findByText("Immutable Manifest JSON")).toBeInTheDocument();
      expect(within(auditPanel).getByTestId("snapshot-id")).toHaveTextContent(snapshotId);
      expect(within(auditPanel).queryByTestId("snapshot-detail-error")).not.toBeInTheDocument();
    } finally {
      anchorClick.mockRestore();
      if (createUrlDescriptor) Object.defineProperty(URL, "createObjectURL", createUrlDescriptor);
      else delete (URL as any).createObjectURL;
      if (revokeUrlDescriptor) Object.defineProperty(URL, "revokeObjectURL", revokeUrlDescriptor);
      else delete (URL as any).revokeObjectURL;
      if (clipboardDescriptor) Object.defineProperty(navigator, "clipboard", clipboardDescriptor);
      else delete (navigator as any).clipboard;
    }
  });
});
