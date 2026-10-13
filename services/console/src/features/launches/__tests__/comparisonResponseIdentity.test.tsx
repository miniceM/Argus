/**
 * issue 119 main-gap fix — U1: unified Comparison / Case response identity.
 *
 * Every frozen-comparison read must prove it belongs to the exact requested
 * identity before it can enter the React Query success cache:
 *   - Comparison: (launch_id, candidate_snapshot_id)
 *   - Case output: (launch_id, candidate_snapshot_id, dataset_item_id)
 *
 * The payloads below are intentionally *wrong* (a foreign Launch, a foreign
 * revision, a foreign case). None of their values may render, and none of them
 * may occupy the success cache shared by the KPI, the paginated comparison and
 * both Case consumers.
 */
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, waitFor, fireEvent } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter, Routes, Route } from "react-router-dom";
import { LaunchDetail } from "../LaunchDetail";
import { ComparisonCaseDrawer } from "../ComparisonCaseDrawer";
import { CasesTraceTab } from "../tabs/CasesTraceTab";
import { api } from "../../../api/client";

vi.mock("../../../api/client", () => ({
  api: { GET: vi.fn(), POST: vi.fn() },
}));

const LAUNCH_ID = "ff04d66b-3d00-4d67-8cf4-d36772fff708";
const FOREIGN_LAUNCH_ID = "99999999-9999-9999-9999-999999999999";
const S1 = "snap-v1-001";
const S2 = "snap-v2-001";
const FOREIGN_SNAPSHOT_ID = "snap-ffff-001";
const CASE_A = "item-001";
const CASE_B = "item-002";
const FOREIGN_CASE = "item-999";

const FOREIGN_P95 = 9876;
const FOREIGN_OUTPUT = "FOREIGN-AGENT-OUTPUT";
const FOREIGN_LATENCY = 4321;

const LAUNCH = {
  id: LAUNCH_ID,
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
    comparison: { environment: "production", baseline_snapshot_id: "base-snap-001" },
    dataset: {
      dataset_name: "banking-agent-regression",
      dataset_version: "2026-10-08T16:08:13Z",
      snapshot_digest: "sha256:19e7bbcbd7c6",
      items_count: 2,
    },
    agent: {
      id: "banking-agent",
      version: "v2",
      endpoint: "http://127.0.0.1:18082/invoke",
      spec_digest: "sha256:banking-v2-digest",
    },
    evaluators: [{ id: "pii_safe", version: "1.0.0", required: true }],
    quality_policy: { policy_id: "banking-policy", version: "1.0", rules: [] },
  },
};

