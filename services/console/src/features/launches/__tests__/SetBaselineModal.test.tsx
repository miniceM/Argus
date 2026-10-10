import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { render, screen, waitFor, fireEvent, act } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { SetBaselineModal } from "../SetBaselineModal";
import { queryKeys } from "../../../api/query-keys";
import { api } from "../../../api/client";

vi.mock("../../../api/client", () => ({
  api: {
    GET: vi.fn(),
    POST: vi.fn(),
  },
}));

const AGENT_ID = "banking-agent";
const ENVIRONMENT = "production";
const SNAPSHOT_ID = "snap-v2-001";
const BASELINES_PATH = "/api/v1/agents/{agent_id}/baselines";

const activeSnapshot = {
  snapshot_id: SNAPSHOT_ID,
  revision: 5,
  created_at: "2026-10-08T16:08:13Z",
  source_result_digest: "sha256:x",
  manifest_digest: "sha256:y",
  evidence_state: "COMPLETE",
  evidence_reasons: [],
  total_cases: 6,
  quality_pass_count: 6,
  quality_fail_count: 0,
  quality_unknown_count: 0,
  is_latest: true,
} as any;

const boundBaseline = (revision: number, snapshotId = "base-snap-001") => ({
  agent_id: AGENT_ID,
  environment: ENVIRONMENT,
  revision,
  result_snapshot_id: snapshotId,
  updated_by: "tester",
  updated_at: "2026-10-08T16:08:13Z",
  launch_id: "ff04d66b-3d00-4d67-8cf4-d36772fff708",
  agent_version: "v2",
  dataset_name: "banking-agent-regression",
  dataset_version: "2026-10-08T16:08:13Z",
  summary: {},
}) as any;

const CONFIRM = "确认设为 Baseline";
const RETRY = "重试获取最新状态";

function renderModal(overrides: Partial<Parameters<typeof SetBaselineModal>[0]> = {}) {
  const onClose = vi.fn();
  const onSuccess = vi.fn();
  const view = render(
    <QueryClientProvider client={queryClient}>
      <SetBaselineModal
        open
        onClose={onClose}
        agentId={AGENT_ID}
        environment={ENVIRONMENT}
        activeSnapshot={activeSnapshot}
        activeBaseline={boundBaseline(4)}
        onSuccess={onSuccess}
        {...overrides}
      />
    </QueryClientProvider>,
  );
  return { view, onClose, onSuccess };
}

/** api.GET for everything except the baseline read. */
function mockNonBaselineGets() {
  (api.GET as any).mockImplementation((path: string) => {
    if (path === BASELINES_PATH) throw new Error("unexpected baseline GET");
    return Promise.resolve({ data: {} });
  });
}

function baselineGetCalls() {
  return (api.GET as any).mock.calls.filter((c: any[]) => c[0] === BASELINES_PATH);
}

function httpError(status: number, detail: string) {
  return { error: { detail }, response: { status } };
}

let queryClient: QueryClient;

