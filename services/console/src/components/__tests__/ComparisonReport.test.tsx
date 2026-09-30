import { beforeEach, describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter, Route, Routes, useLocation, useNavigate, useParams } from "react-router-dom";
import { ComparisonReport } from "../../features/launches/ComparisonReport";
import { api } from "../../api/client";

vi.mock("../../api/client", () => ({
  api: {
    GET: vi.fn(),
    POST: vi.fn(),
  },
}));

const summaryFor = (snapshotId: string, launchId = "candidate-launch") => ({
  launch_id: launchId,
  snapshot_id: snapshotId,
  revision: snapshotId.endsWith("new") ? 2 : 1,
  created_at: "2026-09-28T00:00:00Z",
  manifest_digest: "manifest-digest",
  versions: {
    agent: { id: "banking-agent", version: "2.4.0" },
    dataset: { name: "banking-golden", version: "2026-09" },
    evaluators: [{ id: "correctness", version: "1.0.0" }],
    runner: { runner_version: "0.2.0", build_id: "commit-abc123" },
    environment: "production",
  },
  summary: {
    pass_rate: 0.8,
    evaluation_coverage: 0.8,
    total_cases: 10,
    evaluated_cases: 8,
    critical_failure_count: 0,
    execution_error_count: 1,
    execution_error_rate: 0.1,
    evaluator_error_count: 1,
    p95_latency_ms: 120,
    cost_per_case: null,
  },
  langfuse_score_sync_status: "SYNCED",
});

const metrics = (overrides: Record<string, unknown> = {}) => ({
  pass_rate: 1,
  evaluation_coverage: 1,
  total_cases: 10,
  evaluated_cases: 10,
  critical_failure_count: 0,
  execution_error_count: 0,
  execution_error_rate: 0,
  evaluator_error_count: 0,
  p95_latency_ms: 120,
  cost_per_case: 0.021,
  total_cost: 0.21,
  cost_currency: "USD",
  cost_case_count: 10,
  cost_coverage: 1,
  cost_unavailable_reason: null,
  score_means: { correctness: 0.9 },
  ...overrides,
});

const comparisonPage = (snapshotId: string, caseId: string, nextCursor: number | null, launchId = "candidate-launch"): any => ({
  launch_id: launchId,
  candidate_snapshot_id: snapshotId,
  baseline_snapshot_id: "baseline-snapshot",
  baseline_binding_revision: 3,
  versions: {
    candidate: summaryFor(snapshotId).versions,
    baseline: {
      ...summaryFor(snapshotId).versions,
      agent: { id: "banking-agent", version: "2.3.0" },
    },
  },
  summary: {
    comparable_case_count: 8,
    baseline: metrics(),
    candidate: metrics({
      pass_rate: 0.8,
      evaluation_coverage: 0.8,
      evaluated_cases: 8,
      execution_error_count: 1,
      execution_error_rate: 0.1,
      evaluator_error_count: 1,
    }),
    comparable_cohort: {
      baseline: metrics({ cost_per_case: 0.021, total_cost: 0.168, cost_case_count: 8, total_cases: 8, cost_coverage: 1 }),
      candidate: metrics({ pass_rate: 0.8, score_means: { correctness: 0.8 }, cost_per_case: 0.017, total_cost: 0.136, cost_case_count: 8, total_cases: 8, cost_coverage: 1 }),
    },
    cost_comparison: {
      status: "COMPARABLE", reason: null, cohort: "quality_comparable_cases", case_count: 8,
      currency: "USD", baseline_cost_per_case: 0.021, candidate_cost_per_case: 0.017,
      delta: -0.004, baseline_coverage: 1, candidate_coverage: 1,
    },
  },
  classification_counts: { REGRESSION: 1, IMPROVEMENT: 1, UNCHANGED: 0, NOT_COMPARABLE: 0 },
  items: [{
    dataset_item_id: caseId,
    classification: "REGRESSION",
    reason: "SCORE_CHANGED",
    baseline_scores: { correctness: 0.9 },
    candidate_scores: { correctness: 0.7 },
    score_deltas: { correctness: -0.2 },
    baseline_trace_url: "https://langfuse.example/trace-baseline",
    candidate_trace_url: "https://langfuse.example/trace-candidate",
    baseline_experiment_url: "https://langfuse.example/experiment-baseline",
    candidate_experiment_url: "https://langfuse.example/experiment-candidate",
  }],
  next_cursor: nextCursor,
});