const SUMMARY = {
  launch_id: LAUNCH_ID,
  snapshot_id: S1,
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

const VERSIONS = {
  candidate: SUMMARY.versions,
  baseline: {
    agent: { id: "banking-agent", version: "v1" },
    dataset: SUMMARY.versions.dataset,
    evaluators: SUMMARY.versions.evaluators,
    runner: SUMMARY.versions.runner,
  },
};

const cohortFor = (candidateP95: number) => ({
  baseline: { pass_rate: 1.0, p95_latency_ms: 160, cost_per_case: 0.0019, cost_currency: "USD", cost_coverage: 1.0, score_means: { pii_safe: 1.0 } },
  candidate: { pass_rate: 1.0, p95_latency_ms: candidateP95, cost_per_case: 0.0019, cost_currency: "USD", cost_coverage: 1.0, score_means: { pii_safe: 1.0 } },
});

const ITEM = (datasetItemId: string) => ({
  id: `exec-${datasetItemId}`,
  launch_id: LAUNCH_ID,
  dataset_item_id: datasetItemId,
  execution_status: "succeeded",
  eval_status: "succeeded",
  quality_conclusion: "pass",
  langfuse_trace_url: `https://cloud.langfuse.com/trace/${datasetItemId}`,
  attempt_count: 1,
  final_attempt_http_status: 200,
  final_attempt_latency_ms: 142,
  scores: { pii_safe: 1 },
  quality_evaluation: { conclusion: "pass", rules: [{ evaluator_id: "pii_safe", conclusion: "pass" }] },
});

const comparisonPayload = (overrides: {
  launchId?: string;
  candidateSnapshotId?: string;
  items?: any[];
  nextCursor?: number | null;
  candidateP95?: number;
  regressionCount?: number;
  formalAvailable?: boolean;
  comparable?: boolean;
}) => ({
  launch_id: overrides.launchId ?? LAUNCH_ID,
  candidate_snapshot_id: overrides.candidateSnapshotId ?? S1,
  baseline_snapshot_id: "base-snap-001",
  baseline_binding_revision: 1,
  versions: VERSIONS,
  summary: {
    comparable_case_count: 2,
    candidate: { pass_rate: 1.0, p95_latency_ms: overrides.candidateP95 ?? 148, cost_per_case: 0.0019, cost_currency: "USD", cost_coverage: 1.0 },
    baseline: { pass_rate: 0.5, p95_latency_ms: 160, cost_per_case: 0.0019, cost_currency: "USD", cost_coverage: 1.0 },
    comparable_cohort: cohortFor(overrides.candidateP95 ?? 148),
    cost_comparison: { status: "COMPARABLE", delta: 0, currency: "USD", reason: null },
    pass_rate_delta: 0.5,
    score_mean_deltas: { pii_safe: 0 },
  },
  classification_counts: {
    ALL: 2,
    REGRESSION: overrides.regressionCount ?? 0,
    IMPROVEMENT: 1,
    UNCHANGED: 1,
    NOT_COMPARABLE: 0,
  },
  comparability: {
    comparable: overrides.comparable ?? true,
    reason_codes: [],
    provenance: "VERIFIED",
    dimensions: [],
    suggestions: [],
  },
  formal: {
    available: overrides.formalAvailable ?? true,
    verdict: "IMPROVEMENT",
    reason: null,
    required_cases: 2,
    comparable_cases: 2,
    coverage: 1.0,
    withheld_reasons: [],
  },
  diagnostic: { note: "diagnostic", comparable_cases: 2, classification_counts: { REGRESSION: 0 } },
  items: overrides.items ?? [],
  next_cursor: overrides.nextCursor ?? null,
});

const casePayload = (overrides: {
  launchId?: string;
  candidateSnapshotId?: string;
  datasetItemId?: string;
  candidateOutput?: string;
  candidateLatency?: number;
  traceUrl?: string;
  baseline?: any;
}) => ({
  launch_id: overrides.launchId ?? LAUNCH_ID,
  candidate_snapshot_id: overrides.candidateSnapshotId ?? S1,
  baseline_snapshot_id: "base-snap-001",
  dataset_item_id: overrides.datasetItemId ?? CASE_A,
  classification: "IMPROVEMENT",
  reason: "ok",
  baseline: overrides.baseline ?? { output_status: "AVAILABLE", output: "v1 baseline output", scores: { pii_safe: 0 } },
  candidate: {
    output_status: "AVAILABLE",
    output: overrides.candidateOutput ?? "v2 candidate output",
    scores: { pii_safe: 1 },
    latency_ms: overrides.candidateLatency ?? 142,
    trace_url: overrides.traceUrl ?? `https://cloud.langfuse.com/trace/${CASE_A}`,
  },
});

let queryClient: QueryClient;
let getCalls: Array<{ path: string; query?: any }>;

const installApi = (handlers: Record<string, (options?: any) => Promise<any>>) => {
  (api.GET as any).mockImplementation((path: string, options?: any) => {
    getCalls.push({ path, query: options?.params?.query ?? options?.params?.path });
    const handler = handlers[path];
    if (handler) return handler(options);
    return Promise.resolve({ data: null });
  });
};

const baseHandlers = (overrides: {
  comparison?: (options?: any) => Promise<any>;
  caseOutput?: (options?: any) => Promise<any>;
} = {}) => ({
  "/api/v1/experiment-launches/{launch_id}": () => Promise.resolve({ data: LAUNCH }),
  "/api/v1/experiment-launches/{launch_id}/items": () =>
    Promise.resolve({ data: [ITEM(CASE_A), ITEM(CASE_B)] }),
  "/api/v1/experiment-launches/{launch_id}/result-snapshots": () =>
    Promise.resolve({
      data: {
        latest_snapshot_id: S2,
        latest_revision: 2,
        revisions: [
          { snapshot_id: S2, revision: 2, is_latest: true, created_at: "2026-10-08T16:08:18Z", evidence_state: "COMPLETE", quality_pass_count: 2, quality_fail_count: 0, quality_unknown_count: 0, source_result_digest: "sha256:v2" },
          { snapshot_id: S1, revision: 1, is_latest: false, created_at: "2026-10-08T16:08:10Z", evidence_state: "COMPLETE", quality_pass_count: 2, quality_fail_count: 0, quality_unknown_count: 0, source_result_digest: "sha256:v1" },
        ],
      },
    }),
  "/api/v1/experiment-launches/{launch_id}/result-snapshots/{snapshot_id}": (options?: any) => {
    const requested = options?.params?.path?.snapshot_id ?? S1;
    return Promise.resolve({
      data: {
        launch_id: LAUNCH_ID,
        snapshot_id: requested,
        revision: requested === S1 ? 1 : 2,
        is_latest: requested === S2,
        created_at: "2026-10-08T16:08:18Z",
        evidence_state: "COMPLETE",
        releasable: true,
        manifest: LAUNCH.manifest,
        summary: SUMMARY.summary,
        items: [ITEM(CASE_A), ITEM(CASE_B)],
      },
    });
  },
  "/api/v1/experiment-launches/{launch_id}/summary": (options?: any) =>
    Promise.resolve({ data: { ...SUMMARY, snapshot_id: options?.params?.query?.snapshot_id ?? S1 } }),
  "/api/v1/experiment-launches/{launch_id}/comparison":
    overrides.comparison ?? (() => Promise.resolve({ data: comparisonPayload({}) })),
  "/api/v1/experiment-launches/{launch_id}/comparison/case":
    overrides.caseOutput ??
    ((options?: any) =>
      Promise.resolve({
        data: casePayload({ datasetItemId: options?.params?.query?.dataset_item_id ?? CASE_A }),
      })),
  "/api/v1/agents/{agent_id}/baselines": () =>
    Promise.resolve({
      data: {
        agent_id: "banking-agent",
        environment: "production",
        revision: 1,
        result_snapshot_id: "base-snap-001",
        result_revision: 1,
        result_evidence_state: "COMPLETE",
      },
    }),
});

const renderDetail = (initialUrl: string) =>
  render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter initialEntries={[initialUrl]}>
        <Routes>
          <Route path="/launches/:launchId" element={<LaunchDetail />} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );

beforeEach(() => {
  queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  getCalls = [];
  vi.clearAllMocks();
});

const expandCase = async (datasetItemId: string) => {
  await waitFor(() => {
    expect(screen.getByText(datasetItemId)).toBeInTheDocument();
  });
  const rowIndex = datasetItemId === CASE_A ? 0 : 1;
  const expandButtons = screen.getAllByRole("button", { name: "展开" });
  fireEvent.click(expandButtons[rowIndex]);
};

describe("U1: Comparison response identity", () => {
  it("rejects a comparison page that belongs to a different Launch", async () => {
    installApi(
      baseHandlers({
        comparison: () =>
          Promise.resolve({
            data: comparisonPayload({
              launchId: FOREIGN_LAUNCH_ID,
              candidateP95: FOREIGN_P95,
              regressionCount: 7,
            }),
          }),
      }),
    );
    renderDetail(`/launches/${LAUNCH_ID}?snapshot_id=${S1}`);

    await waitFor(() => {
      expect(screen.getByText(/归属|不一致/)).toBeInTheDocument();
    });
    expect(screen.queryByText(String(FOREIGN_P95))).not.toBeInTheDocument();
    expect(screen.queryByText(/7 项性能退化/)).not.toBeInTheDocument();
  });

  it("rejects a comparison page that belongs to a different snapshot", async () => {
    installApi(
      baseHandlers({
        comparison: () =>
          Promise.resolve({
            data: comparisonPayload({
              candidateSnapshotId: FOREIGN_SNAPSHOT_ID,
              candidateP95: FOREIGN_P95,
              regressionCount: 7,
            }),
          }),
      }),
    );
    renderDetail(`/launches/${LAUNCH_ID}?snapshot_id=${S1}`);

    await waitFor(() => {
      expect(screen.getByText(/不一致|归属/)).toBeInTheDocument();
    });
    expect(screen.queryByText(String(FOREIGN_P95))).not.toBeInTheDocument();
  });

  it("keeps a formal comparison with an explicitly empty items array valid", async () => {
    installApi(baseHandlers());
    renderDetail(`/launches/${LAUNCH_ID}?snapshot_id=${S1}`);

    await waitFor(() => {
      expect(screen.getByTestId("quality-pass-rate")).toBeInTheDocument();
    });
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
    expect(screen.getByText("148")).toBeInTheDocument();
  });
});

