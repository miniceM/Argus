import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { render, screen, waitFor, act, fireEvent } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter, Routes, Route } from "react-router-dom";
import { LaunchDetail } from "../LaunchDetail";
import { SNAPSHOT_DISCOVERY_WINDOW_MS } from "../useSnapshotRevisionDiscovery";

vi.mock("../../../api/client", () => ({
  api: { GET: vi.fn(), POST: vi.fn() },
}));

import { api } from "../../../api/client";
import { queryKeys } from "../../../api/query-keys";

const LAUNCH_ID = "ff04d66b-3d00-4d67-8cf4-d36772fff708";
const S1 = "snap-s1";
const S2 = "snap-s2";

const baseLaunch = {
  id: LAUNCH_ID,
  name: "banking-agent-v2",
  status: "RUNNING",
  quality_conclusion: null,
  dataset_name: "banking-agent-regression",
  agent_id: "banking-agent",
  agent_version: "v2",
  langfuse_sync_status: "PENDING",
  langfuse_experiment_url: null,
  created_at: "2026-10-08T16:08:13Z",
  allowed_actions: ["cancel"],
  manifest: {
    schema_version: "1.0",
    comparison: { environment: "production", baseline_snapshot_id: null },
    dataset: { dataset_name: "banking-agent-regression", items_count: 2 },
    agent: { id: "banking-agent", version: "v2" },
    evaluators: [],
    quality_policy: { policy_id: "banking-policy", version: "1.0", rules: [] },
  },
};

const revisionRow = (snapshotId: string, revision: number, latest: boolean) => ({
  snapshot_id: snapshotId,
  revision,
  is_latest: latest,
  created_at: "2026-10-08T16:08:18Z",
  evidence_state: "COMPLETE",
  quality_pass_count: 2,
  quality_fail_count: 0,
  quality_unknown_count: 0,
  source_result_digest: "sha256:digest",
});

const frozenDetail = (snapshotId: string) => ({
  launch_id: LAUNCH_ID,
  snapshot_id: snapshotId,
  revision: snapshotId === S2 ? 2 : 1,
  created_at: "2026-10-08T16:08:18Z",
  source_result_digest: "sha256:digest",
  manifest_digest: "sha256:manifest",
  evidence_state: "COMPLETE",
  releasable: true,
  versions: {},
  summary: {},
  items: [
    {
      dataset_item_id: "item-001",
      execution_status: "succeeded",
      eval_status: "succeeded",
      quality_conclusion: "pass",
      scores: {},
      trace_url: null,
      latency_ms: 10,
    },
  ],
});

let queryClient: QueryClient;

const renderDetail = (search = "") =>
  render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter initialEntries={[`/launches/${LAUNCH_ID}${search}`]}>
        <Routes>
          <Route path="/launches/:launchId" element={<LaunchDetail />} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );

const historyKey = () => ["launches", "result-snapshots", LAUNCH_ID];

