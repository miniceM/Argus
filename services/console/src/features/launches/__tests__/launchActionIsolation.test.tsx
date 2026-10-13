/**
 * Issue #119 main-gap fix — U3: run/cancel/resume/retry_failed/retry_evaluation
 * feedback isolation.
 *
 * Every action request is bound to the Launch it was clicked on and to the page
 * session that was visible at click time. A late success or failure must never
 * write into a different page identity, close a modal that belongs to a newer
 * request, or disable/re-enable an action of another Launch.
 */
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, waitFor, fireEvent, act } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter, Routes, Route, useNavigate, useSearchParams } from "react-router-dom";
import { LaunchDetail } from "../LaunchDetail";
import { api } from "../../../api/client";

vi.mock("../../../api/client", () => ({
  api: { GET: vi.fn(), POST: vi.fn() },
}));

const LAUNCH_A = "ff04d66b-3d00-4d67-8cf4-d36772fff708";
const LAUNCH_B = "aaaaaaaa-3d00-4d67-8cf4-d36772fff708";
const S1 = "snap-v1-001";
const S2 = "snap-v2-001";

const launchFixture = (id: string, allowedActions: string[]) => ({
  id,
  name: `agent-${id.slice(0, 4)}`,
  status: "COMPLETED",
  quality_conclusion: "pass",
  dataset_name: "banking-agent-regression",
  dataset_version: "2026-10-08T16:08:13Z",
  agent_id: "banking-agent",
  agent_version: "v2",
  langfuse_sync_status: "SYNCED",
  langfuse_experiment_url: null,
  created_at: "2026-10-08T16:08:13Z",
  started_at: "2026-10-08T16:08:14Z",
  completed_at: "2026-10-08T16:08:18.82Z",
  allowed_actions: allowedActions,
  manifest: {
    schema_version: "1.0",
    comparison: { environment: "production", baseline_snapshot_id: "base-snap-001" },
    dataset: {
      dataset_name: "banking-agent-regression",
      dataset_version: "2026-10-08T16:08:13Z",
      snapshot_digest: "sha256:19e7bbcbd7c6",
      items_count: 2,
    },
    agent: { id: "banking-agent", version: "v2" },
    evaluators: [{ id: "pii_safe", version: "1.0.0", required: true }],
    quality_policy: { policy_id: "banking-policy", version: "1.0", rules: [] },
  },
});

const itemFixture = (launchId: string, datasetItemId: string) => ({
  id: `exec-${launchId}-${datasetItemId}`,
  launch_id: launchId,
  dataset_item_id: datasetItemId,
  execution_status: "succeeded",
  eval_status: "succeeded",
  quality_conclusion: "pass",
  langfuse_trace_url: null,
  attempt_count: 1,
  final_attempt_http_status: 200,
  final_attempt_latency_ms: 142,
  scores: { pii_safe: 1 },
  quality_evaluation: { conclusion: "pass", rules: [] },
});

const snapshotDirectory = (launchId: string) => ({
  launch_id: launchId,
  latest_snapshot_id: S2,
  latest_revision: 2,
  revisions: [
    { snapshot_id: S2, revision: 2, is_latest: true, created_at: "2026-10-08T16:08:18Z", evidence_state: "COMPLETE", quality_pass_count: 2, quality_fail_count: 0, quality_unknown_count: 0, source_result_digest: "sha256:v2" },
    { snapshot_id: S1, revision: 1, is_latest: false, created_at: "2026-10-08T16:08:10Z", evidence_state: "COMPLETE", quality_pass_count: 2, quality_fail_count: 0, quality_unknown_count: 0, source_result_digest: "sha256:v1" },
  ],
});

const comparisonPayload = (launchId: string, snapshotId: string) => ({
  launch_id: launchId,
  candidate_snapshot_id: snapshotId,
  baseline_snapshot_id: "base-snap-001",
  versions: {},
  summary: {
    comparable_case_count: 0,
    candidate: {},
    baseline: null,
    comparable_cohort: null,
    cost_comparison: { status: "NOT_COMPARABLE", reason: "BASELINE_NOT_BOUND" },
  },
  classification_counts: { ALL: 0, REGRESSION: 0, IMPROVEMENT: 0, UNCHANGED: 0, NOT_COMPARABLE: 0 },
  comparability: { comparable: false, reason_codes: [], provenance: "VERIFIED", dimensions: [], suggestions: [] },
  formal: { available: false, verdict: null, required_cases: 0, comparable_cases: 0, coverage: 0 },
  diagnostic: { note: "", comparable_cases: 0, classification_counts: {} },
  items: [],
});