describe("U1: paginated comparison identity", () => {
  it("does not append a foreign second page to the verified first page", async () => {
    installApi(
      baseHandlers({
        comparison: (options?: any) => {
          const cursor = options?.params?.query?.cursor;
          if (cursor === 50) {
            return Promise.resolve({
              data: comparisonPayload({
                launchId: FOREIGN_LAUNCH_ID,
                items: [{ dataset_item_id: FOREIGN_CASE, classification: "REGRESSION" }],
                nextCursor: null,
              }),
            });
          }
          return Promise.resolve({
            data: comparisonPayload({
              items: [{ dataset_item_id: CASE_A, classification: "REGRESSION" }],
              nextCursor: 50,
            }),
          });
        },
      }),
    );
    renderDetail(`/launches/${LAUNCH_ID}?snapshot_id=${S1}&tab=compare`);

    await waitFor(() => {
      expect(screen.getByText(CASE_A)).toBeInTheDocument();
    });
    const loadMore = screen.getByRole("button", { name: /加载更多用例/ });
    fireEvent.click(loadMore);

    await waitFor(() => {
      expect(screen.getByRole("alert")).toBeInTheDocument();
    });
    expect(screen.queryByText(FOREIGN_CASE)).not.toBeInTheDocument();
  });

  it("keeps the fixed snapshot identity on every pagination request", async () => {
    installApi(
      baseHandlers({
        comparison: (options?: any) => {
          const cursor = options?.params?.query?.cursor;
          return Promise.resolve({
            data: comparisonPayload({
              items: cursor === 50 ? [{ dataset_item_id: CASE_B, classification: "UNCHANGED" }] : [{ dataset_item_id: CASE_A, classification: "REGRESSION" }],
              nextCursor: cursor === 50 ? null : 50,
            }),
          });
        },
      }),
    );
    renderDetail(`/launches/${LAUNCH_ID}?snapshot_id=${S1}&tab=compare`);

    await waitFor(() => {
      expect(screen.getByText(CASE_A)).toBeInTheDocument();
    });
    fireEvent.click(screen.getByRole("button", { name: /加载更多用例/ }));
    await waitFor(() => {
      expect(screen.getByText(CASE_B)).toBeInTheDocument();
    });

    const comparisonCalls = getCalls.filter((c) => c.path.endsWith("/comparison"));
    expect(comparisonCalls.length).toBeGreaterThanOrEqual(2);
    for (const call of comparisonCalls) {
      expect(call.query?.snapshot_id).toBe(S1);
    }
  });
});