describe("snapshot revision discovery after a run finishes (F02)", () => {
  beforeEach(() => {
    queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    vi.clearAllMocks();
  });

  it("discovers the frozen revision after execution reaches a terminal state", async () => {
    let terminal = false;
    let historyCalls = 0;
    (api.GET as any).mockImplementation((path: string) => {
      if (path === "/api/v1/experiment-launches/{launch_id}") {
        return Promise.resolve({
          data: { ...baseLaunch, status: terminal ? "COMPLETED" : "RUNNING" },
        });
      }
      if (path === "/api/v1/experiment-launches/{launch_id}/items") {
        return Promise.resolve({ data: [] });
      }
      if (path === "/api/v1/experiment-launches/{launch_id}/result-snapshots") {
        historyCalls += 1;
        return Promise.resolve({
          data: terminal
            ? {
                launch_id: LAUNCH_ID,
                latest_snapshot_id: S2,
                latest_revision: 1,
                revisions: [revisionRow(S2, 1, true)],
              }
            : { launch_id: LAUNCH_ID, revisions: [], latest_snapshot_id: null, latest_revision: 0 },
        });
      }
      if (path.includes("/result-snapshots/")) {
        return Promise.resolve({ data: frozenDetail(S2) });
      }
      return Promise.resolve({ data: null });
    });

    renderDetail("?tab=cases");
    expect(await screen.findByTestId("result-snapshot-empty")).toBeInTheDocument();

    const before = historyCalls;
    terminal = true;
    await act(async () => {
      await queryClient.refetchQueries({
        queryKey: queryKeys.launches.detail(LAUNCH_ID),
        exact: true,
      });
    });
    await waitFor(() =>
      expect(
        (queryClient.getQueryData<any>(queryKeys.launches.detail(LAUNCH_ID)) as any)?.status,
      ).toBe("COMPLETED"),
    );

    // The revision is discovered without any manual refresh or window refocus.
    await waitFor(() => expect(historyCalls).toBeGreaterThan(before));
    await waitFor(() =>
      expect(screen.queryByTestId("result-snapshot-empty")).not.toBeInTheDocument(),
    );
    expect(await screen.findByTestId("snapshot-revision")).toHaveTextContent("Revision 1");
    expect(screen.queryByTestId("snapshot-discovering")).not.toBeInTheDocument();
  });

  it("starts bounded revision discovery from retry-evaluation submission when S1 already exists", async () => {
    let showS2 = false;
    let historyCalls = 0;
    (api.GET as any).mockImplementation((path: string) => {
      if (path === "/api/v1/experiment-launches/{launch_id}") {
        return Promise.resolve({ data: { ...baseLaunch, status: "COMPLETED", allowed_actions: ["retry_evaluation"] } });
      }
      if (path === "/api/v1/experiment-launches/{launch_id}/items") {
        // Never expose an evaluating state: the retry can finish between normal /items polls.
        return Promise.resolve({ data: [] });
      }
      if (path === "/api/v1/experiment-launches/{launch_id}/result-snapshots") {
        historyCalls += 1;
        const latest = showS2 && historyCalls >= 3;
        return Promise.resolve({
          data: latest
            ? {
                launch_id: LAUNCH_ID,
                latest_snapshot_id: S2,
                latest_revision: 2,
                revisions: [revisionRow(S2, 2, true), revisionRow(S1, 1, false)],
              }
            : {
                launch_id: LAUNCH_ID,
                latest_snapshot_id: S1,
                latest_revision: 1,
                revisions: [revisionRow(S1, 1, true)],
              },
        });
      }
      if (path.includes("/result-snapshots/")) {
        return Promise.resolve({ data: frozenDetail(S1) });
      }
      return Promise.resolve({ data: null });
    });
    (api.POST as any).mockResolvedValue({ data: { submitted: ["item-001"], blocked: [] } });

    renderDetail(`?snapshot_id=${S1}&tab=cases`);
    await screen.findByTestId("snapshot-revision");
    const historyCallsBeforeRetry = historyCalls;
    fireEvent.click(screen.getByTestId("retry-evaluation-button"));

    await waitFor(() => expect(api.POST).toHaveBeenCalledWith(
      "/api/v1/experiment-launches/{launch_id}/retry-evaluation",
      expect.any(Object),
    ));
    // Let the mutation's one-off invalidation/refetch finish while S1 is still the latest.
    await waitFor(() => expect(historyCalls).toBeGreaterThan(historyCallsBeforeRetry));
    await new Promise((resolve) => setTimeout(resolve, 100));
    expect(screen.getByTestId("snapshot-revision")).toHaveTextContent("Revision 1");

    showS2 = true;
    await waitFor(() => {
      expect(screen.getByTestId("snapshot-newer-available")).toHaveTextContent("Revision 2");
    }, { timeout: 5000 });
    expect(screen.getByTestId("snapshot-revision")).toHaveTextContent("Revision 1");
    expect(historyCalls).toBeGreaterThanOrEqual(3);
  });

  it("keeps a pinned historical revision selected when a newer revision is discovered", async () => {
    let showS2 = false;
    (api.GET as any).mockImplementation((path: string) => {
      if (path === "/api/v1/experiment-launches/{launch_id}") {
        return Promise.resolve({
          data: { ...baseLaunch, status: showS2 ? "COMPLETED" : "RUNNING" },
        });
      }
      if (path === "/api/v1/experiment-launches/{launch_id}/items") {
        return Promise.resolve({ data: [] });
      }
      if (path === "/api/v1/experiment-launches/{launch_id}/result-snapshots") {
        return Promise.resolve({
          data: showS2
            ? {
                launch_id: LAUNCH_ID,
                latest_snapshot_id: S2,
                latest_revision: 2,
                revisions: [revisionRow(S2, 2, true), revisionRow(S1, 1, false)],
              }
            : {
                launch_id: LAUNCH_ID,
                latest_snapshot_id: S1,
                latest_revision: 1,
                revisions: [revisionRow(S1, 1, true)],
              },
        });
      }
      if (path.includes("/result-snapshots/")) {
        const requested = (api.GET as any).mock.calls.length;
        void requested;
        return Promise.resolve({ data: frozenDetail(showS2 ? S2 : S1) });
      }
      return Promise.resolve({ data: null });
    });

    renderDetail(`?snapshot_id=${S1}&tab=cases`);
    await waitFor(() => expect(screen.queryByTestId("result-snapshot-empty")).not.toBeInTheDocument());

    showS2 = true;
    (api.GET as any).mockImplementation((path: string) => {
      if (path === "/api/v1/experiment-launches/{launch_id}") {
        return Promise.resolve({ data: { ...baseLaunch, status: "COMPLETED" } });
      }
      if (path === "/api/v1/experiment-launches/{launch_id}/items") {
        return Promise.resolve({ data: [] });
      }
      if (path === "/api/v1/experiment-launches/{launch_id}/result-snapshots") {
        return Promise.resolve({
          data: {
            launch_id: LAUNCH_ID,
            latest_snapshot_id: S2,
            latest_revision: 2,
            revisions: [revisionRow(S2, 2, true), revisionRow(S1, 1, false)],
          },
        });
      }
      return Promise.resolve({ data: frozenDetail(S2) });
    });
    await act(async () => {
      await queryClient.refetchQueries({ queryKey: historyKey(), exact: true });
    });

    // The selection stays on S1: a new revision never replaces what the user is reading.
    await waitFor(() =>
      expect(screen.getByTestId("snapshot-revision")).toHaveTextContent("Revision 1"),
    );
    expect(screen.getByTestId("snapshot-newer-available")).toHaveTextContent("已有更新的 Revision 2");
    expect(
      (api.GET as any).mock.calls.filter(
        (call: any[]) =>
          call[0].includes("/result-snapshots/{snapshot_id}") &&
          call[1]?.params?.path?.snapshot_id === S2,
      ),
    ).toHaveLength(0);
  });
});