const Navigation: React.FC<{ targets: string[] }> = ({ targets }) => {
  const navigate = useNavigate();
  const [, setParams] = useSearchParams();
  return (
    <div>
      {targets.map((target) =>
        target.startsWith("launch:") ? (
          <button
            key={target}
            data-testid={`go-${target.slice(7)}`}
            onClick={() => navigate(`/launches/${target.slice(7)}`)}
          >
            go {target.slice(7)}
          </button>
        ) : (
          <button
            key={target}
            data-testid={`snap-${target}`}
            onClick={() =>
              setParams((prev) => {
                const next = new URLSearchParams(prev);
                next.set("snapshot_id", target);
                return next;
              })
            }
          >
            snap {target}
          </button>
        ),
      )}
    </div>
  );
};

let queryClient: QueryClient;
let postCalls: Array<{ path: string; body?: unknown; launchId?: string }>;
let pendingPosts: Array<(value: any) => void>;

const installApi = (launches: Record<string, string[]>) => {
  (api.GET as any).mockImplementation((path: string, options?: any) => {
    const launchId = options?.params?.path?.launch_id ?? LAUNCH_A;
    if (path === "/api/v1/experiment-launches/{launch_id}") {
      return Promise.resolve({ data: launchFixture(launchId, launches[launchId] ?? ["retry_evaluation"]) });
    }
    if (path === "/api/v1/experiment-launches/{launch_id}/items") {
      return Promise.resolve({ data: [itemFixture(launchId, "item-001"), itemFixture(launchId, "item-002")] });
    }
    if (path === "/api/v1/experiment-launches/{launch_id}/result-snapshots") {
      return Promise.resolve({ data: snapshotDirectory(launchId) });
    }
    if (path.includes("/result-snapshots/")) {
      const requested = options?.params?.path?.snapshot_id ?? S1;
      return Promise.resolve({
        data: {
          launch_id: launchId,
          snapshot_id: requested,
          revision: requested === S1 ? 1 : 2,
          is_latest: requested === S2,
          created_at: "2026-10-08T16:08:18Z",
          evidence_state: "COMPLETE",
          releasable: true,
          manifest: launchFixture(launchId, []).manifest,
          summary: {},
          items: [itemFixture(launchId, "item-001"), itemFixture(launchId, "item-002")],
        },
      });
    }
    if (path === "/api/v1/experiment-launches/{launch_id}/summary") {
      return Promise.resolve({
        data: {
          launch_id: launchId,
          snapshot_id: options?.params?.query?.snapshot_id ?? S1,
          summary: {},
          evidence_state: "COMPLETE",
        },
      });
    }
    if (path === "/api/v1/experiment-launches/{launch_id}/comparison") {
      return Promise.resolve({
        data: comparisonPayload(launchId, options?.params?.query?.snapshot_id ?? S1),
      });
    }
    return Promise.resolve({ data: null });
  });

  (api.POST as any).mockImplementation((path: string, options?: any) => {
    postCalls.push({ path, body: options?.body, launchId: options?.params?.path?.launch_id });
    return new Promise((resolve) => {
      pendingPosts.push(resolve);
    });
  });
};

const renderDetail = (initialUrl: string, navigationTargets: string[] = []) =>
  render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter initialEntries={[initialUrl]}>
        <Navigation targets={navigationTargets} />
        <Routes>
          <Route path="/launches/:launchId" element={<LaunchDetail />} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );

const expectPostCount = async (count: number) => {
  await waitFor(() => {
    expect(postCalls.length).toBeGreaterThanOrEqual(count);
  });
  expect(postCalls).toHaveLength(count);
};

const settleFirstPost = async (outcome: "success" | "failure", payload?: any) => {
  const resolve = pendingPosts[0];
  expect(resolve).toBeDefined();
  await act(async () => {
    if (outcome === "success") {
      resolve({ data: payload ?? { submitted: ["item-001"], blocked: [] } });
    } else {
      resolve({ error: { detail: "stale-action-failure" } });
    }
    await Promise.resolve();
  });
};

beforeEach(() => {
  queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  postCalls = [];
  pendingPosts = [];
  vi.clearAllMocks();
});