describe("U1: Case output identity", () => {
  const renderCasesTab = () =>
    renderDetail(`/launches/${LAUNCH_ID}?snapshot_id=${S1}&tab=cases`);

  it("rejects a Case output that belongs to a different Launch", async () => {
    installApi(
      baseHandlers({
        caseOutput: () =>
          Promise.resolve({
            data: casePayload({
              launchId: FOREIGN_LAUNCH_ID,
              candidateOutput: FOREIGN_OUTPUT,
              candidateLatency: FOREIGN_LATENCY,
            }),
          }),
      }),
    );
    renderCasesTab();
    await expandCase(CASE_A);

    await waitFor(() => {
      expect(screen.getByText(/读取输出失败/)).toBeInTheDocument();
    });
    expect(screen.queryByText(FOREIGN_OUTPUT)).not.toBeInTheDocument();
    expect(screen.queryByText(`${FOREIGN_LATENCY}ms`)).not.toBeInTheDocument();
  });

  it("rejects a Case output that belongs to a different dataset item", async () => {
    installApi(
      baseHandlers({
        caseOutput: () =>
          Promise.resolve({
            data: casePayload({
              datasetItemId: FOREIGN_CASE,
              candidateOutput: FOREIGN_OUTPUT,
              candidateLatency: FOREIGN_LATENCY,
            }),
          }),
      }),
    );
    renderCasesTab();
    await expandCase(CASE_A);

    await waitFor(() => {
      expect(screen.getByText(/读取输出失败/)).toBeInTheDocument();
    });
    expect(screen.queryByText(FOREIGN_OUTPUT)).not.toBeInTheDocument();
    expect(screen.queryByText(`${FOREIGN_LATENCY}ms`)).not.toBeInTheDocument();
  });

  it("rejects a Case output that belongs to a different snapshot", async () => {
    installApi(
      baseHandlers({
        caseOutput: () =>
          Promise.resolve({
            data: casePayload({
              candidateSnapshotId: FOREIGN_SNAPSHOT_ID,
              candidateOutput: FOREIGN_OUTPUT,
            }),
          }),
      }),
    );
    renderCasesTab();
    await expandCase(CASE_A);

    await waitFor(() => {
      expect(screen.getByText(/读取输出失败/)).toBeInTheDocument();
    });
    expect(screen.queryByText(FOREIGN_OUTPUT)).not.toBeInTheDocument();
  });

  it("keeps a valid no-reference / missing output state readable", async () => {
    installApi(
      baseHandlers({
        caseOutput: () =>
          Promise.resolve({
            data: casePayload({
              baseline: { output_status: "NO_REFERENCE" } as any,
            }),
          }),
      }),
    );
    renderCasesTab();
    await expandCase(CASE_A);

    await waitFor(() => {
      expect(screen.getByText("v2 candidate output")).toBeInTheDocument();
    });
    expect(screen.queryByText(/读取输出失败/)).not.toBeInTheDocument();
  });
});

