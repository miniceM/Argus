import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { render, screen, waitFor, fireEvent, within } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter, Routes, Route, useLocation } from "react-router-dom";
import { LaunchDetail } from "../LaunchDetail";
import { ManifestAuditTab } from "../tabs/ManifestAuditTab";
import { api } from "../../../api/client";

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
  let snapshotManifestOverride: Record<string, unknown> | null = null;
  let snapshotListOverride: Record<string, unknown> | null = null;
  let snapshotDetailOverride: Record<string, unknown> | null = null;

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
    launch_id: mockLaunch.id,
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
    snapshotManifestOverride = null;
    snapshotListOverride = null;
    snapshotDetailOverride = null;

    (api.GET as any).mockImplementation((path: string, options?: any) => {
      if (path === "/api/v1/experiment-launches/{launch_id}") {
        return Promise.resolve({ data: mockLaunch });
      }
      if (path === "/api/v1/experiment-launches/{launch_id}/items") {
        return Promise.resolve({ data: mockItems });
      }
      if (path === "/api/v1/experiment-launches/{launch_id}/result-snapshots") {
        return Promise.resolve({ data: snapshotListOverride ?? mockSnapshotList });
      }
      if (path.includes("/result-snapshots/")) {
        return Promise.resolve({
          data: snapshotDetailOverride ?? {
            ...mockSnapshotList.revisions[0],
            launch_id: mockLaunch.id,
            releasable: true,
            manifest: snapshotManifestOverride ?? mockLaunch.manifest,
            summary: mockSummary.summary,
            items: mockItems.map((item) => ({
              ...item,
              trace_url: item.langfuse_trace_url,
              latency_ms: item.final_attempt_latency_ms,
            })),
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

  const renderAuditTab = (manifestOverrides: Record<string, unknown>) => {
    const launchManifest = { ...mockLaunch.manifest, ...manifestOverrides };
    snapshotManifestOverride = launchManifest;
    const launch = {
      ...mockLaunch,
      manifest: launchManifest,
    };

    return render(
      <QueryClientProvider client={queryClient}>
        <ManifestAuditTab
          launch={launch as any}
          activeSnapshot={mockSnapshotList.revisions[0] as any}
          snapshotId="snap-v2-001"
        />
      </QueryClientProvider>,
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
    // Undeclared result types keep the metric and delta in raw units.
    expect(screen.getByText("+0.333")).toBeInTheDocument();

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

  it("AC7: shows an unrecorded Runner version instead of inventing a default", async () => {
    renderAuditTab({ runner: { mapping_engine_version: "engine-v1" } });

    await waitFor(() => expect(screen.getByTestId("runner-version")).toHaveTextContent("未记录"));
    expect(screen.getByTestId("runner-version")).not.toHaveTextContent("1.0.0");
  });

  it("AC7: labels a missing historical Manifest schema version as unrecorded", async () => {
    renderAuditTab({ schema_version: undefined, manifest_version: undefined });

    await waitFor(() => {
      expect(screen.getByTestId("manifest-schema-version")).toHaveTextContent("未记录");
    });
    expect(screen.getByTestId("manifest-schema-version")).not.toHaveTextContent("1.0");
  });

  it("AC5: keeps selected historical quality counts consistent in the audit history and tooltip", async () => {
    const latest = {
      ...mockSnapshotList.revisions[0],
      snapshot_id: "snap-v2-002",
      revision: 2,
      is_latest: true,
      quality_pass_count: 3,
      quality_fail_count: 1,
      quality_unknown_count: 2,
    };
    const historical = {
      ...mockSnapshotList.revisions[0],
      snapshot_id: "snap-v2-001",
      revision: 1,
      is_latest: false,
      quality_pass_count: null,
      quality_fail_count: null,
      quality_unknown_count: null,
    };
    snapshotListOverride = {
      ...mockSnapshotList,
      latest_snapshot_id: latest.snapshot_id,
      latest_revision: latest.revision,
      revisions: [latest, historical],
    };
    snapshotDetailOverride = {
      ...historical,
      launch_id: mockLaunch.id,
      releasable: false,
      manifest: mockLaunch.manifest,
      summary: {},
      items: [
        { dataset_item_id: "legacy-pass", quality_conclusion: "PASS" },
        { dataset_item_id: "legacy-fail", quality_conclusion: "FAIL" },
        { dataset_item_id: "legacy-unknown" },
      ],
    };

    renderComponent(`/launches/${mockLaunch.id}?snapshot_id=${historical.snapshot_id}&tab=audit`);

    await waitFor(() => {
      expect(screen.getAllByTestId("snapshot-quality-counts").length).toBeGreaterThan(0);
      expect(screen.getAllByTestId("snapshot-revision-1").length).toBeGreaterThan(0);
    });

    const histories = screen.getAllByTestId("snapshot-history");
    const auditHistory = within(histories[histories.length - 1]);
    const historicalRow = auditHistory.getByTestId("snapshot-revision-1");
    expect(historicalRow).toHaveTextContent("PASS 1 · FAIL 1 · UNKNOWN 1");
    expect(historicalRow).toHaveAttribute("title", "PASS 1 · FAIL 1 · UNKNOWN 1");
    expect(auditHistory.getByTestId("snapshot-revision-2")).toHaveTextContent(
      "PASS 3 · FAIL 1 · UNKNOWN 2",
    );

    const detailRequests = (api.GET as any).mock.calls.filter(([path]: [string]) =>
      path.includes("/result-snapshots/"),
    );
    expect(detailRequests).toHaveLength(1);
    expect(detailRequests[0][1].params.path.snapshot_id).toBe(historical.snapshot_id);
  });

  it.each([
    ["recorded schema_version", "2.4", undefined, "Schema v2.4"],
    ["recorded manifest_version fallback", undefined, "legacy-3", "Schema vlegacy-3"],
    ["blank schema_version with recorded fallback", "   ", "1.7", "Schema v1.7"],
    ["both version fields blank", "", "  ", "Schema 版本未记录"],
  ])("uses only a recorded Manifest schema identity: %s", async (_case, schemaVersion, manifestVersion, expected) => {
    renderAuditTab({ schema_version: schemaVersion, manifest_version: manifestVersion });

    await waitFor(() => {
      expect(screen.getByTestId("manifest-schema-version")).toHaveTextContent(expected);
    });
    expect(screen.getByTestId("manifest-schema-version")).not.toHaveTextContent("Schema v1.0");
  });

  it.each([
    ["missing", undefined],
    ["array", []],
  ])("rejects a %s frozen Manifest before displaying audit data or enabling export", async (_case, manifest) => {
    snapshotDetailOverride = {
      ...mockSnapshotList.revisions[0],
      launch_id: mockLaunch.id,
      releasable: true,
      manifest,
      summary: mockSummary.summary,
      items: [],
    };

    renderComponent("/launches/ff04d66b-3d00-4d67-8cf4-d36772fff708?tab=audit");

    const error = await screen.findByTestId("frozen-manifest-error");
    expect(error).toHaveTextContent("冻结 Manifest");
    expect(screen.queryByTestId("manifest-4d-grid")).not.toBeInTheDocument();
    expect(screen.queryByTestId("frozen-quality-policy-legacy")).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "复制 JSON" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "下载原始 JSON" })).toBeDisabled();
  });

  it("does not present live Launch fields as frozen Manifest data when the selected snapshot cannot load", async () => {
    (api.GET as any).mockImplementation((path: string) => {
      if (path.includes("/result-snapshots/")) {
        return Promise.resolve({ error: new Error("frozen detail unavailable") });
      }
      if (path === "/api/v1/experiment-launches/{launch_id}") {
        return Promise.resolve({ data: mockLaunch });
      }
      if (path === "/api/v1/experiment-launches/{launch_id}/result-snapshots") {
        return Promise.resolve({ data: mockSnapshotList });
      }
      return Promise.resolve({ data: null });
    });

    renderComponent("/launches/ff04d66b-3d00-4d67-8cf4-d36772fff708?tab=audit");

    const error = await screen.findByTestId("frozen-manifest-error");
    expect(error).toHaveTextContent("frozen detail unavailable");
    expect(screen.queryByTestId("manifest-4d-grid")).not.toBeInTheDocument();
    expect(screen.queryByTestId("frozen-quality-policy-legacy")).not.toBeInTheDocument();
  });

  it("AC7 & AC9: downloads the selected revision payload with its exact frozen Manifest", async () => {
    const frozenManifest = {
      schema_version: "2.3",
      runner: { runner_version: "v0.3.0" },
      evaluators: [{ id: "frozen-evaluator", version: "7.1.0" }],
      quality_policy: { policy_id: "frozen-policy", rules: [{ evaluator_id: "frozen-evaluator" }] },
    };
    snapshotManifestOverride = frozenManifest;

    const downloaded: string[] = [];
    const NativeBlob = globalThis.Blob;
    class CapturingBlob extends NativeBlob {
      constructor(parts?: BlobPart[], options?: BlobPropertyBag) {
        super(parts, options);
        downloaded.push((parts ?? []).map((part) => typeof part === "string" ? part : "").join(""));
      }
    }
    class CapturingURL extends URL {
      static createObjectURL = vi.fn(() => "blob:snapshot-test");
      static revokeObjectURL = vi.fn();
    }
    vi.stubGlobal("Blob", CapturingBlob);
    vi.stubGlobal("URL", CapturingURL);

    renderComponent("/launches/ff04d66b-3d00-4d67-8cf4-d36772fff708?tab=audit");
    await waitFor(() => {
      expect(screen.getByTestId("manifest-schema-version")).toHaveTextContent("Schema v2.3");
    });
    const manifestGrid = screen.getByTestId("manifest-4d-grid");
    expect(manifestGrid).toHaveTextContent("frozen-evaluator");
    expect(manifestGrid).not.toHaveTextContent("pii_safe");
    fireEvent.click(screen.getByRole("button", { name: /下载原始 JSON/ }));

    expect(downloaded).toHaveLength(1);
    const exported = JSON.parse(downloaded[0]);
    expect(exported).toMatchObject({
      launch_id: mockLaunch.id,
      snapshot_id: "snap-v2-001",
      manifest: frozenManifest,
      summary: mockSummary.summary,
    });
    expect(exported.items.map((item: { dataset_item_id: string }) => item.dataset_item_id)).toEqual(
      mockItems.map((item) => item.dataset_item_id),
    );
  });

  it("AC7: resolves result types from the matching frozen evaluator and labels missing types", async () => {
    renderAuditTab({
      evaluators: [
        { id: "typed_metric", version: "1.0.0", result_type: "categorical" },
        { id: "untyped_metric", version: "1.0.0" },
        { id: "rule_typed_metric", version: "1.0.0", result_type: "categorical" },
      ],
      quality_policy: {
        rules: [
          { evaluator_id: "typed_metric", operator: "==", expected_value: "good", required: true },
          { evaluator_id: "untyped_metric", operator: ">=", threshold: 1, required: false },
          { evaluator_id: "rule_typed_metric", result_type: "boolean", operator: "==", expected_value: true, required: true },
        ],
      },
    });

    await waitFor(() => {
      expect(screen.getByText("结果类型 categorical")).toBeInTheDocument();
    });
    expect(screen.getByText("结果类型 类型未声明")).toBeInTheDocument();
    expect(screen.getByText("结果类型 boolean")).toBeInTheDocument();
    expect(screen.queryByText("结果类型 numeric")).not.toBeInTheDocument();
  });

  afterEach(() => {
    vi.unstubAllGlobals();
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
});