describe("snapshot revision discovery is bounded", () => {
  beforeEach(() => {
    vi.useFakeTimers();
    queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    vi.clearAllMocks();
  });

  afterEach(() => {
    vi.useRealTimers();
  });

  it("stops polling once the discovery window expires", async () => {
    let historyCalls = 0;
    (api.GET as any).mockImplementation((path: string) => {
      if (path === "/api/v1/experiment-launches/{launch_id}") {
        // Execution is already finished: only the initial directory read is reliable.
        return Promise.resolve({ data: { ...baseLaunch, status: "COMPLETED" } });
      }
      if (path === "/api/v1/experiment-launches/{launch_id}/items") {
        return Promise.resolve({ data: [] });
      }
      if (path === "/api/v1/experiment-launches/{launch_id}/result-snapshots") {
        historyCalls += 1;
        return Promise.resolve({
          data: { launch_id: LAUNCH_ID, revisions: [], latest_snapshot_id: null, latest_revision: 0 },
        });
      }
      return Promise.resolve({ data: null });
    });

    renderDetail("?tab=cases");
    await act(async () => {
      await vi.advanceTimersByTimeAsync(2000);
    });
    const duringWindow = historyCalls;

    await act(async () => {
      await vi.advanceTimersByTimeAsync(SNAPSHOT_DISCOVERY_WINDOW_MS + 5000);
    });
    const afterWindow = historyCalls;

    expect(duringWindow).toBeGreaterThan(1);
    expect(afterWindow).toBeGreaterThan(duringWindow);
    // Polling stops: it does not keep hitting the directory forever.
    await act(async () => {
      await vi.advanceTimersByTimeAsync(SNAPSHOT_DISCOVERY_WINDOW_MS * 2);
    });
    expect(historyCalls).toBe(afterWindow);
    expect(screen.getByTestId("result-snapshot-empty")).toBeInTheDocument();
  });
});