describe("U1: ComparisonCaseDrawer identity", () => {
  const renderDrawer = () =>
    render(
      <QueryClientProvider client={queryClient}>
        <MemoryRouter>
          <ComparisonCaseDrawer
            launchId={LAUNCH_ID}
            snapshotId={S1}
            datasetItemId={CASE_A}
            onClose={() => {}}
          />
        </MemoryRouter>
      </QueryClientProvider>,
    );

  it("rejects a foreign Launch in the drawer", async () => {
    installApi(
      baseHandlers({
        caseOutput: () =>
          Promise.resolve({
            data: casePayload({ launchId: FOREIGN_LAUNCH_ID, candidateOutput: FOREIGN_OUTPUT }),
          }),
      }),
    );
    renderDrawer();

    await waitFor(() => {
      expect(screen.getByText(/读取 Case 详情失败/)).toBeInTheDocument();
    });
    expect(screen.queryByText(FOREIGN_OUTPUT)).not.toBeInTheDocument();
  });

  it("rejects a missing launch identity in the drawer", async () => {
    const missingIdentity: Record<string, unknown> = casePayload({
      candidateOutput: FOREIGN_OUTPUT,
    });
    delete missingIdentity.launch_id;
    installApi(
      baseHandlers({
        caseOutput: () => Promise.resolve({ data: missingIdentity }),
      }),
    );
    renderDrawer();

    await waitFor(() => {
      expect(screen.getByText(/读取 Case 详情失败/)).toBeInTheDocument();
    });
    expect(screen.queryByText(FOREIGN_OUTPUT)).not.toBeInTheDocument();
  });

  it("rejects a foreign dataset item in the drawer", async () => {
    installApi(
      baseHandlers({
        caseOutput: () =>
          Promise.resolve({
            data: casePayload({ datasetItemId: FOREIGN_CASE, candidateOutput: FOREIGN_OUTPUT }),
          }),
      }),
    );
    renderDrawer();

    await waitFor(() => {
      expect(screen.getByText(/读取 Case 详情失败/)).toBeInTheDocument();
    });
    expect(screen.queryByText(FOREIGN_OUTPUT)).not.toBeInTheDocument();
  });

  it("recovers on retry once the identity matches", async () => {
    let call = 0;
    installApi(
      baseHandlers({
        caseOutput: () => {
          call += 1;
          return Promise.resolve({
            data:
              call === 1
                ? casePayload({ launchId: FOREIGN_LAUNCH_ID, candidateOutput: FOREIGN_OUTPUT })
                : casePayload({ candidateOutput: "v2 candidate output" }),
          });
        },
      }),
    );
    renderDrawer();

    await waitFor(() => {
      expect(screen.getByText(/读取 Case 详情失败/)).toBeInTheDocument();
    });
    fireEvent.click(screen.getByRole("button", { name: "重试" }));

    await waitFor(() => {
      expect(screen.getByText(/v2 candidate output/)).toBeInTheDocument();
    });
    expect(screen.queryByText(FOREIGN_OUTPUT)).not.toBeInTheDocument();
  });
});

describe("U1: shared Case cache cannot be poisoned", () => {
  const renderSharedConsumers = () =>
    render(
      <QueryClientProvider client={queryClient}>
        <MemoryRouter>
          <ComparisonCaseDrawer
            launchId={LAUNCH_ID}
            snapshotId={S1}
            datasetItemId={CASE_A}
            onClose={() => {}}
          />
          <CasesTraceTab
            launchId={LAUNCH_ID}
            snapshotId={S1}
            items={[ITEM(CASE_A), ITEM(CASE_B)] as any}
            expandedCaseId={CASE_A}
            onExpandedCaseChange={() => {}}
          />
        </MemoryRouter>
      </QueryClientProvider>,
    );

  it("neither consumer renders a bad DTO when they share one key (drawer mounted first)", async () => {
    installApi(
      baseHandlers({
        caseOutput: () =>
          Promise.resolve({
            data: casePayload({ launchId: FOREIGN_LAUNCH_ID, candidateOutput: FOREIGN_OUTPUT }),
          }),
      }),
    );
    renderSharedConsumers();

    await waitFor(() => {
      expect(screen.getByText(/读取 Case 详情失败/)).toBeInTheDocument();
      expect(screen.getByText(/读取输出失败/)).toBeInTheDocument();
    });
    expect(screen.queryByText(FOREIGN_OUTPUT)).not.toBeInTheDocument();
  });

  it("a poisoned cache entry cannot leak into the other consumer later (accordion mounted first)", async () => {
    installApi(
      baseHandlers({
        caseOutput: () =>
          Promise.resolve({
            data: casePayload({ datasetItemId: FOREIGN_CASE, candidateOutput: FOREIGN_OUTPUT }),
          }),
      }),
    );
    // First mount: accordion only. A weak consumer here must not cache the bad DTO.
    const first = renderSharedConsumers();
    await waitFor(() => {
      expect(screen.getByText(/读取输出失败/)).toBeInTheDocument();
    });
    first.unmount();

    // Second mount, same QueryClient: the shared key must not serve the bad DTO from cache.
    installApi(
      baseHandlers({
        caseOutput: () =>
          Promise.resolve({ data: casePayload({ candidateOutput: "v2 candidate output" }) }),
      }),
    );
    renderSharedConsumers();

    await waitFor(() => {
      expect(screen.getByText("v2 candidate output")).toBeInTheDocument();
    });
    expect(screen.queryByText(FOREIGN_OUTPUT)).not.toBeInTheDocument();
  });
});