describe("U3: launch action feedback isolation", () => {
  it("a late retry-evaluation failure cannot pollute the next revision page", async () => {
    installApi({ [LAUNCH_A]: ["retry_evaluation"] });
    renderDetail(`/launches/${LAUNCH_A}?snapshot_id=${S1}`, [S2]);

    fireEvent.click(await screen.findByTestId("retry-evaluation-button"));
    await expectPostCount(1);
    expect(postCalls[0].path).toContain("/retry-evaluation");
    expect(postCalls[0].launchId).toBe(LAUNCH_A);

    // Switch to S2 while the request is still in flight.
    fireEvent.click(screen.getByTestId(`snap-${S2}`));
    await waitFor(() => {
      expect(screen.getByTestId(`snap-${S2}`)).toBeInTheDocument();
    });

    await settleFirstPost("failure");

    expect(postCalls).toHaveLength(1);
    expect(screen.queryByText("stale-action-failure")).not.toBeInTheDocument();
    expect(screen.queryByTestId("retry-evaluation-notice")).not.toBeInTheDocument();
  });

  it("a late retry-evaluation success does not show its notice on another revision", async () => {
    installApi({ [LAUNCH_A]: ["retry_evaluation"] });
    renderDetail(`/launches/${LAUNCH_A}?snapshot_id=${S1}`, [S2]);

    fireEvent.click(await screen.findByTestId("retry-evaluation-button"));
    fireEvent.click(screen.getByTestId(`snap-${S2}`));
    await waitFor(() => {
      expect(screen.getByTestId(`snap-${S2}`)).toBeInTheDocument();
    });

    await settleFirstPost("success");

    expect(screen.queryByTestId("retry-evaluation-notice")).not.toBeInTheDocument();
  });

  it("a late failure from another Launch cannot pollute the target Launch page", async () => {
    installApi({ [LAUNCH_A]: ["retry_evaluation"], [LAUNCH_B]: ["retry_evaluation"] });
    renderDetail(`/launches/${LAUNCH_A}?snapshot_id=${S1}`, [`launch:${LAUNCH_B}`]);

    fireEvent.click(await screen.findByTestId("retry-evaluation-button"));
    await expectPostCount(1);
    expect(postCalls[0].launchId).toBe(LAUNCH_A);

    fireEvent.click(screen.getByTestId(`go-${LAUNCH_B}`));
    await waitFor(() => {
      expect(screen.getByTestId(`go-${LAUNCH_B}`)).toBeInTheDocument();
    });

    await settleFirstPost("failure");

    expect(screen.queryByText("stale-action-failure")).not.toBeInTheDocument();
    expect(screen.queryByTestId("retry-evaluation-notice")).not.toBeInTheDocument();
  });

  it("another Launch's pending request does not disable or block the target Launch action", async () => {
    installApi({ [LAUNCH_A]: ["retry_evaluation"], [LAUNCH_B]: ["retry_evaluation"] });
    renderDetail(`/launches/${LAUNCH_A}?snapshot_id=${S1}`, [`launch:${LAUNCH_B}`]);

    fireEvent.click(await screen.findByTestId("retry-evaluation-button"));
    await expectPostCount(1);

    fireEvent.click(screen.getByTestId(`go-${LAUNCH_B}`));
    const targetButton = await screen.findByTestId("retry-evaluation-button");
    expect(targetButton).not.toBeDisabled();

    fireEvent.click(targetButton);
    await expectPostCount(2);
    expect(postCalls[1].launchId).toBe(LAUNCH_B);
  });

  it("an A-B-A round trip keeps the original request session invalid", async () => {
    installApi({ [LAUNCH_A]: ["retry_evaluation"], [LAUNCH_B]: ["retry_evaluation"] });
    renderDetail(`/launches/${LAUNCH_A}?snapshot_id=${S1}`, [`launch:${LAUNCH_B}`, `launch:${LAUNCH_A}`]);

    fireEvent.click(await screen.findByTestId("retry-evaluation-button"));
    await expectPostCount(1);
    fireEvent.click(screen.getByTestId(`go-${LAUNCH_B}`));
    await waitFor(() => {
      expect(screen.getByTestId(`go-${LAUNCH_B}`)).toBeInTheDocument();
    });
    fireEvent.click(screen.getByTestId(`go-${LAUNCH_A}`));
    await waitFor(() => {
      expect(screen.getByTestId("retry-evaluation-button")).toBeInTheDocument();
    });

    await settleFirstPost("failure");

    expect(screen.queryByText("stale-action-failure")).not.toBeInTheDocument();
    expect(screen.queryByTestId("retry-evaluation-notice")).not.toBeInTheDocument();
  });

  it("unmounting before settle neither writes feedback nor cancels the request", async () => {
    installApi({ [LAUNCH_A]: ["retry_evaluation"] });
    const view = renderDetail(`/launches/${LAUNCH_A}?snapshot_id=${S1}`);

    fireEvent.click(await screen.findByTestId("retry-evaluation-button"));
    await expectPostCount(1);
    view.unmount();

    await settleFirstPost("success");
    await expectPostCount(1);
  });

  it("a retry notice is cleared as soon as the page identity changes", async () => {
    installApi({ [LAUNCH_A]: ["retry_evaluation"] });
    renderDetail(`/launches/${LAUNCH_A}?snapshot_id=${S1}`, [S2]);

    fireEvent.click(await screen.findByTestId("retry-evaluation-button"));
    await expectPostCount(1);
    await settleFirstPost("success");
    expect(screen.getByTestId("retry-evaluation-notice")).toBeInTheDocument();

    fireEvent.click(screen.getByTestId(`snap-${S2}`));
    await waitFor(() => {
      expect(screen.queryByTestId("retry-evaluation-notice")).not.toBeInTheDocument();
    });
  });

  it("a pending action cannot be re-POSTed for the same Launch across a revision switch", async () => {
    installApi({ [LAUNCH_A]: ["retry_evaluation"] });
    renderDetail(`/launches/${LAUNCH_A}?snapshot_id=${S1}`, [S2]);

    fireEvent.click(await screen.findByTestId("retry-evaluation-button"));
    await expectPostCount(1);

    // Revision switch must not duplicate or release the in-flight request.
    fireEvent.click(screen.getByTestId(`snap-${S2}`));
    const stillPending = await screen.findByTestId("retry-evaluation-button");
    expect(stillPending).toBeDisabled();
    fireEvent.click(stillPending);
    await expectPostCount(1);

    // After the request settles, a fresh action on the same Launch is allowed.
    await settleFirstPost("success");
    const next = await screen.findByTestId("retry-evaluation-button");
    expect(next).not.toBeDisabled();
    fireEvent.click(next);
    await expectPostCount(2);
  });

  it("cancel keeps its confirmation and posts exactly once to the clicked Launch", async () => {
    installApi({ [LAUNCH_A]: ["cancel"] });
    renderDetail(`/launches/${LAUNCH_A}?snapshot_id=${S1}`);

    fireEvent.click(await screen.findByRole("button", { name: /取消评测/ }));
    await waitFor(() => {
      expect(screen.getByText("确认取消评测任务")).toBeInTheDocument();
    });
    expect(postCalls).toHaveLength(0);

    fireEvent.click(screen.getByRole("button", { name: /确认取消/ }));
    await expectPostCount(1);
    expect(postCalls[0].path).toContain("/cancel");
    expect(postCalls[0].launchId).toBe(LAUNCH_A);
  });

  it("retry-failed keeps the force replay confirmation and its payload", async () => {
    installApi({ [LAUNCH_A]: ["retry_failed"] });
    renderDetail(`/launches/${LAUNCH_A}?snapshot_id=${S1}`);

    fireEvent.click(await screen.findByRole("button", { name: /重试失败用例/ }));
    await waitFor(() => {
      expect(screen.getByText(/Force Replay/)).toBeInTheDocument();
    });
    expect(postCalls).toHaveLength(0);

    fireEvent.click(screen.getByRole("checkbox"));
    fireEvent.click(screen.getByRole("button", { name: /确认重新调度/ }));

    await expectPostCount(1);
    expect(postCalls[0].path).toContain("/retry-failed");
    expect(postCalls[0].body).toEqual({ force: true });
  });

  it("run and resume keep their semantics on the current Launch", async () => {
    installApi({ [LAUNCH_A]: ["run", "resume"] });
    renderDetail(`/launches/${LAUNCH_A}?snapshot_id=${S1}`);

    await screen.findByRole("button", { name: /启动评测/ });
    fireEvent.click(await screen.findByRole("button", { name: /启动评测/ }));
    await expectPostCount(1);
    expect(postCalls[0].path).toContain("/run");
    expect(postCalls[0].launchId).toBe(LAUNCH_A);

    await settleFirstPost("success");
    fireEvent.click(await screen.findByRole("button", { name: /断点恢复/ }));
    await expectPostCount(2);
    expect(postCalls[1].path).toContain("/resume");
  });

  it("a legitimate notice survives a same-snapshot tab round trip", async () => {
    installApi({ [LAUNCH_A]: ["retry_evaluation"] });
    renderDetail(`/launches/${LAUNCH_A}?snapshot_id=${S1}&tab=compare`);

    fireEvent.click(await screen.findByTestId("retry-evaluation-button"));
    await expectPostCount(1);
    await settleFirstPost("success");
    expect(screen.getByTestId("retry-evaluation-notice")).toBeInTheDocument();

    fireEvent.click(screen.getByRole("tab", { name: /用例排查与 Trace/ }));
    fireEvent.click(screen.getByRole("tab", { name: /门禁与版本对比/ }));

    expect(screen.getByTestId("retry-evaluation-notice")).toBeInTheDocument();
  });

  it("retry-failed defaults to force=false and keeps the explicit confirmation", async () => {
    installApi({ [LAUNCH_A]: ["retry_failed"] });
    renderDetail(`/launches/${LAUNCH_A}?snapshot_id=${S1}`);

    fireEvent.click(await screen.findByRole("button", { name: /重试失败用例/ }));
    await waitFor(() => {
      expect(screen.getByText(/Force Replay/)).toBeInTheDocument();
    });
    fireEvent.click(screen.getByRole("button", { name: /确认重新调度/ }));

    await expectPostCount(1);
    expect(postCalls[0].body).toEqual({ force: false });
  });
});