const caseOutput = (snapshotId: string, launchId = "candidate-launch") => ({
  launch_id: launchId,
  candidate_snapshot_id: snapshotId,
  baseline_snapshot_id: "baseline-snapshot",
  dataset_item_id: "case-1",
  classification: "REGRESSION",
  reason: "SCORE_CHANGED",
  baseline: { output_status: "AVAILABLE", output: { answer: "base" }, scores: { correctness: 0.9 }, retryable: false, truncated: false, trace_url: "https://langfuse.example/trace-baseline" },
  candidate: { output_status: "FETCH_FAILED", output: null, reason: "UPSTREAM_ERROR", scores: { correctness: 0.7 }, retryable: true, truncated: false, trace_url: "https://langfuse.example/trace-candidate" },
});

const ComparisonReportRoute = ({ launchStatus }: { launchStatus: string }) => {
  const { launchId = "" } = useParams();
  const location = useLocation();
  const navigate = useNavigate();
  const goTo = (path: string) => {
    window.history.replaceState(window.history.state, "", path);
    navigate(path);
  };

  return (
    <>
      <output data-testid="router-location">{location.pathname}{location.search}</output>
      <button type="button" onClick={() => goTo("/launches/other-launch?snapshot_id=other-snapshot")}>
        切换到其他 Launch
      </button>
      <button type="button" onClick={() => goTo("/launches/candidate-launch?snapshot_id=history-snapshot")}>
        切换到同 Launch 历史修订
      </button>
      <ComparisonReport launchId={launchId} launchStatus={launchStatus} environment="production" />
    </>
  );
};