describe("U1: delayed cross-revision responses stay with their own key", () => {
  it("a late S1 Case response cannot override the S2 view", async () => {
    const pending: Array<(value: any) => void> = [];
    installApi(
      baseHandlers({
        caseOutput: (options?: any) => {
          const snapshot = options?.params?.query?.snapshot_id;
          if (snapshot === S1) {
            return new Promise((resolve) => {
              pending.push(() =>
                resolve({
                  data: casePayload({
                    candidateSnapshotId: S1,
                    candidateOutput: "S1 late output",
                  }),
                }),
              );
            });
          }
          return Promise.resolve({
            data: casePayload({
              candidateSnapshotId: S2,
              candidateOutput: "S2 fresh output",
            }),
          });
        },
      }),
    );
    renderDetail(`/launches/${LAUNCH_ID}?snapshot_id=${S2}&tab=cases`);
    await expandCase(CASE_A);

    await waitFor(() => {
      expect(screen.getByText("S2 fresh output")).toBeInTheDocument();
    });

    // Resolve the stale S1 request after the S2 view is already on screen.
    pending.forEach((resolve) => resolve(null));
    await waitFor(() => {
      expect(screen.getByText("S2 fresh output")).toBeInTheDocument();
    });
    expect(screen.queryByText("S1 late output")).not.toBeInTheDocument();
  });

  it("a late S1 comparison response cannot override the S2 summary", async () => {
    const pending: Array<(value: any) => void> = [];
    installApi(
      baseHandlers({
        comparison: (options?: any) => {
          const snapshot = options?.params?.query?.snapshot_id;
          if (snapshot === S1) {
            return new Promise((resolve) => {
              pending.push(() =>
                resolve({ data: comparisonPayload({ candidateSnapshotId: S1, candidateP95: 111 }) }),
              );
            });
          }
          return Promise.resolve({
            data: comparisonPayload({ candidateSnapshotId: S2, candidateP95: 222 }),
          });
        },
      }),
    );
    renderDetail(`/launches/${LAUNCH_ID}?snapshot_id=${S2}`);

    await waitFor(() => {
      expect(screen.getByText("222")).toBeInTheDocument();
    });
    pending.forEach((resolve) => resolve(null));
    await waitFor(() => {
      expect(screen.getByText("222")).toBeInTheDocument();
    });
    expect(screen.queryByText("111")).not.toBeInTheDocument();
  });
});

