import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter, Routes, Route } from "react-router-dom";
import { LaunchDetail } from "../LaunchDetail";

vi.mock("../../../api/client", () => ({
  api: { GET: vi.fn(), POST: vi.fn() },
}));

import { api } from "../../../api/client";

const LAUNCH_ID = "ff04d66b-3d00-4d67-8cf4-d36772fff708";
const SNAPSHOT_ID = "snap-v2-001";

const launch = {
  id: LAUNCH_ID,
  name: "banking-agent-v2",
  status: "COMPLETED",
  quality_conclusion: "fail",
  dataset_name: "banking-agent-regression",
  dataset_version: "2026-10-08T16:08:13Z",
  agent_id: "banking-agent",
  agent_version: "v2",
  langfuse_sync_status: "SYNCED",
  langfuse_experiment_url: null,
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
    agent: { id: "banking-agent", version: "v2" },
    evaluators: [{ id: "pii_safe", version: "1.0.0", required: true }],
    quality_policy: { policy_id: "banking-policy", version: "1.0", rules: [] },
  },
};

const revision = {
  snapshot_id: SNAPSHOT_ID,
  revision: 1,
  is_latest: true,
  created_at: "2026-10-08T16:08:18Z",
  evidence_state: "COMPLETE",
  quality_pass_count: 1,
  quality_fail_count: 1,
  quality_unknown_count: 0,
  source_result_digest: "sha256:snap-digest-v2",
};

const directory = {
  launch_id: LAUNCH_ID,
  latest_snapshot_id: SNAPSHOT_ID,
  latest_revision: 1,
  revisions: [revision],
};

const frozenItem = {
  dataset_item_id: "item-001",
  execution_status: "succeeded",
  eval_status: "succeeded",
  quality_conclusion: "fail",
  scores: { correctness: 0.2 },
  evaluation_results: [],
  quality_evaluation: { conclusion: "fail", rules: [] },
  trace_url: null,
  latency_ms: 123,
  final_attempt_id: null,
  dispatch_generation: 1,
};

const validDetail = (overrides: Record<string, unknown> = {}) => ({
  launch_id: LAUNCH_ID,
  snapshot_id: SNAPSHOT_ID,
  revision: 1,
  created_at: "2026-10-08T16:08:18Z",
  source_result_digest: "sha256:snap-digest-v2",
  manifest_digest: "sha256:manifest-digest",
  evidence_state: "COMPLETE",
  releasable: true,
  versions: {},
  summary: {},
  items: [frozenItem],
  ...overrides,
});

let queryClient: QueryClient;

const detailKey = (...rest: string[]) => ["launches", "result-snapshot", ...rest];

const renderDetail = (search = "") => {
  render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter initialEntries={[`/launches/${LAUNCH_ID}${search}`]}>
        <Routes>
          <Route path="/launches/:launchId" element={<LaunchDetail />} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );
};

/** Realistic router: the detail endpoint answers for the revision that was actually asked for. */
const mockApi = (route: (path: string, options?: any) => any, liveItems: unknown[] = []) => {
  (api.GET as any).mockImplementation((path: string, options?: any) => {
    if (path === "/api/v1/experiment-launches/{launch_id}") {
      return Promise.resolve({ data: launch });
    }
    if (path === "/api/v1/experiment-launches/{launch_id}/items") {
      return Promise.resolve({ data: liveItems });
    }
    return Promise.resolve(route(path, options));
  });
};

const detailRoute = (payload: any) => (path: string) => {
  if (path === "/api/v1/experiment-launches/{launch_id}/result-snapshots") {
    return { data: directory };
  }
  if (path.includes("/result-snapshots/")) return { data: payload };
  if (path === "/api/v1/experiment-launches/{launch_id}/summary") {
    return {
      data: {
        launch_id: LAUNCH_ID,
        snapshot_id: SNAPSHOT_ID,
        summary: {},
        evidence_state: "COMPLETE",
      },
    };
  }
  if (path === "/api/v1/experiment-launches/{launch_id}/comparison") return { data: null };
  return { data: null };
};

const settleDetail = async (expected: "error" | "success") => {
  await waitFor(() =>
    expect(
      queryClient.getQueryState(detailKey(LAUNCH_ID, SNAPSHOT_ID))?.status,
    ).toBe(expected),
  );
};