describe("ComparisonReport", () => {
  let queryClient: QueryClient;
  let latestSummaryReads: number;
  let costReason: string | null;
  let fullRunCostUnavailable: boolean;

  beforeEach(() => {
    queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    vi.clearAllMocks();
    latestSummaryReads = 0;
    costReason = null;
    fullRunCostUnavailable = false;
    window.history.replaceState({}, "", "/launches/candidate-launch");
    (api.GET as any).mockImplementation((path: string, options: any) => {
      const launchId = options.params.path.launch_id;
      if (path.endsWith("/summary")) {
        const requested = options.params.query.snapshot_id;
        if (requested) return Promise.resolve({ data: summaryFor(requested, launchId) });
        latestSummaryReads += 1;
        return Promise.resolve({ data: summaryFor(
          latestSummaryReads > 1 ? "candidate-snapshot-new" : "candidate-snapshot",
          launchId,
        ) });
      }
      if (path.includes("/baselines")) {
        return Promise.resolve({ error: {}, response: { status: 404 } });
      }
      if (path.endsWith("/comparison/case")) {
        return Promise.resolve({ data: caseOutput(options.params.query.snapshot_id, launchId) });
      }
      if (path.endsWith("/comparison")) {
        const { cursor, snapshot_id: snapshotId } = options.params.query;
        const page = cursor === 0
          ? comparisonPage(snapshotId, "case-1", 1, launchId)
          : comparisonPage(snapshotId, "case-2", null, launchId);
        if (fullRunCostUnavailable) {
          for (const run of [page.summary.baseline, page.summary.candidate]) {
            run.total_cost = null;
            run.cost_per_case = null;
            run.cost_currency = null;
            run.cost_unavailable_reason = "MIXED_CURRENCIES";
          }
        }
        if (costReason) {
          page.summary.cost_comparison = { ...page.summary.cost_comparison, status: "NOT_COMPARABLE", reason: costReason, delta: null };
          if (costReason === "PARTIAL_COST_COVERAGE") {
            page.summary.comparable_cohort.candidate.cost_case_count = 4;
            page.summary.comparable_cohort.candidate.cost_coverage = 0.5;
            page.summary.cost_comparison.candidate_coverage = 0.5;
          }
          if (costReason === "COST_NOT_RECORDED") {
            page.summary.comparable_cohort.candidate.cost_per_case = null;
            page.summary.comparable_cohort.candidate.cost_case_count = 0;
            page.summary.comparable_cohort.candidate.cost_coverage = 0;
            page.summary.cost_comparison.candidate_cost_per_case = null;
            page.summary.cost_comparison.candidate_coverage = 0;
          }
        }
        return Promise.resolve({ data: page });
      }
      return Promise.resolve({ data: null });
    });
    (api.POST as any).mockResolvedValue({ data: { revision: 4 } });
  });

  const renderReport = (launchStatus = "COMPLETED") => render(
    <MemoryRouter initialEntries={[`${window.location.pathname}${window.location.search}`]}>
      <QueryClientProvider client={queryClient}>
        <Routes>
          <Route path="/launches/:launchId" element={<ComparisonReportRoute launchStatus={launchStatus} />} />
        </Routes>
      </QueryClientProvider>
    </MemoryRouter>,
  );

  it("labels the comparable-cohort ratio as quality pass rate and explains its denominator", async () => {
    // Issue #45: the comparable-cohort ratio is a quality metric over the shared
    // comparable cases, not the live all-cases quality ratio shown on the detail
    // header and not an execution success rate. It must say so explicitly.
    renderReport();

    await waitFor(() => {
      expect(screen.getByText("共同可比样本质量（8 个 Case）")).toBeInTheDocument();
    });

    // The cohort table row is renamed from the ambiguous "Pass Rate".
    const cohortTable = screen.getByRole("table", { name: "Baseline 与 Candidate 聚合指标对比" });
    expect(within(cohortTable).getByText("质量通过率 (Quality Pass Rate)")).toBeInTheDocument();
    expect(within(cohortTable).queryByText("Pass Rate")).not.toBeInTheDocument();

    // Values stay exactly as the API reported them.
    expect(within(cohortTable).getByText("100.0%")).toBeInTheDocument();
    expect(within(cohortTable).getByText("80.0%")).toBeInTheDocument();

    // The denominator rule is stated in always-visible text.
    const help = screen.getByTestId("comparable-quality-pass-rate-help");
    expect(help).toHaveTextContent("仅统计双方共同可比样本");
    expect(help).toHaveTextContent("执行成功、评测成功且质量结论为 PASS 或 FAIL");
    expect(help).toHaveTextContent("不可比或无有效质量结论的用例不参与该比例");
    expect(help).toHaveTextContent("全量运行健康指标");
  });

  it("does not render a fabricated 0% when the comparable cohort has no evaluable case", async () => {
    // aggregate_run returns pass_rate=null when no case has a comparable quality
    // verdict. That must stay visibly absent, never collapse to 0%.
    (api.GET as any).mockImplementation((path: string, options: any) => {
      const launchId = options.params.path.launch_id;
      if (path.endsWith("/summary")) {
        return Promise.resolve({ data: summaryFor("candidate-snapshot", launchId) });
      }
      if (path.includes("/baselines")) {
        return Promise.resolve({ error: {}, response: { status: 404 } });
      }
      if (path.endsWith("/comparison")) {
        const page = comparisonPage(options.params.query.snapshot_id, "case-1", null, launchId);
        return Promise.resolve({
          data: {
            ...page,
            summary: {
              ...page.summary,
              comparable_case_count: 0,
              comparable_cohort: {
                baseline: metrics({ pass_rate: null, evaluated_cases: 0 }),
                candidate: metrics({ pass_rate: null, evaluated_cases: 0 }),
              },
            },
          },
        });
      }
      return Promise.resolve({ data: null });
    });

    renderReport();

    await waitFor(() => {
      expect(screen.getByText("共同可比样本质量（0 个 Case）")).toBeInTheDocument();
    });

    const cohortTable = screen.getByRole("table", { name: "Baseline 与 Candidate 聚合指标对比" });
    const passRateRow = within(cohortTable).getByText("质量通过率 (Quality Pass Rate)").closest("tr");
    expect(passRateRow).not.toBeNull();
    // The absent value renders as a placeholder, never as a measured zero.
    expect(passRateRow).toHaveTextContent("—");
    expect(passRateRow).not.toHaveTextContent("0.0%");
  });

  it("shows the no-comparable-sample notice instead of an empty quality table", async () => {
    (api.GET as any).mockImplementation((path: string, options: any) => {
      const launchId = options.params.path.launch_id;
      if (path.endsWith("/summary")) {
        return Promise.resolve({ data: summaryFor("candidate-snapshot", launchId) });
      }
      if (path.includes("/baselines")) {
        return Promise.resolve({ error: {}, response: { status: 404 } });
      }
      if (path.endsWith("/comparison")) {
        const page = comparisonPage(options.params.query.snapshot_id, "case-1", null, launchId);
        return Promise.resolve({
          data: {
            ...page,
            summary: {
              ...page.summary,
              comparable_case_count: 0,
              comparable_cohort: null,
            },
          },
        });
      }
      return Promise.resolve({ data: null });
    });

    renderReport();

    await waitFor(() => {
      expect(screen.getByText("无可比样本，质量差异未计算。")).toBeInTheDocument();
    });
    expect(
      screen.queryByRole("table", { name: "Baseline 与 Candidate 聚合指标对比" })
    ).not.toBeInTheDocument();
    // The full-run health table is independent of cohort comparability.
    expect(
      screen.getByRole("table", { name: "Baseline 与 Candidate 全量运行健康指标" })
    ).toBeInTheDocument();
  });

  it("pins the first revision in the URL and sends it on every comparison page", async () => {
    renderReport();

    expect(await screen.findByRole("table", { name: "Baseline 与 Candidate 聚合指标对比" })).toBeInTheDocument();
    await waitFor(() => expect(screen.getByTestId("router-location")).toHaveTextContent(
      "/launches/candidate-launch?snapshot_id=candidate-snapshot",
    ));
    expect(screen.getByText("banking-agent@2.3.0")).toBeInTheDocument();
    expect(screen.getByText("banking-agent@2.4.0")).toBeInTheDocument();
    expect(within(screen.getByRole("table", { name: "Baseline 与 Candidate 聚合指标对比" })).getByText("-20.0 pp")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: /Baseline Experiment/ })).toHaveAttribute(
      "href",
      "https://langfuse.example/experiment-baseline",
    );
    expect(screen.getByRole("link", { name: /Candidate Trace/ })).toHaveAttribute(
      "href",
      "https://langfuse.example/trace-candidate",
    );
    expect(screen.getByRole("table", { name: "Baseline 与 Candidate 全量运行健康指标" })).toHaveTextContent("80.0%");
    expect(screen.getByRole("table", { name: "Baseline 与 Candidate 全量运行健康指标" })).toHaveTextContent("1");
    const aggregateTable = screen.getByRole("table", { name: "Baseline 与 Candidate 聚合指标对比" });
    expect(within(aggregateTable).getByRole("row", { name: /Cost \/ Case/ })).toHaveTextContent("$0.021");
    expect(within(aggregateTable).getByRole("row", { name: /Cost \/ Case/ })).toHaveTextContent("$0.017");
    expect(within(aggregateTable).getByRole("row", { name: /Cost \/ Case/ })).toHaveTextContent("-$0.004");
    expect(within(aggregateTable).getByRole("row", { name: /Cost Coverage/ })).toHaveTextContent("8/8 (100.0%)");

    fireEvent.click(await screen.findByRole("button", { name: "加载更多用例（已显示 1 条）" }));
    expect(await screen.findByText("case-2")).toBeInTheDocument();
    await waitFor(() => {
      const comparisonCalls = (api.GET as any).mock.calls.filter((call: any[]) => call[0].endsWith("/comparison"));
      expect(comparisonCalls.length).toBeGreaterThanOrEqual(2);
      expect(comparisonCalls.every((call: any[]) => call[1].params.query.snapshot_id === "candidate-snapshot")).toBe(true);
    });
  });


  it("does not invent a cost delta when coverage is partial and explains why", async () => {
    costReason = "PARTIAL_COST_COVERAGE";
    renderReport();
    const aggregateTable = await screen.findByRole("table", { name: "Baseline 与 Candidate 聚合指标对比" });
    const row = within(aggregateTable).getByRole("row", { name: /Cost \/ Case/ });
    expect(row).toHaveTextContent("$0.021");
    expect(row).toHaveTextContent("$0.017");
    expect(row).toHaveTextContent("—");
    expect(within(aggregateTable).getByRole("row", { name: /Cost Coverage/ })).toHaveTextContent("4/8 (50.0%)");
    expect(await screen.findByText(/成本说明：/)).toHaveTextContent("覆盖不完整");
  });

  it("shows em dashes and an explanation when no cost evidence was recorded", async () => {
    costReason = "COST_NOT_RECORDED";
    renderReport();
    const aggregateTable = await screen.findByRole("table", { name: "Baseline 与 Candidate 聚合指标对比" });
    const row = within(aggregateTable).getByRole("row", { name: /Cost \/ Case/ });
    expect(row).toHaveTextContent("$0.021");
    expect(row).toHaveTextContent("—");
    expect(within(aggregateTable).getByRole("row", { name: /Cost Coverage/ })).toHaveTextContent("0/8 (0.0%)");
    expect(await screen.findByText(/成本说明：/)).toHaveTextContent("未记录成本的 Case 不按 0 计入");
  });

  it("explains full-run mixed currencies without hiding comparable-cohort costs", async () => {
    fullRunCostUnavailable = true;
    renderReport();

    const aggregateTable = await screen.findByRole("table", { name: "Baseline 与 Candidate 聚合指标对比" });
    expect(within(aggregateTable).getByRole("row", { name: /Cost \/ Case/ })).toHaveTextContent("-$0.004");
    expect(within(aggregateTable).getByRole("row", { name: /Cost Coverage/ })).toHaveTextContent("8/8 (100.0%)");

    const fullRunTable = screen.getByRole("table", { name: "Baseline 与 Candidate 全量运行健康指标" });
    expect(within(fullRunTable).getByRole("row", { name: /Run Cost \/ Case/ })).toHaveTextContent("—");
    expect(within(fullRunTable).getByRole("row", { name: /Run Cost Coverage/ })).toHaveTextContent("10/10 (100.0%)");
    expect(await screen.findByText(/全量 Baseline 成本说明：/)).toHaveTextContent("存在多种币种");
    expect(await screen.findByText(/全量 Candidate 成本说明：/)).toHaveTextContent("存在多种币种");
  });

  it("loads an explicitly pinned historical snapshot while a retry is running", async () => {
    window.history.replaceState({}, "", "/launches/candidate-launch?snapshot_id=old-snapshot");
    renderReport("RUNNING");

    expect(await screen.findByRole("button", { name: "查看双侧输出" })).toBeInTheDocument();
    expect((api.GET as any).mock.calls.some((call: any[]) => (
      call[0].endsWith("/summary") && call[1].params.query.snapshot_id === "old-snapshot"
    ))).toBe(true);
    expect((api.GET as any).mock.calls.some((call: any[]) => (
      call[0].endsWith("/comparison") && call[1].params.query.snapshot_id === "old-snapshot"
    ))).toBe(true);
  });

  it("binds the currently displayed snapshot as the environment baseline", async () => {
    renderReport();
    await screen.findByRole("table", { name: "Baseline 与 Candidate 聚合指标对比" });
    await waitFor(() => expect(screen.getByTestId("router-location")).toHaveTextContent(
      "/launches/candidate-launch?snapshot_id=candidate-snapshot",
    ));
    const setBaselineButton = await screen.findByRole("button", { name: "设为当前环境 Baseline" });
    await waitFor(() => expect(setBaselineButton).toBeEnabled());
    fireEvent.click(screen.getByRole("button", { name: "设为当前环境 Baseline" }));
    await waitFor(() => expect(api.POST).toHaveBeenCalledWith(
      "/api/v1/agents/{agent_id}/baselines",
      {
        params: { path: { agent_id: "banking-agent" } },
        body: {
          environment: "production",
          result_snapshot_id: "candidate-snapshot",
          expected_revision: 0,
        },
      },
    ));
  });

  it("explicitly switches to the latest snapshot and replaces the URL revision", async () => {
    renderReport();
    expect(await screen.findByRole("button", { name: "查看最新修订" })).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "查看最新修订" }));
    await waitFor(() => expect(screen.getByTestId("router-location")).toHaveTextContent(
      "/launches/candidate-launch?snapshot_id=candidate-snapshot-new",
    ));
    await waitFor(() => expect((api.GET as any).mock.calls.some((call: any[]) => (
      call[0].endsWith("/comparison") && call[1].params.query.snapshot_id === "candidate-snapshot-new"
    ))).toBe(true));
  });

  it("loads case output from the pinned snapshot and restores focus on Escape", async () => {
    renderReport();
    await screen.findByText("case-1");
    fireEvent.click(screen.getByRole("button", { name: "加载更多用例（已显示 1 条）" }));
    expect(await screen.findByText("case-2")).toBeInTheDocument();
    const trigger = screen.getAllByRole("button", { name: "查看双侧输出" })[0];
    // A real click focuses the button; fireEvent does not. Focus the trigger
    // explicitly so the restore assertion reflects browser behaviour rather
    // than a jsdom gap.
    trigger.focus();
    fireEvent.click(trigger);
    const dialog = await screen.findByRole("dialog", { name: "Case 双侧结果" });
    expect(await screen.findByText("UPSTREAM_ERROR")).toBeInTheDocument();
    expect(dialog).toHaveTextContent("Baseline");
    expect(dialog).toHaveTextContent("Candidate");
    expect((api.GET as any).mock.calls.some((call: any[]) => (
      call[0].endsWith("/comparison/case") && call[1].params.query.snapshot_id === "candidate-snapshot"
    ))).toBe(true);

    fireEvent.keyDown(window, { key: "Escape" });
    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
    await waitFor(() => expect(document.activeElement).toBe(trigger));
  });

  it("uses the route snapshot when a cached report is reused for another Launch", async () => {
    renderReport();
    await screen.findByText("case-1");
    fireEvent.click(await screen.findByRole("button", { name: "加载更多用例（已显示 1 条）" }));
    expect(await screen.findByText("case-2")).toBeInTheDocument();
    fireEvent.click(screen.getAllByRole("button", { name: "查看双侧输出" })[0]);
    expect(await screen.findByRole("dialog", { name: "Case 双侧结果" })).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "REGRESSION (1)" }));

    fireEvent.click(screen.getByRole("button", { name: "切换到其他 Launch" }));
    await waitFor(() => expect(screen.getByTestId("router-location")).toHaveTextContent(
      "/launches/other-launch?snapshot_id=other-snapshot",
    ));
    await waitFor(() => {
      expect((api.GET as any).mock.calls.some((call: any[]) => (
        call[0].endsWith("/summary") && call[1].params.path.launch_id === "other-launch"
          && call[1].params.query.snapshot_id === "other-snapshot"
      ))).toBe(true);
      expect((api.GET as any).mock.calls.some((call: any[]) => (
        call[0].endsWith("/comparison") && call[1].params.path.launch_id === "other-launch"
          && call[1].params.query.snapshot_id === "other-snapshot"
          && call[1].params.query.classification === undefined
          && call[1].params.query.cursor === 0
      ))).toBe(true);
    });
    expect(screen.queryByRole("dialog", { name: "Case 双侧结果" })).not.toBeInTheDocument();
  });

  it("uses a new route snapshot when the same Launch switches to a historical revision", async () => {
    renderReport();
    await screen.findByText("case-1");
    fireEvent.click(await screen.findByRole("button", { name: "加载更多用例（已显示 1 条）" }));
    expect(await screen.findByText("case-2")).toBeInTheDocument();
    fireEvent.click(screen.getAllByRole("button", { name: "查看双侧输出" })[0]);
    expect(await screen.findByRole("dialog", { name: "Case 双侧结果" })).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "REGRESSION (1)" }));
    fireEvent.click(screen.getByRole("button", { name: "切换到同 Launch 历史修订" }));
    await waitFor(() => expect(screen.getByTestId("router-location")).toHaveTextContent(
      "/launches/candidate-launch?snapshot_id=history-snapshot",
    ));
    await waitFor(() => {
      expect((api.GET as any).mock.calls.some((call: any[]) => (
        call[0].endsWith("/summary") && call[1].params.path.launch_id === "candidate-launch"
          && call[1].params.query.snapshot_id === "history-snapshot"
      ))).toBe(true);
      expect((api.GET as any).mock.calls.some((call: any[]) => (
        call[0].endsWith("/comparison") && call[1].params.path.launch_id === "candidate-launch"
          && call[1].params.query.snapshot_id === "history-snapshot"
          && call[1].params.query.classification === undefined
          && call[1].params.query.cursor === 0
      ))).toBe(true);
    });
    expect(screen.queryByRole("dialog", { name: "Case 双侧结果" })).not.toBeInTheDocument();
  });
});
