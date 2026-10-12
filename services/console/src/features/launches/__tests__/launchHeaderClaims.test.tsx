/**
 * Issue #119 main-gap fix — U5: quality, evidence and eligibility claims.
 *
 * Frozen quality, evidence completeness, Baseline eligibility, Langfuse sync
 * and the independent Release Gate are separate facts. The header must not
 * derive one from another: a frozen PASS is not release admission, COMPLETE
 * evidence is not a verified signature, and a missing snapshot is not a
 * diagnostic one.
 */
import { describe, it, expect, vi, afterEach } from "vitest";
import { render, screen } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter } from "react-router-dom";
import { LaunchHeader } from "../LaunchHeader";
import { ResultSnapshotPanel } from "../resultSnapshot";
import { api } from "../../../api/client";

vi.mock("../../../api/client", () => ({ api: { GET: vi.fn(), POST: vi.fn() } }));

type Rev = Record<string, unknown>;

const revision = (over: Partial<Rev> = {}): Rev => ({
  snapshot_id: "snap-1",
  revision: 1,
  created_at: "2026-09-30T00:00:00Z",
  source_result_digest: "abcdef0123456789abcdef0123456789",
  manifest_digest: "manifest-digest",
  evidence_state: "COMPLETE",
  evidence_reasons: [],
  total_cases: 6,
  quality_pass_count: 6,
  quality_fail_count: 0,
  quality_unknown_count: 0,
  is_latest: true,
  ...over,
});

const list = (revisions: Rev[]) => ({
  launch_id: "launch-119",
  latest_snapshot_id: revisions[0]?.snapshot_id ?? null,
  latest_revision: revisions[0]?.revision ?? null,
  revisions,
});

const detail = (rev: Rev, over: Record<string, unknown> = {}) => ({
  launch_id: "launch-119",
  snapshot_id: rev.snapshot_id,
  revision: rev.revision,
  created_at: rev.created_at,
  source_result_digest: rev.source_result_digest,
  manifest_digest: rev.manifest_digest,
  evidence_state: rev.evidence_state,
  evidence_reasons: rev.evidence_reasons,
  releasable: rev.evidence_state === "COMPLETE",
  manifest: {},
  versions: {},
  summary: {},
  items: [],
  ...over,
});

const mockSnapshots = (revisions: Rev[]) => {
  (api.GET as any).mockImplementation((path: string, options?: any) => {
    if (path.endsWith("/result-snapshots")) return Promise.resolve({ data: list(revisions) });
    if (path.includes("/result-snapshots/")) {
      const id = options?.params?.path?.snapshot_id ?? revisions[0].snapshot_id;
      const found = revisions.find((r) => r.snapshot_id === id) ?? revisions[0];
      return Promise.resolve({ data: detail(found) });
    }
    return Promise.resolve({ data: null });
  });
};

const launchFixture = {
  id: "ff04d66b-3d00-4d67-8cf4-d36772fff708",
  name: "banking-agent-v2",
  status: "COMPLETED",
  quality_conclusion: "pass",
  dataset_name: "banking-agent-regression",
  langfuse_sync_status: "SYNCED",
  langfuse_experiment_url: null,
  created_at: "2026-10-08T16:08:13Z",
  started_at: "2026-10-08T16:08:14Z",
  completed_at: "2026-10-08T16:08:18.82Z",
  allowed_actions: [],
  manifest: {
    agent: { id: "banking-agent", version: "v2" },
    dataset: { dataset_name: "banking-agent-regression", items_count: 6 },
  },
} as any;

const headerProps = (overrides: Record<string, unknown> = {}) => ({
  launch: launchFixture,
  activeSnapshot: null,
  allowedActions: [],
  langfuseLink: { kind: "reason" as const, label: "尚未创建 Langfuse Run", title: "not created" },
  isFetching: false,
  qualityConclusion: "pass",
  onRefresh: vi.fn(),
  ...overrides,
});

const renderHeader = (props: Record<string, unknown>) =>
  render(
    <MemoryRouter>
      <LaunchHeader {...(props as any)} />
    </MemoryRouter>,
  );

const renderPanel = (revisions: Rev[]) => {
  mockSnapshots(revisions);
  return render(
    <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
      <ResultSnapshotPanel
        launchId="launch-119"
        selectedSnapshotId={(revisions[0]?.snapshot_id as string) ?? null}
        onSelect={() => {}}
      />
    </QueryClientProvider>,
  );
};

afterEach(() => {
  vi.clearAllMocks();
});

