import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter } from "react-router-dom";
import { GateComparisonTab } from "../tabs/GateComparisonTab";
import { ComparisonReport } from "../ComparisonReport";
import { api } from "../../../api/client";

vi.mock("../../../api/client", () => ({
  api: {
    GET: vi.fn(),
    POST: vi.fn(),
  },
}));

describe("Baseline empty guide vs comparability conflict", () => {
  let queryClient: QueryClient;

  beforeEach(() => {
    queryClient = new QueryClient({
      defaultOptions: { queries: { retry: false } },
    });
    vi.clearAllMocks();
  });

  const mockSummary = {
    launch_id: "launch-1",
    snapshot_id: "snap-1",
    revision: 1,
    evidence_state: "COMPLETE",
    summary: {
      pass_rate: 1.0,
      total_cases: 2,
    },
    versions: {
      agent: { id: "agent-1", version: "v1" },
      dataset: { name: "dataset-1", version: "v1" },
      evaluators: [],
      runner: { runner_version: "v1" },
      environment: "production",
    },
  };

  const emptyBaselineComparison = {
    launch_id: "launch-1",
    candidate_snapshot_id: "snap-1",
    baseline_snapshot_id: null,
    classification_counts: { ALL: 2, NOT_COMPARABLE: 2 },
    summary: {
      comparable_case_count: 0,
      candidate: mockSummary.summary,
      baseline: null,
      comparable_cohort: null,
    },
    items: [],
    comparability: {
      comparable: false,
      reason_codes: ["SNAPSHOT_ENVIRONMENT_MISSING", "CONTRACT_PROVENANCE_UNKNOWN"],
      provenance: "UNKNOWN",
      dimensions: [
        {
          dimension: "MEASUREMENT",
          status: "UNKNOWN",
          candidate_version: "1.2",
        },
      ],
      suggestions: ["请在同一环境重新评测并冻结完整环境证据，跨环境结果不能作为正式回归结论。"],
    },
    formal: {
      available: false,
      withheld_reasons: ["BASELINE_NOT_BOUND"],
    },
    versions: {
      candidate: mockSummary.versions,
      baseline: null,
    },
  };

  it("GateComparisonTab displays friendly baseline-guide-card and hides scary comparability banner when baseline is empty", async () => {
    (api.GET as any).mockImplementation((path: string) => {
      if (path.endsWith("/comparison")) {
        return Promise.resolve({ data: emptyBaselineComparison });
      }
      return Promise.resolve({ data: null });
    });

    const onSetBaseline = vi.fn();

    render(
      <QueryClientProvider client={queryClient}>
        <MemoryRouter>
          <GateComparisonTab
            launchId="launch-1"
            snapshotId="snap-1"
            environment="production"
            summary={mockSummary as any}
            candidateQualityConclusion="pass"
            activeBaseline={null}
            onSetBaselineModal={onSetBaseline}
          />
        </MemoryRouter>
      </QueryClientProvider>,
    );

    // 1. Should show friendly baseline guide card
    const guideCard = await screen.findByTestId("baseline-guide-card");
    expect(guideCard).toBeInTheDocument();
    expect(guideCard).toHaveTextContent("当前版本尚未绑定基准 (Baseline)");
    expect(guideCard).toHaveTextContent("建议点击上方");

    // 2. Click quick action on guide card
    const btn = screen.getByRole("button", { name: /立即设为当前环境 Baseline/ });
    fireEvent.click(btn);
    expect(onSetBaseline).toHaveBeenCalledTimes(1);

    // 3. MUST NOT display the scary warning banner
    expect(screen.queryByTestId("comparison-comparability-banner")).toBeNull();
    expect(screen.queryByText(/SNAPSHOT_ENVIRONMENT_MISSING/)).toBeNull();
    expect(screen.queryByText(/判定规则不同，无法正式比较/)).toBeNull();
  });

  it("ComparisonReport displays friendly baseline-guide-card and hides scary comparability banner when baseline is empty", async () => {
    (api.GET as any).mockImplementation((path: string) => {
      if (path.endsWith("/summary")) {
        return Promise.resolve({ data: mockSummary });
      }
      if (path.endsWith("/baselines")) {
        return Promise.resolve({ error: {}, response: { status: 404 } });
      }
      if (path.endsWith("/comparison")) {
        return Promise.resolve({ data: emptyBaselineComparison });
      }
      return Promise.resolve({ data: null });
    });

    render(
      <QueryClientProvider client={queryClient}>
        <MemoryRouter initialEntries={["/launches/launch-1?snapshot_id=snap-1"]}>
          <ComparisonReport
            launchId="launch-1"
            launchStatus="COMPLETED"
            environment="production"
          />
        </MemoryRouter>
      </QueryClientProvider>,
    );

    // 1. Should show friendly baseline guide card
    const guideCard = await screen.findByTestId("baseline-guide-card");
    expect(guideCard).toBeInTheDocument();
    expect(guideCard).toHaveTextContent("当前版本尚未绑定基准 (Baseline)");

    // 2. MUST NOT display the scary warning banner
    expect(screen.queryByTestId("comparison-comparability-banner")).toBeNull();
    expect(screen.queryByText(/SNAPSHOT_ENVIRONMENT_MISSING/)).toBeNull();
  });

  it("withholds baseline button in guide card when evidence_state is DIAGNOSTIC", async () => {
    const diagnosticSummary = {
      ...mockSummary,
      evidence_state: "DIAGNOSTIC",
      evidence_reasons: ["RUNNER_BUILD_ID_MISSING"],
    };

    (api.GET as any).mockImplementation((path: string) => {
      if (path.endsWith("/comparison")) {
        return Promise.resolve({ data: emptyBaselineComparison });
      }
      return Promise.resolve({ data: null });
    });

    const onSetBaseline = vi.fn();

    render(
      <QueryClientProvider client={queryClient}>
        <MemoryRouter>
          <GateComparisonTab
            launchId="launch-1"
            snapshotId="snap-1"
            environment="production"
            summary={diagnosticSummary as any}
            candidateQualityConclusion="pass"
            activeBaseline={null}
            onSetBaselineModal={onSetBaseline}
          />
        </MemoryRouter>
      </QueryClientProvider>,
    );

    // 1. Should still render friendly guide card
    const guideCard = await screen.findByTestId("baseline-guide-card");
    expect(guideCard).toBeInTheDocument();
    expect(guideCard).toHaveTextContent("本次评测证据尚在生成或不完整");

    // 2. MUST NOT offer any baseline creation button
    expect(screen.queryByRole("button", { name: /设为当前环境 Baseline/ })).toBeNull();
  });

  it("ComparisonReport displays confirmation modal on guide button click before executing baseline mutation", async () => {
    (api.GET as any).mockImplementation((path: string) => {
      if (path.endsWith("/summary")) {
        return Promise.resolve({ data: mockSummary });
      }
      if (path.endsWith("/baselines")) {
        return Promise.resolve({ error: {}, response: { status: 404 } });
      }
      if (path.endsWith("/comparison")) {
        return Promise.resolve({ data: emptyBaselineComparison });
      }
      if (path.includes("/experiment-launches/launch-1")) {
        return Promise.resolve({ data: { id: "launch-1", agent_id: "banking-agent" } });
      }
      return Promise.resolve({ data: null });
    });

    (api.POST as any).mockResolvedValue({ data: { id: "base-1" } });

    render(
      <QueryClientProvider client={queryClient}>
        <MemoryRouter initialEntries={["/launches/launch-1?snapshot_id=snap-1"]}>
          <ComparisonReport
            launchId="launch-1"
            launchStatus="COMPLETED"
            environment="production"
          />
        </MemoryRouter>
      </QueryClientProvider>,
    );

    const guideBtn = await screen.findByRole("button", { name: /立即设为当前环境 Baseline/ });
    expect(guideBtn).toBeInTheDocument();

    // Click guide button -> opens confirmation modal, NOT directly mutating
    fireEvent.click(guideBtn);
    expect(api.POST).not.toHaveBeenCalled();

    // Modal is visible with details
    expect(screen.getByText("设为当前环境 Baseline 确认")).toBeInTheDocument();
    expect(screen.getByText(/确定要将当前评测快照设为/)).toBeInTheDocument();

    // Confirm button in modal triggers the mutation
    const confirmBtn = screen.getByRole("button", { name: "确认设为 Baseline" });
    fireEvent.click(confirmBtn);

    await waitFor(() => {
      expect(api.POST).toHaveBeenCalledWith(
        "/api/v1/agents/{agent_id}/baselines",
        expect.objectContaining({
          params: { path: { agent_id: "agent-1" } },
        }),
      );
    });
  });

  it("ComparisonReport renders collapsible pass rate details that defaults to collapsed", async () => {
    const comparisonWithCohort = {
      ...emptyBaselineComparison,
      summary: {
        ...emptyBaselineComparison.summary,
        comparable_cohort: {
          baseline: { pass_rate: 1.0, score_means: {} },
          candidate: { pass_rate: 1.0, score_means: {} },
        },
      },
    };

    (api.GET as any).mockImplementation((path: string) => {
      if (path.endsWith("/summary")) {
        return Promise.resolve({ data: mockSummary });
      }
      if (path.endsWith("/baselines")) {
        return Promise.resolve({ error: {}, response: { status: 404 } });
      }
      if (path.endsWith("/comparison")) {
        return Promise.resolve({ data: comparisonWithCohort });
      }
      return Promise.resolve({ data: null });
    });

    render(
      <QueryClientProvider client={queryClient}>
        <MemoryRouter initialEntries={["/launches/launch-1?snapshot_id=snap-1"]}>
          <ComparisonReport
            launchId="launch-1"
            launchStatus="COMPLETED"
            environment="production"
          />
        </MemoryRouter>
      </QueryClientProvider>,
    );

    const details = await screen.findByTestId("comparable-quality-pass-rate-details");
    expect(details).toBeInTheDocument();
    expect(details).not.toHaveAttribute("open");
    expect(screen.getByText("质量通过率口径说明")).toBeInTheDocument();
  });
});