describe("U2/F2: formal comparison eligibility is shared by KPI and summary", () => {
  const renderCompare = async (payload: Record<string, any>) => {
    installApi(baseHandlers({ comparison: () => Promise.resolve({ data: payload }) }));
    renderDetail(`/launches/${LAUNCH_ID}?snapshot_id=${S1}&tab=compare`);
    await screen.findByTestId("capabilities-delta-summary");
  };

  it("withholds the formal count and verdict in KPI and summary when comparable=false", async () => {
    const payload = comparisonPayload({ comparable: false, formalAvailable: true, regressionCount: 3 });
    payload.formal.verdict = "REGRESSION";
    await renderCompare(payload);

    expect(screen.getByTestId("regression-count-value")).not.toHaveTextContent("3");
    expect(screen.getByTestId("regression-count-value")).toHaveTextContent("不可比");
    expect(screen.queryByText(/正式比较发现 3 个回归 Case/)).not.toBeInTheDocument();
    expect(screen.getByTestId("formal-comparison-verdict")).toHaveTextContent("暂不可判定");
    expect(screen.getByTestId("comparison-formal-verdict")).toHaveTextContent("无法给出正式结论");
    expect(screen.getByTestId("comparison-comparability-banner")).toBeInTheDocument();
    expect(screen.getByTestId("comparison-diagnostic-label")).toBeInTheDocument();
  });

  it("withholds the formal count and verdict when the comparison never returned comparability", async () => {
    const payload = comparisonPayload({ formalAvailable: true, regressionCount: 3 });
    delete (payload as any).comparability;
    payload.formal.verdict = "REGRESSION";
    await renderCompare(payload);

    expect(screen.getByTestId("regression-count-value")).not.toHaveTextContent("3");
    expect(screen.getByTestId("regression-count-note")).not.toHaveTextContent("3 项性能退化");
    expect(screen.queryByText(/正式比较发现 3 个回归 Case/)).not.toBeInTheDocument();
    expect(screen.getByTestId("formal-comparison-verdict")).toHaveTextContent("暂不可判定");
    expect(screen.getByTestId("comparison-formal-verdict")).toHaveTextContent("无法给出正式结论");
  });

  it("does not turn a missing comparability field into a green no-regression zero", async () => {
    const payload = comparisonPayload({ formalAvailable: true, regressionCount: 0 });
    delete (payload as any).comparability;
    payload.formal.verdict = "UNCHANGED";
    await renderCompare(payload);

    expect(screen.getByTestId("regression-count-value")).not.toHaveTextContent("0");
    expect(screen.getByTestId("regression-count-value")).toHaveTextContent("不可用");
    expect(screen.queryByText(/未发现回归/)).not.toBeInTheDocument();
    expect(screen.queryByText(/正式比较发现 0 个回归 Case/)).not.toBeInTheDocument();
  });

  it("withholds the formal count and verdict when formal evidence is unavailable", async () => {
    const payload = comparisonPayload({
      comparable: true,
      formalAvailable: false,
      regressionCount: 3,
    });
    (payload as any).formal.verdict = "REGRESSION";
    (payload as any).formal.withheld_reasons = ["COVERAGE_INCOMPLETE"];
    await renderCompare(payload);

    expect(screen.getByTestId("regression-count-value")).not.toHaveTextContent("3");
    expect(screen.getByTestId("formal-comparison-verdict")).toHaveTextContent("暂不可判定");
    expect(screen.getByTestId("comparison-formal-verdict")).toHaveTextContent("无法给出正式结论");
    expect(screen.getByTestId("formal-withheld-COVERAGE_INCOMPLETE")).toBeInTheDocument();
    expect(screen.queryByText(/正式比较发现 3 个回归 Case/)).not.toBeInTheDocument();
  });

  it("keeps a real positive count and its verdict when both eligibility fields are true", async () => {
    const payload = comparisonPayload({ comparable: true, formalAvailable: true, regressionCount: 3 });
    payload.formal.verdict = "REGRESSION";
    await renderCompare(payload);

    expect(screen.getByTestId("regression-count-value")).toHaveTextContent("3");
    expect(screen.getByTestId("regression-count-note")).toHaveTextContent("3 项性能退化");
    expect(screen.getByText(/正式比较发现 3 个回归 Case/)).toBeInTheDocument();
    expect(screen.getByTestId("formal-comparison-verdict")).toHaveTextContent("存在回归");
    expect(screen.getByTestId("comparison-formal-verdict")).toHaveTextContent("Regression");
    expect(screen.queryByTestId("comparison-diagnostic-label")).not.toBeInTheDocument();
  });

  it("keeps a real formal zero when both eligibility fields are true", async () => {
    const payload = comparisonPayload({ comparable: true, formalAvailable: true, regressionCount: 0 });
    payload.formal.verdict = "UNCHANGED";
    await renderCompare(payload);

    expect(screen.getByTestId("regression-count-value")).toHaveTextContent("0");
    expect(screen.getByTestId("regression-count-note")).toHaveTextContent("正式比较未发现回归");
    expect(screen.getByTestId("formal-comparison-verdict")).toHaveTextContent("无变化");
    expect(screen.getByText(/正式比较显示相对基线无变化/)).toBeInTheDocument();
  });
});