describe("LaunchHeader claim separation (U5)", () => {
  it("names a frozen PASS as frozen quality, never as release admission", () => {
    renderHeader(headerProps({
      activeSnapshot: revision(),
      qualityConclusion: "pass",
    }));
    expect(screen.getByText("冻结质量通过 (PASS)")).toBeInTheDocument();
    expect(screen.getByTestId("quality-badge")).toHaveTextContent("PASS");
    expect(screen.queryByText("门禁准入通过 (PASS)")).not.toBeInTheDocument();
    expect(screen.queryByText(/门禁/)).not.toBeInTheDocument();
    expect(screen.queryByText(/发布通过|发布准入|Release/)).not.toBeInTheDocument();
  });

  it("keeps COMPLETE evidence visible next to a failed frozen quality", () => {
    renderHeader(headerProps({
      activeSnapshot: revision(),
      qualityConclusion: "fail",
    }));
    expect(screen.getByText("冻结质量未通过 (FAIL)")).toBeInTheDocument();
    // Evidence completeness is independent of the quality outcome.
    expect(screen.getByText("冻结证据完整")).toBeInTheDocument();
    expect(screen.queryByText("冻结质量通过 (PASS)")).not.toBeInTheDocument();
    expect(screen.queryByText(/发布通过|发布准入/)).not.toBeInTheDocument();
  });

  it("names an unknown frozen quality and a diagnostic snapshot as unknown", () => {
    renderHeader(headerProps({
      launch: { ...launchFixture, status: "RUNNING" },
      activeSnapshot: revision({ evidence_state: "DIAGNOSTIC", evidence_reasons: ["1/6 个用例评测失败或未产出结果"] }),
      qualityConclusion: "unknown",
    }));
    expect(screen.getByText("冻结质量未知 (UNKNOWN)")).toBeInTheDocument();
    expect(screen.getByText("诊断快照")).toBeInTheDocument();
    expect(screen.queryByText("冻结证据完整")).not.toBeInTheDocument();
  });

  it("says when no frozen snapshot exists instead of implying a diagnostic one", () => {
    renderHeader(headerProps({
      launch: { ...launchFixture, status: "RUNNING", quality_conclusion: null },
      activeSnapshot: null,
      qualityConclusion: null,
    }));
    expect(screen.getByText("尚未生成冻结结果")).toBeInTheDocument();
    expect(screen.queryByText("诊断快照")).not.toBeInTheDocument();
    expect(screen.getByText("冻结质量未知 (UNKNOWN)")).toBeInTheDocument();
  });

  it("never presents COMPLETE evidence as a verified signature or baseline entitlement", () => {
    renderHeader(headerProps({
      activeSnapshot: revision(),
      qualityConclusion: "pass",
      onOpenBaselineModal: vi.fn(),
    }));
    const evidence = screen.getByText("冻结证据完整");
    expect(evidence).toHaveAttribute("title", "当前选定 Revision 的冻结证据完整");
    expect(evidence.getAttribute("title")).not.toMatch(/签名/);
    expect(evidence.getAttribute("title")).not.toMatch(/可作为基线/);
  });

  it("keeps the diagnostic badge free of signature and release claims", () => {
    renderHeader(headerProps({
      activeSnapshot: revision({ evidence_state: "DIAGNOSTIC" }),
      qualityConclusion: "unknown",
    }));
    const diagnostic = screen.getByText("诊断快照");
    expect(diagnostic.getAttribute("title")).not.toMatch(/签名/);
    expect(diagnostic.getAttribute("title")).not.toMatch(/发布/);
  });
});

describe("ResultSnapshotPanel evidence help (U5)", () => {
  it("separates complete evidence from baseline and release decisions", async () => {
    renderPanel([revision()]);
    const help = await screen.findByTestId("snapshot-evidence-help");
    expect(help).toHaveTextContent("证据完整");
    // No signature, baseline-entitlement or release-basis claim.
    expect(help).not.toHaveTextContent("签名");
    expect(help).not.toHaveTextContent("发布依据");
    expect(help).not.toHaveTextContent("可作为正式 Baseline");
  });

  it("keeps the fail-closed diagnostic baseline claim", async () => {
    renderPanel([revision({ evidence_state: "DIAGNOSTIC", evidence_reasons: ["1/6 个用例评测失败或未产出结果"] })]);
    const help = await screen.findByTestId("snapshot-evidence-help");
    expect(help).toHaveTextContent("不可作为正式 Baseline");
  });
});