describe("snapshot report identity isolation (F01)", () => {
  beforeEach(() => {
    queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    vi.clearAllMocks();
  });

  it("rejects a detail response that carries a different Snapshot identity", async () => {
    mockApi(detailRoute(validDetail({ snapshot_id: "OTHER-SNAPSHOT" })));
    renderDetail(`?snapshot_id=${SNAPSHOT_ID}&tab=cases`);

    await settleDetail("error");
    expect(screen.getByText(/快照 ID 不一致/)).toBeInTheDocument();
    expect(screen.queryByTestId("case-row-expand-item-001")).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /全部用例/ })).not.toBeInTheDocument();
  });

  it("rejects a detail response that belongs to another Launch", async () => {
    mockApi(detailRoute(validDetail({ launch_id: "another-launch" })));
    renderDetail(`?snapshot_id=${SNAPSHOT_ID}&tab=cases`);

    await settleDetail("error");
    expect(screen.getByText(/快照归属 Launch 不一致/)).toBeInTheDocument();
  });

  it("rejects a detail response without items instead of presenting a normal empty list", async () => {
    mockApi(detailRoute(validDetail({ items: undefined })));
    renderDetail(`?snapshot_id=${SNAPSHOT_ID}&tab=cases`);

    await settleDetail("error");
    expect(screen.getByText(/items 字段缺失或非数组/)).toBeInTheDocument();
    expect(screen.queryByTestId("empty-state")).not.toBeInTheDocument();
  });

  it("keeps a legitimately empty revision visible as an empty result, not an error", async () => {
    mockApi(detailRoute(validDetail({ items: [] })));
    // A revision that really froze zero cases also reports zero counts.
    (api.GET as any).mockImplementation((path: string) => {
      if (path === "/api/v1/experiment-launches/{launch_id}") {
        return Promise.resolve({ data: launch });
      }
      if (path === "/api/v1/experiment-launches/{launch_id}/items") {
        return Promise.resolve({ data: [] });
      }
      if (path === "/api/v1/experiment-launches/{launch_id}/result-snapshots") {
        return Promise.resolve({
          data: {
            ...directory,
            revisions: [{ ...revision, quality_pass_count: 0, quality_fail_count: 0 }],
          },
        });
      }
      if (path.includes("/result-snapshots/")) {
        return Promise.resolve({ data: validDetail({ items: [] }) });
      }
      if (path === "/api/v1/experiment-launches/{launch_id}/summary") {
        return Promise.resolve({
          data: {
            launch_id: LAUNCH_ID,
            snapshot_id: SNAPSHOT_ID,
            summary: {},
            evidence_state: "COMPLETE",
          },
        });
      }
      return Promise.resolve({ data: null });
    });
    renderDetail(`?snapshot_id=${SNAPSHOT_ID}&tab=cases`);

    await settleDetail("success");
    expect(screen.getByRole("button", { name: /全部用例 \(0\)/ })).toBeInTheDocument();
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });

  it("does not export or copy a payload whose identity failed verification", async () => {
    mockApi(detailRoute(validDetail({ snapshot_id: "OTHER-SNAPSHOT" })));
    renderDetail(`?snapshot_id=${SNAPSHOT_ID}&tab=audit`);

    await settleDetail("error");
    const download = await screen.findByRole("button", { name: "下载原始 JSON" });
    expect(download).toBeDisabled();
  });

  it("exports a verified payload with the identity the user selected", async () => {
    mockApi(detailRoute(validDetail()));
    renderDetail(`?snapshot_id=${SNAPSHOT_ID}&tab=audit`);

    await settleDetail("success");
    expect(await screen.findByRole("button", { name: "下载原始 JSON" })).toBeEnabled();
    // Both the global snapshot panel and the audit header must name the same revision.
    for (const label of screen.getAllByTestId("snapshot-revision")) {
      expect(label).toHaveTextContent("Revision 1");
    }
  });

  it("renders the frozen dispatch generation instead of a newer live generation", async () => {
    mockApi(
      detailRoute(validDetail({ items: [{ ...frozenItem, dispatch_generation: 4 }] })),
      [{ ...frozenItem, id: "live-item-001", dispatch_generation: 9 }],
    );
    renderDetail(`?snapshot_id=${SNAPSHOT_ID}&tab=cases`);

    await settleDetail("success");
    expect(await screen.findByText("gen #4")).toBeInTheDocument();
    expect(screen.queryByText("gen #9")).not.toBeInTheDocument();
  });

  it("still reads an explicitly requested revision when the revision directory fails", async () => {
    // A failing directory must not be reported as "this revision does not exist".
    (api.GET as any).mockImplementation((path: string) => {
      if (path === "/api/v1/experiment-launches/{launch_id}") {
        return Promise.resolve({ data: launch });
      }
      if (path === "/api/v1/experiment-launches/{launch_id}/items") {
        return Promise.resolve({ data: [] });
      }
      if (path === "/api/v1/experiment-launches/{launch_id}/result-snapshots") {
        return Promise.resolve({
          error: { detail: "history temporarily unavailable" },
          response: { status: 503 },
        });
      }
      if (path.includes("/result-snapshots/")) {
        return Promise.resolve({ data: validDetail() });
      }
      return Promise.resolve({ data: null });
    });
    renderDetail(`?snapshot_id=${SNAPSHOT_ID}&tab=cases`);

    await settleDetail("success");
    expect(
      (api.GET as any).mock.calls.some(
        (call: any[]) =>
          call[0].includes("/result-snapshots/{snapshot_id}") &&
          call[1]?.params?.path?.snapshot_id === SNAPSHOT_ID,
      ),
    ).toBe(true);
    expect(screen.getByRole("button", { name: /全部用例 \(1\)/ })).toBeInTheDocument();
  });

  it("does not read any detail for a revision the directory answered without", async () => {
    mockApi(detailRoute(validDetail()));
    renderDetail("?snapshot_id=WRONG&tab=audit");

    await waitFor(() =>
      expect(queryClient.getQueryState(["launches", "result-snapshots", LAUNCH_ID])?.status).toBe(
        "success",
      ),
    );
    expect(
      (api.GET as any).mock.calls.filter((call: any[]) => call[0].includes("/result-snapshots/")),
    ).toHaveLength(0);
    expect(screen.queryByRole("button", { name: "设为新 Baseline" })).not.toBeInTheDocument();
  });
});