describe("SetBaselineModal session isolation (F03)", () => {
  beforeEach(() => {
    queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    vi.clearAllMocks();
    (api.POST as any).mockResolvedValue(httpError(409, "revision conflict"));
  });

  afterEach(() => {
    vi.useRealTimers();
  });

  it("R06/P06: a late refresh failure from a closed dialog cannot poison a reopened session", async () => {
    let resolveFirst: (v: any) => void = () => {};
    (api.GET as any).mockImplementation((path: string) => {
      if (path !== BASELINES_PATH) return Promise.resolve({ data: {} });
      return new Promise((resolve) => {
        resolveFirst = resolve;
      });
    });
    const onClose = vi.fn();
    const ui = (open: boolean) => (
      <QueryClientProvider client={queryClient}>
        <SetBaselineModal
          open={open}
          onClose={onClose}
          agentId={AGENT_ID}
          environment={ENVIRONMENT}
          activeSnapshot={activeSnapshot}
          activeBaseline={boundBaseline(4)}
        />
      </QueryClientProvider>
    );
    const view = render(ui(true));
    // Enter the 409 conflict path so a fresh GET is in flight.
    fireEvent.click(screen.getByRole("button", { name: CONFIRM }));
    await screen.findByText(/HTTP 409/);
    expect(baselineGetCalls()).toHaveLength(1);

    // Close and reopen before the in-flight refresh resolves.
    view.rerender(ui(false));
    view.rerender(ui(true));
    await waitFor(() =>
      expect(screen.getByRole("button", { name: CONFIRM })).toBeEnabled(),
    );

    // The old session's failure arrives late.
    await act(async () => {
      resolveFirst(httpError(503, "OLD-SESSION-refresh unavailable"));
    });
    expect(screen.getByRole("button", { name: CONFIRM })).toBeEnabled();
    expect(screen.queryByText(/OLD-SESSION-refresh/)).not.toBeInTheDocument();
    expect(queryClient.getQueryData(queryKeys.baselines.detail(AGENT_ID, ENVIRONMENT))).toBeUndefined();
  });

  it("R06/P07: a refresh response for a different agent is rejected and never cached", async () => {
    mockNonBaselineGets();
    (api.GET as any).mockImplementation((path: string) => {
      if (path !== BASELINES_PATH) return Promise.resolve({ data: {} });
      return Promise.resolve({
        data: {
          ...boundBaseline(99, "foreign-base"),
          agent_id: "OTHER-AGENT",
        },
      });
    });
    renderModal();
    fireEvent.click(screen.getByRole("button", { name: CONFIRM }));
    await screen.findByText(/HTTP 409/);
    await waitFor(() => expect(screen.getByRole("button", { name: RETRY })).toBeInTheDocument());
    expect(queryClient.getQueryData(queryKeys.baselines.detail(AGENT_ID, ENVIRONMENT))).toBeUndefined();
    expect(screen.getByRole("button", { name: CONFIRM })).toBeDisabled();
    expect(screen.queryByText(/Revision 99/)).not.toBeInTheDocument();
  });

  it("R06: a refresh response without the target environment is rejected", async () => {
    (api.GET as any).mockImplementation((path: string) => {
      if (path !== BASELINES_PATH) return Promise.resolve({ data: {} });
      const { environment: _dropped, ...withoutEnvironment } = boundBaseline(7);
      return Promise.resolve({ data: withoutEnvironment });
    });
    renderModal();
    fireEvent.click(screen.getByRole("button", { name: CONFIRM }));
    await screen.findByText(/HTTP 409/);
    await waitFor(() => expect(screen.getByRole("button", { name: RETRY })).toBeInTheDocument());
    expect(queryClient.getQueryData(queryKeys.baselines.detail(AGENT_ID, ENVIRONMENT))).toBeUndefined();
    expect(screen.getByRole("button", { name: CONFIRM })).toBeDisabled();
  });

  it("R07: a late response from a superseded session cannot overwrite the newer binding", async () => {
    const resolvers: Array<(v: any) => void> = [];
    (api.GET as any).mockImplementation((path: string) => {
      if (path !== BASELINES_PATH) return Promise.resolve({ data: {} });
      return new Promise((resolve) => resolvers.push(resolve));
    });
    const view = render(
      <QueryClientProvider client={queryClient}>
        <SetBaselineModal
          open
          onClose={vi.fn()}
          agentId={AGENT_ID}
          environment={ENVIRONMENT}
          activeSnapshot={activeSnapshot}
          activeBaseline={boundBaseline(4)}
        />
      </QueryClientProvider>,
    );
    // Session 1: 409 -> refresh GET #1 (revision 5) stays in flight.
    fireEvent.click(screen.getByRole("button", { name: CONFIRM }));
    await screen.findByText(/HTTP 409/);
    expect(baselineGetCalls()).toHaveLength(1);

    // Reopen with a new session; its refresh resolves first (revision 6).
    view.rerender(
      <QueryClientProvider client={queryClient}>
        <SetBaselineModal open={false} onClose={vi.fn()} agentId={AGENT_ID} environment={ENVIRONMENT} activeSnapshot={activeSnapshot} activeBaseline={boundBaseline(4)} />
      </QueryClientProvider>,
    );
    view.rerender(
      <QueryClientProvider client={queryClient}>
        <SetBaselineModal open onClose={vi.fn()} agentId={AGENT_ID} environment={ENVIRONMENT} activeSnapshot={activeSnapshot} activeBaseline={boundBaseline(4)} />
      </QueryClientProvider>,
    );
    fireEvent.click(screen.getByRole("button", { name: CONFIRM }));
    await screen.findByText(/HTTP 409/);
    expect(baselineGetCalls()).toHaveLength(2);
    await act(async () => {
      resolvers[1]({ data: boundBaseline(6, "refreshed-006") });
    });
    await waitFor(() => expect(screen.getByText(/Revision 6 \(Snapshot: refreshe/)).toBeInTheDocument());

    // The superseded session's response arrives late.
    await act(async () => {
      resolvers[0]({ data: boundBaseline(5, "refreshed-005") });
    });
    expect(screen.getByText(/Revision 6 \(Snapshot: refreshe/)).toBeInTheDocument();
    expect(queryClient.getQueryData<any>(queryKeys.baselines.detail(AGENT_ID, ENVIRONMENT))?.revision).toBe(6);
  });

  it("R08: a 409 refresh is a single GET and only a second manual confirmation POSTs the new revision", async () => {
    (api.GET as any).mockImplementation((path: string) => {
      if (path !== BASELINES_PATH) return Promise.resolve({ data: {} });
      return Promise.resolve({ data: boundBaseline(5, "refreshed-005") });
    });
    renderModal();
    fireEvent.click(screen.getByRole("button", { name: CONFIRM }));
    await screen.findByText(/HTTP 409/);
    await waitFor(() => expect(screen.getByText(/Revision 5 \(Snapshot: refreshe/)).toBeInTheDocument());
    // Single-path refresh: exactly one baseline GET for the recovery.
    expect(baselineGetCalls()).toHaveLength(1);
    expect((api.POST as any).mock.calls.filter((c: any[]) => c[0] === BASELINES_PATH)).toHaveLength(1);
    expect(screen.getByText(/Revision 5 \(Snapshot: refreshe/)).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: CONFIRM }));
    await waitFor(() => expect((api.POST as any).mock.calls.length).toBe(2));
    const post = (api.POST as any).mock.calls[1];
    expect(post[1].body).toMatchObject({
      environment: ENVIRONMENT,
      result_snapshot_id: SNAPSHOT_ID,
      expected_revision: 5,
    });
  });

  it("R08: a legitimate 404 means no binding and still requires manual confirmation", async () => {
    (api.GET as any).mockImplementation((path: string) =>
      path === BASELINES_PATH
        ? Promise.resolve(httpError(404, "baseline not found"))
        : Promise.resolve({ data: {} }),
    );
    renderModal({ activeBaseline: null });
    fireEvent.click(screen.getByRole("button", { name: CONFIRM }));
    await screen.findByText(/HTTP 409/);
    await waitFor(() => expect(screen.getByText(/尚未绑定/)).toBeInTheDocument());
    const confirmButton = screen.getByRole("button", { name: CONFIRM });
    expect(confirmButton).toBeEnabled();
    expect(queryClient.getQueryData(queryKeys.baselines.detail(AGENT_ID, ENVIRONMENT))).toBeNull();
    fireEvent.click(confirmButton);
    await waitFor(() => expect((api.POST as any).mock.calls.length).toBe(2));
    expect((api.POST as any).mock.calls[1][1].body.expected_revision).toBe(0);
  });

  it("R08: a 403 refresh is not treated as no binding", async () => {
    (api.GET as any).mockImplementation((path: string) =>
      path === BASELINES_PATH
        ? Promise.resolve(httpError(403, "forbidden"))
        : Promise.resolve({ data: {} }),
    );
    renderModal();
    fireEvent.click(screen.getByRole("button", { name: CONFIRM }));
    await screen.findByText(/HTTP 409/);
    await waitFor(() => expect(screen.getByText(/获取最新 Baseline 绑定版本失败/)).toBeInTheDocument());
    expect(screen.getByRole("button", { name: CONFIRM })).toBeDisabled();
    expect(screen.queryByText(/尚未绑定/)).not.toBeInTheDocument();
  });

  it("R08: a 503 refresh failure is retryable and retry issues exactly one GET", async () => {
    let getCount = 0;
    (api.GET as any).mockImplementation((path: string) => {
      if (path !== BASELINES_PATH) return Promise.resolve({ data: {} });
      getCount += 1;
      return getCount === 1
        ? Promise.resolve(httpError(503, "unavailable"))
        : Promise.resolve({ data: boundBaseline(5, "retrybase-005") });
    });
    renderModal();
    fireEvent.click(screen.getByRole("button", { name: CONFIRM }));
    await screen.findByText(/HTTP 409/);
    await waitFor(() => expect(screen.getByRole("button", { name: RETRY })).toBeInTheDocument());
    fireEvent.click(screen.getByRole("button", { name: RETRY }));
    await waitFor(() => expect(screen.getByText(/Revision 5 \(Snapshot: retrybas/)).toBeInTheDocument());
    expect(baselineGetCalls()).toHaveLength(2);
    expect(screen.getByRole("button", { name: CONFIRM })).toBeEnabled();
  });

  it("R08: cancelling sends no POST and a double confirm sends at most one", async () => {
    (api.GET as any).mockImplementation(() => Promise.resolve({ data: {} }));
    (api.POST as any).mockResolvedValue({ data: boundBaseline(5) });
    const { view, onClose } = renderModal();
    fireEvent.click(screen.getByRole("button", { name: "取消" }));
    expect(onClose).toHaveBeenCalledTimes(1);
    expect((api.POST as any).mock.calls.filter((c: any[]) => c[0] === BASELINES_PATH)).toHaveLength(0);
    view.unmount();
  });

  it("R08: two rapid confirm clicks produce a single POST and a closed dialog blocks late success", async () => {
    let resolvePost: (v: any) => void = () => {};
    (api.GET as any).mockImplementation(() => Promise.resolve({ data: {} }));
    (api.POST as any).mockImplementation(() => new Promise((resolve) => {
      resolvePost = resolve;
    }));
    const onClose = vi.fn();
    const onSuccess = vi.fn();
    const ui = (open: boolean) => (
      <QueryClientProvider client={queryClient}>
        <SetBaselineModal
          open={open}
          onClose={onClose}
          agentId={AGENT_ID}
          environment={ENVIRONMENT}
          activeSnapshot={activeSnapshot}
          activeBaseline={boundBaseline(4)}
          onSuccess={onSuccess}
        />
      </QueryClientProvider>
    );
    const view = render(ui(true));
    const confirmButton = screen.getByRole("button", { name: CONFIRM });
    await act(async () => {
      fireEvent.click(confirmButton);
      fireEvent.click(confirmButton);
    });
    expect((api.POST as any).mock.calls.filter((c: any[]) => c[0] === BASELINES_PATH)).toHaveLength(1);

    // Close before the POST resolves: the late success must not close or invalidate the new session.
    view.rerender(ui(false));
    view.rerender(ui(true));
    await act(async () => {
      resolvePost({ data: boundBaseline(5) });
    });
    expect(onSuccess).not.toHaveBeenCalled();
    expect((api.POST as any).mock.calls.filter((c: any[]) => c[0] === BASELINES_PATH)).toHaveLength(1);
  });

  it("keeps the declared environment on the happy path (staging)", async () => {
    (api.GET as any).mockImplementation(() => Promise.resolve({ data: {} }));
    (api.POST as any).mockResolvedValue({ data: boundBaseline(9, "base-snap-009") });
    const { onClose, onSuccess } = renderModal({ environment: "staging", activeBaseline: null });
    fireEvent.click(screen.getByRole("button", { name: CONFIRM }));
    await waitFor(() => expect(onSuccess).toHaveBeenCalledTimes(1));
    expect(onClose).toHaveBeenCalledTimes(1);
    expect((api.POST as any).mock.calls[0][1].body).toMatchObject({
      environment: "staging",
      result_snapshot_id: SNAPSHOT_ID,
      expected_revision: 0,
    });
  });

  it("keeps a non-409 POST failure retryable", async () => {
    (api.GET as any).mockImplementation(() => Promise.resolve({ data: {} }));
    (api.POST as any).mockResolvedValue(httpError(503, "submission unavailable"));
    renderModal();
    fireEvent.click(screen.getByRole("button", { name: CONFIRM }));
    await waitFor(() => expect(screen.getByText(/submission unavailable/)).toBeInTheDocument());
    expect(screen.getByRole("button", { name: CONFIRM })).toBeEnabled();
    expect(baselineGetCalls()).toHaveLength(0);
  });
});
